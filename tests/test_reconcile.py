import json

from conftest import release, upload

from music_sprite.models import AudioFeatures, Choices, Findings, LlmVerdict
from music_sprite.reconcile import ambiguous_fields, reconcile


def verdict(answers, asked=None, confidence=0.9) -> LlmVerdict:
    return LlmVerdict("m", asked or list(answers), answers, "because", confidence)


class TestPrecedence:
    def test_musicbrainz_beats_llm_beats_upload(self):
        findings = Findings(
            upload=upload(title="low tide (official)"),
            releases=[release(title="Low Tide")],
            llm=verdict({"title": "Low Tide (LLM)"}),
        )
        result = reconcile(findings, Choices())
        assert result.metadata.title == "Low Tide"
        assert [c.sources for c in result.fields["title"].candidates] == [["musicbrainz"], ["llm"], ["upload"]]

    def test_llm_beats_upload_without_a_musicbrainz_match(self):
        findings = Findings(
            upload=upload(artist="Epic Network", confidence=0.1), llm=verdict({"artist": "Mira Vale"})
        )
        assert reconcile(findings, Choices()).metadata.artist == "Mira Vale"

    def test_spellings_differing_only_in_case_or_punctuation_are_one_candidate(self):
        findings = Findings(upload=upload(title="Low Tide"), llm=verdict({"title": "low-tide!"}))
        field = reconcile(findings, Choices()).fields["title"]
        assert len(field.candidates) == 1
        assert field.candidates[0].sources == ["llm", "upload"]
        assert not field.conflicted

    def test_a_pick_overrides_precedence_and_lowers_confidence_to_that_source(self):
        findings = Findings(upload=upload(artist="Tidal Pools", confidence=0.5), releases=[release(artist="Tidal Pools feat. Ana")])
        default = reconcile(findings, Choices())
        picked = reconcile(findings, Choices(fields={"artist": "upload"}))
        assert default.metadata.artist == "Tidal Pools feat. Ana"
        assert picked.metadata.artist == "Tidal Pools"
        assert picked.metadata.confidence < default.metadata.confidence

    def test_a_pick_for_a_source_that_no_longer_applies_is_ignored(self):
        # The MusicBrainz title vanishes when the choice moves to a source without a release.
        findings = Findings(upload=upload(title="Low Tide"))
        assert reconcile(findings, Choices(fields={"title": "musicbrainz"})).metadata.title == "Low Tide"


class TestRelease:
    def test_release_fields_come_from_one_release_even_if_the_upload_disagrees(self):
        findings = Findings(upload=upload(album="Other", year=1999), releases=[release(album="Salt", year=2021)])
        result = reconcile(findings, Choices())
        assert (result.metadata.album, result.metadata.year) == ("Salt", 2021)
        assert not result.fields["album"].selectable and not result.fields["year"].selectable
        # the upload's value stays visible for comparison
        assert [c.sources for c in result.fields["album"].candidates] == [["musicbrainz"], ["upload"]]

    def test_without_a_release_album_and_year_are_chosen_per_field(self):
        findings = Findings(upload=upload(album="Salt", year=2020), llm=verdict({"album": "Salt EP", "year": 2021}))
        result = reconcile(findings, Choices(fields={"year": "upload"}))
        assert (result.metadata.album, result.metadata.year) == ("Salt EP", 2020)
        assert result.conflicts == ["album", "year"]

    def test_ties_are_resolved_by_user_then_llm_then_rank(self):
        releases = [release(album="Best of", year=2023, recording="a"), release(album="Salt", year=2021, recording="b"),
                    release(album="Live", year=2022, recording="c")]
        findings = Findings(upload=upload(), releases=releases)
        assert reconcile(findings, Choices()).metadata.album == "Best of"
        findings.llm = verdict({"release": 1})
        assert reconcile(findings, Choices()).metadata.album == "Salt"
        assert reconcile(findings, Choices(release=2)).metadata.album == "Live"
        assert reconcile(findings, Choices()).conflicts == ["release"]

    def test_out_of_range_picks_fall_back(self):
        findings = Findings(upload=upload(), releases=[release(album="A"), release(album="B", recording="b")],
                            llm=verdict({"release": 7}))
        assert reconcile(findings, Choices(release=9)).release_index == 0


class TestGenresAndAudio:
    def test_genres_from_every_source_are_combined_without_duplicates(self):
        audio = AudioFeatures(bpm=120, key="C major", genres=[("Indie Rock", 0.4), ("Techno", 0.05)])
        findings = Findings(
            upload=upload(genres=["indie"]), releases=[release(genres=["indie rock", "folk"])], audio=audio
        )
        genres = reconcile(findings, Choices()).metadata.genres
        assert genres == ["indie rock", "folk", "indie"]  # MusicBrainz first; Essentia's duplicate and its 5% guess are out

    def test_essentia_fills_in_when_nothing_else_has_genres(self):
        audio = AudioFeatures(bpm=1, key="x", genres=[("Techno", 0.5)])
        assert reconcile(Findings(upload=upload(), audio=audio), Choices()).metadata.genres == ["Techno"]

    def test_audio_features_are_carried_to_the_metadata(self):
        audio = AudioFeatures(bpm=120, key="C major")
        assert reconcile(Findings(upload=upload(), audio=audio), Choices()).metadata.audio == audio


class TestManualEdits:
    def test_manual_values_win_and_are_marked(self):
        findings = Findings(upload=upload(), releases=[release()])
        result = reconcile(findings, Choices(manual={"year": 1999, "genres": ["A", "B"]}))
        assert (result.metadata.year, result.metadata.genres) == (1999, ["A", "B"])
        assert result.metadata.origin.endswith("+edited")
        assert result.fields["year"].manual

    def test_choices_made_later_still_apply_to_other_fields(self):
        findings = Findings(upload=upload(artist="A"), releases=[release(artist="B")])
        result = reconcile(findings, Choices(manual={"title": "Mine"}, fields={"artist": "upload"}))
        assert (result.metadata.title, result.metadata.artist) == ("Mine", "A")


class TestAmbiguity:
    def test_nothing_is_asked_when_a_match_and_the_upload_agree(self):
        findings = Findings(upload=upload(), releases=[release()])
        assert ambiguous_fields(findings) == []

    def test_a_guess_from_the_upload_alone_is_asked_unless_the_site_stated_it(self):
        guessed = Findings(upload=upload(confidence=0.5), releases=[])
        stated = Findings(upload=upload(confidence=0.9), releases=[])
        assert {"title", "artist"} <= set(ambiguous_fields(guessed))
        assert not {"title", "artist"} & set(ambiguous_fields(stated))

    def test_a_missing_artist_is_asked(self):
        assert "artist" in ambiguous_fields(Findings(upload=upload(artist="", confidence=0.1)))

    def test_disagreement_and_release_ties_are_asked(self):
        findings = Findings(upload=upload(title="Low Tide (Official)"),
                            releases=[release(), release(album="B", recording="b")])
        assert ambiguous_fields(findings) == ["title", "release"]

    def test_album_and_year_are_asked_only_without_a_release(self):
        assert {"album", "year"} <= set(ambiguous_fields(Findings(upload=upload())))
        assert not {"album", "year"} & set(ambiguous_fields(Findings(upload=upload(), releases=[release()])))

    def test_the_llms_answers_are_ignored(self):
        findings = Findings(upload=upload(confidence=0.5), llm=verdict({"title": "x", "artist": "y"}))
        assert {"title", "artist"} <= set(ambiguous_fields(findings))


def test_findings_and_choices_survive_json():
    findings = Findings(
        upload=upload(genres=["x"]),
        releases=[release(credits={"composer": ["A"]})],
        audio=AudioFeatures(bpm=1, key="k", genres=[("Techno", 0.5)]),
        llm=verdict({"title": "t", "release": 0}),
    )
    choices = Choices(release=0, fields={"artist": "llm"}, manual={"year": 1})
    back_findings = Findings.from_dict(json.loads(json.dumps(findings.to_dict())))
    back_choices = Choices.from_dict(json.loads(json.dumps(choices.to_dict())))
    assert back_choices == choices
    # JSON turns the audio's (name, probability) tuples into lists; what matters is that the result is the same.
    assert reconcile(back_findings, back_choices).metadata.to_dict() == json.loads(
        json.dumps(reconcile(findings, choices).metadata.to_dict())
    )
