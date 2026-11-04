import asyncio
import logging
import math
import shlex
import shutil
import time
import uuid
from dataclasses import replace

import discord
import httpx
from discord import app_commands

from . import heuristics, library
from .acoustid import AcoustIdClient
from .config import Config
from .downloader import DownloadError, DownloadedTrack, download, sanitize_extra_args
from .llm import LlmClient
from .musicbrainz import MusicBrainzClient
from .resolver import MetadataResolver
from .staging import QueueEntry, Staging

log = logging.getLogger(__name__)

REVIEW_TIMEOUT_SECONDS = 3600
QUEUE_VIEW_TIMEOUT_SECONDS = 900
QUEUE_PAGE_SIZE = 25
MAX_REVIEWS_AT_ONCE = 10
STATUS_EDIT_INTERVAL_SECONDS = 2.0
# Webhook errors Discord returns once an interaction token (valid for 15 minutes) is no longer usable.
INTERACTION_EXPIRED_CODES = frozenset({10015, 50027})
ACCENT_BY_CONFIDENCE = ((0.85, discord.Colour.green()), (0.5, discord.Colour.gold()))
STATUS_ICON = {"resolved": "✅", "partial": "⚠️", "pending": "⏳"}
EDITED_ICON = "✏️"


def status_view(text: str) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView()
    view.add_item(discord.ui.Container(discord.ui.TextDisplay(text)))
    return view


def _accent(confidence: float) -> discord.Colour:
    for threshold, colour in ACCENT_BY_CONFIDENCE:
        if confidence >= threshold:
            return colour
    return discord.Colour.red()


def _field(label: str, value: object) -> str:
    shown = discord.utils.escape_markdown(str(value)) if value else "*none*"
    return f"**{label}:** {shown}"


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def entry_label(entry: QueueEntry) -> str:
    if entry.meta and entry.meta.title:
        return f"{entry.meta.artist or '?'} – {entry.meta.title}"
    return entry.info.get("title") or entry.audio_file


def entry_icon(entry: QueueEntry) -> str:
    return EDITED_ICON if entry.edited else STATUS_ICON.get(entry.status, "⏳")


class ThrottledStatus:
    def __init__(self, interaction: discord.Interaction):
        self._interaction = interaction
        self._last_edit = 0.0
        self._expired = False

    async def show(self, text: str, force: bool = False) -> None:
        now = time.monotonic()
        if self._expired or not (force or now - self._last_edit >= STATUS_EDIT_INTERVAL_SECONDS):
            return
        self._last_edit = now
        try:
            await self._interaction.edit_original_response(view=status_view(text))
        except discord.HTTPException as error:
            if error.code not in INTERACTION_EXPIRED_CODES:
                raise
            self._expired = True


class EditModal(discord.ui.Modal, title="Edit metadata"):
    def __init__(self, review: "ReviewView"):
        super().__init__()
        self.review = review
        meta = review.entry.require_meta()
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
        entry = self.review.entry
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
        self.review.bot.staging.save(entry)
        self.review.render()
        await interaction.response.edit_message(view=self.review)


class ReviewView(discord.ui.LayoutView):
    def __init__(self, bot: "MusicBot", entry: QueueEntry, note: str | None = None):
        super().__init__(timeout=REVIEW_TIMEOUT_SECONDS)
        self.bot = bot
        self.entry = entry
        self.note = note
        if entry.meta is None:
            entry.meta = heuristics.guess(entry.info, bot.config.known_publishers)

        self.approve_button = discord.ui.Button(label="Approve", style=discord.ButtonStyle.success)
        self.edit_button = discord.ui.Button(label="Edit", style=discord.ButtonStyle.primary)
        self.lookup_button = discord.ui.Button(label="Re-lookup", style=discord.ButtonStyle.secondary)
        self.close_button = discord.ui.Button(label="Close", style=discord.ButtonStyle.secondary)
        self.approve_button.callback = self._approve
        self.edit_button.callback = self._edit
        self.lookup_button.callback = self._lookup
        self.close_button.callback = self._close
        self.render()

    def render(self) -> None:
        self.clear_items()
        meta = self.entry.require_meta()
        lines = [
            f"### {discord.utils.escape_markdown(meta.title)}",
            _field("Artist", meta.artist),
            _field("Album", meta.album or f"{meta.title} (single)"),
            _field("Year", meta.year),
            _field("Genre", meta.genre),
            f"-# source: {meta.origin} · confidence {meta.confidence:.0%}"
            + (" · MusicBrainz match" if meta.musicbrainz else "")
            + f" · queue id `{self.entry.id}`",
        ]
        lines.extend(f"-# {step}" for step in self.entry.resolve_steps)
        if self.entry.resolve_error:
            lines.append(f"-# ⚠️ {self.entry.resolve_error}")
        if self.note:
            lines.append(f"-# {self.note}")
        details = discord.ui.TextDisplay("\n".join(lines))
        thumbnail_url = self.entry.info.get("thumbnail")
        body = (
            discord.ui.Section(details, accessory=discord.ui.Thumbnail(thumbnail_url)) if thumbnail_url else details
        )
        buttons = discord.ui.ActionRow(self.approve_button, self.edit_button, self.lookup_button, self.close_button)
        self.add_item(discord.ui.Container(body, discord.ui.Separator(), buttons, accent_colour=_accent(meta.confidence)))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return self.bot.is_allowed(interaction.user.id)

    async def _approve(self, interaction: discord.Interaction):
        meta = self.entry.require_meta()
        if not meta.artist or not meta.title:
            await interaction.response.send_message("Artist and title are required. Use Edit first.", ephemeral=True)
            return
        self.stop()
        await interaction.response.edit_message(view=status_view("Tagging and adding to library…"))
        await interaction.edit_original_response(view=status_view(await self.bot.import_entry(self.entry)))

    async def _edit(self, interaction: discord.Interaction):
        await interaction.response.send_modal(EditModal(self))

    async def _lookup(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=status_view("Looking up metadata…"))
        await self.bot.resolve_entry(self.entry, overwrite_edits=True)
        self.render()
        await interaction.edit_original_response(view=self)

    async def _close(self, interaction: discord.Interaction):
        self.stop()
        await interaction.response.edit_message(view=status_view(f"Closed. `{self.entry.id}` is still in the queue."))


class QueueView(discord.ui.LayoutView):
    def __init__(self, bot: "MusicBot", note: str | None = None):
        super().__init__(timeout=QUEUE_VIEW_TIMEOUT_SECONDS)
        self.bot = bot
        self.note = note
        self.page = 0
        self.selected: set[str] = {entry.id for entry in bot.staging.list()[:1]}
        self.render()

    def render(self) -> None:
        self.clear_items()
        entries = self.bot.staging.list()
        page_count = max(1, math.ceil(len(entries) / QUEUE_PAGE_SIZE))
        self.page = min(self.page, page_count - 1)
        self.selected &= {entry.id for entry in entries}
        page_entries = entries[self.page * QUEUE_PAGE_SIZE : (self.page + 1) * QUEUE_PAGE_SIZE]

        if not entries:
            self.add_item(discord.ui.Container(discord.ui.TextDisplay("### Download queue\nEmpty.")))
            return

        lines = [
            f"{entry_icon(entry)} `{entry.id}` {discord.utils.escape_markdown(_truncate(entry_label(entry), 70))}"
            for entry in page_entries
        ]
        header = f"### Download queue · {len(entries)} track(s) · page {self.page + 1}/{page_count}"
        legend = "-# ✅ looked up · ⚠️ lookup incomplete · ⏳ not looked up · ✏️ edited by you"

        select = discord.ui.Select(
            placeholder="Select tracks…",
            min_values=0,
            max_values=len(page_entries),
            options=[
                discord.SelectOption(
                    label=_truncate(entry_label(entry), 100),
                    value=entry.id,
                    description=f"{entry.status} · {entry.meta.confidence:.0%}" if entry.meta else entry.status,
                    default=entry.id in self.selected,
                )
                for entry in page_entries
            ],
        )
        select.callback = self._on_select
        self._page_ids = {entry.id for entry in page_entries}

        buttons = []
        for label, style, callback in (
            ("Review", discord.ButtonStyle.primary, self._review),
            ("Look up", discord.ButtonStyle.secondary, self._lookup),
            ("Discard", discord.ButtonStyle.danger, self._discard),
            ("◀", discord.ButtonStyle.secondary, self._previous_page),
            ("▶", discord.ButtonStyle.secondary, self._next_page),
        ):
            button = discord.ui.Button(label=label, style=style)
            button.callback = callback
            buttons.append(button)
        buttons[3].disabled = self.page == 0
        buttons[4].disabled = self.page >= page_count - 1

        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("\n".join([*([f"-# {self.note}"] if self.note else []), header, *lines, legend])),
                discord.ui.ActionRow(select),
                discord.ui.ActionRow(*buttons),
            )
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return self.bot.is_allowed(interaction.user.id)

    def _selected_entries(self) -> list[QueueEntry]:
        found = (self.bot.staging.get(entry_id) for entry_id in self.selected)
        return [entry for entry in found if entry]

    async def _on_select(self, interaction: discord.Interaction):
        chosen = set(interaction.data.get("values", []))
        self.selected = (self.selected - self._page_ids) | chosen
        await interaction.response.defer()

    async def _review(self, interaction: discord.Interaction):
        entries = self._selected_entries()[:MAX_REVIEWS_AT_ONCE]
        if not entries:
            await interaction.response.send_message("Select tracks first.", ephemeral=True)
            return
        reviews = [ReviewView(self.bot, entry) for entry in entries]
        await interaction.response.send_message(view=reviews[0], ephemeral=True)
        for review in reviews[1:]:
            await interaction.followup.send(view=review, ephemeral=True)

    async def _lookup(self, interaction: discord.Interaction):
        entries = self._selected_entries()
        if not entries:
            await interaction.response.send_message("Select tracks first.", ephemeral=True)
            return
        await interaction.response.defer()
        for entry in entries:
            await self.bot.resolve_entry(entry, overwrite_edits=True)
        self.render()
        await interaction.edit_original_response(view=self)

    async def _discard(self, interaction: discord.Interaction):
        entries = self._selected_entries()
        if not entries:
            await interaction.response.send_message("Select tracks first.", ephemeral=True)
            return
        for entry in entries:
            self.bot.staging.remove(entry)
        self.render()
        await interaction.response.edit_message(view=self)

    async def _previous_page(self, interaction: discord.Interaction):
        self.page -= 1
        self.render()
        await interaction.response.edit_message(view=self)

    async def _next_page(self, interaction: discord.Interaction):
        self.page += 1
        self.render()
        await interaction.response.edit_message(view=self)


class QueueCommands(app_commands.Group):
    def __init__(self, bot: "MusicBot"):
        super().__init__(name="queue", description="Manage downloaded tracks waiting to be imported")
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.bot.ensure_allowed(interaction)

    @app_commands.command(name="list", description="Show downloaded tracks; select some to review, look up or discard")
    async def list_queue(self, interaction: discord.Interaction):
        await interaction.response.send_message(view=QueueView(self.bot), ephemeral=True)

    @app_commands.command(name="lookup", description="Retry metadata lookup for tracks that are pending or incomplete")
    async def lookup(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)
        count = await self.bot.resolve_unfinished()
        await interaction.followup.send(f"Looked up {count} track(s). Use `{self.bot.queue_list_command}` to review them.")


class MusicBot(discord.Client):
    def __init__(self, config: Config):
        super().__init__(intents=discord.Intents.default())
        self.config = config
        self.tree = app_commands.CommandTree(self)
        self.staging = Staging(config.queue_dir)
        self._download_slots = asyncio.Semaphore(config.max_concurrent_downloads)
        self._importing: set[str] = set()
        self.http_client: httpx.AsyncClient
        self.resolver: MetadataResolver

    def is_allowed(self, user_id: int) -> bool:
        return user_id in self.config.allowed_user_ids

    async def setup_hook(self) -> None:
        self.http_client = httpx.AsyncClient()
        self.resolver = MetadataResolver(
            self.config,
            AcoustIdClient(self.http_client, self.config),
            MusicBrainzClient(self.http_client, self.config.musicbrainz_contact),
            LlmClient(self.http_client, self.config),
        )
        if self.config.guild_id:
            self._register_commands(discord.Object(id=self.config.guild_id), contexts=None)
            await self.tree.sync(guild=discord.Object(id=self.config.guild_id))
        if self.config.global_commands:
            # With a guild configured, the global copies cover DMs only so servers don't list every command twice.
            dm_only = (
                app_commands.AppCommandContext(guild=False, dm_channel=True, private_channel=False)
                if self.config.guild_id
                else None
            )
            self._register_commands(None, contexts=dm_only)
        # Always sync the global set, even when empty, so turning the toggle off removes previously registered commands.
        await self.tree.sync()

    @property
    def queue_list_command(self) -> str:
        return f"/{self.config.command_name} queue list"

    def _register_commands(self, guild: discord.Object | None, contexts: app_commands.AppCommandContext | None) -> None:
        # One group per registration (a command can only have one parent) so the guild and global copies can differ.
        root = app_commands.Group(name=self.config.command_name, description="Download music and manage the import queue")
        for command in (QueueCommands(self), make_download_command(), make_clean_command()):
            root.add_command(command)
        if contexts is not None:
            root.allowed_contexts = contexts
        self.tree.add_command(root, guild=guild)

    async def close(self) -> None:
        await super().close()
        await self.http_client.aclose()

    async def on_ready(self) -> None:
        log.info("logged in as %s", self.user)

    async def ensure_allowed(self, interaction: discord.Interaction) -> bool:
        if self.is_allowed(interaction.user.id):
            return True
        await interaction.response.send_message("You are not allowed to use this bot.", ephemeral=True)
        return False

    async def clean_dm(self, interaction: discord.Interaction) -> None:
        assert self.user is not None
        await interaction.response.defer(thinking=True, ephemeral=True)
        dm = await interaction.user.create_dm()
        deleted = 0
        async for message in dm.history(limit=None):
            if message.author.id == self.user.id:
                await message.delete()
                deleted += 1
        await interaction.followup.send(f"Deleted {deleted} of my messages from this DM.")

    async def start_download(self, interaction: discord.Interaction, url: str, options: str) -> None:
        url = url.strip()
        try:
            if not url.startswith(("http://", "https://")):
                raise ValueError("the URL must start with http:// or https://")
            extra_args = sanitize_extra_args(shlex.split(options))
        except ValueError as error:
            await interaction.response.send_message(f"Rejected: {error}", ephemeral=True)
            return

        await interaction.response.send_message(view=status_view("Downloading…"), ephemeral=True)
        await self._run_download(interaction, url, extra_args)

    async def _run_download(self, interaction: discord.Interaction, url: str, extra_args: list[str]) -> None:
        status = ThrottledStatus(interaction)
        batch_id = uuid.uuid4().hex[:8]
        incoming = self.staging.incoming_dir(batch_id)
        entries: list[QueueEntry] = []

        async def adopt(track: DownloadedTrack) -> None:
            entries.append(self.staging.adopt(track, url, interaction.user.id, batch_id))
            await status.show(f"Downloaded {len(entries)} track(s)…")

        try:
            async with self._download_slots:
                failed_items = await download(url, extra_args, incoming, self.config.max_playlist_items, adopt)
        except DownloadError as error:
            await self._finish(interaction, f"Download failed:\n```\n{_truncate(str(error), 1500)}\n```")
            return
        except Exception:
            log.exception("download failed for %s", url)
            await self._finish(interaction, "Download failed unexpectedly; see the service log.")
            return
        finally:
            shutil.rmtree(incoming, ignore_errors=True)

        view: discord.ui.LayoutView | None = None
        try:
            imported = 0
            for position, entry in enumerate(entries, start=1):
                await status.show(f"Looking up metadata {position}/{len(entries)}…")
                await self.resolve_entry(entry)
                if self._qualifies_for_auto_import(entry):
                    imported += "Added" in await self.import_entry(entry)
            waiting = [entry for entry in entries if self.staging.get(entry.id)]
            summary = self._batch_summary(entries, waiting, imported, len(failed_items))
            if len(waiting) == 1:
                view = ReviewView(self, waiting[0], note=summary)
            elif waiting:
                view = QueueView(self, note=summary)
            text = f"{summary} Use `{self.queue_list_command}` to review." if waiting else summary
        except Exception:
            log.exception("lookup stage failed for %s", url)
            text = f"Queued {len(entries)} track(s), but metadata lookup crashed; see the service log."
        await self._finish(interaction, text, view)

    async def _finish(self, interaction: discord.Interaction, text: str, view: discord.ui.LayoutView | None = None) -> None:
        try:
            await interaction.edit_original_response(view=view if view is not None else status_view(text))
        except discord.HTTPException as error:
            if error.code not in INTERACTION_EXPIRED_CODES:
                raise
            # Long downloads outlive the interaction token, so fall back to a DM with the plain-text result.
            try:
                await interaction.user.send(text)
            except discord.HTTPException as dm_error:
                log.warning("could not deliver the finished download to user %s: %s", interaction.user.id, dm_error)

    def _qualifies_for_auto_import(self, entry: QueueEntry) -> bool:
        threshold = self.config.auto_import_confidence
        return bool(
            threshold is not None
            and entry.status == "resolved"
            and entry.meta
            and entry.meta.artist
            and entry.meta.confidence >= threshold
        )

    def _batch_summary(self, entries: list[QueueEntry], waiting: list[QueueEntry], imported: int, failed_items: int) -> str:
        incomplete = sum(entry.status != "resolved" for entry in waiting)
        parts = [f"Downloaded {len(entries)} track(s)."]
        if imported:
            parts.append(f"{imported} added to the library automatically.")
        if waiting:
            parts.append(f"{len(waiting)} waiting in the queue ({incomplete} with incomplete lookup).")
        if failed_items:
            parts.append(f"{failed_items} item(s) failed to download.")
        return " ".join(parts)

    async def resolve_entry(self, entry: QueueEntry, overwrite_edits: bool = False) -> None:
        if entry.edited and not overwrite_edits:
            return
        try:
            resolution = await self.resolver.resolve(entry.info, entry.audio_path)
        except Exception:
            log.exception("metadata resolution crashed for %s", entry.id)
            entry.status = "pending"
            entry.resolve_error = "Lookup crashed; see the service log."
        else:
            entry.meta = resolution.metadata
            entry.edited = False
            entry.resolve_steps = resolution.steps
            entry.status = "partial" if resolution.unavailable_services else "resolved"
            entry.resolve_error = (
                f"{', '.join(resolution.unavailable_services)} unavailable; use Re-lookup later"
                if resolution.unavailable_services
                else None
            )
        self.staging.save(entry)

    async def resolve_unfinished(self) -> int:
        unfinished = [entry for entry in self.staging.list() if entry.status != "resolved" and not entry.edited]
        for entry in unfinished:
            await self.resolve_entry(entry)
        return len(unfinished)

    async def import_entry(self, entry: QueueEntry) -> str:
        if entry.id in self._importing:
            return "This track is already being imported."
        meta = entry.require_meta()
        self._importing.add(entry.id)
        try:
            target = await asyncio.to_thread(library.add_to_library, self.config, entry, meta)
        except Exception:
            log.exception("failed to add %s to library", entry.id)
            return f"Tagging or moving failed; `{entry.id}` is still in the queue. See the service log."
        finally:
            self._importing.discard(entry.id)
        await library.trigger_navidrome_scan(self.http_client, self.config)
        relative = target.relative_to(self.config.music_root)
        artist = discord.utils.escape_markdown(meta.artist)
        title = discord.utils.escape_markdown(meta.title)
        return f"Added **{artist} – {title}**\n-# {relative}"


@app_commands.describe(url="Link to download", options="Optional yt-dlp options, e.g. --no-playlist --playlist-items 1-5")
async def _download_callback(interaction: discord.Interaction, url: str, options: str = "") -> None:
    bot = interaction.client
    assert isinstance(bot, MusicBot)
    if await bot.ensure_allowed(interaction):
        await bot.start_download(interaction, url, options)


def make_download_command() -> app_commands.Command:
    # A fresh instance per registration: contexts differ between the guild and global copies.
    return app_commands.Command(
        name="download", description="Download a track or playlist into the queue", callback=_download_callback
    )


async def _clean_callback(interaction: discord.Interaction) -> None:
    bot = interaction.client
    assert isinstance(bot, MusicBot)
    if await bot.ensure_allowed(interaction):
        await bot.clean_dm(interaction)


def make_clean_command() -> app_commands.Command:
    return app_commands.Command(
        name="clean",
        description="Delete the bot's old messages in your DM with it",
        callback=_clean_callback,
    )
