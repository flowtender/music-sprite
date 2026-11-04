import re

from .models import TrackMetadata

TITLE_SPLIT = re.compile(r"\s+[-–—]\s+")
BRACKETED_JUNK = re.compile(
    r"\s*[\(\[][^\)\]]*(?:official|lyric|audio|video|visuali[sz]er|free download|out now|remaster)[^\)\]]*[\)\]]",
    re.IGNORECASE,
)
PIPE_TAIL = re.compile(r"\s*\|.*$")
CHANNEL_SUFFIX = re.compile(r"\s*(?:-\s*Topic|VEVO)$", re.IGNORECASE)


def clean_title(title: str) -> str:
    title = BRACKETED_JUNK.sub("", title)
    title = PIPE_TAIL.sub("", title)
    return title.strip()


def clean_channel(channel: str) -> str:
    return CHANNEL_SUFFIX.sub("", channel).strip()


def is_known_publisher(channel: str, known_publishers: frozenset[str]) -> bool:
    folded = channel.casefold()
    return any(publisher in folded for publisher in known_publishers)


def _year(info: dict) -> int | None:
    for key in ("release_year", "release_date", "upload_date"):
        value = str(info.get(key) or "")
        if len(value) >= 4 and value[:4].isdigit():
            return int(value[:4])
    return None


def guess(info: dict, known_publishers: frozenset[str]) -> TrackMetadata:
    channel = clean_channel(info.get("channel") or info.get("uploader") or "")
    publisher = is_known_publisher(channel, known_publishers)
    shared = dict(
        year=_year(info),
        genre=info.get("genre"),
        from_known_publisher=publisher,
    )

    artists = info.get("artists") or ([info["artist"]] if info.get("artist") else [])
    if artists and info.get("track"):
        return TrackMetadata(
            title=info["track"],
            artist=", ".join(artists),
            album=info.get("album"),
            confidence=0.9,
            origin="source-tags",
            **shared,
        )

    title = clean_title(info.get("title") or "")
    parts = TITLE_SPLIT.split(title, maxsplit=1)
    if len(parts) == 2:
        artist, song = parts
        confidence = 0.4 if publisher else 0.65
    elif publisher:
        artist, song, confidence = "", title, 0.1
    else:
        artist, song, confidence = channel, title, 0.5

    return TrackMetadata(
        title=song.strip(), artist=artist.strip(), confidence=confidence, origin="heuristic", **shared
    )
