import json

from conftest import release, upload

from music_sprite.models import AudioFeatures, Findings, TrackMetadata


def test_findings_choices_and_card_location_survive_a_restart(staging, make_entry):
    entry = make_entry()
    entry.set_findings(Findings(upload=upload(), releases=[release(), release(album="B", recording="b")]))
    entry.choices.release = 1
    entry.choices.manual = {"year": 1999}
    entry.refresh_meta()
    entry.review_channel, entry.review_messages, entry.review_note = 555, [1, 2], "note"
    staging.save(entry)

    loaded = staging.get(entry.id)
    assert loaded.meta == entry.meta
    assert loaded.choices == entry.choices
    assert loaded.findings == entry.findings
    assert (loaded.review_channel, loaded.review_messages, loaded.review_note) == (555, [1, 2], "note")
    assert loaded.edited and loaded.needs_choice


def test_an_entry_saved_before_findings_existed_keeps_its_tags_and_audio_features(staging, make_entry):
    entry = make_entry()
    legacy = {
        "id": entry.id, "audio_file": "a.opus", "info_file": "info.json", "cover_file": None, "source_url": "u",
        "requested_by": 1, "batch_id": "b", "created_at": "t", "status": "resolved", "resolve_error": None,
        "resolve_steps": [], "candidates": [], "candidate_index": None, "edited": True,
        "meta": TrackMetadata(title="T", artist="A", audio=AudioFeatures(bpm=90, key="D minor")).to_dict(),
    }
    (entry.directory / "entry.json").write_text(json.dumps(legacy), encoding="utf-8")

    loaded = staging.get(entry.id)
    assert (loaded.meta.title, loaded.meta.artist, loaded.meta.audio.bpm) == ("T", "A", 90)
    assert loaded.reconcile().metadata.audio.bpm == 90
    assert not loaded.needs_choice


def test_needs_choice_means_a_tie_between_releases(make_entry):
    entry = make_entry()
    entry.set_findings(Findings(upload=upload(), releases=[release()]))
    assert not entry.needs_choice
    entry.set_findings(Findings(upload=upload(), releases=[release(), release(album="B", recording="b")]))
    assert entry.needs_choice


def test_a_new_lookup_discards_earlier_choices_and_hand_edits(make_entry):
    entry = make_entry()
    entry.set_findings(Findings(upload=upload(), releases=[release(), release(album="B", recording="b")]))
    entry.choices.release = 1
    entry.choices.manual = {"year": 1999}
    entry.set_findings(Findings(upload=upload()))
    assert entry.choices.release is None and not entry.edited


def test_an_entry_saved_before_its_first_lookup_can_be_listed(staging, make_entry):
    entry = make_entry()
    staging.save(entry)
    assert [loaded.id for loaded in staging.list()] == [entry.id]
    assert staging.get(entry.id).meta is None
