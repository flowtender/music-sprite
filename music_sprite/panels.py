"""Discord panels: the download queue and the per-track review card.

Every interactive item is a DynamicItem whose custom_id carries the state it needs (queue entry id, page, ...), and
everything else is read back from the message the click came from or from the queue on disk. Nothing lives in
memory, so panels keep working after the bot restarts.
"""

import math
import re
from dataclasses import replace
from typing import TYPE_CHECKING

import discord
from discord.ui import (
    ActionRow,
    Button,
    Container,
    DynamicItem,
    LayoutView,
    Section,
    Select,
    Separator,
    TextDisplay,
    Thumbnail,
)

from . import heuristics
from .models import TrackMetadata
from .staging import QueueEntry

if TYPE_CHECKING:
    from .bot import MusicBot

QUEUE_PAGE_SIZE = 25
# Discord allows 40 components per message. A queue row (Section + text + button) costs 3, so up to 10 rows plus the
# header, legend, notes and action buttons still fit; beyond that the queue becomes a paginated drop-down.
QUEUE_BUTTON_LIST_MAX = 10
# Same budget for the review card, which also carries the track details and thumbnail.
CANDIDATE_BUTTON_LIST_MAX = 8
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


def _accent(confidence: float) -> discord.Colour:
    for threshold, colour in ACCENT_BY_CONFIDENCE:
        if confidence >= threshold:
            return colour
    return discord.Colour.red()


def _field(label: str, value: object) -> str:
    shown = discord.utils.escape_markdown(str(value)) if value else "*none*"
    return f"**{label}:** {shown}"


def entry_label(entry: QueueEntry) -> str:
    if entry.meta and entry.meta.title:
        return f"{entry.meta.artist or '?'} – {entry.meta.title}"
    return entry.info.get("title") or entry.audio_file


def entry_icon(entry: QueueEntry) -> str:
    if entry.edited:
        return EDITED_ICON
    if entry.candidates:
        return CHOICE_ICON
    return STATUS_ICON.get(entry.status, "⏳")


def _bot(interaction: discord.Interaction) -> "MusicBot":
    return interaction.client  # type: ignore[return-value]


def _unwrap(item):
    return item.item if isinstance(item, DynamicItem) else item


def _note_display(note: str) -> TextDisplay:
    return TextDisplay(f"-# {note}", id=NOTE_COMPONENT_ID)


def _note_of(view: LayoutView) -> str | None:
    for child in view.walk_children():
        if isinstance(child, TextDisplay) and child.id == NOTE_COMPONENT_ID:
            return child.content.removeprefix("-# ")
    return None


class _AllowedUsersOnly:
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await _bot(interaction).ensure_allowed(interaction)


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
        child = _unwrap(child)
        custom_id = getattr(child, "custom_id", None) or ""
        if isinstance(child, Button) and (match := QUEUE_TOGGLE_ID.fullmatch(custom_id)):
            if child.style is STYLE.primary:
                selected.add(match["id"])
        elif isinstance(child, Select) and QUEUE_SELECT_ID.fullmatch(custom_id):
            selected |= {option.value for option in child.options if option.default}
    return selected


class QueueToggle(_AllowedUsersOnly, DynamicItem[Button], template=QUEUE_TOGGLE_ID):
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
        return cls(int(match["page"]), match["id"], _unwrap(item).style is STYLE.primary)

    async def callback(self, interaction: discord.Interaction):
        selected = _selected_in(self.view) ^ {self.entry_id}
        view = build_queue_view(_bot(interaction), page=self.page, selected=selected, note=_note_of(self.view))
        await interaction.response.edit_message(view=view)


class QueueSelect(_AllowedUsersOnly, DynamicItem[Select], template=QUEUE_SELECT_ID):
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
        return cls(int(match["page"]), _unwrap(item).options)

    async def callback(self, interaction: discord.Interaction):
        # The client never reports drop-down changes on its own, so write them into the message's defaults.
        chosen = set(interaction.data.get("values", []))
        view = build_queue_view(_bot(interaction), page=self.page, selected=chosen, note=_note_of(self.view))
        await interaction.response.edit_message(view=view)


class QueueAction(_AllowedUsersOnly, DynamicItem[Button], template=r"ms:q:(?P<action>review|lookup|discard|prev|next):(?P<page>\d+)"):
    def __init__(self, action: str, page: int, *, disabled: bool = False):
        label, style = QUEUE_ACTIONS[action]
        super().__init__(Button(label=label, style=style, disabled=disabled, custom_id=f"ms:q:{action}:{page}"))
        self.action = action
        self.page = page

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: Button, match: re.Match[str]):
        return cls(match["action"], int(match["page"]))

    async def callback(self, interaction: discord.Interaction):
        bot = _bot(interaction)
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
            reviews = [build_review_view(bot, entry) for entry in entries[:MAX_REVIEWS_AT_ONCE]]
            await interaction.response.send_message(view=reviews[0])
            for review in reviews[1:]:
                await interaction.followup.send(view=review)
        elif self.action == "lookup":
            # Lookups take several seconds each; swap the buttons for progress so nothing is clicked meanwhile.
            await interaction.response.edit_message(view=status_view(f"Looking up 1/{len(entries)}…"))
            for position, entry in enumerate(entries, start=1):
                if position > 1:
                    await interaction.edit_original_response(view=status_view(f"Looking up {position}/{len(entries)}…"))
                await bot.resolve_entry(entry, overwrite_edits=True)
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
        return _container_view(children)

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
    return _container_view(children)


def _container_view(children: list, accent: discord.Colour | None = None) -> LayoutView:
    view = LayoutView(timeout=None)
    view.add_item(Container(*children, accent_colour=accent))
    return view


# --- track review ---------------------------------------------------------------------------------------------

REVIEW_ACTIONS = {
    "approve": ("Approve", STYLE.success),
    "edit": ("Edit", STYLE.primary),
    "lookup": ("Re-lookup", STYLE.secondary),
    "close": ("Close", STYLE.secondary),
}


def describe_candidate(meta: TrackMetadata) -> str:
    album = discord.utils.escape_markdown(meta.album or f"{meta.title} (single)")
    year = f" ({meta.year})" if meta.year else ""
    length = meta.musicbrainz.length_seconds if meta.musicbrainz else None
    duration = f" · {int(length // 60)}:{int(length % 60):02d}" if length else ""
    credit = discord.utils.escape_markdown(f"{meta.artist} – {meta.title}")
    return f"**{truncate(album, 120)}**{year}\n-# {truncate(credit, 150)}{duration}"


async def _gone(interaction: discord.Interaction) -> None:
    await interaction.response.edit_message(view=status_view("This track is no longer in the queue."))


async def _choose_candidate(interaction: discord.Interaction, entry_id: str, index: int, view: LayoutView) -> None:
    bot = _bot(interaction)
    entry = bot.staging.get(entry_id)
    if entry is None or not 0 <= index < len(entry.candidates):
        await _gone(interaction)
        return
    entry.meta = entry.candidates[index]
    entry.candidate_index = index
    entry.edited = False
    bot.staging.save(entry)
    await interaction.response.edit_message(view=build_review_view(bot, entry, note=_note_of(view)))


class ReviewAction(_AllowedUsersOnly, DynamicItem[Button], template=r"ms:r:(?P<action>approve|edit|lookup|close):(?P<id>\w+)"):
    def __init__(self, action: str, entry_id: str):
        label, style = REVIEW_ACTIONS[action]
        super().__init__(Button(label=label, style=style, custom_id=f"ms:r:{action}:{entry_id}"))
        self.action = action
        self.entry_id = entry_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: Button, match: re.Match[str]):
        return cls(match["action"], match["id"])

    async def callback(self, interaction: discord.Interaction):
        bot = _bot(interaction)
        entry = bot.staging.get(self.entry_id)
        if entry is None:
            await _gone(interaction)
            return
        note = _note_of(self.view)

        if self.action == "approve":
            meta = entry.require_meta()
            if not meta.artist or not meta.title:
                await interaction.response.send_message("Artist and title are required. Use Edit first.", ephemeral=True)
                return
            await interaction.response.edit_message(view=status_view("Tagging and adding to library…"))
            await interaction.edit_original_response(view=status_view(await bot.import_entry(entry)))
        elif self.action == "edit":
            await interaction.response.send_modal(EditModal(bot, entry, note))
        elif self.action == "lookup":
            await interaction.response.edit_message(view=status_view("Looking up metadata…"))
            await bot.resolve_entry(entry, overwrite_edits=True)
            await interaction.edit_original_response(view=build_review_view(bot, entry, note=note))
        else:
            await interaction.response.edit_message(view=status_view(f"Closed. `{entry.id}` is still in the queue."))


class ReviewPick(_AllowedUsersOnly, DynamicItem[Button], template=r"ms:r:pick:(?P<id>\w+):(?P<index>\d+)"):
    def __init__(self, entry_id: str, index: int, chosen: bool):
        super().__init__(
            Button(
                label="Selected" if chosen else "Use this",
                style=STYLE.success if chosen else STYLE.secondary,
                disabled=chosen,
                custom_id=f"ms:r:pick:{entry_id}:{index}",
            )
        )
        self.entry_id = entry_id
        self.index = index

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: Button, match: re.Match[str]):
        return cls(match["id"], int(match["index"]), False)

    async def callback(self, interaction: discord.Interaction):
        await _choose_candidate(interaction, self.entry_id, self.index, self.view)


class ReviewPickSelect(_AllowedUsersOnly, DynamicItem[Select], template=r"ms:r:pickselect:(?P<id>\w+)"):
    def __init__(self, entry_id: str, options: list[discord.SelectOption]):
        super().__init__(
            Select(
                placeholder="Pick the release to use…",
                min_values=1,
                max_values=1,
                options=options,
                custom_id=f"ms:r:pickselect:{entry_id}",
            )
        )
        self.entry_id = entry_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: Select, match: re.Match[str]):
        return cls(match["id"], _unwrap(item).options)

    async def callback(self, interaction: discord.Interaction):
        await _choose_candidate(interaction, self.entry_id, int(interaction.data["values"][0]), self.view)


class EditModal(discord.ui.Modal, title="Edit metadata"):
    def __init__(self, bot: "MusicBot", entry: QueueEntry, note: str | None):
        super().__init__()
        self.bot = bot
        self.entry = entry
        self.note = note
        meta = entry.require_meta()
        self.title_input = discord.ui.TextInput(label="Title", default=meta.title, max_length=200)
        self.artist_input = discord.ui.TextInput(label="Artist", default=meta.artist, max_length=200, required=False)
        self.album_input = discord.ui.TextInput(
            label="Album (empty = single)", default=meta.album or "", max_length=200, required=False
        )
        self.year_input = discord.ui.TextInput(label="Year", default=str(meta.year or ""), max_length=4, required=False)
        self.genre_input = discord.ui.TextInput(label="Genre", default=meta.genre or "", max_length=100, required=False)
        for item in (self.title_input, self.artist_input, self.album_input, self.year_input, self.genre_input):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        entry = self.entry
        current = entry.require_meta()
        year_text = self.year_input.value.strip()
        entry.meta = replace(
            current,
            title=self.title_input.value.strip(),
            artist=self.artist_input.value.strip(),
            album=self.album_input.value.strip() or None,
            album_artist=None,
            year=int(year_text) if year_text.isdigit() else None,
            genre=self.genre_input.value.strip() or None,
            origin=current.origin if current.origin.endswith("+edited") else f"{current.origin}+edited",
        )
        entry.edited = True
        entry.candidate_index = None
        self.bot.staging.save(entry)
        await interaction.response.edit_message(view=build_review_view(self.bot, entry, note=self.note))


def build_review_view(bot: "MusicBot", entry: QueueEntry, note: str | None = None) -> LayoutView:
    if entry.meta is None:
        entry.meta = heuristics.guess(entry.info, bot.config.known_publishers)
    meta = entry.meta
    lines = [
        f"### {discord.utils.escape_markdown(meta.title)}",
        _field("Artist", meta.artist),
        _field("Album", meta.album or f"{meta.title} (single)"),
        _field("Year", meta.year),
        _field("Genre", meta.genre),
        _field("BPM", meta.bpm),
        _field("Key", meta.key),
        f"-# source: {meta.origin} · confidence {meta.confidence:.0%}"
        + (" · MusicBrainz match" if meta.musicbrainz else "")
        + f" · queue id `{entry.id}`",
    ]
    lines.extend(f"-# {step}" for step in entry.resolve_steps)
    if entry.resolve_error:
        lines.append(f"-# ⚠️ {entry.resolve_error}")
    details = TextDisplay("\n".join(lines))
    thumbnail_url = entry.info.get("thumbnail")
    children: list = [Section(details, accessory=Thumbnail(thumbnail_url)) if thumbnail_url else details]
    if note:
        children.append(_note_display(note))

    if len(entry.candidates) > 1:
        children.append(Separator())
        children.append(TextDisplay(f"**{len(entry.candidates)} equally good matches.** Pick the release to use:"))
        if len(entry.candidates) <= CANDIDATE_BUTTON_LIST_MAX:
            for index, candidate in enumerate(entry.candidates):
                pick = ReviewPick(entry.id, index, index == entry.candidate_index)
                children.append(Section(TextDisplay(describe_candidate(candidate)), accessory=pick))
        else:
            options = [
                discord.SelectOption(
                    label=truncate(candidate.album or f"{candidate.title} (single)", 100),
                    value=str(index),
                    description=truncate(f"{candidate.year or '?'} · {candidate.artist} – {candidate.title}", 100),
                    default=index == entry.candidate_index,
                )
                for index, candidate in enumerate(entry.candidates)
            ]
            children.append(ActionRow(ReviewPickSelect(entry.id, options)))

    children.append(Separator())
    children.append(ActionRow(*(ReviewAction(action, entry.id) for action in REVIEW_ACTIONS)))
    return _container_view(children, accent=_accent(meta.confidence))


DYNAMIC_ITEMS = (QueueToggle, QueueSelect, QueueAction, ReviewAction, ReviewPick, ReviewPickSelect)
