"""The per-track review card: every field the sources found, a drop-down wherever they disagree, and the buttons.

Every interactive item is a DynamicItem whose custom_id carries what it needs (queue entry id, field), and the state
itself lives in the queue entry on disk, so the card keeps working after the bot restarts.

A message holds at most 40 components (30 of them interactive) and 4000 characters of text, and a track with credits
and a long lookup log needs more than that, so the card is split over several messages. Fields are packed in order;
the buttons sit on the last message, where the chat ends. Choosing something changes more than one field (a release
brings its album, date, label, ...), so every message of the card is re-rendered after each choice.
"""

import re
from dataclasses import dataclass, replace

import discord
from discord.ui import ActionRow, Button, DynamicItem, LayoutView, Section, Select, Separator, TextDisplay, Thumbnail

from . import heuristics
from .models import SOURCE_LABELS, AudioFeatures, Findings, TrackMetadata
from .panels import (
    STEP_MAX_CHARS,
    STYLE,
    AllowedUsersOnly,
    accent,
    bot_of,
    container_view,
    edit_status_of,
    lookup_progress,
    lookup_status,
    status_view,
    truncate,
    unwrap,
)
from .reconcile import RELEASE_FIELDS, Candidate, FieldReport, Reconciled, reconcile
from .staging import QueueEntry

MAX_COMPONENTS = 40
MAX_INTERACTIVE = 30
MAX_TEXT = 4000
# Room left on every page for its "page 2/3" tag, which is only known once the pages are counted.
PAGE_TAG_MARGIN = 40
UNIT_MAX_CHARS = 1500
MAX_LISTED_RELEASES = 6
MAX_SELECT_OPTIONS = 25
DESCRIPTION_CHARS = 600

REVIEW_ACTIONS = {
    "approve": ("Approve", STYLE.success),
    "edit": ("Edit", STYLE.primary),
    "lookup": ("Re-lookup", STYLE.secondary),
    "close": ("Close", STYLE.secondary),
}
LABELS = {"title": "Title", "artist": "Artist", "album": "Album", "album_artist": "Album artist", "year": "Year"}
CHOSEN, OTHER = "✅", "▫️"


def _escape(value: object) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(str(value)))


def _field(label: str, value: object) -> str:
    shown = ", ".join(map(str, value)) if isinstance(value, (list, tuple)) else value
    return f"**{label}:** {_escape(shown) if shown not in (None, '') else '*none*'}"


def _sources(candidate: Candidate) -> str:
    return ", ".join(SOURCE_LABELS[source] for source in candidate.sources)


def _track_position(meta: TrackMetadata) -> str | None:
    release = meta.release
    if not release.track_number:
        return None
    position = f"{release.track_number}/{release.track_total}" if release.track_total else str(release.track_number)
    if release.disc_total and release.disc_total > 1:
        position += f" · disc {release.disc_number}/{release.disc_total}"
    return position


@dataclass
class Unit:
    """One field's worth of the card: its text, then optionally the drop-down that settles it."""

    text: str
    select: ActionRow | None = None


# --- drop-downs -----------------------------------------------------------------------------------------------


class FieldSelect(AllowedUsersOnly, DynamicItem[Select], template=r"ms:r:field:(?P<id>\w+):(?P<field>\w+)"):
    def __init__(self, entry_id: str, name: str, options: list[discord.SelectOption]):
        super().__init__(
            Select(
                placeholder=f"Choose the {LABELS[name].lower()}…",
                min_values=1,
                max_values=1,
                options=options,
                custom_id=f"ms:r:field:{entry_id}:{name}",
            )
        )
        self.entry_id = entry_id
        self.name = name

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: Select, match: re.Match[str]):
        return cls(match["id"], match["field"], unwrap(item).options)

    async def callback(self, interaction: discord.Interaction):
        bot = bot_of(interaction)
        entry = bot.staging.get(self.entry_id)
        if entry is None or self.name not in LABELS:
            await gone(interaction)
            return
        entry.choices.fields[self.name] = interaction.data["values"][0]
        entry.choices.manual.pop(self.name, None)
        entry.refresh_meta()
        bot.staging.save(entry)
        await interaction.response.defer()
        await bot.sync_review(entry)


class ReleaseSelect(AllowedUsersOnly, DynamicItem[Select], template=r"ms:r:release:(?P<id>\w+)"):
    def __init__(self, entry_id: str, options: list[discord.SelectOption]):
        super().__init__(
            Select(
                placeholder="Choose the release…",
                min_values=1,
                max_values=1,
                options=options,
                custom_id=f"ms:r:release:{entry_id}",
            )
        )
        self.entry_id = entry_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: Select, match: re.Match[str]):
        return cls(match["id"], unwrap(item).options)

    async def callback(self, interaction: discord.Interaction):
        bot = bot_of(interaction)
        entry = bot.staging.get(self.entry_id)
        index = int(interaction.data["values"][0])
        if entry is None or entry.findings is None or not 0 <= index < len(entry.findings.releases):
            await gone(interaction)
            return
        entry.choices.release = index
        # Album, album artist and year belong to the release that was just replaced.
        for name in RELEASE_FIELDS:
            entry.choices.manual.pop(name, None)
        entry.refresh_meta()
        bot.staging.save(entry)
        await interaction.response.defer()
        await bot.sync_review(entry)


def _field_select(entry_id: str, report: FieldReport) -> ActionRow:
    options = [
        discord.SelectOption(
            label=truncate(str(candidate.value), 100),
            value=candidate.sources[0],
            description=truncate(_sources(candidate), 100),
            default=index == report.chosen and not report.manual,
        )
        for index, candidate in enumerate(report.candidates)
    ]
    return ActionRow(FieldSelect(entry_id, report.name, options))


def _release_select(entry_id: str, reconciled: Reconciled) -> ActionRow:
    options = [
        discord.SelectOption(
            label=truncate(release.album or f"{release.title} (single)", 100),
            value=str(index),
            description=truncate(f"{release.year or '?'} · {release.artist} – {release.title}", 100),
            default=index == reconciled.release_index,
        )
        for index, release in enumerate(reconciled.releases[:MAX_SELECT_OPTIONS])
    ]
    return ActionRow(ReleaseSelect(entry_id, options))


# --- the fields -----------------------------------------------------------------------------------------------


def _field_unit(entry: QueueEntry, report: FieldReport, final: object) -> Unit:
    label = LABELS[report.name]
    if report.conflicted:
        heading = (
            f"{_field(label, final)} ✏️ edited by hand · the sources disagree:"
            if report.manual
            else f"**{label}** · the sources disagree, pick one:"
        )
        lines = [heading]
        for index, candidate in enumerate(report.candidates):
            mark = CHOSEN if index == report.chosen and not report.manual else OTHER
            lines.append(f"{mark} {_escape(truncate(str(candidate.value), 200))} *({_sources(candidate)})*")
        return Unit("\n".join(lines), _field_select(entry.id, report))

    text = _field(label, final)
    if report.manual:
        text += " ✏️ edited by hand"
    elif report.chosen is not None:
        text += f" *({_sources(report.candidates[report.chosen])})*"
    if len(report.candidates) > 1:
        # The release decides this field, so the other sources' values are shown only for comparison.
        others = [
            f"{_escape(truncate(str(candidate.value), 100))} ({_sources(candidate)})"
            for index, candidate in enumerate(report.candidates)
            if index != report.chosen
        ]
        text += "\n-# other sources: " + ", ".join(others)
    return Unit(text)


def _present(label: str, value: object) -> str | None:
    """A field line, or nothing when no source has a value; the optional details list only what was found."""
    return _field(label, value) if value not in (None, "", [], ()) else None


def _release_unit(entry: QueueEntry, reconciled: Reconciled) -> Unit | None:
    """The choice between equally good releases; a single match needs no choice and shows up in the details."""
    if not reconciled.release_conflicted:
        return None
    releases = reconciled.releases
    lines = [f"**Release** · {len(releases)} equally good matches, pick one:"]
    for index, option in enumerate(releases[:MAX_LISTED_RELEASES]):
        date = option.release.release_date or option.year
        length = option.musicbrainz.length_seconds if option.musicbrainz else None
        position = _track_position(option)
        extras = [
            f"{option.artist} – {option.title}",
            f"{int(length // 60)}:{int(length % 60):02d}" if length else None,
            f"track {position}" if position else None,
            option.release.label,
            option.release.country,
        ]
        album = _escape(truncate(option.album or f"{option.title} (single)", 120))
        lines.append(f"{CHOSEN if index == reconciled.release_index else OTHER} **{album}**" + (f" ({date})" if date else ""))
        lines.append("-# " + _escape(" · ".join(extra for extra in extras if extra)))
    hidden = len(releases) - MAX_LISTED_RELEASES
    if hidden > 0:
        lines.append(f"-# …and {hidden} more in the drop-down")
    return Unit("\n".join(lines), _release_select(entry.id, reconciled))


def _release_details(meta: TrackMetadata) -> str:
    release = meta.release
    catalog = f"{release.label} {release.catalog_number}" if release.label and release.catalog_number else release.label
    lines = [
        _present("Release date", release.release_date),
        _present("Original date", release.original_date if release.original_date != release.release_date else None),
        _present("Track", _track_position(meta)),
        _present("Label", catalog),
        _present("Barcode", release.barcode),
        _present("Country", release.country),
        _present("Release type", " / ".join(filter(None, [*release.types, release.status, release.media_format]))),
        _present("Language", " · ".join(filter(None, [release.language, release.script]))),
        _present("ISRC", release.isrcs),
        _present("ISWC", release.iswcs),
    ]
    if meta.musicbrainz:
        ids = meta.musicbrainz
        links = [
            (name, path, found)
            for name, path, found in (
                ("recording", "recording", ids.recording_id),
                ("release", "release", ids.release_id),
                ("release group", "release-group", ids.release_group_id),
            )
            if found
        ]
        lines.append("**MusicBrainz:** " + " · ".join(f"[{name}](https://musicbrainz.org/{path}/{found})" for name, path, found in links))
    return "\n".join(line for line in lines if line)


def _credits_unit(meta: TrackMetadata) -> str:
    return "\n".join(
        _field(role.capitalize(), truncate(", ".join(names), 300)) for role, names in meta.release.credits.items()
    )


def _audio_unit(audio: AudioFeatures | None) -> str:
    if audio is None:
        return _field("Audio analysis", None)
    vocals = None
    if audio.vocals:
        vocals = "instrumental" if audio.vocals == "instrumental" else f"{audio.vocals} vocals"
    loudness = None
    if audio.loudness is not None and audio.replaygain is not None:
        loudness = f"{audio.loudness:.1f} LUFS · ReplayGain {audio.replaygain:+.1f} dB"
        if audio.loudness_range is not None:
            loudness += f" · range {audio.loudness_range:.1f} LU"
        if audio.peak is not None:
            loudness += f" · peak {audio.peak:.2f}"
    lines = [
        _field("BPM", audio.bpm),
        _field("Key", audio.key),
        _present("Vocals", vocals),
        _present("Genre guesses", [f"{name} {probability:.0%}" for name, probability in audio.genres]),
        _present("Moods", audio.moods),
        _present("Instruments", audio.instruments),
        _present("Scores", [f"{name} {score:.0%}" for name, score in audio.scores.items()]),
        _present("Loudness", loudness),
    ]
    return "\n".join(line for line in lines if line)


def _description_unit(info: dict) -> Unit | None:
    description = (info.get("description") or "").strip()
    if not description:
        return None
    quoted = "\n".join(f"> {line}" for line in _escape(truncate(description, DESCRIPTION_CHARS)).splitlines())
    return Unit(f"**Upload description** *(the LLM reads all of it, so check it for anything odd)*\n{quoted}")


def build_units(entry: QueueEntry, reconciled: Reconciled) -> list[Unit]:
    meta = reconciled.metadata
    units: list[Unit] = []

    def add(text: str | None, select: ActionRow | None = None) -> None:
        if text:
            units.append(Unit(truncate(text, UNIT_MAX_CHARS), select))

    values = {"title": meta.title, "artist": meta.artist, "album": meta.album, "album_artist": meta.album_artist, "year": meta.year}
    if entry.resolve_error:
        add(f"⚠️ {_escape(entry.resolve_error)}")
    for name in ("title", "artist"):
        units.append(_field_unit(entry, reconciled.fields[name], values[name]))
    if release := _release_unit(entry, reconciled):
        units.append(release)
    for name in ("album", "album_artist", "year"):
        report = reconciled.fields[name]
        if name == "album_artist" and not report.candidates and not report.manual:
            continue
        units.append(_field_unit(entry, report, values[name]))
    add(_release_details(meta))
    add(_field("Genres", meta.genres))
    add(_credits_unit(meta))
    add(_audio_unit(meta.audio))
    if description := _description_unit(entry.info):
        units.append(description)
    for step in entry.resolve_steps:
        add(f"-# {_escape(truncate(step, STEP_MAX_CHARS))}")
    return units


# --- pagination -----------------------------------------------------------------------------------------------


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


ACTIONS_COMPONENTS = 1 + 1 + len(REVIEW_ACTIONS)  # separator, row, buttons


def paginate(units: list[Unit], first_components: int, first_text: int, later_components: int, later_text: int) -> list[_Page]:
    """Packs units, in order, into as few messages as the limits allow, leaving room for the buttons on the last."""
    pages = [_Page(first_components, first_text)]
    for unit in units:
        if not pages[-1].fits(unit) and not pages[-1].empty:
            pages.append(_Page(later_components, later_text))
        pages[-1].add(unit)
    last = pages[-1]
    last.flush()
    if last.components + ACTIONS_COMPONENTS > MAX_COMPONENTS or last.interactive + len(REVIEW_ACTIONS) > MAX_INTERACTIVE:
        pages.append(_Page(later_components, later_text))
    for page in pages:
        page.flush()
    return pages


def ensure_findings(bot, entry: QueueEntry) -> None:
    """A track that was never looked up (or whose lookup crashed) still gets a card, from its own tags."""
    if entry.findings is None:
        entry.findings = Findings(upload=heuristics.guess(entry.info, bot.config.known_publishers))
        entry.refresh_meta()
        bot.staging.save(entry)


def build_review_pages(bot, entry: QueueEntry) -> list[LayoutView]:
    ensure_findings(bot, entry)
    reconciled = entry.reconcile()
    assert reconciled is not None
    meta = reconciled.metadata

    title = f"### {_escape(meta.title)}"
    thumbnail_url = entry.info.get("thumbnail")
    note = f"-# {_escape(entry.review_note)}" if entry.review_note else None

    def subline(page: int, count: int) -> str:
        parts = [f"source: {meta.origin} · confidence {meta.confidence:.0%}"]
        parts.append(f"queue id `{entry.id}`")
        if count > 1:
            parts.append(f"page {page}/{count}")
        return "-# " + " · ".join(parts)

    header_text = title + "\n" + subline(1, 1)
    first_components = 1 + (3 if thumbnail_url else 1) + (1 if note else 0)
    first_text = len(header_text) + len(note or "") + PAGE_TAG_MARGIN
    tag_example = f"-# {_escape(meta.artist)} – {_escape(meta.title)} · page 10/10 · `{entry.id}`"
    pages = paginate(build_units(entry, reconciled), first_components, first_text, 2, len(tag_example) + PAGE_TAG_MARGIN)

    views = []
    for number, page in enumerate(pages, start=1):
        if number == 1:
            details = TextDisplay(title + "\n" + subline(1, len(pages)))
            children: list = [Section(details, accessory=Thumbnail(thumbnail_url)) if thumbnail_url else details]
            if note:
                children.append(TextDisplay(note))
        else:
            children = [TextDisplay(f"-# {_escape(meta.artist)} – {_escape(meta.title)} · page {number}/{len(pages)} · `{entry.id}`")]
        children.extend(page.children)
        if number == len(pages):
            children.append(Separator())
            children.append(ActionRow(*(ReviewAction(action, entry.id) for action in REVIEW_ACTIONS)))
        views.append(container_view(children, accent_colour=accent(meta.confidence)))
    return views


# --- buttons and editing --------------------------------------------------------------------------------------


async def gone(interaction: discord.Interaction) -> None:
    await interaction.response.edit_message(view=status_view("This track is no longer in the queue."))


class ReviewAction(AllowedUsersOnly, DynamicItem[Button], template=r"ms:r:(?P<action>approve|edit|lookup|close):(?P<id>\w+)"):
    def __init__(self, action: str, entry_id: str):
        label, style = REVIEW_ACTIONS[action]
        super().__init__(Button(label=label, style=style, custom_id=f"ms:r:{action}:{entry_id}"))
        self.action = action
        self.entry_id = entry_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: Button, match: re.Match[str]):
        return cls(match["action"], match["id"])

    async def callback(self, interaction: discord.Interaction):
        bot = bot_of(interaction)
        entry = bot.staging.get(self.entry_id)
        if entry is None:
            await gone(interaction)
            return
        clicked = interaction.message
        assert clicked is not None

        if self.action == "approve":
            meta = entry.require_meta()
            if not meta.artist or not meta.title:
                await interaction.response.send_message("Artist and title are required. Use Edit first.", ephemeral=True)
                return
            await interaction.response.edit_message(view=status_view("Tagging and adding to library…"))
            await bot.discard_review_pages(entry, keep=clicked.id)
            await interaction.edit_original_response(view=status_view(await bot.import_entry(entry)))
        elif self.action == "edit":
            ensure_findings(bot, entry)
            await interaction.response.send_modal(EditModal(bot, entry))
        elif self.action == "lookup":
            await interaction.response.edit_message(view=status_view(lookup_status(entry, 1, 1)))
            await bot.discard_review_pages(entry, keep=clicked.id)
            progress = lookup_progress(entry, 1, 1, edit_status_of(interaction))
            await bot.resolve_entry(entry, overwrite_edits=True, progress=progress)
            await bot.publish_review(entry, interaction.channel_id, first=clicked)
        else:
            await interaction.response.edit_message(view=status_view(f"Closed. `{entry.id}` is still in the queue."))
            await bot.discard_review_pages(entry, keep=clicked.id)
            entry.review_channel, entry.review_messages = None, []
            bot.staging.save(entry)


def apply_edits(entry: QueueEntry, entered: dict[str, object]) -> None:
    """Records what differs from what the sources would give as hand edits, so choices made later still apply."""
    assert entry.findings is not None
    base = reconcile(entry.findings, replace(entry.choices, manual={})).metadata
    manual = {name: value for name, value in entered.items() if value != getattr(base, name)}
    # The album artist belongs to the album and artist that were looked up; it is only dropped once those change.
    if ("artist" in manual or "album" in manual) and base.album_artist is not None:
        manual["album_artist"] = None
    entry.choices.manual = manual


class EditModal(discord.ui.Modal, title="Edit metadata"):
    def __init__(self, bot, entry: QueueEntry):
        super().__init__()
        self.bot = bot
        self.entry = entry
        meta = entry.require_meta()
        self.title_input = discord.ui.TextInput(label="Title", default=meta.title, max_length=200)
        self.artist_input = discord.ui.TextInput(label="Artist", default=meta.artist, max_length=200, required=False)
        self.album_input = discord.ui.TextInput(
            label="Album (empty = single)", default=meta.album or "", max_length=200, required=False
        )
        self.year_input = discord.ui.TextInput(label="Year", default=str(meta.year or ""), max_length=4, required=False)
        self.genre_input = discord.ui.TextInput(
            label="Genres (comma-separated)", default=", ".join(meta.genres), max_length=200, required=False
        )
        for item in (self.title_input, self.artist_input, self.album_input, self.year_input, self.genre_input):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        entry = self.entry
        year_text = self.year_input.value.strip()
        apply_edits(
            entry,
            {
                "title": self.title_input.value.strip(),
                "artist": self.artist_input.value.strip(),
                "album": self.album_input.value.strip() or None,
                "year": int(year_text) if year_text.isdigit() else None,
                "genres": [genre.strip() for genre in re.split(r"[;,]", self.genre_input.value) if genre.strip()],
            },
        )
        entry.refresh_meta()
        self.bot.staging.save(entry)
        await interaction.response.defer()
        await self.bot.sync_review(entry)


DYNAMIC_ITEMS = (ReviewAction, FieldSelect, ReleaseSelect)
