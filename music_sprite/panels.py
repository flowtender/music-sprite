"""Discord panels: the download queue and the per-track review card.

Every interactive item is a DynamicItem whose custom_id carries the state it needs (queue entry id, page, ...), and
everything else is read back from the message the click came from or from the queue on disk. Nothing lives in
memory, so panels keep working after the bot restarts.
"""

import math
import re
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING

import discord
from discord.ui import ActionRow, Button, Container, DynamicItem, LayoutView, Section, Select, TextDisplay

from .resolver import ProgressCallback
from .staging import QueueEntry

if TYPE_CHECKING:
    from .bot import MusicBot

QUEUE_PAGE_SIZE = 25
# Discord allows 40 components per message. A queue row (Section + text + button) costs 3, so up to 10 rows plus the
# header, legend, notes and action buttons still fit; beyond that the queue becomes a paginated drop-down.
QUEUE_BUTTON_LIST_MAX = 10
MAX_REVIEWS_AT_ONCE = 10
NOTE_COMPONENT_ID = 7001

ACCENT_BY_CONFIDENCE = ((0.85, discord.Colour.green()), (0.5, discord.Colour.gold()))
STATUS_ICON = {"resolved": "✅", "partial": "⚠️", "pending": "⏳"}
EDITED_ICON = "✏️"
CHOICE_ICON = "❓"

STYLE = discord.ButtonStyle


def status_view(text: str) -> LayoutView:
    view = LayoutView(timeout=None)
    view.add_item(Container(TextDisplay(text)))
    return view


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def accent(confidence: float) -> discord.Colour:
    for threshold, colour in ACCENT_BY_CONFIDENCE:
        if confidence >= threshold:
            return colour
    return discord.Colour.red()


def entry_label(entry: QueueEntry) -> str:
    if entry.meta and entry.meta.title:
        return f"{entry.meta.artist or '?'} – {entry.meta.title}"
    return entry.info.get("title") or entry.audio_file


def entry_icon(entry: QueueEntry) -> str:
    if entry.edited:
        return EDITED_ICON
    if entry.needs_choice:
        return CHOICE_ICON
    return STATUS_ICON.get(entry.status, "⏳")


STEP_MAX_CHARS = 300


def lookup_status(entry: QueueEntry, position: int, total: int, steps: Sequence[str] = (), working: Sequence[str] = ()) -> str:
    """The text shown while a track is being looked up: what each source found so far and what is running now."""
    counter = f" {position}/{total}" if total > 1 else ""
    lines = [f"**Looking up metadata{counter}** · {discord.utils.escape_markdown(entry_label(entry))}"]
    lines.extend(f"-# {truncate(step, STEP_MAX_CHARS)}" for step in steps)
    lines.extend(f"-# ⏳ {text}…" for text in working)
    return "\n".join(lines)


def lookup_progress(
    entry: QueueEntry, position: int, total: int, display: Callable[[str], Awaitable[None]]
) -> ProgressCallback:
    async def update(steps: list[str], working: list[str]) -> None:
        await display(lookup_status(entry, position, total, steps, working))

    return update


def edit_status_of(interaction: discord.Interaction) -> Callable[[str], Awaitable[None]]:
    async def display(text: str) -> None:
        await interaction.edit_original_response(view=status_view(text))

    return display


def bot_of(interaction: discord.Interaction) -> "MusicBot":
    return interaction.client  # type: ignore[return-value]


def unwrap(item):
    return item.item if isinstance(item, DynamicItem) else item


def _note_display(note: str) -> TextDisplay:
    return TextDisplay(f"-# {note}", id=NOTE_COMPONENT_ID)


def _note_of(view: LayoutView) -> str | None:
    for child in view.walk_children():
        if isinstance(child, TextDisplay) and child.id == NOTE_COMPONENT_ID:
            return child.content.removeprefix("-# ")
    return None


class AllowedUsersOnly:
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await bot_of(interaction).ensure_allowed(interaction)


# --- download queue -------------------------------------------------------------------------------------------

QUEUE_TOGGLE_ID = re.compile(r"ms:q:t:(?P<page>\d+):(?P<id>\w+)")
QUEUE_SELECT_ID = re.compile(r"ms:q:s:(?P<page>\d+)")
QUEUE_ACTIONS = {
    "review": ("Review", STYLE.primary),
    "lookup": ("Look up", STYLE.secondary),
    "discard": ("Discard", STYLE.danger),
    "prev": ("◀", STYLE.secondary),
    "next": ("▶", STYLE.secondary),
}


def _selected_in(view: LayoutView) -> set[str]:
    """Reads which queue entries are selected from the message itself: pressed toggles or default drop-down options."""
    selected: set[str] = set()
    for child in view.walk_children():
        child = unwrap(child)
        custom_id = getattr(child, "custom_id", None) or ""
        if isinstance(child, Button) and (match := QUEUE_TOGGLE_ID.fullmatch(custom_id)):
            if child.style is STYLE.primary:
                selected.add(match["id"])
        elif isinstance(child, Select) and QUEUE_SELECT_ID.fullmatch(custom_id):
            selected |= {option.value for option in child.options if option.default}
    return selected


class QueueToggle(AllowedUsersOnly, DynamicItem[Button], template=QUEUE_TOGGLE_ID):
    def __init__(self, page: int, entry_id: str, selected: bool):
        super().__init__(
            Button(
                label="Selected" if selected else "Select",
                style=STYLE.primary if selected else STYLE.secondary,
                custom_id=f"ms:q:t:{page}:{entry_id}",
            )
        )
        self.page = page
        self.entry_id = entry_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: Button, match: re.Match[str]):
        return cls(int(match["page"]), match["id"], unwrap(item).style is STYLE.primary)

    async def callback(self, interaction: discord.Interaction):
        selected = _selected_in(self.view) ^ {self.entry_id}
        view = build_queue_view(bot_of(interaction), page=self.page, selected=selected, note=_note_of(self.view))
        await interaction.response.edit_message(view=view)


class QueueSelect(AllowedUsersOnly, DynamicItem[Select], template=QUEUE_SELECT_ID):
    def __init__(self, page: int, options: list[discord.SelectOption]):
        super().__init__(
            Select(
                placeholder="Select tracks…",
                min_values=0,
                max_values=len(options),
                options=options,
                custom_id=f"ms:q:s:{page}",
            )
        )
        self.page = page

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: Select, match: re.Match[str]):
        return cls(int(match["page"]), unwrap(item).options)

    async def callback(self, interaction: discord.Interaction):
        # The client never reports drop-down changes on its own, so write them into the message's defaults.
        chosen = set(interaction.data.get("values", []))
        view = build_queue_view(bot_of(interaction), page=self.page, selected=chosen, note=_note_of(self.view))
        await interaction.response.edit_message(view=view)


class QueueAction(AllowedUsersOnly, DynamicItem[Button], template=r"ms:q:(?P<action>review|lookup|discard|prev|next):(?P<page>\d+)"):
    def __init__(self, action: str, page: int, *, disabled: bool = False):
        label, style = QUEUE_ACTIONS[action]
        super().__init__(Button(label=label, style=style, disabled=disabled, custom_id=f"ms:q:{action}:{page}"))
        self.action = action
        self.page = page

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: Button, match: re.Match[str]):
        return cls(match["action"], int(match["page"]))

    async def callback(self, interaction: discord.Interaction):
        bot = bot_of(interaction)
        note = _note_of(self.view)
        if self.action in ("prev", "next"):
            page = self.page + (1 if self.action == "next" else -1)
            await interaction.response.edit_message(view=build_queue_view(bot, page=page, note=note))
            return

        found = (bot.staging.get(entry_id) for entry_id in _selected_in(self.view))
        entries = [entry for entry in found if entry]
        if not entries:
            await interaction.response.send_message("Select tracks first.", ephemeral=True)
            return

        if self.action == "review":
            await interaction.response.defer()
            for entry in entries[:MAX_REVIEWS_AT_ONCE]:
                await bot.publish_review(entry, interaction.channel_id)
        elif self.action == "lookup":
            # Lookups take several seconds each; swap the buttons for progress so nothing is clicked meanwhile.
            await interaction.response.edit_message(view=status_view(lookup_status(entries[0], 1, len(entries))))
            display = edit_status_of(interaction)
            for position, entry in enumerate(entries, start=1):
                if position > 1:
                    await display(lookup_status(entry, position, len(entries)))
                progress = lookup_progress(entry, position, len(entries), display)
                await bot.resolve_entry(entry, overwrite_edits=True, progress=progress)
            view = build_queue_view(bot, page=self.page, selected={entry.id for entry in entries}, note=note)
            await interaction.edit_original_response(view=view)
        else:
            for entry in entries:
                bot.staging.remove(entry)
            await interaction.response.edit_message(view=build_queue_view(bot, page=self.page, note=note))


def build_queue_view(
    bot: "MusicBot", *, page: int = 0, selected: set[str] | None = None, note: str | None = None
) -> LayoutView:
    """selected=None selects the first track on the page, which saves a click in the common one-track case."""
    entries = bot.staging.list()
    children: list = [_note_display(note)] if note else []
    if not entries:
        children.append(TextDisplay("### Download queue\nEmpty."))
        return container_view(children)

    as_buttons = len(entries) <= QUEUE_BUTTON_LIST_MAX
    page_size = len(entries) if as_buttons else QUEUE_PAGE_SIZE
    page_count = math.ceil(len(entries) / page_size)
    page = min(max(page, 0), page_count - 1)
    page_entries = entries[page * page_size : (page + 1) * page_size]
    selected = {page_entries[0].id} if selected is None else selected & {entry.id for entry in page_entries}

    def row_text(entry: QueueEntry) -> str:
        return f"{entry_icon(entry)} `{entry.id}` {discord.utils.escape_markdown(truncate(entry_label(entry), 70))}"

    header = f"### Download queue · {len(entries)} track(s)"
    legend = f"-# ✅ looked up · ⚠️ lookup incomplete · ⏳ not looked up · ✏️ edited by you · {CHOICE_ICON} several matches"
    if as_buttons:
        children.append(TextDisplay(header))
        for entry in page_entries:
            toggle = QueueToggle(page, entry.id, entry.id in selected)
            children.append(Section(TextDisplay(row_text(entry)), accessory=toggle))
        children.append(TextDisplay(legend + "\n-# Select tracks, then use the buttons below."))
        children.append(ActionRow(QueueAction("review", page), QueueAction("lookup", page), QueueAction("discard", page)))
    else:
        options = [
            discord.SelectOption(
                label=truncate(entry_label(entry), 100),
                value=entry.id,
                description=f"{entry.status} · {entry.meta.confidence:.0%}" if entry.meta else entry.status,
                default=entry.id in selected,
            )
            for entry in page_entries
        ]
        children.append(TextDisplay("\n".join([f"{header} · page {page + 1}/{page_count}", *map(row_text, page_entries), legend])))
        children.append(ActionRow(QueueSelect(page, options)))
        children.append(
            ActionRow(
                QueueAction("review", page),
                QueueAction("lookup", page),
                QueueAction("discard", page),
                QueueAction("prev", page, disabled=page == 0),
                QueueAction("next", page, disabled=page >= page_count - 1),
            )
        )
    return container_view(children)


def container_view(children: list, accent_colour: discord.Colour | None = None) -> LayoutView:
    view = LayoutView(timeout=None)
    view.add_item(Container(*children, accent_colour=accent_colour))
    return view


DYNAMIC_ITEMS = (QueueToggle, QueueSelect, QueueAction)
