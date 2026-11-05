import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord.ui import TextDisplay

from music_sprite import panels, review
from music_sprite.panels import QueueAction, QueueSelect, QueueToggle, build_queue_view


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
    def fill(count):
        entries = [make_entry(f"e{index:07d}", {"title": f"Track {index}"}) for index in range(count)]
        for entry in entries:
            staging.save(entry)
        return [entry.id for entry in entries]

    return fill


def interaction_for(bot):
    interaction = MagicMock()
    interaction.client = bot
    interaction.response.edit_message = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    return interaction


def last_view(interaction):
    calls = interaction.edit_original_response.await_args_list or interaction.response.edit_message.await_args_list
    return calls[-1].kwargs["view"]


class TestQueueView:
    def test_an_empty_queue_says_so(self, queue_bot):
        assert "Empty." in texts(build_queue_view(queue_bot))

    def test_a_short_queue_is_a_button_list_with_the_first_track_selected(self, queue_bot, queued):
        ids = queued(3)
        view = build_queue_view(queue_bot)
        toggles = items(view, QueueToggle)
        assert [toggle.entry_id for toggle in toggles] == ids
        assert panels._selected_in(view) == {ids[0]}
        assert [action.action for action in items(view, QueueAction)] == ["review", "lookup", "discard"]
        assert not items(view, QueueSelect)

    def test_a_long_queue_is_a_paged_drop_down(self, queue_bot, queued):
        ids = queued(30)
        view = build_queue_view(queue_bot, page=9)  # clamped to the last page
        (select,) = items(view, QueueSelect)
        assert [option.value for option in select.item.options] == ids[25:]
        assert "page 2/2" in texts(view)
        disabled = {action.action: action.item.disabled for action in items(view, QueueAction)}
        assert disabled == {"review": False, "lookup": False, "discard": False, "prev": False, "next": True}

    @pytest.mark.parametrize("count", [3, 12])
    def test_the_selection_and_note_are_read_back_from_the_message(self, queue_bot, queued, count):
        ids = queued(count)
        view = build_queue_view(queue_bot, selected={ids[1], ids[2]}, note="Downloaded 3 track(s).")
        assert panels._selected_in(view) == {ids[1], ids[2]}
        assert panels._note_of(view) == "Downloaded 3 track(s)."


class TestQueueClicks:
    def test_a_toggle_flips_its_track_and_keeps_the_note(self, queue_bot, queued):
        ids = queued(3)
        view = build_queue_view(queue_bot, note="hi")
        toggle = next(toggle for toggle in items(view, QueueToggle) if toggle.entry_id == ids[2])
        interaction = interaction_for(queue_bot)
        run(toggle.callback(interaction))
        after = last_view(interaction)
        assert panels._selected_in(after) == {ids[0], ids[2]}
        assert panels._note_of(after) == "hi"

    def test_discard_removes_the_selected_tracks(self, queue_bot, queued):
        ids = queued(3)
        view = build_queue_view(queue_bot, selected={ids[0], ids[1]})
        discard = next(action for action in items(view, QueueAction) if action.action == "discard")
        run(discard.callback(interaction_for(queue_bot)))
        assert [entry.id for entry in queue_bot.staging.list()] == [ids[2]]

    def test_lookup_reruns_the_selected_tracks_and_keeps_them_selected(self, queue_bot, queued):
        ids = queued(3)
        view = build_queue_view(queue_bot, selected={ids[0], ids[2]})
        lookup = next(action for action in items(view, QueueAction) if action.action == "lookup")
        interaction = interaction_for(queue_bot)
        run(lookup.callback(interaction))
        looked_up = [call.args[0].id for call in queue_bot.pipeline.resolve_entry.await_args_list]
        assert sorted(looked_up) == [ids[0], ids[2]]
        assert all(call.kwargs["overwrite_edits"] for call in queue_bot.pipeline.resolve_entry.await_args_list)
        assert panels._selected_in(last_view(interaction)) == {ids[0], ids[2]}

    def test_review_publishes_a_card_per_selected_track(self, queue_bot, queued):
        ids = queued(3)
        view = build_queue_view(queue_bot, selected={ids[1]})
        action = next(action for action in items(view, QueueAction) if action.action == "review")
        interaction = interaction_for(queue_bot)
        interaction.response.defer = AsyncMock()
        run(action.callback(interaction))
        assert [call.args[0].id for call in queue_bot.publish_review.await_args_list] == [ids[1]]


OPTIONS = [discord.SelectOption(label="x", value="0")]
# Every custom_id below is in messages already posted to Discord: changing one breaks those messages.
POSTED = [
    (QueueToggle(2, "abcd1234", True), "ms:q:t:2:abcd1234", {"page": 2, "entry_id": "abcd1234"}),
    (QueueSelect(1, OPTIONS), "ms:q:s:1", {"page": 1}),
    *[(QueueAction(name, 3), f"ms:q:{name}:3", {"action": name, "page": 3}) for name in panels.QUEUE_ACTIONS],
    (review.FieldSelect("abcd1234", "album_artist", OPTIONS), "ms:r:field:abcd1234:album_artist",
     {"entry_id": "abcd1234", "name": "album_artist"}),
    (review.ReleaseSelect("abcd1234", OPTIONS), "ms:r:release:abcd1234", {"entry_id": "abcd1234"}),
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
