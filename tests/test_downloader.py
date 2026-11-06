import json

import pytest

from music_sprite import heuristics
from music_sprite.downloader import DownloadedTrack, split_into_chapters

ALBUM = {
    "id": "abc123",
    "title": "Mira Vale - Salt (Full Album)",
    "fulltitle": "Mira Vale - Salt (Full Album)",
    "channel": "Mira Vale",
    "duration": 600,
    # YouTube Music's tags name the whole upload as one song.
    "track": "Salt (Full Album)",
    "artist": "Mira Vale",
    "chapters": [
        {"start_time": 0, "end_time": 200, "title": "Low Tide"},
        {"start_time": 200, "end_time": 410.5, "title": "Glasshouse"},
        {"start_time": 410.5, "title": "Undertow"},  # yt-dlp leaves the last end out of the info JSON
    ],
}


def downloaded(directory, info, chapter_files=(), cover=True) -> DownloadedTrack:
    """What yt-dlp leaves behind: the whole upload's audio, info JSON and cover, plus the split chapters' audio."""
    audio = directory / "abc123.opus"
    audio.write_bytes(b"whole")
    info_path = directory / "abc123.info.json"
    info_path.write_text(json.dumps(info), encoding="utf-8")
    cover_path = directory / "abc123.jpg" if cover else None
    if cover_path:
        cover_path.write_bytes(b"cover")
    for number in chapter_files:
        (directory / f"abc123.{number:03d}.opus").write_bytes(b"chapter")
    return DownloadedTrack(audio, info_path, cover_path)


def test_a_split_upload_arrives_as_its_chapters_each_a_track_of_its_own(tmp_path):
    whole = downloaded(tmp_path, ALBUM, chapter_files=(1, 2, 3))

    tracks = split_into_chapters(whole)

    infos = [json.loads(track.info_path.read_text(encoding="utf-8")) for track in tracks]
    assert [track.audio_path.name for track in tracks] == ["abc123.001.opus", "abc123.002.opus", "abc123.003.opus"]
    assert [(info["title"], info["duration"]) for info in infos] == [
        ("Low Tide", 200), ("Glasshouse", 210.5), ("Undertow", 189.5)
    ]
    assert [(info["section_start"], info["section_end"]) for info in infos][2] == (410.5, 600)
    assert infos[1]["fulltitle"] == "Mira Vale - Salt (Full Album)"
    # The upload's song tags would otherwise title every chapter after the whole album.
    assert heuristics.guess(infos[1], frozenset()).title == "Glasshouse"
    assert [track.cover_path.read_bytes() for track in tracks] == [b"cover"] * 3
    assert not any(path.exists() for path in (whole.audio_path, whole.info_path, whole.cover_path))


@pytest.mark.parametrize(
    "chapters, chapter_files",
    [
        pytest.param(ALBUM["chapters"], (), id="not split"),
        pytest.param(ALBUM["chapters"][:1], (1,), id="one chapter"),
        pytest.param(ALBUM["chapters"], (1, 2), id="a chapter missing"),
    ],
)
def test_an_upload_that_was_not_fully_split_arrives_whole(tmp_path, chapters, chapter_files):
    whole = downloaded(tmp_path, {**ALBUM, "chapters": chapters}, chapter_files)

    assert split_into_chapters(whole) == [whole]
    assert whole.audio_path.read_bytes() == b"whole"
