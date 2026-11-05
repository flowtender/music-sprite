import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import resolution
from discord.ui import TextDisplay

from music_sprite import downloader
from music_sprite.bot import MusicBot
from music_sprite.downloader import DownloadedTrack, DownloadError


def run(coroutine):
    return asyncio.run(coroutine)


def texts(view):
    return "\n".join(child.content for child in view.walk_children() if isinstance(child, TextDisplay))


@pytest.fixture
def music_bot(staging, make_pipeline):
    instance = MusicBot.__new__(MusicBot)
    instance.staging = staging
    instance.pipeline = make_pipeline()
    instance.publish_review = AsyncMock()
    return instance


class FakeReply:
    def __init__(self):
        self.id = 7
        self.channel = SimpleNamespace(id=555)
        self.views = []

    async def edit(self, view):
        self.views.append(view)


@pytest.fixture
def downloads(monkeypatch):
    """Makes yt-dlp produce `count` tracks, then report `failed` failed items or fail with `error`."""
    plan = SimpleNamespace(count=1, failed=0, error=None)

    async def download(url, extra_args, output_dir, max_items, on_track):
        output_dir.mkdir(parents=True, exist_ok=True)
        for index in range(plan.count):
            audio = output_dir / f"t{index}.opus"
            audio.write_bytes(b"")
            info = output_dir / f"t{index}.info.json"
            info.write_text(json.dumps({"title": f"Track {index}"}), encoding="utf-8")
            await on_track(DownloadedTrack(audio, info, None))
        if plan.error:
            raise DownloadError(plan.error)
        return ["ERROR: gone"] * plan.failed

    monkeypatch.setattr(downloader, "download", download)
    return plan


class TestDownloadBatch:
    def test_a_single_track_gets_its_review_card_in_the_progress_message(self, music_bot, downloads):
        reply = FakeReply()
        run(music_bot._run_download(reply, "https://x", [], 1))
        (entry,) = music_bot.staging.list()
        music_bot.publish_review.assert_awaited_once()
        args, kwargs = music_bot.publish_review.await_args
        assert (args[0].id, args[1], kwargs["first"]) == (entry.id, 555, reply)
        assert kwargs["note"] == "Downloaded 1 track(s). 1 waiting in the queue (0 with incomplete lookup)."
        assert not any((music_bot.staging.root / ".incoming").glob("*"))  # the download's scratch folder is gone

    def test_several_tracks_end_in_the_queue_list_with_a_summary(self, music_bot, resolver, downloads):
        downloads.count, downloads.failed = 2, 1
        resolver.resolve.side_effect = [resolution(), resolution(unavailable=["llm"])]
        reply = FakeReply()
        run(music_bot._run_download(reply, "https://x", [], 1))
        final = texts(reply.views[-1])
        assert (
            "Downloaded 2 track(s). 2 waiting in the queue (1 with incomplete lookup). 1 item(s) failed to download."
            in final
        )
        assert "### Download queue · 2 track(s)" in final
        music_bot.publish_review.assert_not_awaited()

    def test_confident_tracks_are_imported_and_ties_wait_for_a_choice(
        self, music_bot, make_pipeline, resolver, downloads, imported
    ):
        downloads.count = 2
        music_bot.pipeline = make_pipeline(auto_import_confidence=0.9)
        resolver.resolve.side_effect = [resolution(), resolution(releases=2)]
        run(music_bot._run_download(FakeReply(), "https://x", [], 1))
        assert len(imported) == 1
        (waiting,) = music_bot.staging.list()
        assert waiting.needs_choice
        assert music_bot.publish_review.await_args.kwargs["note"] == (
            "Downloaded 2 track(s). 1 added to the library automatically. "
            "1 waiting in the queue (0 with incomplete lookup). 1 need a release choice."
        )

    def test_a_failed_download_is_reported(self, music_bot, resolver, downloads):
        downloads.count, downloads.error = 0, "ERROR: unsupported URL"
        reply = FakeReply()
        run(music_bot._run_download(reply, "https://x", [], 1))
        assert texts(reply.views[-1]) == "Download failed:\n```\nERROR: unsupported URL\n```"
        assert resolver.resolve.await_count == 0

    def test_lookup_progress_is_shown_for_each_track(self, music_bot, resolver, downloads):
        downloads.count = 2

        async def resolve(info, audio_path, progress):
            await progress(["Upload: found"], ["Asking the LLM"])
            return resolution()

        resolver.resolve.side_effect = resolve
        reply = FakeReply()
        run(music_bot._run_download(reply, "https://x", [], 1))
        shown = [texts(view) for view in reply.views]
        for position in (1, 2):
            assert any(
                text.startswith(f"**Looking up metadata {position}/2**") and "-# ⏳ Asking the LLM…" in text
                for text in shown
            )
