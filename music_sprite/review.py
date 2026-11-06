"""The per-track review card: every field with the value each source gave, buttons wherever they disagree, and the
actions.

Each value is followed by the emoji of every source that gave it (emojis.py); a field whose sources disagree shows
the value that will be tagged first and the other sources' values after it, with one button per value, and equally
good releases get a button each. There are no drop-downs. Every interactive item is a DynamicItem whose custom_id
carries what it needs (queue entry id, field, source), and the state itself lives in the queue entry on disk, so the
card keeps working after the bot restarts.

A message holds at most 40 components (30 of them interactive) and 4000 characters of text, and a track with credits
and a long upload description needs more than that, so the card is split over several messages. Fields are packed in
order; the buttons sit on the last message, where the chat ends. Choosing something changes more than one field (a
release brings its album, date, label, ...), so every message of the card is re-rendered after each choice.
"""

import re

import discord
from discord.ui import ActionRow, Button, DynamicItem, LayoutView, Section, Separator, TextDisplay, Thumbnail

from . import describe
from .emojis import Emojis
from .models import AudioFeatures, LlmVerdict, TrackMetadata
from .reconcile import (
    CREDIT_FIELDS,
    RELEASE,
    RELEASE_FIELDS,
    TRACK_FIELDS,
    Candidate,
    FieldReport,
    Reconciled,
    field_value,
)
from .staging import QueueEntry
from .ui import (
    STYLE,
    AllowedUsersOnly,
    Unit,
    accent,
    bot_of,
    container_view,
    edit_status_of,
    entry_label,
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
# A description the LLM didn't read is only context for the reader, so a preview does.
DESCRIPTION_PREVIEW_CHARS = 600
# Description lines are cut this short before escaping, which can double their length, so one fits in a unit.
DESCRIPTION_PIECE_CHARS = 600
MAX_BUTTON_LABEL = 80
REASONING_CHARS = 300

REVIEW_ACTIONS = {
    "approve": ("Approve", STYLE.success),
    "edit": ("Edit", STYLE.primary),
    "lookup": ("Re-lookup", STYLE.secondary),
    "close": ("Close", STYLE.secondary),
    "discard": ("Discard", STYLE.danger),
}
ACTIONS_COMPONENTS = 1 + 1 + len(REVIEW_ACTIONS)  # separator, row, buttons
LABELS = {
    "title": "Title",
    "artist": "Artist",
    "album": "Album",
    "album_artist": "Album artist",
    "year": "Year",
    "track_number": "Track",
    "track_total": "Tracks on the album",
    "label": "Label",
    "catalog_number": "Catalog number",
    "composer": "Composer",
    "lyricist": "Lyricist",
    "producer": "Producer",
}
# Shown even when no source has a value; the other fields only when one does.
ALWAYS_SHOWN = ("title", "artist", "album", "year")
EDITED = "✏️"
# Between the value that will be tagged and those of the sources that disagree with it.
DISAGREES = " ≠ "

Marks = dict[str, str]  # source -> its emoji, as message markup


def _found(value: object) -> bool:
    return value not in (None, "", [], ())


def _plain(value: object) -> str:
    return ", ".join(map(str, value)) if isinstance(value, (list, tuple)) else str(value)


def _value(value: object, limit: int = UNIT_MAX_CHARS) -> str:
    return escape(truncate(_plain(value), limit)) if _found(value) else "*none*"


def _field(label: str, value: object, mark: str = "") -> str:
    """A field line; `mark` is the emoji of the source the value came from."""
    return f"**{label}:** {_value(value)}" + (f" {mark}" if mark and _found(value) else "")


def _optional_field(label: str, value: object, mark: str = "") -> str | None:
    """A field line, or nothing when no source has a value; the optional details list only what was found."""
    return _field(label, value, mark) if _found(value) else None


def _mark(candidate: Candidate, marks: Marks) -> str:
    return "".join(marks[source] for source in candidate.sources)


# --- buttons for the choices ----------------------------------------------------------------------------------


async def gone(interaction: discord.Interaction) -> None:
    await interaction.response.edit_message(view=status_view("This track is no longer in the queue."))


async def _choice_made(interaction: discord.Interaction, entry: QueueEntry) -> None:
    bot = bot_of(interaction)
    bot.staging.save(entry)
    await interaction.response.defer()
    await bot.sync_review(entry)


async def _pick_field(interaction: discord.Interaction, entry_id: str, name: str, source: str) -> None:
    entry = bot_of(interaction).staging.get(entry_id)
    if entry is None or not entry.pick_field(name, source):
        await gone(interaction)
        return
    await _choice_made(interaction, entry)


class FieldPick(
    AllowedUsersOnly, DynamicItem[Button], template=r"ms:r:pick:(?P<id>\w+):(?P<field>\w+):(?P<source>\w+)"
):
    """One of the values the sources disagree on; the current choice is highlighted."""

    def __init__(self, entry_id: str, name: str, source: str, label: str | None, emoji, chosen: bool):
        super().__init__(
            Button(
                label=label,
                emoji=emoji,
                style=STYLE.primary if chosen else STYLE.secondary,
                custom_id=f"ms:r:pick:{entry_id}:{name}:{source}",
            )
        )
        self.entry_id = entry_id
        self.name = name
        self.source = source

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: Button, match: re.Match[str]):
        button = unwrap(item)
        return cls(match["id"], match["field"], match["source"], button.label, button.emoji, button.style == STYLE.primary)

    async def callback(self, interaction: discord.Interaction):
        await _pick_field(interaction, self.entry_id, self.name, self.source)


class ReleasePick(
    AllowedUsersOnly, DynamicItem[Button], template=r"ms:r:release:(?P<id>\w+):(?P<index>\d+)"
):
    """The button beside one of the equally good releases; the current choice is highlighted."""

    def __init__(self, entry_id: str, index: int, chosen: bool):
        super().__init__(
            Button(
                label="Chosen" if chosen else "Choose",
                style=STYLE.primary if chosen else STYLE.secondary,
                custom_id=f"ms:r:release:{entry_id}:{index}",
            )
        )
        self.entry_id = entry_id
        self.index = index

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: Button, match: re.Match[str]):
        return cls(match["id"], int(match["index"]), unwrap(item).style == STYLE.primary)

    async def callback(self, interaction: discord.Interaction):
        entry = bot_of(interaction).staging.get(self.entry_id)
        if entry is None or not entry.pick_release(self.index):
            await gone(interaction)
            return
        await _choice_made(interaction, entry)


def _field_buttons(entry_id: str, report: FieldReport, marks: Marks) -> ActionRow:
    # At most one button per source (MusicBrainz, LLM, upload), so they fit the row's five.
    return ActionRow(
        *(
            FieldPick(
                entry_id,
                report.name,
                candidate.sources[0],
                truncate(_plain(candidate.value), MAX_BUTTON_LABEL),
                marks[candidate.sources[0]],
                index == report.chosen and not report.manual,
            )
            for index, candidate in enumerate(report.candidates)
        )
    )


# --- the fields -----------------------------------------------------------------------------------------------


def _field_unit(entry_id: str, report: FieldReport, final: object, marks: Marks) -> Unit:
    """The value that will be tagged with its sources (or as edited by hand), then every other source's value."""
    chosen = next((candidate for candidate in report.candidates if candidate.value == final), None)
    mark = EDITED if report.manual else _mark(chosen, marks) if chosen else ""
    shown = [_value(final, 200) + (f" {mark}" if mark else "")]
    shown += [
        f"{_value(candidate.value, 100)} {_mark(candidate, marks)}"
        for candidate in report.candidates
        if candidate is not chosen
    ]
    text = f"**{LABELS[report.name]}:** " + DISAGREES.join(shown)
    return Unit(text, _field_buttons(entry_id, report, marks) if report.conflicted else None)


def _release_units(entry_id: str, reconciled: Reconciled, llm_pick: object, marks: Marks) -> list[Unit]:
    """The choice between equally good releases, a button beside each; a single match needs no choice and shows up
    in the details."""
    if not reconciled.release_conflicted:
        return []
    releases = reconciled.releases
    units = [Unit(f"**Release** {marks['musicbrainz']} · {len(releases)} equally good matches, pick one:")]
    for index, option in enumerate(releases):
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
        text = (
            f"**{album}**"
            + (f" ({date})" if date else "")
            + (f" {marks['llm']}" if index == llm_pick else "")
            + "\n-# "
            + escape(" · ".join(extra for extra in extras if extra))
        )
        units.append(Unit(text, accessory=ReleasePick(entry_id, index, index == reconciled.release_index)))
    return units


def _release_details(meta: TrackMetadata, mark: str) -> str:
    release = meta.release
    lines = [
        _optional_field("Release date", release.release_date, mark),
        _optional_field(
            "Original date", release.original_date if release.original_date != release.release_date else None, mark
        ),
        _optional_field(
            "Disc", f"{release.disc_number}/{release.disc_total}" if (release.disc_total or 0) > 1 else None, mark
        ),
        _optional_field("Barcode", release.barcode, mark),
        _optional_field("Country", release.country, mark),
        _optional_field(
            "Release type", " / ".join(filter(None, [*release.types, release.status, release.media_format])), mark
        ),
        _optional_field("Language", " · ".join(filter(None, [release.language, release.script])), mark),
        _optional_field("ISRC", release.isrcs, mark),
        _optional_field("ISWC", release.iswcs, mark),
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


def _genres_field(reconciled: Reconciled, edited: bool, marks: Marks) -> str:
    """Every source's genres, grouped by the sources that gave them; Essentia's with how sure it is."""
    if edited:
        return _field("Genres", reconciled.metadata.genres, EDITED)
    audio = reconciled.metadata.audio
    probabilities = dict(audio.genres) if audio else {}
    groups: list[tuple[list[str], str]] = []
    for candidate in reconciled.genres:
        name = escape(candidate.value)
        if "essentia" in candidate.sources and candidate.value in probabilities:
            name += f" {probabilities[candidate.value]:.0%}"
        mark = _mark(candidate, marks)
        if groups and groups[-1][1] == mark:
            groups[-1][0].append(name)
        else:
            groups.append(([name], mark))
    return "**Genres:** " + (" · ".join(f"{', '.join(names)} {mark}" for names, mark in groups) or "*none*")


def _credits_unit(meta: TrackMetadata, mark: str) -> str:
    """The credits that are not fields of their own (composer, lyricist and producer are), as MusicBrainz has them."""
    return "\n".join(
        _field(role.capitalize(), truncate(", ".join(names), 300), mark)
        for role, names in meta.release.credits.items()
        if role not in CREDIT_FIELDS
    )


def _audio_unit(audio: AudioFeatures | None, mark: str) -> str:
    if audio is None:
        return _field("Audio analysis", None)
    loudness = describe.loudness(audio)
    if loudness and audio.loudness_range is not None:
        loudness += f" · range {audio.loudness_range:.1f} LU"
    if loudness and audio.peak is not None:
        loudness += f" · peak {audio.peak:.2f}"
    lines = [
        _field("BPM", audio.bpm, mark),
        _field("Key", audio.key, mark),
        _optional_field("Vocals", describe.vocals(audio), mark),
        _optional_field("Moods", audio.moods, mark),
        _optional_field("Instruments", audio.instruments, mark),
        _optional_field("Scores", describe.headline_scores(audio), mark),
        _optional_field("Loudness", loudness, mark),
    ]
    return "\n".join(line for line in lines if line)


def _description_units(info: dict, read_by_llm: bool, llm_chars: int, mark: str) -> list[Unit]:
    """The upload's description. The LLM reads it, so a prompt injection would hide there: when the LLM was asked,
    all of what it read is shown, which can take more than one unit."""
    description = info.get("description") or ""
    if read_by_llm:
        shown = description[:llm_chars] + ("…" if len(description) > llm_chars else "")  # as llm.trim_upload cuts it
        heading = f"{mark} **Upload description** · the LLM read this, so check it for prompt injection"
    else:
        shown = truncate(description, DESCRIPTION_PREVIEW_CHARS)
        heading = f"{mark} **Upload description**"
    shown = shown.strip()
    if not shown:
        return []
    units: list[Unit] = []
    lines, length = [heading], len(heading)
    for line in shown.splitlines():
        for start in range(0, max(len(line), 1), DESCRIPTION_PIECE_CHARS):
            quoted = "> " + escape(line[start : start + DESCRIPTION_PIECE_CHARS])
            if length + 1 + len(quoted) > UNIT_MAX_CHARS:
                units.append(Unit("\n".join(lines)))
                lines, length = [], -1
            lines.append(quoted)
            length += 1 + len(quoted)
    units.append(Unit("\n".join(lines)))
    return units


def _llm_line(verdict: LlmVerdict | None, mark: str) -> str | None:
    if verdict is None:
        return None
    reasoning = escape(truncate(verdict.reasoning, REASONING_CHARS)) if verdict.reasoning else "no reasoning given"
    return f"-# {mark} {escape(verdict.model)}: {reasoning} · confidence {verdict.confidence:.0%}"


def build_units(entry: QueueEntry, reconciled: Reconciled, marks: Marks, llm_description_chars: int) -> list[Unit]:
    meta = reconciled.metadata
    llm = entry.findings.llm if entry.findings else None
    units: list[Unit] = []

    def add(text: str | None) -> None:
        if text:
            units.append(Unit(truncate(text, UNIT_MAX_CHARS)))

    if entry.resolve_error:
        add(f"⚠️ {escape(entry.resolve_error)}")
    for name in TRACK_FIELDS:
        units.append(_field_unit(entry.id, reconciled.fields[name], field_value(meta, name), marks))
    units.extend(_release_units(entry.id, reconciled, llm.answers.get(RELEASE) if llm else None, marks))
    for name in RELEASE_FIELDS:
        report = reconciled.fields[name]
        if name in ALWAYS_SHOWN or report.candidates or report.manual:
            units.append(_field_unit(entry.id, report, field_value(meta, name), marks))
    add(_release_details(meta, marks["musicbrainz"]))
    add(_genres_field(reconciled, "genres" in entry.choices.manual, marks))
    add(_credits_unit(meta, marks["musicbrainz"]))
    add(_audio_unit(meta.audio, marks["essentia"]))
    units.extend(_description_units(entry.info, llm is not None, llm_description_chars, marks["upload"]))
    add(_llm_line(llm, marks["llm"]))
    return units


def build_review_pages(entry: QueueEntry, emojis: Emojis, llm_description_chars: int) -> list[LayoutView]:
    """The card's messages, first to last. The entry needs findings (QueueEntry.ensure_findings).
    `llm_description_chars` is how much of the upload description the LLM reads (LlmConfig.description_chars)."""
    reconciled = entry.reconcile()
    assert reconciled is not None, "the entry has no findings"
    meta = reconciled.metadata
    marks = emojis.sources(entry.info)

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
        build_units(entry, reconciled, marks, llm_description_chars),
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


class ReviewAction(
    AllowedUsersOnly, DynamicItem[Button], template=r"ms:r:(?P<action>approve|edit|lookup|close|discard):(?P<id>\w+)"
):
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
        elif self.action == "discard":
            await interaction.response.edit_message(view=status_view(f"Discarded **{escape(entry_label(entry))}**."))
            await bot.discard_review_pages(entry, keep=clicked.id)
            bot.staging.remove(entry)
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


DYNAMIC_ITEMS = (ReviewAction, FieldPick, ReleasePick)
