import asyncio
import logging
import shlex
import shutil
import time
import uuid

import discord
import httpx
from discord import app_commands

from . import library
from .acoustid import AcoustIdClient
from .analysis import Analyzer
from .config import Config
from .downloader import DownloadError, DownloadedTrack, download, sanitize_extra_args
from .llm import LlmClient
from .musicbrainz import MusicBrainzClient
from .panels import DYNAMIC_ITEMS, build_queue_view, build_review_view, lookup_progress, lookup_status, status_view, truncate
from .resolver import MetadataResolver, ProgressCallback
from .staging import QueueEntry, Staging

log = logging.getLogger(__name__)

STATUS_EDIT_INTERVAL_SECONDS = 2.0


class ThrottledStatus:
    def __init__(self, message: discord.PartialMessage):
        self._message = message
        self._last_edit = 0.0

    async def show(self, text: str, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last_edit >= STATUS_EDIT_INTERVAL_SECONDS:
            self._last_edit = now
            await self._message.edit(view=status_view(text))


class QueueCommands(app_commands.Group):
    def __init__(self, bot: "MusicBot"):
        super().__init__(name="queue", description="Manage downloaded tracks waiting to be imported")
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.bot.ensure_allowed(interaction)

    @app_commands.command(name="list", description="Show downloaded tracks; select some to review, look up or discard")
    async def list_queue(self, interaction: discord.Interaction):
        await interaction.response.send_message(view=build_queue_view(self.bot))

    @app_commands.command(name="lookup", description="Retry metadata lookup for tracks that are pending or incomplete")
    async def lookup(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
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
        self.add_dynamic_items(*DYNAMIC_ITEMS)
        self.resolver = MetadataResolver(
            self.config,
            AcoustIdClient(self.http_client, self.config),
            MusicBrainzClient(self.http_client, self.config.musicbrainz_contact),
            LlmClient(self.http_client, self.config),
            Analyzer(self.config),
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

        await interaction.response.send_message(view=status_view("Downloading…"))
        original = await interaction.original_response()
        # Interaction tokens expire after 15 minutes but downloads can run for hours, so edit with the bot token.
        assert interaction.channel_id is not None
        channel = self.get_partial_messageable(interaction.channel_id, guild_id=interaction.guild_id)
        await self._run_download(channel.get_partial_message(original.id), url, extra_args, interaction.user.id)

    async def _run_download(self, reply: discord.PartialMessage, url: str, extra_args: list[str], user_id: int) -> None:
        status = ThrottledStatus(reply)
        batch_id = uuid.uuid4().hex[:8]
        incoming = self.staging.incoming_dir(batch_id)
        entries: list[QueueEntry] = []

        async def adopt(track: DownloadedTrack) -> None:
            entries.append(self.staging.adopt(track, url, user_id, batch_id))
            await status.show(f"Downloaded {len(entries)} track(s)…")

        try:
            async with self._download_slots:
                failed_items = await download(url, extra_args, incoming, self.config.max_playlist_items, adopt)
        except DownloadError as error:
            await reply.edit(view=status_view(f"Download failed:\n```\n{truncate(str(error), 1500)}\n```"))
            return
        except Exception:
            log.exception("download failed for %s", url)
            await reply.edit(view=status_view("Download failed unexpectedly; see the service log."))
            return
        finally:
            shutil.rmtree(incoming, ignore_errors=True)

        try:
            imported = 0

            # Lookup steps are infrequent and each one is worth seeing, so they skip the download-progress throttle.
            async def display(text: str) -> None:
                await status.show(text, force=True)

            for position, entry in enumerate(entries, start=1):
                await display(lookup_status(entry, position, len(entries)))
                await self.resolve_entry(entry, progress=lookup_progress(entry, position, len(entries), display))
                if self._qualifies_for_auto_import(entry):
                    imported += "Added" in await self.import_entry(entry)
            waiting = [entry for entry in entries if self.staging.get(entry.id)]
            summary = self._batch_summary(entries, waiting, imported, len(failed_items))
            if len(waiting) == 1:
                view = build_review_view(self, waiting[0], note=summary)
            elif waiting:
                view = build_queue_view(self, note=summary)
            else:
                view = status_view(summary)
        except Exception:
            log.exception("lookup stage failed for %s", url)
            view = status_view(f"Queued {len(entries)} track(s), but metadata lookup crashed; see the service log.")
        await reply.edit(view=view)

    def _qualifies_for_auto_import(self, entry: QueueEntry) -> bool:
        threshold = self.config.auto_import_confidence
        return bool(
            threshold is not None
            and entry.status == "resolved"
            and entry.meta
            and entry.meta.artist
            and entry.meta.confidence >= threshold
            and not entry.candidates
        )

    def _batch_summary(self, entries: list[QueueEntry], waiting: list[QueueEntry], imported: int, failed_items: int) -> str:
        incomplete = sum(entry.status != "resolved" for entry in waiting)
        needs_choice = sum(bool(entry.candidates) for entry in waiting)
        parts = [f"Downloaded {len(entries)} track(s)."]
        if imported:
            parts.append(f"{imported} added to the library automatically.")
        if waiting:
            parts.append(f"{len(waiting)} waiting in the queue ({incomplete} with incomplete lookup).")
        if needs_choice:
            parts.append(f"{needs_choice} need a release choice.")
        if failed_items:
            parts.append(f"{failed_items} item(s) failed to download.")
        return " ".join(parts)

    async def resolve_entry(
        self, entry: QueueEntry, overwrite_edits: bool = False, progress: ProgressCallback | None = None
    ) -> None:
        if entry.edited and not overwrite_edits:
            return
        try:
            resolution = await self.resolver.resolve(entry.info, entry.audio_path, progress)
        except Exception:
            log.exception("metadata resolution crashed for %s", entry.id)
            entry.status = "pending"
            entry.resolve_error = "Lookup crashed; see the service log."
        else:
            entry.meta = resolution.metadata
            entry.edited = False
            entry.candidates = resolution.candidates
            entry.candidate_index = 0 if resolution.candidates else None
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
