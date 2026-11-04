"""Audio analysis with Essentia: tempo, musical key and a genre guess, computed from a slice of the audio itself.

essentia-tensorflow is an optional dependency (pip install '.[analysis]'). The models are small and are downloaded
into ANALYSIS_MODEL_DIR the first time they are needed. Inference runs on the GPU when TensorFlow can find CUDA 11.2
and cuDNN 8.1 (see INSTALL.md) and on the CPU otherwise; ANALYSIS_DEVICE=cpu forces the CPU.
"""

import asyncio
import json
import logging
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from importlib.util import find_spec
from pathlib import Path

import httpx

from .config import Config

log = logging.getLogger(__name__)

SAMPLE_RATE = 44100
MIN_AUDIO_SECONDS = 5
DECODE_TIMEOUT_SECONDS = 120
MIN_GENRE_CONFIDENCE = 0.10
GENRE_SEPARATOR = "---"
TOP_GENRES = 3

MODEL_BASE_URL = "https://essentia.upf.edu/models/"
EFFNET = "discogs-effnet-bs64-1.pb"
GENRE_HEAD = "genre_discogs400-discogs-effnet-1.pb"
GENRE_LABELS = "genre_discogs400-discogs-effnet-1.json"
TEMPO = "deeptemp-k16-3.pb"
MODEL_FILES = {
    EFFNET: "feature-extractors/discogs-effnet/",
    GENRE_HEAD: "classification-heads/genre_discogs400/",
    GENRE_LABELS: "classification-heads/genre_discogs400/",
    TEMPO: "tempo/tempocnn/",
}


class AnalysisError(RuntimeError):
    """The audio could not be analyzed; the message is shown to the user."""


@dataclass
class Analysis:
    bpm: int
    key: str
    # Best guesses first, as (style, probability); styles come from the Discogs taxonomy, e.g. "Deep House".
    genres: list[tuple[str, float]] = field(default_factory=list)

    @property
    def genre(self) -> str | None:
        if self.genres and self.genres[0][1] >= MIN_GENRE_CONFIDENCE:
            return self.genres[0][0]
        return None

    def describe(self) -> str:
        guesses = ", ".join(f"{name} {probability:.0%}" for name, probability in self.genres)
        return f"{self.bpm} BPM · {self.key} · genre guesses: {guesses}"


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
        self._genre = es.TensorflowPredict2D(
            graphFilename=str(models / GENRE_HEAD),
            input="serving_default_model_Placeholder",
            output="PartitionedCall:0",
        )
        labels = json.loads((models / GENRE_LABELS).read_text(encoding="utf-8"))["classes"]
        self._styles = [label.split(GENRE_SEPARATOR)[-1] for label in labels]

    def run(self, audio) -> Analysis:
        key, scale, _strength = self._key(audio)
        bpm, _local, _probabilities = self._tempo(self._resample_tempo(audio))
        patches = self._genre(self._embed(self._resample_genre(audio)))
        mean = patches.mean(axis=0)
        best = mean.argsort()[::-1][:TOP_GENRES]
        return Analysis(
            bpm=round(float(bpm)),
            key=f"{key}m" if scale == "minor" else key,
            genres=[(self._styles[index], float(mean[index])) for index in best],
        )


def _gpu_in_use() -> bool:
    # cuDNN is loaded lazily by the first convolution, so after one analysis its presence tells which device ran it.
    try:
        return "libcudnn" in Path("/proc/self/maps").read_text()
    except OSError:
        return False


class Analyzer:
    def __init__(self, config: Config):
        self._config = config
        self._lock = threading.Lock()
        self._engine: _Engine | None = None
        self._device_logged = False

    @property
    def disabled_reason(self) -> str | None:
        if not self._config.analysis_enabled:
            return "ANALYSIS_ENABLED is off"
        if find_spec("essentia") is None:
            return "essentia-tensorflow is not installed"
        if shutil.which("ffmpeg") is None:
            return "ffmpeg not found"
        return None

    async def analyze(self, audio_path: Path) -> Analysis:
        return await asyncio.to_thread(self._analyze, audio_path)

    def _analyze(self, audio_path: Path) -> Analysis:
        audio = _decode(audio_path)
        # One at a time: TensorFlow already uses every core, so parallel runs only compete for memory.
        with self._lock:
            if self._engine is None:
                self._download_models()
                self._engine = _Engine(self._config.analysis_model_dir, self._config.analysis_device == "cpu")
            try:
                analysis = self._engine.run(audio)
            except RuntimeError as error:
                raise AnalysisError(f"Essentia failed: {error}") from error
            if not self._device_logged:
                self._device_logged = True
                log.info("audio analysis is running on the %s", "GPU" if _gpu_in_use() else "CPU")
            return analysis

    def _download_models(self) -> None:
        directory = self._config.analysis_model_dir
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


def _decode(audio_path: Path):
    import numpy

    command = [
        "ffmpeg", "-nostdin", "-v", "error",
        "-i", str(audio_path), "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", "-",
    ]
    try:
        result = subprocess.run(command, capture_output=True, timeout=DECODE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as error:
        raise AnalysisError("decoding the audio timed out") from error
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip().splitlines()
        raise AnalysisError(f"ffmpeg could not decode the audio: {detail[-1] if detail else result.returncode}")
    audio = numpy.frombuffer(result.stdout, dtype=numpy.float32)
    if len(audio) < MIN_AUDIO_SECONDS * SAMPLE_RATE:
        raise AnalysisError("the audio is too short to analyze")
    return audio
