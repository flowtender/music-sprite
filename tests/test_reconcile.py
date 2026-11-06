import json

from conftest import release, upload

from music_sprite.models import AudioFeatures, Choices, Findings, LlmVerdict
from music_sprite.reconcile import reconcile


def verdict(answers, confidence=0.9) -> LlmVerdict:
    return LlmVerdict("m", answers, "because", confidence)


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
    def test_release_fields_come_from_the_release_unless_another_sources_value_is_picked(self):
        findings = Findings(upload=upload(album="Other", year=1999), releases=[release(album="Salt", year=2021)],
                            llm=verdict({"track_number": 5}))
        result = reconcile(findings, Choices())
        assert (result.metadata.album, result.metadata.year, result.metadata.release.track_number) == ("Salt", 2021, 3)
        assert [c.sources for c in result.fields["album"].candidates] == [["musicbrainz"], ["upload"]]
        assert result.fields["album"].conflicted and result.fields["track_number"].conflicted
        picked = reconcile(findings, Choices(fields={"album": "upload", "track_number": "llm"})).metadata
        assert (picked.album, picked.release.track_number, picked.release.label) == ("Other", 5, "Label Co")

    def test_a_year_from_another_source_drops_the_release_date_that_would_be_tagged_instead(self):
        findings = Findings(upload=upload(year=1999), releases=[release(year=2021)])
        assert reconcile(findings, Choices()).metadata.release.release_date == "2021-05-01"
        for choices in (Choices(fields={"year": "upload"}), Choices(manual={"year": 1999})):
            metadata = reconcile(findings, choices).metadata
            assert (metadata.year, metadata.release.release_date, metadata.release.original_date) == (1999, None, "2019")

    def test_without_a_release_album_and_year_are_chosen_per_field(self):
        findings = Findings(upload=upload(album="Salt", year=2020), llm=verdict({"album": "Salt EP", "year": 2021}))
        result = reconcile(findings, Choices(fields={"year": "upload"}))
        assert (result.metadata.album, result.metadata.year) == ("Salt EP", 2020)
        assert [name for name, report in result.fields.items() if report.conflicted] == ["album", "year"]

    def test_ties_are_resolved_by_user_then_llm_then_rank(self):
        releases = [release(album="Best of", year=2023, recording="a"), release(album="Salt", year=2021, recording="b"),
                    release(album="Live", year=2022, recording="c")]
        findings = Findings(upload=upload(), releases=releases)
        assert reconcile(findings, Choices()).metadata.album == "Best of"
        findings.llm = verdict({"release": 1})
        assert reconcile(findings, Choices()).metadata.album == "Salt"
        assert reconcile(findings, Choices(release=2)).metadata.album == "Live"
        assert reconcile(findings, Choices()).release_conflicted

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


class TestLlmGuesses:
    def test_without_a_release_the_llms_track_number_label_and_credits_are_tagged(self):
        guesses = {"track_number": 7, "track_total": 9, "label": "Driftwood", "catalog_number": "DW012",
                   "composer": ["Ana Reyes"]}
        release_info = reconcile(Findings(upload=upload(), llm=verdict(guesses)), Choices()).metadata.release
        assert (release_info.track_number, release_info.track_total) == (7, 9)
        assert (release_info.label, release_info.catalog_number) == ("Driftwood", "DW012")
        assert release_info.credits == {"composer": ["Ana Reyes"]}

    def test_a_credit_is_taken_whole_from_one_source_and_the_releases_other_credits_stay(self):
        match = release(credits={"composer": ["Kim Shore"], "engineer": ["Lee Park"]})
        findings = Findings(upload=upload(), releases=[match], llm=verdict({"composer": ["Ana Reyes", "Kim Shore"]}))
        assert reconcile(findings, Choices()).metadata.release.credits["composer"] == ["Kim Shore"]
        picked = reconcile(findings, Choices(fields={"composer": "llm"})).metadata.release.credits
        assert picked == {"engineer": ["Lee Park"], "composer": ["Ana Reyes", "Kim Shore"]}

    def test_the_llms_genres_join_the_others(self):
        audio = AudioFeatures(bpm=1, key="x", genres=[("Techno", 0.5)])
        findings = Findings(upload=upload(genres=["electronic"]), audio=audio, llm=verdict({"genres": ["Acid Techno", "techno"]}))
        assert reconcile(findings, Choices()).metadata.genres == ["Techno", "Acid Techno", "electronic"]


def test_findings_and_choices_survive_json():
    findings = Findings(
        upload=upload(genres=["x"]),
        releases=[release(credits={"composer": ["A"]})],
        audio=AudioFeatures(bpm=1, key="k", genres=[("Techno", 0.5)]),
        llm=verdict({"title": "t", "release": 0, "composer": ["A", "B"]}),
    )
    choices = Choices(release=0, fields={"artist": "llm"}, manual={"year": 1})
    back_findings = Findings.from_dict(json.loads(json.dumps(findings.to_dict())))
    back_choices = Choices.from_dict(json.loads(json.dumps(choices.to_dict())))
    assert back_choices == choices
    # JSON turns the audio's (name, probability) tuples into lists; what matters is that the result is the same.
    assert reconcile(back_findings, back_choices).metadata.to_dict() == json.loads(
        json.dumps(reconcile(findings, choices).metadata.to_dict())
    )


def test_findings_saved_when_the_llm_was_asked_about_some_fields_only_still_load():
    saved = {"upload": upload().to_dict(), "llm": {"model": "m", "asked": ["title"], "answers": {"title": "t"},
                                                   "reasoning": "r", "confidence": 0.5}}
    assert Findings.from_dict(saved).llm == LlmVerdict("m", {"title": "t"}, "r", 0.5)
