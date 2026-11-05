import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from conftest import release, upload

from music_sprite.llm import LlmClient, trim_upload
from music_sprite.models import AudioFeatures, Findings, LookupUnavailable


def config(**overrides):
    values = dict(
        known_publishers=frozenset({"epic network"}),
        llm_model="m",
        llm_base_url="http://llm",
        llm_api_key="",
        llm_json_mode="schema",
        llm_timeout_seconds=5,
        llm_description_chars=100,
        llm_call_log=None,
    )
    return SimpleNamespace(**{**values, **overrides})


def reply(**fields):
    answer = {"reasoning": "r", "title": None, "artist": None, "album": None, "album_artist": None,
              "year": None, "release": None, "confidence": 0.8, **fields}
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(answer)}}], "usage": {}})


def resolve(findings, asked, handler, info=None):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await LlmClient(http, config()).resolve(info or {"title": "t"}, findings, asked)

    return asyncio.run(run())


class TestTrimUpload:
    def test_bulky_format_data_is_dropped_and_site_specific_fields_are_kept(self):
        info = {
            "title": "t",
            "formats": [{"x": 1}] * 50,
            "thumbnails": [1],
            "http_headers": {"a": "b"},
            "_filename": "f",
            "genre": None,
            "bandcamp_tag_list": ["ambient"],
            "track_number": 3,
        }
        assert trim_upload(info, 100) == {"title": "t", "bandcamp_tag_list": ["ambient"], "track_number": 3}

    def test_the_description_is_capped(self):
        assert len(trim_upload({"description": "d" * 500}, 100)["description"]) == 100


class TestRequest:
    def test_the_model_sees_the_whole_blob_and_what_is_settled(self):
        seen = {}

        def handler(request):
            seen.update(json.loads(json.loads(request.content)["messages"][1]["content"]))
            return reply()

        findings = Findings(
            upload=upload(title="low tide (official)"),
            releases=[release(), release(album="Live", recording="b")],
            audio=AudioFeatures(bpm=120, key="C major", genres=[("Techno", 0.5)]),
        )
        resolve(findings, ["title", "release"], handler, info={"title": "t", "formats": [1], "uploader": "u"})
        assert seen["ask"] == ["title", "release"]
        assert seen["upload"] == {"title": "t", "uploader": "u"}
        assert "title" not in seen["settled"] and seen["settled"]["artist"] == "Tidal Pools"
        assert [entry["album"] for entry in seen["musicbrainz"]] == ["Salt", "Live"]
        assert seen["musicbrainz"][0]["matched_by"] == "audio fingerprint"
        assert seen["audio_analysis"]["bpm"] == 120


class TestAnswers:
    def test_only_asked_fields_count(self):
        verdict = resolve(Findings(upload=upload()), ["title"], lambda r: reply(title="Low Tide", artist="Someone Else"))
        assert verdict.answers == {"title": "Low Tide"}
        assert verdict.confidence == 0.8 and verdict.asked == ["title"]

    def test_unusable_answers_are_dropped(self):
        findings = Findings(upload=upload(), releases=[release(), release(album="B", recording="b")])
        verdict = resolve(findings, ["year", "release", "album"], lambda r: reply(year="1998", release=5, album="  "))
        assert verdict.answers == {"year": 1998}

    def test_a_valid_release_index_is_kept(self):
        findings = Findings(upload=upload(), releases=[release(), release(album="B", recording="b")])
        assert resolve(findings, ["release"], lambda r: reply(release=1)).answers == {"release": 1}

    def test_an_unreachable_or_garbled_model_raises_lookup_unavailable(self):
        with pytest.raises(LookupUnavailable):
            resolve(Findings(upload=upload()), ["title"], lambda r: httpx.Response(500))
        garbled = httpx.Response(200, json={"choices": [{"message": {"content": "no json here"}}]})
        with pytest.raises(LookupUnavailable):
            resolve(Findings(upload=upload()), ["title"], lambda r: garbled)
