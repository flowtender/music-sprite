"""The download queue list: every queued track with a button beside it to review it, or to look it up first.

Every interactive item is a DynamicItem whose custom_id carries the state it needs (page, entry id); the note is read
back from the message the click came from and everything else from the queue on disk, so the list keeps working
after the bot restarts.
"""

import math
import re
from typing import TYPE_CHECKING

import discord
from discord.ui import ActionRow, Button, DynamicItem, LayoutView, Section, TextDisplay

from .staging import EntryStatus, QueueEntry
from .ui import (
    STYLE,
    AllowedUsersOnly,
    bot_of,
    container_view,
    edit_status_of,
    entry_label,
    escape,
    look_up,
    lookup_status,
    status_view,
    truncate,
    unwrap,
)

if TYPE_CHECKING:
    from .bot import MusicBot

# Discord allows 40 components per message. A track (Section + text + button) costs 3, so a page of 10 plus the
# note, header, legend and page buttons fits.
QUEUE_PAGE_SIZE = 10
NOTE_COMPONENT_ID = 7001

STATUS_ICON = {EntryStatus.RESOLVED: "✅", EntryStatus.PARTIAL: "⚠️", EntryStatus.PENDING: "⏳"}
EDITED_ICON = "✏️"
CHOICE_ICON = "❓"
LEGEND = (
    f"-# ✅ looked up · ⚠️ lookup incomplete · ⏳ not looked up · {EDITED_ICON} edited by you · {CHOICE_ICON} several"
    " matches\n-# Discard a track from its review card."
)

TRACK_ACTIONS = {"review": ("Review", STYLE.primary), "lookup": ("Look up", STYLE.secondary)}
PAGE_ACTIONS = {"prev": "◀", "next": "▶"}


def entry_icon(entry: QueueEntry) -> str:
    if entry.edited:
        return EDITED_ICON
    if entry.needs_choice:
        return CHOICE_ICON
    return STATUS_ICON[entry.status]


def _note_display(note: str) -> TextDisplay:
    return TextDisplay(f"-# {note}", id=NOTE_COMPONENT_ID)


def _note_of(view: LayoutView) -> str | None:
    for child in view.walk_children():
        if isinstance(child, TextDisplay) and child.id == NOTE_COMPONENT_ID:
            return child.content.removeprefix("-# ")
    return None


class QueueTrackAction(
    AllowedUsersOnly, DynamicItem[Button], template=r"ms:q:(?P<action>review|lookup):(?P<page>\d+):(?P<id>\w+)"
):
    """The button beside a track: Review shows its card; Look up, for a track that was never looked up, looks it up
    first."""

    def __init__(self, action: str, page: int, entry_id: str):
        label, style = TRACK_ACTIONS[action]
        super().__init__(Button(label=label, style=style, custom_id=f"ms:q:{action}:{page}:{entry_id}"))
        self.action = action
        self.page = page
        self.entry_id = entry_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: Button, match: re.Match[str]):
        return cls(match["action"], int(match["page"]), match["id"])

    async def callback(self, interaction: discord.Interaction):
        bot = bot_of(interaction)
        note = _note_of(self.view)
        entry = bot.staging.get(self.entry_id)
        if entry is None:
            view = build_queue_view(bot, page=self.page, note="That track is no longer in the queue.")
            await interaction.response.edit_message(view=view)
            return
        if self.action == "lookup":
            # A lookup takes several seconds; the list makes way for its progress so nothing is clicked meanwhile.
            await interaction.response.edit_message(view=status_view(lookup_status(entry, 1, 1)))
            await look_up(bot.pipeline, [entry], edit_status_of(interaction), overwrite_edits=True)
            await interaction.edit_original_response(view=build_queue_view(bot, page=self.page, note=note))
        else:
            await interaction.response.defer()
        await bot.publish_review(entry, interaction.channel_id)


class QueuePage(AllowedUsersOnly, DynamicItem[Button], template=r"ms:q:(?P<action>prev|next):(?P<page>\d+)"):
    def __init__(self, action: str, page: int, *, disabled: bool = False):
        super().__init__(
            Button(label=PAGE_ACTIONS[action], style=STYLE.secondary, disabled=disabled, custom_id=f"ms:q:{action}:{page}")
        )
        self.action = action
        self.page = page

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: Button, match: re.Match[str]):
        return cls(match["action"], int(match["page"]))

    async def callback(self, interaction: discord.Interaction):
        page = self.page + (1 if self.action == "next" else -1)
        view = build_queue_view(bot_of(interaction), page=page, note=_note_of(self.view))
        await interaction.response.edit_message(view=view)


class RetiredQueueButton(
    AllowedUsersOnly, DynamicItem[Button], template=r"ms:q:(?:t:\d+:\w+|(?:review|lookup|discard):\d+)"
):
    """The select-then-act buttons of lists posted before every track got its own button. A click turns the list into
    the current one."""

    def __init__(self, custom_id: str):
        super().__init__(Button(custom_id=custom_id))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: Button, match: re.Match[str]):
        return cls(unwrap(item).custom_id)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=build_queue_view(bot_of(interaction), note=_note_of(self.view)))


def build_queue_view(bot: "MusicBot", *, page: int = 0, note: str | None = None) -> LayoutView:
    entries = bot.staging.list()
    children: list = [_note_display(note)] if note else []
    if not entries:
        children.append(TextDisplay("### Download queue\nEmpty."))
        return container_view(children)

    page_count = math.ceil(len(entries) / QUEUE_PAGE_SIZE)
    page = min(max(page, 0), page_count - 1)
    header = f"### Download queue · {len(entries)} track(s)"
    children.append(TextDisplay(header + (f" · page {page + 1}/{page_count}" if page_count > 1 else "")))
    for entry in entries[page * QUEUE_PAGE_SIZE : (page + 1) * QUEUE_PAGE_SIZE]:
        text = f"{entry_icon(entry)} `{entry.id}` {escape(truncate(entry_label(entry), 70))}"
        action = "lookup" if entry.status == EntryStatus.PENDING else "review"
        children.append(Section(TextDisplay(text), accessory=QueueTrackAction(action, page, entry.id)))
    children.append(TextDisplay(LEGEND))
    if page_count > 1:
        children.append(
            ActionRow(
                QueuePage("prev", page, disabled=page == 0),
                QueuePage("next", page, disabled=page >= page_count - 1),
            )
        )
    return container_view(children)


DYNAMIC_ITEMS = (QueueTrackAction, QueuePage, RetiredQueueButton)
