import re
from pathlib import Path, PurePosixPath

from .models import TrackMetadata

TEMPLATE_FIELDS = ("artist", "album_artist", "album", "title", "year", "genre")
DEFAULT_TEMPLATE = "{album_artist}/{album}/{title}"

UNSAFE_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
MAX_NAME_LENGTH = 150


def safe_name(name: str) -> str:
    cleaned = UNSAFE_FILENAME_CHARS.sub("_", name).strip(" .")[:MAX_NAME_LENGTH]
    return cleaned or "Unknown"


def validate_template(template: str) -> None:
    fields = ", ".join(f"{{{name}}}" for name in TEMPLATE_FIELDS)
    if "{title}" not in template:
        raise ValueError(f"LIBRARY_PATH_TEMPLATE must contain {{title}}; available fields: {fields}")
    try:
        rendered = template.format_map(dict.fromkeys(TEMPLATE_FIELDS, "x"))
    except (KeyError, IndexError, ValueError) as error:
        raise ValueError(f"invalid LIBRARY_PATH_TEMPLATE ({error!r}); available fields: {fields}") from error
    path = PurePosixPath(rendered)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("LIBRARY_PATH_TEMPLATE must be a relative path without '..'")


def render_relative_path(template: str, meta: TrackMetadata) -> Path:
    values = {
        "artist": meta.artist,
        "album_artist": meta.effective_album_artist,
        "album": meta.effective_album,
        "title": meta.title,
        "year": meta.year or "",
        "genre": meta.genre or "",
    }
    return Path(template.format_map({name: safe_name(str(value)) for name, value in values.items()}))
