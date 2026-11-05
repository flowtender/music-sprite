"""The per-track review card: every field the sources found, a drop-down wherever they disagree, and the buttons.

Every interactive item is a DynamicItem whose custom_id carries what it needs (queue entry id, field), and the state
itself lives in the queue entry on disk, so the card keeps working after the bot restarts.

A message holds at most 40 components (30 of them interactive) and 4000 characters of text, and a track with credits
and a long lookup log needs more than that, so the card is split over several messages. Fields are packed in order;
the buttons sit on the last message, where the chat ends. Choosing something changes more than one field (a release
brings its album, date, label, ...), so every message of the card is re-rendered after each choice.
"""

import re

import discord
from discord.ui import ActionRow, Button, DynamicItem, LayoutView, Section, Select, Separator, TextDisplay, Thumbnail

from . import describe
from .models import SOURCE_LABELS, AudioFeatures, TrackMetadata
from .reconcile import CHOOSABLE_FIELDS, FIELDS, RELEASE_FIELDS, Candidate, FieldReport, Reconciled
from .staging import QueueEntry
from .ui import (
    MAX_SELECT_OPTIONS,
    STEP_MAX_CHARS,
    STYLE,
    AllowedUsersOnly,
    Unit,
    accent,
    bot_of,
    container_view,
    edit_status_of,
    escape,
    import_message,
    look_up,
    lookup_status,
    paginate,
    status_view,
    truncate,
    unwrap,
)

# Room left on every page for its "page 2/3" tag, which is only known once the pages are counted.
PAGE_TAG_MARGIN = 40
UNIT_MAX_CHARS = 1500
MAX_SHOWN_RELEASES = 6
DESCRIPTION_CHARS = 600

REVIEW_ACTIONS = {
    "approve": ("Approve", STYLE.success),
    "edit": ("Edit", STYLE.primary),
    "lookup": ("Re-lookup", STYLE.secondary),
    "close": ("Close", STYLE.secondary),
}
ACTIONS_COMPONENTS = 1 + 1 + len(REVIEW_ACTIONS)  # separator, row, buttons
LABELS = {"title": "Title", "artist": "Artist", "album": "Album", "album_artist": "Album artist", "year": "Year"}
CHOSEN, OTHER = "✅", "▫️"


def _field(label: str, value: object) -> str:
    shown = ", ".join(map(str, value)) if isinstance(value, (list, tuple)) else value
    return f"**{label}:** {escape(shown) if shown not in (None, '') else '*none*'}"


def _optional_field(label: str, value: object) -> str | None:
    """A field line, or nothing when no source has a value; the optional details list only what was found."""
    return _field(label, value) if value not in (None, "", [], ()) else None


def _sources(candidate: Candidate) -> str:
    return ", ".join(SOURCE_LABELS[source] for source in candidate.sources)


# --- drop-downs -----------------------------------------------------------------------------------------------


async def gone(interaction: discord.Interaction) -> None:
    await interaction.response.edit_message(view=status_view("This track is no longer in the queue."))


async def _choice_made(interaction: discord.Interaction, entry: QueueEntry) -> None:
    bot = bot_of(interaction)
    bot.staging.save(entry)
    await interaction.response.defer()
    await bot.sync_review(entry)


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
        entry = bot_of(interaction).staging.get(self.entry_id)
        if entry is None or not entry.pick_field(self.name, interaction.data["values"][0]):
            await gone(interaction)
            return
        await _choice_made(interaction, entry)


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
        entry = bot_of(interaction).staging.get(self.entry_id)
        if entry is None or not entry.pick_release(int(interaction.data["values"][0])):
            await gone(interaction)
            return
        await _choice_made(interaction, entry)


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
            label=truncate(describe.release_name(release), 100),
            value=str(index),
            description=truncate(f"{release.year or '?'} · {describe.artist_title(release)}", 100),
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
            lines.append(f"{mark} {escape(truncate(str(candidate.value), 200))} *({_sources(candidate)})*")
        return Unit("\n".join(lines), _field_select(entry.id, report))

    text = _field(label, final)
    if report.manual:
        text += " ✏️ edited by hand"
    elif report.chosen is not None:
        text += f" *({_sources(report.candidates[report.chosen])})*"
    if len(report.candidates) > 1:
        # The release decides this field, so the other sources' values are shown only for comparison.
        others = [
            f"{escape(truncate(str(candidate.value), 100))} ({_sources(candidate)})"
            for index, candidate in enumerate(report.candidates)
            if index != report.chosen
        ]
        text += "\n-# other sources: " + ", ".join(others)
    return Unit(text)


def _release_unit(entry: QueueEntry, reconciled: Reconciled) -> Unit | None:
    """The choice between equally good releases; a single match needs no choice and shows up in the details."""
    if not reconciled.release_conflicted:
        return None
    releases = reconciled.releases
    lines = [f"**Release** · {len(releases)} equally good matches, pick one:"]
    for index, option in enumerate(releases[:MAX_SHOWN_RELEASES]):
        date = option.release.release_date or option.year
        position = describe.track_position(option)
        extras = [
            describe.artist_title(option),
            describe.duration(option.length_seconds) if option.length_seconds else None,
            f"track {position}" if position else None,
            option.release.label,
            option.release.country,
        ]
        album = escape(truncate(describe.release_name(option), 120))
        lines.append(f"{CHOSEN if index == reconciled.release_index else OTHER} **{album}**" + (f" ({date})" if date else ""))
        lines.append("-# " + escape(" · ".join(extra for extra in extras if extra)))
    hidden = len(releases) - MAX_SHOWN_RELEASES
    if hidden > 0:
        lines.append(f"-# …and {hidden} more in the drop-down")
    return Unit("\n".join(lines), _release_select(entry.id, reconciled))


def _release_details(meta: TrackMetadata) -> str:
    release = meta.release
    lines = [
        _optional_field("Release date", release.release_date),
        _optional_field("Original date", release.original_date if release.original_date != release.release_date else None),
        _optional_field("Track", describe.track_position(meta)),
        _optional_field("Label", describe.label(meta)),
        _optional_field("Barcode", release.barcode),
        _optional_field("Country", release.country),
        _optional_field("Release type", " / ".join(filter(None, [*release.types, release.status, release.media_format]))),
        _optional_field("Language", " · ".join(filter(None, [release.language, release.script]))),
        _optional_field("ISRC", release.isrcs),
        _optional_field("ISWC", release.iswcs),
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
    loudness = describe.loudness(audio)
    if loudness and audio.loudness_range is not None:
        loudness += f" · range {audio.loudness_range:.1f} LU"
    if loudness and audio.peak is not None:
        loudness += f" · peak {audio.peak:.2f}"
    lines = [
        _field("BPM", audio.bpm),
        _field("Key", audio.key),
        _optional_field("Vocals", describe.vocals(audio)),
        _optional_field("Genre guesses", [f"{name} {probability:.0%}" for name, probability in audio.genres]),
        _optional_field("Moods", audio.moods),
        _optional_field("Instruments", audio.instruments),
        _optional_field("Scores", [f"{name} {score:.0%}" for name, score in audio.scores.items()]),
        _optional_field("Loudness", loudness),
    ]
    return "\n".join(line for line in lines if line)


def _description_unit(info: dict) -> Unit | None:
    description = (info.get("description") or "").strip()
    if not description:
        return None
    quoted = "\n".join(f"> {line}" for line in escape(truncate(description, DESCRIPTION_CHARS)).splitlines())
    return Unit(f"**Upload description** *(the LLM reads all of it, so check it for anything odd)*\n{quoted}")


def build_units(entry: QueueEntry, reconciled: Reconciled) -> list[Unit]:
    meta = reconciled.metadata
    units: list[Unit] = []

    def add(text: str | None, select: ActionRow | None = None) -> None:
        if text:
            units.append(Unit(truncate(text, UNIT_MAX_CHARS), select))

    values = {name: getattr(meta, name) for name in FIELDS}
    if entry.resolve_error:
        add(f"⚠️ {escape(entry.resolve_error)}")
    for name in CHOOSABLE_FIELDS:
        units.append(_field_unit(entry, reconciled.fields[name], values[name]))
    if release := _release_unit(entry, reconciled):
        units.append(release)
    for name in RELEASE_FIELDS:
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
        add(f"-# {escape(truncate(step, STEP_MAX_CHARS))}")
    return units


def build_review_pages(entry: QueueEntry) -> list[LayoutView]:
    """The card's messages, first to last. The entry needs findings (QueueEntry.ensure_findings)."""
    reconciled = entry.reconcile()
    assert reconciled is not None, "the entry has no findings"
    meta = reconciled.metadata

    title = f"### {escape(meta.title)}"
    thumbnail_url = entry.info.get("thumbnail")
    note = f"-# {escape(entry.review_note)}" if entry.review_note else None
    track = escape(describe.artist_title(meta))

    def subline(page: int, count: int) -> str:
        parts = [f"source: {meta.origin} · confidence {meta.confidence:.0%}"]
        parts.append(f"queue id `{entry.id}`")
        if count > 1:
            parts.append(f"page {page}/{count}")
        return "-# " + " · ".join(parts)

    def page_tag(page: int, count: int) -> str:
        return f"-# {track} · page {page}/{count} · `{entry.id}`"

    header_text = title + "\n" + subline(1, 1)
    first_components = 1 + (3 if thumbnail_url else 1) + (1 if note else 0)
    first_text = len(header_text) + len(note or "") + PAGE_TAG_MARGIN
    pages = paginate(
        build_units(entry, reconciled),
        first_components,
        first_text,
        2,
        len(page_tag(10, 10)) + PAGE_TAG_MARGIN,
        reserve_components=ACTIONS_COMPONENTS,
        reserve_interactive=len(REVIEW_ACTIONS),
    )

    views = []
    for number, page in enumerate(pages, start=1):
        if number == 1:
            details = TextDisplay(title + "\n" + subline(1, len(pages)))
            children: list = [Section(details, accessory=Thumbnail(thumbnail_url)) if thumbnail_url else details]
            if note:
                children.append(TextDisplay(note))
        else:
            children = [TextDisplay(page_tag(number, len(pages)))]
        children.extend(page)
        if number == len(pages):
            children.append(Separator())
            children.append(ActionRow(*(ReviewAction(action, entry.id) for action in REVIEW_ACTIONS)))
        views.append(container_view(children, accent_colour=accent(meta.confidence)))
    return views


# --- buttons and editing --------------------------------------------------------------------------------------


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
            result = await bot.pipeline.import_entry(entry)
            await interaction.edit_original_response(view=status_view(import_message(result)))
        elif self.action == "edit":
            entry.ensure_findings(bot.config.lookup.known_publishers)
            await interaction.response.send_modal(EditModal(entry))
        elif self.action == "lookup":
            await interaction.response.edit_message(view=status_view(lookup_status(entry, 1, 1)))
            await bot.discard_review_pages(entry, keep=clicked.id)
            await look_up(bot.pipeline, [entry], edit_status_of(interaction), overwrite_edits=True)
            await bot.publish_review(entry, interaction.channel_id, first=clicked)
        else:
            await interaction.response.edit_message(view=status_view(f"Closed. `{entry.id}` is still in the queue."))
            await bot.discard_review_pages(entry, keep=clicked.id)
            entry.review_channel, entry.review_messages = None, []
            bot.staging.save(entry)


class EditModal(discord.ui.Modal, title="Edit metadata"):
    def __init__(self, entry: QueueEntry):
        super().__init__()
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
        year_text = self.year_input.value.strip()
        self.entry.apply_edits(
            {
                "title": self.title_input.value.strip(),
                "artist": self.artist_input.value.strip(),
                "album": self.album_input.value.strip() or None,
                "year": int(year_text) if year_text.isdigit() else None,
                "genres": [genre.strip() for genre in re.split(r"[;,]", self.genre_input.value) if genre.strip()],
            }
        )
        await _choice_made(interaction, self.entry)


DYNAMIC_ITEMS = (ReviewAction, FieldSelect, ReleaseSelect)
