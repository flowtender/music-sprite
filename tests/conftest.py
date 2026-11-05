import json
import shutil
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from music_sprite import library
from music_sprite.config import LibraryConfig, LlmConfig, QueueConfig
from music_sprite.models import Findings, MusicBrainzIds, ReleaseInfo, TrackMetadata
from music_sprite.pipeline import Pipeline
from music_sprite.resolver import Resolution
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


def resolution(*, releases=1, unavailable=()) -> Resolution:
    """What a lookup returns: the upload's own tags plus `releases` tied MusicBrainz matches."""
    matches = [release(album=f"Album {i}", recording=f"r{i}") for i in range(releases)]
    return Resolution(Findings(upload=upload(), releases=matches), list(unavailable))


def llm_config(**overrides) -> LlmConfig:
    values = dict(
        model="m", base_url="http://llm", api_key="", json_mode="schema", timeout_seconds=5, description_chars=100,
        call_log=None,
    )
    return LlmConfig(**{**values, **overrides})


@pytest.fixture
def staging(tmp_path) -> Staging:
    return Staging(tmp_path)


@pytest.fixture
def resolver():
    """A metadata resolver whose answer each test sets on resolver.resolve."""
    return SimpleNamespace(resolve=AsyncMock(return_value=resolution()))


@pytest.fixture
def make_pipeline(staging, resolver, tmp_path):
    def make(auto_import_confidence=None) -> Pipeline:
        queue = QueueConfig(
            directory=staging.root, max_concurrent_downloads=1, max_playlist_items=10,
            auto_import_confidence=auto_import_confidence,
        )
        library_config = LibraryConfig(
            music_root=tmp_path / "music", path_template="{artist}/{title}", archive_dir=tmp_path / "archive",
            navidrome_url="", navidrome_user="", navidrome_password="",
        )
        return Pipeline(queue, library_config, staging, resolver, http=None)

    return make


@pytest.fixture
def imported(monkeypatch):
    """Records library imports instead of tagging files; like the real one, an import empties the queue entry."""
    calls = []

    def add_to_library(config, entry, meta):
        calls.append(entry.id)
        shutil.rmtree(entry.directory)
        return config.music_root / meta.artist / f"{meta.title}.opus"

    monkeypatch.setattr(library, "add_to_library", add_to_library)
    monkeypatch.setattr(library, "trigger_navidrome_scan", AsyncMock())
    return calls


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
