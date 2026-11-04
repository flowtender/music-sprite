import hashlib
import logging
import secrets
import shutil
from pathlib import Path

import httpx

from .config import Config
from .layout import render_relative_path, safe_name
from .models import TrackMetadata
from .staging import QueueEntry
from .tagger import source_fields, write_tags

log = logging.getLogger(__name__)


def destination_for(audio_path: Path, meta: TrackMetadata, config: Config) -> Path:
    relative = render_relative_path(config.library_path_template, meta)
    folder = config.music_root / relative.parent
    target = folder / f"{relative.name}{audio_path.suffix}"
    counter = 2
    while target.exists():
        target = folder / f"{relative.name} ({counter}){audio_path.suffix}"
        counter += 1
    return target


def add_to_library(config: Config, entry: QueueEntry, meta: TrackMetadata) -> Path:
    write_tags(entry.audio_path, meta, source_fields(entry.info, meta), entry.cover_path)
    _archive_source_metadata(config, entry)
    target = destination_for(entry.audio_path, meta, config)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(entry.audio_path, target)
    shutil.rmtree(entry.directory, ignore_errors=True)
    return target


def _archive_source_metadata(config: Config, entry: QueueEntry) -> None:
    extractor = safe_name(entry.info.get("extractor_key") or "unknown")
    name = safe_name(entry.info.get("id") or entry.audio_path.stem)
    destination = config.archive_dir / extractor / f"{name}.info.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(entry.info_path, destination)


async def trigger_navidrome_scan(http: httpx.AsyncClient, config: Config) -> None:
    if not config.navidrome_url:
        return
    salt = secrets.token_hex(6)
    token = hashlib.md5((config.navidrome_password + salt).encode()).hexdigest()
    try:
        response = await http.get(
            f"{config.navidrome_url}/rest/startScan",
            params={"u": config.navidrome_user, "t": token, "s": salt, "v": "1.16.1", "c": "music-sprite", "f": "json"},
            timeout=15,
        )
        response.raise_for_status()
    except httpx.HTTPError as error:
        log.warning("Navidrome scan trigger failed: %s", error)
