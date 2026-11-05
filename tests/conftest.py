import json
from types import SimpleNamespace

import pytest

from music_sprite.models import MusicBrainzIds, ReleaseInfo, TrackMetadata
from music_sprite.staging import QueueEntry, Staging


def release(artist="Tidal Pools", title="Low Tide", album="Salt", year=2021, recording="rec1", *,
            origin="acoustid+musicbrainz", confidence=0.95, genres=(), credits=None) -> TrackMetadata:
    """One MusicBrainz match: a recording on one release."""
    return TrackMetadata(
        title=title,
        artist=artist,
        album=album,
        year=year,
        genres=list(genres),
        confidence=confidence,
        origin=origin,
        musicbrainz=MusicBrainzIds(
            recording_id=recording, release_id=f"{recording}-{album}", release_group_id=f"{recording}-g", length_seconds=200.0
        ),
        release=ReleaseInfo(
            release_date=f"{year}-05-01",
            original_date="2019",
            track_number=3,
            track_total=10,
            disc_number=1,
            disc_total=2,
            label="Label Co",
            catalog_number="LC-1",
            barcode="123456",
            country="US",
            status="official",
            types=["album"],
            language="eng",
            isrcs=["US1234567890"],
            credits=credits or {},
        ),
    )


def upload(title="Low Tide", artist="Tidal Pools", confidence=0.9, **kwargs) -> TrackMetadata:
    return TrackMetadata(title=title, artist=artist, confidence=confidence, origin="source-tags", **kwargs)


@pytest.fixture
def staging(tmp_path) -> Staging:
    return Staging(tmp_path)


@pytest.fixture
def bot(staging):
    return SimpleNamespace(config=SimpleNamespace(known_publishers=frozenset()), staging=staging)


@pytest.fixture
def make_entry(staging):
    def make(entry_id="aaaaaaa1", info=None) -> QueueEntry:
        directory = staging.root / entry_id
        directory.mkdir()
        info = info if info is not None else {"title": "Tidal Pools - Low Tide", "description": "hello"}
        (directory / "info.json").write_text(json.dumps(info), encoding="utf-8")
        return QueueEntry(
            id=entry_id,
            directory=directory,
            audio_file="a.opus",
            info_file="info.json",
            cover_file=None,
            source_url="u",
            requested_by=1,
            batch_id="b",
            created_at="t",
        )

    return make
