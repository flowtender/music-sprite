"""Discord building blocks shared by the queue list (panels.py), the review card (review.py) and the bot's messages."""

import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord
from discord.ui import ActionRow, Container, DynamicItem, LayoutView, TextDisplay

from . import describe
from .pipeline import ImportResult, Pipeline
from .resolver import ProgressCallback
from .staging import QueueEntry

if TYPE_CHECKING:
    from .bot import MusicBot

# What Discord allows in one message.
MAX_COMPONENTS = 40
MAX_INTERACTIVE = 30
MAX_TEXT = 4000
MAX_SELECT_OPTIONS = 25

STYLE = discord.ButtonStyle
ACCENT_BY_CONFIDENCE = ((0.85, discord.Colour.green()), (0.5, discord.Colour.gold()))
STEP_MAX_CHARS = 300
STATUS_EDIT_INTERVAL_SECONDS = 2.0

Display = Callable[[str], Awaitable[None]]


def escape(value: object) -> str:
    """Shows text from uploads and sources as written: no markdown, no pings."""
    return discord.utils.escape_mentions(discord.utils.escape_markdown(str(value)))


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def accent(confidence: float) -> discord.Colour:
    for threshold, colour in ACCENT_BY_CONFIDENCE:
        if confidence >= threshold:
            return colour
    return discord.Colour.red()


def container_view(children: list, accent_colour: discord.Colour | None = None) -> LayoutView:
    view = LayoutView(timeout=None)
    view.add_item(Container(*children, accent_colour=accent_colour))
    return view


def status_view(text: str) -> LayoutView:
    return container_view([TextDisplay(text)])


def bot_of(interaction: discord.Interaction) -> "MusicBot":
    return interaction.client  # type: ignore[return-value]


def unwrap(item):
    return item.item if isinstance(item, DynamicItem) else item


class AllowedUsersOnly:
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await bot_of(interaction).ensure_allowed(interaction)


def entry_label(entry: QueueEntry) -> str:
    if entry.meta and entry.meta.title:
        return describe.artist_title(entry.meta)
    return entry.info.get("title") or entry.audio_file


def import_message(result: ImportResult) -> str:
    if result.error:
        return result.error
    return f"Added **{escape(describe.artist_title(result.meta))}**\n-# {result.path}"


# --- lookup progress ----------------------------------------------------------------------------------------------


def lookup_status(entry: QueueEntry, position: int, total: int, steps: Sequence[str] = (), working: Sequence[str] = ()) -> str:
    """The text shown while a track is being looked up: what each source found so far and what is running now."""
    counter = f" {position}/{total}" if total > 1 else ""
    lines = [f"**Looking up metadata{counter}** · {escape(entry_label(entry))}"]
    lines.extend(f"-# {escape(truncate(step, STEP_MAX_CHARS))}" for step in steps)
    lines.extend(f"-# ⏳ {text}…" for text in working)
    return "\n".join(lines)


def lookup_progress(entry: QueueEntry, position: int, total: int, display: Display) -> ProgressCallback:
    async def update(steps: list[str], working: list[str]) -> None:
        await display(lookup_status(entry, position, total, steps, working))

    return update


async def look_up(pipeline: Pipeline, entries: list[QueueEntry], display: Display, *, overwrite_edits: bool = False) -> None:
    """Looks the entries up one after another, showing each one's progress through `display`."""
    for position, entry in enumerate(entries, start=1):
        progress = lookup_progress(entry, position, len(entries), display)
        await pipeline.resolve_entry(entry, overwrite_edits=overwrite_edits, progress=progress)


def edit_status_of(interaction: discord.Interaction) -> Display:
    async def display(text: str) -> None:
        await interaction.edit_original_response(view=status_view(text))

    return display


class ThrottledStatus:
    """A status message that is edited at most every few seconds unless forced, to stay clear of rate limits."""

    def __init__(self, message: discord.PartialMessage):
        self._message = message
        self._last_edit = 0.0

    async def show(self, text: str, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last_edit >= STATUS_EDIT_INTERVAL_SECONDS:
            self._last_edit = now
            await self._message.edit(view=status_view(text))


# --- pagination ---------------------------------------------------------------------------------------------------


@dataclass
class Unit:
    """One piece of a long card: its text, then optionally the drop-down that belongs to it."""

    text: str
    select: ActionRow | None = None


class _Page:
    def __init__(self, components: int, text: int):
        self.children: list = []
        self._lines: list[str] = []
        self.components = components
        self.text = text
        self.interactive = 0

    def fits(self, unit: Unit) -> bool:
        components = self.components + (0 if self._lines else 1) + (2 if unit.select else 0)
        text = self.text + len(unit.text) + (1 if self._lines else 0)
        interactive = self.interactive + (1 if unit.select else 0)
        return components <= MAX_COMPONENTS and text <= MAX_TEXT and interactive <= MAX_INTERACTIVE

    def add(self, unit: Unit) -> None:
        self.components += 0 if self._lines else 1
        self.text += len(unit.text) + (1 if self._lines else 0)
        self._lines.append(unit.text)
        if unit.select:
            self.flush()
            self.children.append(unit.select)
            self.components += 2
            self.interactive += 1

    def flush(self) -> None:
        if self._lines:
            self.children.append(TextDisplay("\n".join(self._lines)))
            self._lines = []

    @property
    def empty(self) -> bool:
        return not self._lines and not self.children


def paginate(
    units: list[Unit],
    first_components: int,
    first_text: int,
    later_components: int,
    later_text: int,
    *,
    reserve_components: int,
    reserve_interactive: int,
) -> list[list]:
    """Packs units, in order, into as few messages as the limits allow, each starting with the given components and
    text already used, and leaves the reserved room on the last message (for its buttons). Returns each message's
    children."""
    pages = [_Page(first_components, first_text)]
    for unit in units:
        if not pages[-1].fits(unit) and not pages[-1].empty:
            pages.append(_Page(later_components, later_text))
        pages[-1].add(unit)
    last = pages[-1]
    last.flush()
    if (
        last.components + reserve_components > MAX_COMPONENTS
        or last.interactive + reserve_interactive > MAX_INTERACTIVE
    ):
        pages.append(_Page(later_components, later_text))
    for page in pages:
        page.flush()
    return [page.children for page in pages]
