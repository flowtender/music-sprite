import base64
import json
from pathlib import Path

from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, COMM, ID3, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TXXX, UFID, WOAS, ID3NoHeaderError
from mutagen.mp4 import MP4, AtomDataType, MP4Cover, MP4FreeForm
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

from .models import TrackMetadata

MAX_COMMENT_CHARS = 4000
BULKY_INFO_KEYS = frozenset({
    "formats", "requested_formats", "requested_downloads", "thumbnails", "http_headers",
    "automatic_captions", "subtitles", "requested_subtitles", "heatmap", "url", "manifest_url",
})


class UnsupportedFormat(RuntimeError):
    pass


def source_fields(info: dict, meta: TrackMetadata) -> dict[str, str]:
    fields = {
        "source_url": info.get("webpage_url"),
        "source_extractor": info.get("extractor_key"),
        "source_id": info.get("id"),
        "source_channel": info.get("channel") or info.get("uploader"),
        "source_uploader_id": info.get("uploader_id"),
        "source_upload_date": info.get("upload_date"),
        "source_playlist": info.get("playlist_title"),
        "description": (info.get("description") or "")[:MAX_COMMENT_CHARS],
        "metadata_origin": meta.origin,
        "source_metadata": json.dumps(_portable_info(info), ensure_ascii=False, default=str),
    }
    return {key: str(value) for key, value in fields.items() if value}


def _portable_info(info: dict) -> dict:
    return {
        key: value
        for key, value in info.items()
        if key not in BULKY_INFO_KEYS and not key.startswith("_")
    }


def write_tags(path: Path, meta: TrackMetadata, source: dict[str, str], cover_path: Path | None) -> None:
    writer = {
        ".opus": _write_ogg(OggOpus),
        ".ogg": _write_ogg(OggVorbis),
        ".flac": _write_flac,
        ".mp3": _write_mp3,
        ".m4a": _write_mp4,
        ".mp4": _write_mp4,
    }.get(path.suffix.lower())
    if writer is None:
        raise UnsupportedFormat(f"cannot tag {path.suffix} files")
    cover = cover_path.read_bytes() if cover_path else None
    writer(path, meta, source, cover)


def _vorbis_fields(meta: TrackMetadata, source: dict[str, str]) -> dict[str, list[str]]:
    fields: dict[str, list[str]] = {
        "title": [meta.title],
        "artist": [meta.artist],
        "album": [meta.effective_album],
        "albumartist": [meta.effective_album_artist],
    }
    if meta.year:
        fields["date"] = [str(meta.year)]
    if meta.genre:
        fields["genre"] = [meta.genre]
    if meta.musicbrainz:
        fields["musicbrainz_trackid"] = [meta.musicbrainz.recording_id]
        if meta.musicbrainz.release_id:
            fields["musicbrainz_albumid"] = [meta.musicbrainz.release_id]
        if meta.musicbrainz.artist_ids:
            fields["musicbrainz_artistid"] = meta.musicbrainz.artist_ids
    for key, value in source.items():
        fields["comment" if key == "description" else key] = [value]
    return fields


def _picture(cover: bytes) -> Picture:
    picture = Picture()
    picture.type = 3
    picture.mime = "image/jpeg"
    picture.data = cover
    return picture


def _write_ogg(container):
    def write(path: Path, meta: TrackMetadata, source: dict[str, str], cover: bytes | None) -> None:
        audio = container(path)
        for key, values in _vorbis_fields(meta, source).items():
            audio[key] = values
        if cover:
            audio["metadata_block_picture"] = [base64.b64encode(_picture(cover).write()).decode("ascii")]
        audio.save()

    return write


def _write_flac(path: Path, meta: TrackMetadata, source: dict[str, str], cover: bytes | None) -> None:
    audio = FLAC(path)
    for key, values in _vorbis_fields(meta, source).items():
        audio[key] = values
    if cover:
        audio.clear_pictures()
        audio.add_picture(_picture(cover))
    audio.save()


def _write_mp3(path: Path, meta: TrackMetadata, source: dict[str, str], cover: bytes | None) -> None:
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
    tags.add(TIT2(encoding=3, text=meta.title))
    tags.add(TPE1(encoding=3, text=meta.artist))
    tags.add(TALB(encoding=3, text=meta.effective_album))
    tags.add(TPE2(encoding=3, text=meta.effective_album_artist))
    if meta.year:
        tags.add(TDRC(encoding=3, text=str(meta.year)))
    if meta.genre:
        tags.add(TCON(encoding=3, text=meta.genre))
    if meta.musicbrainz:
        tags.add(UFID(owner="http://musicbrainz.org", data=meta.musicbrainz.recording_id.encode()))
        if meta.musicbrainz.release_id:
            tags.add(TXXX(encoding=3, desc="MusicBrainz Album Id", text=meta.musicbrainz.release_id))
        if meta.musicbrainz.artist_ids:
            tags.add(TXXX(encoding=3, desc="MusicBrainz Artist Id", text=meta.musicbrainz.artist_ids))
    for key, value in source.items():
        if key == "description":
            tags.add(COMM(encoding=3, lang="eng", desc="", text=value))
        elif key == "source_url":
            tags.add(WOAS(url=value))
            tags.add(TXXX(encoding=3, desc=key, text=value))
        else:
            tags.add(TXXX(encoding=3, desc=key, text=value))
    if cover:
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover))
    tags.save(path)


def _freeform(value: str) -> MP4FreeForm:
    return MP4FreeForm(value.encode("utf-8"), dataformat=AtomDataType.UTF8)


def _write_mp4(path: Path, meta: TrackMetadata, source: dict[str, str], cover: bytes | None) -> None:
    audio = MP4(path)
    audio["\xa9nam"] = [meta.title]
    audio["\xa9ART"] = [meta.artist]
    audio["\xa9alb"] = [meta.effective_album]
    audio["aART"] = [meta.effective_album_artist]
    if meta.year:
        audio["\xa9day"] = [str(meta.year)]
    if meta.genre:
        audio["\xa9gen"] = [meta.genre]
    if meta.musicbrainz:
        audio["----:com.apple.iTunes:MusicBrainz Track Id"] = [_freeform(meta.musicbrainz.recording_id)]
        if meta.musicbrainz.release_id:
            audio["----:com.apple.iTunes:MusicBrainz Album Id"] = [_freeform(meta.musicbrainz.release_id)]
        if meta.musicbrainz.artist_ids:
            audio["----:com.apple.iTunes:MusicBrainz Artist Id"] = [
                _freeform(artist_id) for artist_id in meta.musicbrainz.artist_ids
            ]
    for key, value in source.items():
        if key == "description":
            audio["\xa9cmt"] = [value]
        else:
            audio[f"----:com.apple.iTunes:{key}"] = [_freeform(value)]
    if cover:
        audio["covr"] = [MP4Cover(cover, imageformat=MP4Cover.FORMAT_JPEG)]
    audio.save()
