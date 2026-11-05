"""Audio analysis: tempo, key, genre guesses, moods, instruments, vocals, danceability and loudness.

Everything but loudness comes from Essentia. essentia-tensorflow is an optional dependency (pip install
'.[analysis]'). The models are small and are downloaded into ANALYSIS_MODEL_DIR the first time they are needed.
Inference runs on the GPU when TensorFlow can find CUDA 11.2 and cuDNN 8.1 (see INSTALL.md) and on the CPU
otherwise; ANALYSIS_DEVICE=cpu forces the CPU. Loudness (EBU R128, from which ReplayGain follows) comes from ffmpeg.
"""

import asyncio
import json
import logging
import multiprocessing
import os
import re
import shutil
import subprocess
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import replace
from importlib.util import find_spec
from pathlib import Path

import httpx

from .config import AnalysisConfig
from .models import AudioFeatures

log = logging.getLogger(__name__)

SAMPLE_RATE = 44100
MIN_AUDIO_SECONDS = 5
FFMPEG_TIMEOUT_SECONDS = 300
GENRE_SEPARATOR = "---"

TOP_GENRES = 3
# Probabilities of the binary heads count as a yes above this.
POSITIVE = 0.5
# The multi-label Jamendo heads give small probabilities even for good matches.
MIN_MOOD_THEME = 0.10
MAX_MOOD_THEMES = 5
MIN_INSTRUMENT = 0.25
MAX_INSTRUMENTS = 6

MODEL_BASE_URL = "https://essentia.upf.edu/models/"
EFFNET = "discogs-effnet-bs64-1.pb"
TEMPO = "deeptemp-k16-3.pb"
GENRE_HEAD = "genre_discogs400"
MOOD_THEME_HEAD = "mtg_jamendo_moodtheme"
INSTRUMENT_HEAD = "mtg_jamendo_instrument"
VOICE_HEAD = "voice_instrumental"
GENDER_HEAD = "gender"
# Heads read the Discogs-EffNet embedding that the genre guess needs anyway.
HEADS = (
    GENRE_HEAD, MOOD_THEME_HEAD, INSTRUMENT_HEAD, VOICE_HEAD, GENDER_HEAD,
    "danceability", "mood_happy", "mood_sad", "mood_aggressive", "mood_relaxed", "mood_party", "mood_acoustic",
    "mood_electronic", "approachability_2c", "engagement_2c",
)
# Tag name -> (head, class whose probability it is). The mood_* scores that are feelings also name the moods.
SCORES = {
    "danceability": ("danceability", "danceable"),
    "mood_happy": ("mood_happy", "happy"),
    "mood_sad": ("mood_sad", "sad"),
    "mood_aggressive": ("mood_aggressive", "aggressive"),
    "mood_relaxed": ("mood_relaxed", "relaxed"),
    "mood_party": ("mood_party", "party"),
    "mood_acoustic": ("mood_acoustic", "acoustic"),
    "mood_electronic": ("mood_electronic", "electronic"),
    "approachability": ("approachability_2c", "approachable"),
    "engagement": ("engagement_2c", "engaging"),
}
FEELINGS = ("happy", "sad", "aggressive", "relaxed", "party")
INSTRUMENT_NAMES = {
    "acousticbassguitar": "acoustic bass guitar",
    "acousticguitar": "acoustic guitar",
    "classicalguitar": "classical guitar",
    "doublebass": "double bass",
    "drummachine": "drum machine",
    "electricguitar": "electric guitar",
    "electricpiano": "electric piano",
    "pipeorgan": "pipe organ",
}


def _head_files(name: str) -> tuple[str, str]:
    return f"{name}-discogs-effnet-1.pb", f"{name}-discogs-effnet-1.json"


def _model_files() -> dict[str, str]:
    """File name -> folder under MODEL_BASE_URL."""
    files = {EFFNET: "feature-extractors/discogs-effnet/", TEMPO: "tempo/tempocnn/"}
    for head in HEADS:
        folder = f"classification-heads/{head.removesuffix('_2c')}/"
        files.update(dict.fromkeys(_head_files(head), folder))
    return files


MODEL_FILES = _model_files()


class AnalysisError(RuntimeError):
    """The audio could not be analyzed; the message is shown to the user."""


class _Head:
    """A classification head: embeddings in, the mean probability of every class out."""

    def __init__(self, es, models: Path, name: str):
        graph, labels = _head_files(name)
        meta = json.loads((models / labels).read_text(encoding="utf-8"))
        schema = meta["schema"]
        output = next(item["name"] for item in schema["outputs"] if item.get("output_purpose") == "predictions")
        self.classes: list[str] = meta["classes"]
        self._model = es.TensorflowPredict2D(
            graphFilename=str(models / graph), input=schema["inputs"][0]["name"], output=output
        )

    def __call__(self, embeddings) -> dict[str, float]:
        mean = self._model(embeddings).mean(axis=0)
        return {name: float(probability) for name, probability in zip(self.classes, mean)}


def _top(probabilities: dict[str, float], minimum: float, limit: int) -> list[str]:
    ranked = sorted(probabilities.items(), key=lambda item: -item[1])
    return [name for name, probability in ranked[:limit] if probability >= minimum]


class _Engine:
    """The loaded Essentia algorithms. Importing essentia pulls in TensorFlow, so this is built on first use."""

    def __init__(self, models: Path, force_cpu: bool):
        os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")  # errors only; TensorFlow is chatty about missing CUDA
        if force_cpu:
            os.environ["CUDA_VISIBLE_DEVICES"] = "-1"
        import essentia
        import essentia.standard as es

        essentia.log.infoActive = False
        essentia.log.warningActive = False
        self._resample_tempo = es.Resample(inputSampleRate=SAMPLE_RATE, outputSampleRate=11025)
        self._resample_genre = es.Resample(inputSampleRate=SAMPLE_RATE, outputSampleRate=16000)
        self._key = es.KeyExtractor()
        self._tempo = es.TempoCNN(graphFilename=str(models / TEMPO))
        self._embed = es.TensorflowPredictEffnetDiscogs(graphFilename=str(models / EFFNET), output="PartitionedCall:1")
        self._heads = {name: _Head(es, models, name) for name in HEADS}

    def run(self, audio) -> AudioFeatures:
        key, scale, _strength = self._key(audio)
        bpm, _local, _probabilities = self._tempo(self._resample_tempo(audio))
        embeddings = self._embed(self._resample_genre(audio))
        outputs = {name: head(embeddings) for name, head in self._heads.items()}

        styles = {label.split(GENRE_SEPARATOR)[-1]: p for label, p in outputs[GENRE_HEAD].items()}
        genres = sorted(styles.items(), key=lambda item: -item[1])[:TOP_GENRES]
        scores = {tag: round(outputs[head][label], 2) for tag, (head, label) in SCORES.items()}

        moods = [feeling for feeling in FEELINGS if scores[f"mood_{feeling}"] >= POSITIVE]
        moods += [m for m in _top(outputs[MOOD_THEME_HEAD], MIN_MOOD_THEME, MAX_MOOD_THEMES) if m not in moods]
        instruments = [
            INSTRUMENT_NAMES.get(name, name)
            for name in _top(outputs[INSTRUMENT_HEAD], MIN_INSTRUMENT, MAX_INSTRUMENTS + 1)
            if name != "voice"  # that is the vocals' business
        ][:MAX_INSTRUMENTS]
        if outputs[VOICE_HEAD]["instrumental"] >= POSITIVE:
            vocals = "instrumental"
        else:
            vocals = "female" if outputs[GENDER_HEAD]["female"] >= POSITIVE else "male"

        return AudioFeatures(
            bpm=round(float(bpm)),
            key=f"{key}m" if scale == "minor" else key,
            genres=genres,
            moods=moods,
            instruments=instruments,
            vocals=vocals,
            scores=scores,
        )


def _gpu_in_use() -> bool:
    # cuDNN is loaded lazily by the first convolution, so after one analysis its presence tells which device ran it.
    try:
        return "libcudnn" in Path("/proc/self/maps").read_text()
    except OSError:
        return False


# Lives in the worker process, which keeps the loaded models between tracks.
_worker_engine: _Engine | None = None
_worker_device_reported = False


def _download_models(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, folder in MODEL_FILES.items():
        target = directory / name
        if target.exists():
            continue
        log.info("downloading analysis model %s", name)
        partial = target.with_name(f"{name}.part")
        try:
            with httpx.stream("GET", f"{MODEL_BASE_URL}{folder}{name}", follow_redirects=True, timeout=60) as response:
                response.raise_for_status()
                with partial.open("wb") as file:
                    for chunk in response.iter_bytes():
                        file.write(chunk)
        except httpx.HTTPError as error:
            partial.unlink(missing_ok=True)
            raise AnalysisError(f"could not download model {name}: {error}") from error
        partial.replace(target)


def _analyze_in_worker(models: Path, force_cpu: bool, audio_path: Path) -> tuple[AudioFeatures, str | None]:
    """Runs in the worker process. Returns the features and, on the first track, the device that did the work."""
    global _worker_engine, _worker_device_reported
    audio = _decode(audio_path)
    loudness, loudness_range, peak = _measure_loudness(audio_path)
    if _worker_engine is None:
        _download_models(models)
        _worker_engine = _Engine(models, force_cpu)
    try:
        features = _worker_engine.run(audio)
    except RuntimeError as error:
        raise AnalysisError(f"Essentia failed: {error}") from error
    device = None
    if not _worker_device_reported:
        _worker_device_reported = True
        device = "GPU" if _gpu_in_use() else "CPU"
    return replace(features, loudness=loudness, loudness_range=loudness_range, peak=peak), device


class Analyzer:
    """Runs the analysis in a separate process. Essentia holds Python's global interpreter lock while it computes, so
    in a thread it froze the bot for seconds at a time, long enough for Discord to drop button clicks."""

    def __init__(self, config: AnalysisConfig):
        self._config = config
        self._pool: ProcessPoolExecutor | None = None

    @property
    def disabled_reason(self) -> str | None:
        if not self._config.enabled:
            return "ANALYSIS_ENABLED is off"
        if find_spec("essentia") is None:
            return "essentia-tensorflow is not installed"
        if shutil.which("ffmpeg") is None:
            return "ffmpeg not found"
        return None

    async def analyze(self, audio_path: Path) -> AudioFeatures:
        if self._pool is None:
            # Spawned rather than forked: forking a process that runs threads and an event loop is unsafe.
            self._pool = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"))
        config = self._config
        try:
            # One worker, so tracks go one at a time: TensorFlow already uses every core.
            features, device = await asyncio.get_running_loop().run_in_executor(
                self._pool, _analyze_in_worker, config.model_dir, config.device == "cpu", audio_path
            )
        except BrokenProcessPool as error:
            self.close()
            raise AnalysisError("the analysis process died (out of memory?); it restarts on the next track") from error
        if device:
            log.info("audio analysis is running on the %s", device)
        return features

    def close(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None


def _ffmpeg(arguments: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", *arguments], capture_output=True, timeout=FFMPEG_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as error:
        raise AnalysisError("ffmpeg timed out") from error


def _decode(audio_path: Path):
    import numpy

    result = _ffmpeg([
        "-v", "error", "-i", str(audio_path), "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", "-",
    ])
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip().splitlines()
        raise AnalysisError(f"ffmpeg could not decode the audio: {detail[-1] if detail else result.returncode}")
    audio = numpy.frombuffer(result.stdout, dtype=numpy.float32)
    if len(audio) < MIN_AUDIO_SECONDS * SAMPLE_RATE:
        raise AnalysisError("the audio is too short to analyze")
    return audio


def _measure(summary: str, label: str, unit: str) -> float | None:
    found = re.search(rf"^\s*{label}:\s+(-?[\d.]+|-inf)\s+{unit}", summary, re.MULTILINE)
    return float(found.group(1)) if found and found.group(1) != "-inf" else None


def _measure_loudness(audio_path: Path) -> tuple[float | None, float | None, float | None]:
    """Integrated loudness (LUFS), loudness range (LU) and true peak (linear) per EBU R128, on the stereo original."""
    result = _ffmpeg(["-nostats", "-i", str(audio_path), "-vn", "-af", "ebur128=peak=true", "-f", "null", "-"])
    text = result.stderr.decode(errors="replace")
    start = text.rfind("Summary:")
    if result.returncode != 0 or start < 0:
        log.warning("could not measure the loudness of %s", audio_path)
        return None, None, None
    summary = text[start:]
    peak_db = _measure(summary, "Peak", "dBFS")
    return (
        _measure(summary, "I", "LUFS"),
        _measure(summary, "LRA", "LU"),
        None if peak_db is None else 10 ** (peak_db / 20),
    )
