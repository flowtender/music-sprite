import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from conftest import llm_config, release

from music_sprite.llm import LlmClient
from music_sprite.models import AudioFeatures, Choices, LookupUnavailable
from music_sprite.reconcile import reconcile
from music_sprite.resolver import MetadataResolver

INFO = {"channel": "Tidal Pools", "title": "Tidal Pools - Low Tide", "description": "hi", "duration": 200}


class Barrier:
    """Every source waits here for all the others, so the test hangs (and fails) if they run one after another."""

    def __init__(self, parties):
        self._parties = parties
        self._arrived = 0
        self._all = asyncio.Event()

    async def wait(self):
        self._arrived += 1
        if self._arrived == self._parties:
            self._all.set()
        await asyncio.wait_for(self._all.wait(), timeout=2)


class FakeAcoustId:
    disabled_reason = None

    def __init__(self, barrier=None, recordings=("rec1",), fail=False):
        self.barrier, self.recordings, self.fail = barrier, recordings, fail

    async def identify(self, path):
        if self.barrier:
            await self.barrier.wait()
        if self.fail:
            raise LookupUnavailable("acoustid")
        return SimpleNamespace(recording_ids=list(self.recordings), score=0.97) if self.recordings else None


class FakeMusicBrainz:
    def __init__(self, fail=False):
        self.fail = fail

    async def fetch_recording(self, recording_id, draft, confidence, origin):
        if self.fail:
            raise LookupUnavailable("musicbrainz")
        return release(recording=recording_id, origin=origin, confidence=confidence)


class FakeAnalyzer:
    disabled_reason = None

    def __init__(self, barrier=None):
        self.barrier = barrier

    async def analyze(self, path):
        if self.barrier:
            await self.barrier.wait()
        return AudioFeatures(bpm=100, key="A minor", genres=[("Techno", 0.5)])


def resolve(*, acoustid=None, musicbrainz=None, analyzer=None, llm_reply=None, info=None, model="m"):
    requests = []

    def handler(request):
        requests.append(json.loads(json.loads(request.content)["messages"][1]["content"]))
        answer = {"reasoning": "r", "title": None, "artist": None, "album": None, "album_artist": None,
                  "year": None, "release": None, "confidence": 0.8, **(llm_reply or {})}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(answer)}}]})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            resolver = MetadataResolver(
                frozenset(),
                acoustid or FakeAcoustId(),
                musicbrainz or FakeMusicBrainz(),
                LlmClient(http, llm_config(model=model), frozenset()),
                analyzer or FakeAnalyzer(),
            )
            return await resolver.resolve(info or INFO, "x.opus")

    return asyncio.run(run()), requests


def test_identification_and_analysis_run_side_by_side():
    barrier = Barrier(2)
    resolution, _ = resolve(acoustid=FakeAcoustId(barrier), analyzer=FakeAnalyzer(barrier))
    assert resolution.findings.audio is not None
    assert [r.musicbrainz.recording_id for r in resolution.findings.releases] == ["rec1"]


def test_the_llm_is_skipped_when_nothing_is_ambiguous():
    info = {**INFO, "artist": "Tidal Pools", "track": "Low Tide"}  # source tags: confidence 0.9, matches MusicBrainz
    resolution, requests = resolve(info=info)
    assert requests == [] and resolution.findings.llm is None


def test_the_llm_is_asked_only_about_ambiguous_fields_and_cannot_override_the_rest():
    # The upload says "Low Tide (Official)" while MusicBrainz says "Low Tide": only the title is in dispute.
    info = {**INFO, "title": "Tidal Pools - Low Tide (Official Audio) feat", "artist": "Tidal Pools", "track": "Low Tide (feat)"}
    resolution, requests = resolve(info=info, llm_reply={"title": "Low Tide", "artist": "Impostor", "album": "Nope"})
    assert requests[0]["ask"] == ["title"]
    assert resolution.findings.llm.answers == {"title": "Low Tide"}
    metadata = reconcile(resolution.findings, Choices()).metadata
    assert (metadata.artist, metadata.album) == ("Tidal Pools", "Salt")


@pytest.mark.parametrize(
    "acoustid, musicbrainz, service",
    [
        (FakeAcoustId(fail=True), FakeMusicBrainz(), "acoustid"),
        (FakeAcoustId(recordings=("a", "b")), FakeMusicBrainz(fail=True), "musicbrainz"),
    ],
)
def test_an_outage_is_reported_once_and_the_other_sources_still_count(acoustid, musicbrainz, service):
    resolution, _ = resolve(acoustid=acoustid, musicbrainz=musicbrainz)
    assert resolution.unavailable_services == [service]
    assert resolution.findings.audio is not None
    assert resolution.findings.releases == []


def test_a_disabled_llm_is_skipped_without_a_request():
    resolution, requests = resolve(acoustid=FakeAcoustId(recordings=()), model="")
    assert requests == [] and resolution.findings.llm is None
