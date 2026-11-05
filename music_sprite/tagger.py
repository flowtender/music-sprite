"""Writes TrackMetadata into the audio file's own tags (Vorbis comments, ID3 or MP4 atoms)."""

import base64
import json
from pathlib import Path

from mutagen.flac import FLAC, Picture
from mutagen.id3 import (
    APIC, COMM, ID3, TALB, TBPM, TCOM, TCON, TDOR, TDRC, TEXT, TIPL, TIT2, TKEY, TLAN, TMCL, TMED, TMOO, TPE1, TPE2,
    TPE3, TPE4, TPOS, TPUB, TRCK, TSO2, TSOP, TSRC, TXXX, UFID, WOAS, ID3NoHeaderError,
)
from mutagen.mp4 import MP4, AtomDataType, MP4Cover, MP4FreeForm
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

from .downloader import portable_info
from .models import REPLAYGAIN_REFERENCE_LUFS, TrackMetadata

MAX_COMMENT_CHARS = 4000

# Every tag is first built under its Vorbis comment name (see _fields) and translated from there. These names have a
# native frame or atom in some formats; the rest are written as custom text frames (ID3) or free-form atoms (MP4).
ID3_FRAMES = {
    "title": TIT2, "artist": TPE1, "album": TALB, "albumartist": TPE2, "genre": TCON, "date": TDRC,
    "originaldate": TDOR, "label": TPUB, "media": TMED, "language": TLAN, "composer": TCOM, "lyricist": TEXT,
    "conductor": TPE3, "remixer": TPE4, "isrc": TSRC, "artistsort": TSOP, "albumartistsort": TSO2, "mood": TMOO,
    "bpm": TBPM, "initialkey": TKEY,
}
# ID3 "involved people" roles
ID3_PEOPLE = {"arranger": "arranger", "engineer": "engineer", "producer": "producer", "mixer": "mix"}
MP4_ATOMS = {
    "title": "\xa9nam", "artist": "\xa9ART", "album": "\xa9alb", "albumartist": "aART", "genre": "\xa9gen",
    "date": "\xa9day", "composer": "\xa9wrt", "artistsort": "soar", "albumartistsort": "soaa",
}
# Written as the track/disc number and total, or implied by the date
NUMBERING = frozenset({"tracknumber", "tracktotal", "totaltracks", "discnumber", "disctotal", "totaldiscs"})
FOLDED = NUMBERING | {"originalyear"}
# Names that picard-style taggers use for custom frames; anything else is its upper-case name.
DESCRIPTORS = {
    "releasecountry": "MusicBrainz Album Release Country",
    "releasestatus": "MusicBrainz Album Status",
    "releasetype": "MusicBrainz Album Type",
    "musicbrainz_trackid": "MusicBrainz Track Id",
    "musicbrainz_albumid": "MusicBrainz Album Id",
    "musicbrainz_artistid": "MusicBrainz Artist Id",
    "musicbrainz_albumartistid": "MusicBrainz Album Artist Id",
    "musicbrainz_releasegroupid": "MusicBrainz Release Group Id",
    "musicbrainz_releasetrackid": "MusicBrainz Release Track Id",
    "musicbrainz_workid": "MusicBrainz Work Id",
}


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
        "source_metadata": json.dumps(portable_info(info), ensure_ascii=False, default=str),
    }
    return {key: str(value) for key, value in fields.items() if value}


def write_tags(path: Path, meta: TrackMetadata, source: dict[str, str], cover_path: Path | None) -> None:
    writer = WRITERS.get(path.suffix.lower())
    if writer is None:
        raise UnsupportedFormat(f"cannot tag {path.suffix} files")
    cover = cover_path.read_bytes() if cover_path else None
    writer(path, meta, source, cover)


def _fields(meta: TrackMetadata, opus: bool) -> dict[str, list[str]]:
    """Every tag under its Vorbis comment name. Empty values are left out."""
    fields: dict[str, list[str]] = {}

    def put(name: str, value: object) -> None:
        values = value if isinstance(value, list) else [value]
        cleaned = [str(item) for item in values if item not in (None, "")]
        if cleaned:
            fields[name] = cleaned

    release, ids, audio = meta.release, meta.musicbrainz, meta.audio
    put("title", meta.title)
    put("artist", meta.artist)
    put("album", meta.effective_album)
    put("albumartist", meta.effective_album_artist)
    put("date", release.release_date or meta.year)
    put("originaldate", release.original_date)
    put("originalyear", (release.original_date or "")[:4])
    put("genre", meta.genres)
    put("tracknumber", release.track_number)
    put("tracktotal", release.track_total)
    put("totaltracks", release.track_total)
    put("discnumber", release.disc_number)
    put("disctotal", release.disc_total)
    put("totaldiscs", release.disc_total)
    put("artistsort", release.artist_sort)
    put("albumartistsort", release.album_artist_sort)
    put("isrc", release.isrcs)
    put("iswc", release.iswcs)
    put("label", release.label)
    put("catalognumber", release.catalog_number)
    put("barcode", release.barcode)
    put("releasecountry", release.country)
    put("releasestatus", release.status)
    put("releasetype", release.types)
    put("media", release.media_format)
    put("language", release.language)
    put("script", release.script)
    for role, names in release.credits.items():  # roles are named like their tags
        put(role, names)
    if ids:
        put("musicbrainz_trackid", ids.recording_id)
        put("musicbrainz_albumid", ids.release_id)
        put("musicbrainz_artistid", ids.artist_ids)
        put("musicbrainz_albumartistid", ids.album_artist_ids)
        put("musicbrainz_releasegroupid", ids.release_group_id)
        put("musicbrainz_releasetrackid", ids.release_track_id)
        put("musicbrainz_workid", ids.work_ids)
    if audio:
        put("bpm", audio.bpm)
        put("initialkey", audio.key)
        put("mood", audio.moods)
        put("instruments", audio.instruments)
        put("vocals", audio.vocals)
        for name, score in audio.scores.items():
            put(name, f"{score:.2f}")
        if audio.loudness is not None and audio.replaygain is not None:
            if opus:
                # Opus players read the gain from R128_TRACK_GAIN; ReplayGain tags there would be applied twice.
                put("r128_track_gain", audio.r128_gain)
            else:
                put("replaygain_track_gain", f"{audio.replaygain:.2f} dB")
                put("replaygain_reference_loudness", f"{REPLAYGAIN_REFERENCE_LUFS:.2f} LUFS")
                if audio.peak is not None:
                    put("replaygain_track_peak", f"{audio.peak:.6f}")
                if audio.loudness_range is not None:
                    put("replaygain_track_range", f"{audio.loudness_range:.2f} dB")
    return fields


def _descriptor(name: str, *, keep_case: bool = False) -> str:
    return DESCRIPTORS.get(name) or (name if keep_case else name.upper())


def _vorbis_fields(meta: TrackMetadata, source: dict[str, str], opus: bool) -> dict[str, list[str]]:
    fields = _fields(meta, opus)
    for key, value in source.items():
        fields["comment" if key == "description" else key] = [value]
    return fields


def _picture(cover: bytes) -> Picture:
    picture = Picture()
    picture.type = 3
    picture.mime = "image/jpeg"
    picture.data = cover
    return picture


def _write_ogg(container, opus: bool):
    def write(path: Path, meta: TrackMetadata, source: dict[str, str], cover: bytes | None) -> None:
        audio = container(path)
        for key, values in _vorbis_fields(meta, source, opus).items():
            audio[key] = values
        if cover:
            audio["metadata_block_picture"] = [base64.b64encode(_picture(cover).write()).decode("ascii")]
        audio.save()

    return write


def _write_flac(path: Path, meta: TrackMetadata, source: dict[str, str], cover: bytes | None) -> None:
    audio = FLAC(path)
    for key, values in _vorbis_fields(meta, source, opus=False).items():
        audio[key] = values
    if cover:
        audio.clear_pictures()
        audio.add_picture(_picture(cover))
    audio.save()


def _performer_pairs(entries: list[str]) -> list[list[str]]:
    """["Jonny (guitar, piano)"] -> [["guitar", "Jonny"], ["piano", "Jonny"]] for ID3's musician credits."""
    pairs = []
    for entry in entries:
        name, opening, parts = entry.rpartition(" (")
        if opening and parts.endswith(")"):
            pairs.extend([part.strip(), name] for part in parts.removesuffix(")").split(","))
        else:
            pairs.append(["performer", entry])
    return pairs


def _number_of_total(fields: dict[str, list[str]], number: str, total: str) -> str | None:
    if number not in fields:
        return None
    return f"{fields[number][0]}/{fields[total][0]}" if total in fields else fields[number][0]


def _write_mp3(path: Path, meta: TrackMetadata, source: dict[str, str], cover: bytes | None) -> None:
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
    fields = _fields(meta, opus=False)
    people = [[ID3_PEOPLE[role], name] for role in ID3_PEOPLE for name in fields.get(role, [])]
    for name, values in fields.items():
        if name in ID3_FRAMES:
            tags.add(ID3_FRAMES[name](encoding=3, text=values))
        elif name == "musicbrainz_trackid":
            tags.add(UFID(owner="http://musicbrainz.org", data=values[0].encode()))
        elif name not in FOLDED and name not in ID3_PEOPLE and name != "performer":
            tags.add(TXXX(encoding=3, desc=_descriptor(name), text=values))
    if track := _number_of_total(fields, "tracknumber", "tracktotal"):
        tags.add(TRCK(encoding=3, text=track))
    if disc := _number_of_total(fields, "discnumber", "disctotal"):
        tags.add(TPOS(encoding=3, text=disc))
    if people:
        tags.add(TIPL(encoding=3, people=people))
    if "performer" in fields:
        tags.add(TMCL(encoding=3, people=_performer_pairs(fields["performer"])))
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
    fields = _fields(meta, opus=False)
    for name, values in fields.items():
        if name in MP4_ATOMS:
            audio[MP4_ATOMS[name]] = values
        elif name == "bpm":
            audio["tmpo"] = [int(values[0])]
        elif name not in FOLDED:
            keep_case = name == "initialkey" or name.startswith("replaygain_")
            audio[f"----:com.apple.iTunes:{_descriptor(name, keep_case=keep_case)}"] = [
                _freeform(value) for value in values
            ]
    if "tracknumber" in fields:
        audio["trkn"] = [(int(fields["tracknumber"][0]), int(fields.get("tracktotal", ["0"])[0]))]
    if "discnumber" in fields:
        audio["disk"] = [(int(fields["discnumber"][0]), int(fields.get("disctotal", ["0"])[0]))]
    for key, value in source.items():
        if key == "description":
            audio["\xa9cmt"] = [value]
        else:
            audio[f"----:com.apple.iTunes:{key}"] = [_freeform(value)]
    if cover:
        audio["covr"] = [MP4Cover(cover, imageformat=MP4Cover.FORMAT_JPEG)]
    audio.save()


WRITERS = {
    ".opus": _write_ogg(OggOpus, opus=True),
    ".ogg": _write_ogg(OggVorbis, opus=False),
    ".flac": _write_flac,
    ".mp3": _write_mp3,
    ".m4a": _write_mp4,
    ".mp4": _write_mp4,
}
