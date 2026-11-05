"""Runs yt-dlp, and knows what its info JSON holds."""

import asyncio
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
ALLOWED_SWITCH_FLAGS = frozenset({"--no-playlist", "--no-check-certificates"})

DOWNLOAD_TIMEOUT_SECONDS = 2 * 60 * 60
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")
# Info JSON keys that are bulky and say nothing about the music: format lists, thumbnails, subtitles, ...
BULKY_INFO_KEYS = frozenset({
    "formats", "requested_formats", "requested_downloads", "thumbnails", "http_headers",
    "automatic_captions", "subtitles", "requested_subtitles", "heatmap", "url", "manifest_url",
})


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
    """Downloads one video or a whole playlist, calling on_track as each file completes.

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
                await on_track(
                    DownloadedTrack(audio_path, audio_path.with_suffix(".info.json"), _find_cover(audio_path))
                )
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


def _find_cover(audio_path: Path) -> Path | None:
    for suffix in IMAGE_SUFFIXES:
        candidate = audio_path.with_suffix(suffix)
        if candidate.exists():
            return candidate
    return None
