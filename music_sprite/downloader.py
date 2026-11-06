"""Runs yt-dlp, and knows what its info JSON holds."""

import asyncio
import json
import shutil
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

ALLOWED_VALUE_FLAGS = frozenset({
    "-f", "--format",
    "--audio-format", "--audio-quality",
    "--download-sections",
    "--extractor-args",
    "--age-limit",
    "--playlist-items",
})
ALLOWED_SWITCH_FLAGS = frozenset({"--no-playlist", "--no-check-certificates", "--split-chapters"})

DOWNLOAD_TIMEOUT_SECONDS = 2 * 60 * 60
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")
# Info JSON keys that are bulky and say nothing about the music: format lists, thumbnails, subtitles, ...
BULKY_INFO_KEYS = frozenset({
    "formats", "requested_formats", "requested_downloads", "thumbnails", "http_headers",
    "automatic_captions", "subtitles", "requested_subtitles", "heatmap", "url", "manifest_url",
})
# Where --split-chapters puts an upload's chapters: next to it as <id>.001.<ext>, <id>.002.<ext>, ...
CHAPTER_TEMPLATE = "chapter:%(id)s.%(section_number)03d.%(ext)s"
# Info JSON keys that describe the whole upload as one song (YouTube Music's track, ...) or as a list of chapters;
# none of them fits a single chapter.
WHOLE_UPLOAD_KEYS = frozenset({"track", "track_number", "track_id", "alt_title", "chapters", "duration_string"})


class RejectedArguments(ValueError):
    pass


class DownloadError(RuntimeError):
    pass


@dataclass
class DownloadedTrack:
    audio_path: Path
    info_path: Path
    cover_path: Path | None


def sanitize_extra_args(tokens: list[str]) -> list[str]:
    sanitized: list[str] = []
    remaining = iter(tokens)
    for token in remaining:
        flag, equals, inline_value = token.partition("=")
        if flag in ALLOWED_SWITCH_FLAGS and not equals:
            sanitized.append(flag)
        elif flag in ALLOWED_VALUE_FLAGS:
            value = inline_value if equals else next(remaining, None)
            if value is None:
                raise RejectedArguments(f"`{flag}` needs a value")
            sanitized.extend([flag, value])
        else:
            allowed = ", ".join(sorted(ALLOWED_VALUE_FLAGS | ALLOWED_SWITCH_FLAGS))
            raise RejectedArguments(f"`{token}` is not an allowed argument. Allowed: {allowed}")
    return sanitized


def portable_info(info: dict, dropped: frozenset[str] = BULKY_INFO_KEYS) -> dict:
    """The info JSON without the dropped keys and yt-dlp's private ones ("_filename", ...)."""
    return {key: value for key, value in info.items() if key not in dropped and not key.startswith("_")}


async def download(
    url: str,
    extra_args: list[str],
    output_dir: Path,
    max_items: int,
    on_track: Callable[[DownloadedTrack], Awaitable[None]],
) -> list[str]:
    """Downloads one video or a whole playlist, calling on_track as each file completes; a video split with
    --split-chapters arrives as its chapters.

    Returns yt-dlp's error lines for items that failed.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    command = [
        # The bot's own yt-dlp and its optional dependencies (curl_cffi), not whichever one comes first on PATH.
        sys.executable, "-m", "yt_dlp",
        "--ignore-config",
        "--force-ipv4",
        "--concurrent-fragments", "4",
        "--prefer-free-formats",
        "-x",
        "--yes-playlist",
        "--playlist-end", str(max_items),
        "--no-write-playlist-metafiles",
        "--write-info-json",
        "--write-thumbnail",
        "--convert-thumbnails", "jpg",
        "--no-simulate",
        "--print", "after_move:filepath",
        "-P", str(output_dir),
        "-o", "%(id)s.%(ext)s",
        "-o", CHAPTER_TEMPLATE,
        *extra_args,
        "--", url,
    ]
    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    assert process.stdout is not None and process.stderr is not None
    stderr_reader = asyncio.create_task(process.stderr.read())
    track_count = 0
    try:
        async with asyncio.timeout(DOWNLOAD_TIMEOUT_SECONDS):
            async for raw_line in process.stdout:
                printed = raw_line.decode().strip()
                if not printed:
                    continue
                audio_path = Path(printed)
                whole = DownloadedTrack(audio_path, audio_path.with_suffix(".info.json"), _find_cover(audio_path))
                for track in split_into_chapters(whole):
                    await on_track(track)
                    track_count += 1
            await process.wait()
    except TimeoutError:
        raise DownloadError("yt-dlp timed out")
    finally:
        if process.returncode is None:
            process.kill()

    stderr = (await stderr_reader).decode(errors="replace")
    error_lines = [line for line in stderr.splitlines() if line.startswith("ERROR")]
    if track_count == 0:
        raise DownloadError("\n".join(error_lines) or stderr.strip()[-800:] or "yt-dlp produced no files")
    return error_lines


def split_into_chapters(track: DownloadedTrack) -> list[DownloadedTrack]:
    """The chapters yt-dlp cut the track into, each with its own info JSON and a copy of the cover, after which the
    whole track's files are deleted; or just the track when it wasn't split. A lone chapter is the whole track."""
    audio = track.audio_path
    if not _chapter_path(audio, 1).exists():
        return [track]
    info = json.loads(track.info_path.read_text(encoding="utf-8"))
    chapters = info.get("chapters") or []
    pieces = [_chapter_path(audio, number) for number in range(1, len(chapters) + 1)]
    if len(pieces) < 2 or not all(piece.exists() for piece in pieces):
        return [track]
    tracks = []
    for number, (chapter, piece) in enumerate(zip(chapters, pieces), start=1):
        info_path = piece.with_suffix(".info.json")
        info_path.write_text(json.dumps(chapter_info(info, number, chapter), ensure_ascii=False), encoding="utf-8")
        cover = None
        if track.cover_path:
            cover = piece.with_suffix(track.cover_path.suffix)
            shutil.copyfile(track.cover_path, cover)
        tracks.append(DownloadedTrack(piece, info_path, cover))
    # A whole album or mix is large, and a long playlist of them would pile up until the batch ends.
    for path in (track.audio_path, track.info_path, track.cover_path):
        if path:
            path.unlink(missing_ok=True)
    return tracks


def chapter_info(info: dict, number: int, chapter: dict) -> dict:
    """One chapter's info JSON: the upload's, titled and timed as the chapter, with yt-dlp's section_* keys saying
    where in the upload it was. "fulltitle" keeps the upload's own title (the album or mix) as context."""
    start = chapter.get("start_time") or 0
    # yt-dlp fills in the last chapter's end only in memory, from the file's length.
    end = chapter.get("end_time") or info.get("duration") or start
    return {
        **{key: value for key, value in info.items() if key not in WHOLE_UPLOAD_KEYS},
        "title": chapter.get("title") or f"{info.get('title')} ({number})",
        "duration": round(end - start, 3),
        "section_number": number,
        "section_title": chapter.get("title"),
        "section_start": start,
        "section_end": end,
    }


def _chapter_path(audio_path: Path, number: int) -> Path:
    """Where CHAPTER_TEMPLATE puts the chapter; the audio file is named %(id)s.%(ext)s."""
    return audio_path.with_name(f"{audio_path.stem}.{number:03d}{audio_path.suffix}")


def _find_cover(audio_path: Path) -> Path | None:
    for suffix in IMAGE_SUFFIXES:
        candidate = audio_path.with_suffix(suffix)
        if candidate.exists():
            return candidate
    return None
