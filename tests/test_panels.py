import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from discord.ui import ActionRow, Button, TextDisplay

from music_sprite import panels, review
from music_sprite.panels import QueuePage, QueueTrackAction, RetiredQueueButton, build_queue_view
from music_sprite.staging import EntryStatus
from music_sprite.ui import container_view


def run(coroutine):
    return asyncio.run(coroutine)


def items(view, kind):
    return [child for child in view.walk_children() if isinstance(child, kind)]


def texts(view):
    return "\n".join(child.content for child in items(view, TextDisplay))


@pytest.fixture
def queue_bot(staging):
    return SimpleNamespace(staging=staging, pipeline=SimpleNamespace(resolve_entry=AsyncMock()), publish_review=AsyncMock())


@pytest.fixture
def queued(staging, make_entry):
    def fill(count, status=EntryStatus.RESOLVED):
        entries = [make_entry(f"e{index:07d}", {"title": f"Track {index}"}) for index in range(count)]
        for entry in entries:
            entry.status = status
            staging.save(entry)
        return [entry.id for entry in entries]

    return fill


def interaction_for(bot):
    interaction = MagicMock()
    interaction.client = bot
    interaction.response.edit_message = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    return interaction


def last_view(interaction):
    calls = interaction.edit_original_response.await_args_list or interaction.response.edit_message.await_args_list
    return calls[-1].kwargs["view"]


def track_button(view, entry_id):
    return next(action for action in items(view, QueueTrackAction) if action.entry_id == entry_id)


class TestQueueView:
    def test_an_empty_queue_says_so(self, queue_bot):
        assert "Empty." in texts(build_queue_view(queue_bot))

    def test_every_track_has_its_own_button_and_one_never_looked_up_is_looked_up_first(self, queue_bot, queued, staging):
        ids = queued(3)
        pending = staging.get(ids[1])
        pending.status = EntryStatus.PENDING
        staging.save(pending)
        view = build_queue_view(queue_bot)
        assert [(action.entry_id, action.action) for action in items(view, QueueTrackAction)] == [
            (ids[0], "review"), (ids[1], "lookup"), (ids[2], "review")
        ]
        assert not items(view, QueuePage)

    def test_a_long_queue_is_paged_within_discords_limits(self, queue_bot, queued):
        ids = queued(25)
        view = build_queue_view(queue_bot, page=9, note="Downloaded 25 track(s).")  # clamped to the last page
        assert [action.entry_id for action in items(view, QueueTrackAction)] == ids[20:]
        assert "page 3/3" in texts(view)
        assert {page.action: page.item.disabled for page in items(view, QueuePage)} == {"prev": False, "next": True}
        full = build_queue_view(queue_bot, page=0, note="Downloaded 25 track(s).")
        assert len(items(full, QueueTrackAction)) == panels.QUEUE_PAGE_SIZE
        assert full.total_children_count <= 40
        assert sum(isinstance(getattr(child, "item", child), Button) for child in full.walk_children()) <= 30
        full.to_components()


class TestQueueClicks:
    def test_review_publishes_the_tracks_card(self, queue_bot, queued):
        ids = queued(3)
        interaction = interaction_for(queue_bot)
        run(track_button(build_queue_view(queue_bot), ids[1]).callback(interaction))
        assert [call.args[0].id for call in queue_bot.publish_review.await_args_list] == [ids[1]]

    def test_look_up_looks_the_track_up_then_restores_the_list_and_shows_the_card(self, queue_bot, queued):
        ids = queued(2, status=EntryStatus.PENDING)
        interaction = interaction_for(queue_bot)
        run(track_button(build_queue_view(queue_bot, note="hi"), ids[1]).callback(interaction))
        (call,) = queue_bot.pipeline.resolve_entry.await_args_list
        assert call.args[0].id == ids[1] and call.kwargs["overwrite_edits"]
        assert panels._note_of(last_view(interaction)) == "hi"
        assert [call.args[0].id for call in queue_bot.publish_review.await_args_list] == [ids[1]]

    def test_a_track_no_longer_in_the_queue_refreshes_the_list(self, queue_bot, queued, staging):
        ids = queued(2)
        view = build_queue_view(queue_bot)
        staging.remove(staging.get(ids[0]))
        interaction = interaction_for(queue_bot)
        run(track_button(view, ids[0]).callback(interaction))
        assert [action.entry_id for action in items(last_view(interaction), QueueTrackAction)] == [ids[1]]
        queue_bot.publish_review.assert_not_awaited()

    def test_paging_keeps_the_note(self, queue_bot, queued):
        ids = queued(12)
        following = next(page for page in items(build_queue_view(queue_bot, note="hi"), QueuePage) if page.action == "next")
        interaction = interaction_for(queue_bot)
        run(following.callback(interaction))
        after = last_view(interaction)
        assert [action.entry_id for action in items(after, QueueTrackAction)] == ids[10:]
        assert panels._note_of(after) == "hi"

    def test_buttons_of_lists_posted_before_turn_them_into_the_current_list(self, queue_bot, queued):
        ids = queued(2)
        retired = RetiredQueueButton("ms:q:review:0")
        container_view([panels._note_display("hi"), ActionRow(retired)])  # the old list, as posted
        interaction = interaction_for(queue_bot)
        run(retired.callback(interaction))
        after = last_view(interaction)
        assert [action.entry_id for action in items(after, QueueTrackAction)] == ids
        assert panels._note_of(after) == "hi"


# Every custom_id below is in messages already posted to Discord: changing one breaks those messages.
POSTED = [
    *[(QueueTrackAction(name, 2, "abcd1234"), f"ms:q:{name}:2:abcd1234", {"action": name, "page": 2, "entry_id": "abcd1234"})
      for name in panels.TRACK_ACTIONS],
    *[(QueuePage(name, 3), f"ms:q:{name}:3", {"action": name, "page": 3}) for name in panels.PAGE_ACTIONS],
    # The select-then-act list's toggles and action buttons.
    *[(RetiredQueueButton(custom_id), custom_id, {})
      for custom_id in ("ms:q:t:2:abcd1234", "ms:q:review:3", "ms:q:lookup:3", "ms:q:discard:3")],
    (review.FieldPick("abcd1234", "title", "llm", "Low Tide", "✨", True), "ms:r:pick:abcd1234:title:llm",
     {"entry_id": "abcd1234", "name": "title", "source": "llm"}),
    (review.ReleasePick("abcd1234", 2, True), "ms:r:release:abcd1234:2", {"entry_id": "abcd1234", "index": 2}),
    *[(review.ReviewAction(name, "abcd1234"), f"ms:r:{name}:abcd1234", {"action": name, "entry_id": "abcd1234"})
      for name in review.REVIEW_ACTIONS],
]


@pytest.mark.parametrize("item, custom_id, state", POSTED, ids=[custom_id for _, custom_id, _ in POSTED])
def test_posted_custom_ids_still_route_to_their_item_and_restore_its_state(item, custom_id, state):
    assert item.item.custom_id == custom_id
    registered = [*panels.DYNAMIC_ITEMS, *review.DYNAMIC_ITEMS]
    routed = [cls for cls in registered if cls.__discord_ui_compiled_template__.fullmatch(custom_id)]
    assert routed == [type(item)]
    match = type(item).__discord_ui_compiled_template__.fullmatch(custom_id)
    restored = run(type(item).from_custom_id(MagicMock(), item.item, match))
    assert {name: getattr(restored, name) for name in state} == state
