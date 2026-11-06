import asyncio
import json

import httpx
import pytest
from conftest import llm_config, release, upload

from music_sprite.llm import ANSWER_KEYS, LlmClient, trim_upload
from music_sprite.models import AudioFeatures, Findings, LookupUnavailable


def reply(**fields):
    answer = {"reasoning": "r", **{name: None for name in ANSWER_KEYS}, "confidence": 0.8, **fields}
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(answer)}}], "usage": {}})


def resolve(findings, handler, info=None):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = LlmClient(http, llm_config(), frozenset({"epic network"}))
            return await client.resolve(info or {"title": "t"}, findings)

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
    def test_the_model_sees_everything_the_other_sources_found(self):
        seen = {}

        def handler(request):
            seen.update(json.loads(json.loads(request.content)["messages"][1]["content"]))
            return reply()

        findings = Findings(
            upload=upload(title="low tide (official)"),
            releases=[release(), release(album="Live", recording="b")],
            audio=AudioFeatures(bpm=120, key="C major", genres=[("Techno", 0.5)]),
        )
        resolve(findings, handler, info={"title": "t", "formats": [1], "uploader": "u"})
        assert seen["upload"] == {"title": "t", "uploader": "u"}
        assert seen["heuristic_guess"]["title"] == "low tide (official)"
        assert [entry["album"] for entry in seen["musicbrainz"]] == ["Salt", "Live"]
        assert seen["audio_analysis"]["bpm"] == 120


class TestAnswers:
    def test_every_field_answered_counts(self):
        answers = {"title": "Low Tide", "artist": "Tidal Pools", "year": 2021, "track_number": 3, "track_total": 10,
                   "label": "Driftwood", "composer": ["Ana Reyes"], "genres": ["Ambient"]}
        verdict = resolve(Findings(upload=upload()), lambda r: reply(**answers))
        assert verdict.answers == answers
        assert verdict.confidence == 0.8

    def test_unusable_answers_are_dropped_and_sloppy_ones_repaired(self):
        findings = Findings(upload=upload(), releases=[release(), release(album="B", recording="b")])
        sloppy = {"year": "1998", "release": 5, "album": "  ", "track_number": 0, "track_total": "12",
                  "composer": ["", " "], "producer": "Odd Harbor", "lyricist": ["Ana", "ana", " Kim "],
                  "genres": [f"g{index}" for index in range(8)]}
        verdict = resolve(findings, lambda r: reply(**sloppy))
        assert verdict.answers == {"year": 1998, "track_total": 12, "producer": ["Odd Harbor"], "lyricist": ["Ana", "Kim"],
                                   "genres": ["g0", "g1", "g2", "g3", "g4"]}

    def test_a_valid_release_index_is_kept(self):
        findings = Findings(upload=upload(), releases=[release(), release(album="B", recording="b")])
        assert resolve(findings, lambda r: reply(release=1)).answers == {"release": 1}

    def test_an_unreachable_or_garbled_model_raises_lookup_unavailable(self):
        with pytest.raises(LookupUnavailable):
            resolve(Findings(upload=upload()), lambda r: httpx.Response(500))
        garbled = httpx.Response(200, json={"choices": [{"message": {"content": "no json here"}}]})
        with pytest.raises(LookupUnavailable):
            resolve(Findings(upload=upload()), lambda r: garbled)
