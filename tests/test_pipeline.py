import asyncio
from pathlib import Path

from conftest import resolution

from music_sprite import library
from music_sprite.staging import EntryStatus
from music_sprite.ui import import_message


def run(coroutine):
    return asyncio.run(coroutine)


class TestResolveEntry:
    def test_a_complete_lookup_resolves_the_entry_and_saves_it(self, make_pipeline, staging, make_entry):
        entry = make_entry()
        run(make_pipeline().resolve_entry(entry))
        saved = staging.get(entry.id)
        assert (saved.status, saved.resolve_error, saved.resolve_steps) == (EntryStatus.RESOLVED, None, ["Upload: found"])
        assert saved.meta.album == "Album 0"

    def test_an_unavailable_service_leaves_the_entry_partial(self, make_pipeline, resolver, make_entry):
        resolver.resolve.return_value = resolution(unavailable=["acoustid", "llm"])
        entry = make_entry()
        run(make_pipeline().resolve_entry(entry))
        assert entry.status == EntryStatus.PARTIAL
        assert entry.resolve_error == "acoustid, llm unavailable; use Re-lookup later"

    def test_a_crash_leaves_the_entry_pending(self, make_pipeline, resolver, staging, make_entry):
        resolver.resolve.side_effect = RuntimeError("boom")
        entry = make_entry()
        run(make_pipeline().resolve_entry(entry))
        saved = staging.get(entry.id)
        assert (saved.status, saved.resolve_error) == (EntryStatus.PENDING, "Lookup crashed; see the service log.")

    def test_hand_edits_are_kept_unless_overwriting_is_asked_for(self, make_pipeline, resolver, make_entry):
        pipeline, entry = make_pipeline(), make_entry()
        entry.choices.manual = {"year": 1999}
        run(pipeline.resolve_entry(entry))
        assert resolver.resolve.await_count == 0
        run(pipeline.resolve_entry(entry, overwrite_edits=True))
        assert resolver.resolve.await_count == 1 and not entry.edited

    def test_only_unfinished_unedited_entries_are_looked_up_again(self, make_pipeline, staging, make_entry):
        done, edited, pending = make_entry("aaaaaaa1"), make_entry("aaaaaaa2"), make_entry("aaaaaaa3")
        done.status = EntryStatus.RESOLVED
        edited.choices.manual = {"year": 1999}
        for entry in (done, edited, pending):
            staging.save(entry)
        assert run(make_pipeline().resolve_unfinished()) == 1
        assert staging.get("aaaaaaa3").status == EntryStatus.RESOLVED


class TestAutoImport:
    def _resolved(self, pipeline, resolver, make_entry, entry_id="aaaaaaa1", **kwargs):
        resolver.resolve.return_value = resolution(**kwargs)
        entry = make_entry(entry_id)
        run(pipeline.resolve_entry(entry))
        return entry

    def test_off_without_a_threshold(self, make_pipeline, resolver, make_entry):
        pipeline = make_pipeline()
        assert not pipeline.qualifies_for_auto_import(self._resolved(pipeline, resolver, make_entry))

    def test_a_confident_complete_lookup_qualifies(self, make_pipeline, resolver, make_entry):
        pipeline = make_pipeline(auto_import_confidence=0.9)
        assert pipeline.qualifies_for_auto_import(self._resolved(pipeline, resolver, make_entry))

    def test_a_release_tie_or_an_incomplete_lookup_does_not(self, make_pipeline, resolver, make_entry):
        pipeline = make_pipeline(auto_import_confidence=0.9)
        tie = self._resolved(pipeline, resolver, make_entry, "aaaaaaa1", releases=2)
        incomplete = self._resolved(pipeline, resolver, make_entry, "aaaaaaa2", unavailable=["llm"])
        assert not pipeline.qualifies_for_auto_import(tie)
        assert not pipeline.qualifies_for_auto_import(incomplete)

    def test_low_confidence_does_not(self, make_pipeline, resolver, make_entry):
        pipeline = make_pipeline(auto_import_confidence=0.99)
        assert not pipeline.qualifies_for_auto_import(self._resolved(pipeline, resolver, make_entry))

    def test_only_qualifying_entries_are_imported_and_counted(self, make_pipeline, resolver, make_entry, imported):
        pipeline = make_pipeline(auto_import_confidence=0.9)
        sure = self._resolved(pipeline, resolver, make_entry, "aaaaaaa1")
        tie = self._resolved(pipeline, resolver, make_entry, "aaaaaaa2", releases=2)
        assert run(pipeline.auto_import([sure, tie])) == 1
        assert imported == [sure.id]


class TestImport:
    def test_an_import_reports_where_the_track_went(self, make_pipeline, staging, make_entry, imported):
        pipeline, entry = make_pipeline(), make_entry()
        run(pipeline.resolve_entry(entry))
        result = run(pipeline.import_entry(entry))
        assert result.added and result.path == Path("Tidal Pools/Low Tide.opus")
        assert import_message(result) == "Added **Tidal Pools – Low Tide**\n-# Tidal Pools/Low Tide.opus"
        assert imported == [entry.id] and staging.get(entry.id) is None

    def test_a_track_already_being_imported_is_not_imported_twice(self, make_pipeline, make_entry, imported):
        pipeline, entry = make_pipeline(), make_entry()
        run(pipeline.resolve_entry(entry))

        async def twice():
            return await asyncio.gather(pipeline.import_entry(entry), pipeline.import_entry(entry))

        first, second = run(twice())
        assert first.added and not second.added
        assert second.error == "This track is already being imported."
        assert imported == [entry.id]

    def test_a_failed_import_keeps_the_entry_queued_and_can_be_retried(self, make_pipeline, staging, make_entry, monkeypatch):
        def broken(config, entry, meta):
            raise OSError("disk full")

        monkeypatch.setattr(library, "add_to_library", broken)
        pipeline, entry = make_pipeline(), make_entry()
        run(pipeline.resolve_entry(entry))
        result = run(pipeline.import_entry(entry))
        assert not result.added
        assert result.error == f"Tagging or moving failed; `{entry.id}` is still in the queue. See the service log."
        assert staging.get(entry.id) is not None
        assert run(pipeline.import_entry(entry)).error == result.error  # not stuck as "already being imported"
