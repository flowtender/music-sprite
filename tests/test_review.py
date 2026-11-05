import asyncio
import itertools
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from conftest import release, upload
from discord.ui import Button, Select, TextDisplay

from music_sprite import review
from music_sprite.bot import MusicBot
from music_sprite.models import AudioFeatures, Findings, LlmVerdict
from music_sprite.pipeline import ImportResult
from music_sprite.review import ReviewAction, build_review_pages

BIG_CREDITS = {f"role{i}": [f"Person {i}-{j}" for j in range(50)] for i in range(20)}
NOISY_DESCRIPTION = "Line one\n@everyone buy my stuff\n" * 300


def item_of(child):
    return getattr(child, "item", child)


def audit(views):
    """The limits Discord enforces on one message, for every message of the card."""
    for view in views:
        interactive = sum(isinstance(item_of(child), (Button, Select)) for child in view.walk_children())
        assert view.total_children_count <= 40
        assert view.content_length() <= 4000
        assert interactive <= 30
        view.to_components()  # serializes without error


def texts(view):
    return [child.content for child in view.walk_children() if isinstance(child, TextDisplay)]


def buttons(view):
    return [item_of(child).label for child in view.walk_children() if isinstance(item_of(child), Button)]


@pytest.fixture
def heavy(make_entry):
    entry = make_entry("bbbbbbb2", {"title": "t", "description": NOISY_DESCRIPTION, "thumbnail": "https://example.com/t.jpg"})
    releases = [release(album=f"Album {i}", year=2000 + i, recording=f"r{i}", credits=BIG_CREDITS) for i in range(12)]
    entry.set_findings(
        Findings(
            upload=upload(title="Low Tide (Official)"),
            releases=releases,
            audio=AudioFeatures(bpm=120, key="C", genres=[("Techno", 0.5)], moods=["happy"], scores={"danceability": 0.8}),
            llm=LlmVerdict("m", ["release", "title"], {"release": 4, "title": "Low Tide (LLM)"}, "because", 0.9),
        )
    )
    entry.resolve_steps = ["step " * 70 for _ in range(25)]
    entry.review_note = "Downloaded 1 track(s)."
    return entry


class TestPagination:
    def test_a_small_card_is_one_message_with_the_buttons(self, make_entry):
        entry = make_entry()
        entry.set_findings(Findings(upload=upload(), releases=[release()]))
        views = build_review_pages(entry)
        audit(views)
        assert len(views) == 1
        assert buttons(views[0]) == ["Approve", "Edit", "Re-lookup", "Close"]

    def test_a_heavy_card_splits_within_discords_limits_and_buttons_come_last(self, heavy):
        views = build_review_pages(heavy)
        audit(views)
        assert len(views) > 1
        assert [buttons(view) for view in views[:-1]] == [[]] * (len(views) - 1)
        assert buttons(views[-1]) == ["Approve", "Edit", "Re-lookup", "Close"]

    def test_every_field_is_still_shown_somewhere(self, heavy):
        everything = "\n".join(text for view in build_review_pages(heavy) for text in texts(view))
        for label in ("**Title**", "**Artist:**", "**Release**", "**Album:**", "**Genres:**", "**BPM:**", "Upload description"):
            assert label in everything

    def test_each_conflict_gets_a_drop_down_with_the_current_choice_selected(self, heavy):
        views = build_review_pages(heavy)
        selects = [item_of(c) for view in views for c in view.walk_children() if isinstance(item_of(c), Select)]
        assert len(selects) == 2  # release and title
        release_select = next(s for s in selects if s.custom_id.startswith("ms:r:release:"))
        assert [o.value for o in release_select.options if o.default] == ["4"]

    def test_a_card_that_fills_the_last_message_gets_its_own_for_the_buttons(self, make_entry):
        entry = make_entry()
        entry.set_findings(Findings(upload=upload(), releases=[release()]))
        entry.resolve_steps = ["x" * 280 for _ in range(14)]  # fills a message to the brim
        views = build_review_pages(entry)
        audit(views)
        assert buttons(views[-1]) == ["Approve", "Edit", "Re-lookup", "Close"]

    def test_mentions_in_the_upload_text_are_defused(self, heavy):
        everything = "\n".join(text for view in build_review_pages(heavy) for text in texts(view))
        assert "@everyone" not in everything


class TestEdits:
    def _entry(self, make_entry, **findings):
        entry = make_entry()
        entry.set_findings(Findings(upload=upload(), releases=[release(artist="Tidal Pools feat. Ana")], **findings))
        return entry

    def test_only_what_differs_from_the_sources_is_recorded_as_edited(self, make_entry):
        entry = self._entry(make_entry)
        base = entry.meta
        entry.apply_edits({"title": base.title, "artist": base.artist, "album": base.album, "year": 1999, "genres": base.genres})
        assert entry.choices.manual == {"year": 1999}
        assert entry.edited and entry.meta.year == 1999

    def test_changing_the_artist_drops_the_album_artist_that_belonged_to_it(self, make_entry):
        entry = make_entry()
        match = release()
        match.album_artist = "Tidal Pools"
        entry.set_findings(Findings(upload=upload(), releases=[match]))
        base = entry.meta
        entry.apply_edits({"title": base.title, "artist": "Someone Else", "album": base.album, "year": base.year, "genres": base.genres})
        assert entry.meta.album_artist is None

    def test_editing_a_value_back_to_what_the_sources_say_clears_the_edit(self, make_entry):
        entry = self._entry(make_entry)
        entry.choices.manual = {"year": 1999}
        base = entry.reconcile().metadata
        entry.apply_edits({"title": base.title, "artist": base.artist, "album": base.album, "year": 2021, "genres": base.genres})
        assert entry.choices.manual == {}


class FakeMessage:
    def __init__(self, log, message_id):
        self.id, self._log = message_id, log

    async def edit(self, view):
        self._log["edited"].append(self.id)

    async def delete(self):
        self._log["deleted"].append(self.id)


class FakeChannel:
    def __init__(self, log, counter, vanished=()):
        self._log, self._counter, self._vanished = log, counter, set(vanished)

    async def send(self, view):
        message = FakeMessage(self._log, next(self._counter))
        self._log["sent"].append(message.id)
        return message

    def get_partial_message(self, message_id):
        message = FakeMessage(self._log, message_id)
        if message_id in self._vanished:
            async def gone(view):
                raise discord.NotFound(MagicMock(status=404, reason="x"), "gone")

            message.edit = gone
        return message


@pytest.fixture
def discord_bot(staging):
    """A MusicBot whose channel is recorded instead of sent to."""
    instance = MusicBot.__new__(MusicBot)
    instance.config = SimpleNamespace(lookup=SimpleNamespace(known_publishers=frozenset()))
    instance.staging = staging
    instance.log = {"sent": [], "edited": [], "deleted": []}
    instance.vanished = set()
    counter = itertools.count(100)
    instance.get_partial_messageable = lambda channel_id: FakeChannel(instance.log, counter, instance.vanished)
    instance.pipeline = SimpleNamespace(
        import_entry=AsyncMock(return_value=ImportResult(upload(), error="x")), resolve_entry=AsyncMock()
    )
    return instance


def run(coroutine):
    return asyncio.run(coroutine)


def click(bot, **data):
    interaction = MagicMock()
    interaction.client = bot
    interaction.data = data
    interaction.response.defer = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    return interaction


class TestPublishing:
    def test_the_first_message_is_reused_and_the_rest_follow(self, discord_bot, heavy):
        discord_bot.staging.save(heavy)
        run(discord_bot.publish_review(heavy, 555, first=FakeMessage(discord_bot.log, 7), note="hi"))
        assert heavy.review_messages[0] == 7
        assert heavy.review_messages[1:] == discord_bot.log["sent"]
        assert discord_bot.staging.get("bbbbbbb2").review_messages == heavy.review_messages

    def test_a_track_that_was_never_looked_up_still_gets_a_card(self, discord_bot, make_entry):
        entry = make_entry(info={"title": "Tidal Pools - Low Tide", "channel": "Tidal Pools"})
        run(discord_bot.publish_review(entry, 555))
        saved = discord_bot.staging.get(entry.id)
        assert (saved.meta.artist, saved.meta.title) == ("Tidal Pools", "Low Tide")
        assert saved.review_messages == discord_bot.log["sent"]

    def test_a_release_pick_rederives_the_metadata_and_refreshes_every_message(self, discord_bot, heavy):
        discord_bot.staging.save(heavy)
        run(discord_bot.publish_review(heavy, 555))
        pages = len(heavy.review_messages)
        discord_bot.log["edited"].clear()
        heavy.choices.manual = {"album": "Hand typed"}
        discord_bot.staging.save(heavy)

        select = review.ReleaseSelect(heavy.id, [discord.SelectOption(label="x", value="0")])
        run(select.callback(click(discord_bot, values=["2"])))

        fresh = discord_bot.staging.get(heavy.id)
        assert fresh.meta.album == "Album 2"  # the hand-typed album belonged to the release that was replaced
        assert fresh.choices.release == 2
        assert discord_bot.log["edited"] == fresh.review_messages and len(fresh.review_messages) == pages

    def test_a_field_pick_replaces_a_hand_edit_of_that_field(self, discord_bot, heavy):
        discord_bot.staging.save(heavy)
        run(discord_bot.publish_review(heavy, 555))
        heavy.choices.manual = {"title": "Mine", "year": 1999}
        discord_bot.staging.save(heavy)

        select = review.FieldSelect(heavy.id, "title", [discord.SelectOption(label="x", value="llm")])
        run(select.callback(click(discord_bot, values=["llm"])))

        fresh = discord_bot.staging.get(heavy.id)
        assert fresh.meta.title == "Low Tide (LLM)" and fresh.meta.year == 1999
        assert fresh.choices.manual == {"year": 1999}

    def test_a_card_that_shrinks_deletes_the_surplus_messages(self, discord_bot, heavy):
        discord_bot.staging.save(heavy)
        run(discord_bot.publish_review(heavy, 555))
        before = list(heavy.review_messages)
        heavy.resolve_steps = []
        heavy.findings.releases = heavy.findings.releases[:1]
        discord_bot.log["deleted"].clear()
        run(discord_bot.sync_review(heavy))
        kept = len(heavy.review_messages)
        assert kept < len(before)
        assert discord_bot.log["deleted"] == before[kept:]
        assert heavy.review_messages == before[:kept]

    def test_a_message_deleted_by_hand_is_sent_again(self, discord_bot, heavy):
        discord_bot.staging.save(heavy)
        run(discord_bot.publish_review(heavy, 555))
        discord_bot.vanished = {heavy.review_messages[0]}
        discord_bot.log["sent"].clear()
        run(discord_bot.sync_review(heavy))
        assert len(discord_bot.log["sent"]) == 1
        assert heavy.review_messages[0] == discord_bot.log["sent"][0]

    def test_approve_removes_the_other_messages_and_imports(self, discord_bot, heavy):
        discord_bot.staging.save(heavy)
        run(discord_bot.publish_review(heavy, 555))
        clicked = heavy.review_messages[-1]
        interaction = click(discord_bot)
        interaction.message.id = clicked
        discord_bot.log["deleted"].clear()
        run(ReviewAction("approve", heavy.id).callback(interaction))
        assert sorted(discord_bot.log["deleted"]) == sorted(set(heavy.review_messages) - {clicked})
        assert discord_bot.pipeline.import_entry.await_count == 1

    def test_close_keeps_the_entry_and_forgets_the_card(self, discord_bot, heavy):
        discord_bot.staging.save(heavy)
        run(discord_bot.publish_review(heavy, 555))
        interaction = click(discord_bot)
        interaction.message.id = heavy.review_messages[-1]
        run(ReviewAction("close", heavy.id).callback(interaction))
        fresh = discord_bot.staging.get(heavy.id)
        assert fresh is not None and fresh.review_messages == [] and fresh.review_channel is None
