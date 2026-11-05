"""The Discord client: slash commands, the download command's progress message, and the review card's messages.

The work itself (download, lookup, import) is pipeline.py; the panels are panels.py and review.py.
"""

import logging
import shlex

import discord
import httpx
from discord import app_commands

from . import emojis
from .acoustid import AcoustIdClient
from .analysis import Analyzer
from .config import Config
from .downloader import DownloadError, sanitize_extra_args
from .llm import LlmClient
from .musicbrainz import MusicBrainzClient
from .panels import DYNAMIC_ITEMS as PANEL_ITEMS
from .panels import build_queue_view
from .pipeline import Pipeline, summarize
from .resolver import MetadataResolver
from .review import DYNAMIC_ITEMS as REVIEW_ITEMS
from .review import build_review_pages
from .staging import QueueEntry, Staging
from .ui import ThrottledStatus, look_up, status_view, truncate

log = logging.getLogger(__name__)


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
        count = await self.bot.pipeline.resolve_unfinished()
        await interaction.followup.send(f"Looked up {count} track(s). Use `{self.bot.queue_list_command}` to review them.")


class MusicBot(discord.Client):
    def __init__(self, config: Config):
        super().__init__(intents=discord.Intents.default())
        self.config = config
        self.tree = app_commands.CommandTree(self)
        self.staging = Staging(config.queue.directory)
        self.http_client: httpx.AsyncClient
        self.analyzer: Analyzer
        self.pipeline: Pipeline
        self.app_emojis = emojis.Emojis()  # discord.Client.emojis is the guilds' emojis

    def is_allowed(self, user_id: int) -> bool:
        return user_id in self.config.discord.allowed_user_ids

    async def setup_hook(self) -> None:
        self.http_client = httpx.AsyncClient()
        self.add_dynamic_items(*PANEL_ITEMS, *REVIEW_ITEMS)
        try:
            self.app_emojis = await emojis.sync(self)
        except discord.HTTPException:
            log.warning("could not load the application's emojis; the review card uses plain ones", exc_info=True)
        self.analyzer = Analyzer(self.config.analysis)
        lookup = self.config.lookup
        resolver = MetadataResolver(
            lookup.known_publishers,
            AcoustIdClient(self.http_client, lookup.acoustid_api_key),
            MusicBrainzClient(self.http_client, lookup.musicbrainz_contact),
            LlmClient(self.http_client, self.config.llm, lookup.known_publishers),
            self.analyzer,
        )
        self.pipeline = Pipeline(self.config.queue, self.config.library, self.staging, resolver, self.http_client)

        settings = self.config.discord
        if settings.guild_id:
            self._register_commands(discord.Object(id=settings.guild_id), contexts=None)
            await self.tree.sync(guild=discord.Object(id=settings.guild_id))
        if settings.global_commands:
            # With a guild configured, the global copies cover DMs only so servers don't list every command twice.
            dm_only = (
                app_commands.AppCommandContext(guild=False, dm_channel=True, private_channel=False)
                if settings.guild_id
                else None
            )
            self._register_commands(None, contexts=dm_only)
        # Always sync the global set, even when empty, so turning the toggle off removes previously registered commands.
        await self.tree.sync()

    @property
    def queue_list_command(self) -> str:
        return f"/{self.config.discord.command_name} queue list"

    def _register_commands(self, guild: discord.Object | None, contexts: app_commands.AppCommandContext | None) -> None:
        # One group per registration (a command can only have one parent) so the guild and global copies can differ.
        root = app_commands.Group(
            name=self.config.discord.command_name, description="Download music and manage the import queue"
        )
        for command in (QueueCommands(self), make_download_command(), make_clean_command()):
            root.add_command(command)
        if contexts is not None:
            root.allowed_contexts = contexts
        self.tree.add_command(root, guild=guild)

    async def close(self) -> None:
        await super().close()
        self.analyzer.close()
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

    # --- the download command -------------------------------------------------------------------------------------

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
        """Downloads, looks every track up and imports the sure ones; the progress message ends as the review card
        of a lone track, or as the queue list."""
        status = ThrottledStatus(reply)

        async def downloaded(count: int) -> None:
            await status.show(f"Downloaded {count} track(s)…")

        try:
            batch = await self.pipeline.download(url, extra_args, user_id, downloaded)
        except DownloadError as error:
            await reply.edit(view=status_view(f"Download failed:\n```\n{truncate(str(error), 1500)}\n```"))
            return
        except Exception:
            log.exception("download failed for %s", url)
            await reply.edit(view=status_view("Download failed unexpectedly; see the service log."))
            return

        # Lookup steps are infrequent and each one is worth seeing, so they skip the download-progress throttle.
        async def display(text: str) -> None:
            await status.show(text, force=True)

        try:
            await look_up(self.pipeline, batch.entries, display)
            imported = await self.pipeline.auto_import(batch.entries)
            waiting = [entry for entry in batch.entries if self.staging.get(entry.id)]
            summary = summarize(batch, waiting, imported)
            if len(waiting) == 1:
                await self.publish_review(waiting[0], reply.channel.id, first=reply, note=summary)
                return
            view = build_queue_view(self, note=summary) if waiting else status_view(summary)
        except Exception:
            log.exception("lookup stage failed for %s", url)
            view = status_view(f"Queued {len(batch.entries)} track(s), but metadata lookup crashed; see the service log.")
        await reply.edit(view=view)

    # --- review card messages ---------------------------------------------------------------------------------------

    def _review_pages(self, entry: QueueEntry) -> list[discord.ui.LayoutView]:
        entry.ensure_findings(self.config.lookup.known_publishers)
        return build_review_pages(entry, self.app_emojis, self.config.llm.description_chars)

    async def publish_review(
        self,
        entry: QueueEntry,
        channel_id: int | None,
        *,
        first: discord.PartialMessage | None = None,
        note: str | None = None,
    ) -> None:
        """Shows the review card in a channel: `first` (say, the lookup progress message) becomes its first message
        and the rest are posted after it. Any card the entry already had is removed."""
        assert channel_id is not None
        await self.discard_review_pages(entry, keep=first.id if first else None)
        entry.review_channel = channel_id
        entry.review_note = note
        channel = self.get_partial_messageable(channel_id)
        ids: list[int] = []
        for index, page in enumerate(self._review_pages(entry)):
            if index == 0 and first is not None:
                await first.edit(view=page)
                ids.append(first.id)
            else:
                ids.append((await channel.send(view=page)).id)
        entry.review_messages = ids
        self.staging.save(entry)

    async def sync_review(self, entry: QueueEntry) -> None:
        """Re-renders every message of the review card; a choice can change fields on any of them, and the number
        of messages can change with it."""
        if entry.review_channel is None:
            return
        channel = self.get_partial_messageable(entry.review_channel)
        pages = self._review_pages(entry)
        existing = list(entry.review_messages)
        ids: list[int] = []
        for index, page in enumerate(pages):
            if index < len(existing):
                try:
                    await channel.get_partial_message(existing[index]).edit(view=page)
                    ids.append(existing[index])
                    continue
                except discord.NotFound:
                    pass
            ids.append((await channel.send(view=page)).id)
        await self._delete_messages(entry.review_channel, existing[len(pages) :])
        entry.review_messages = ids
        self.staging.save(entry)

    async def discard_review_pages(self, entry: QueueEntry, keep: int | None = None) -> None:
        """Deletes the review card's messages except `keep`. The caller saves the entry if it stays in the queue."""
        if entry.review_channel is not None:
            await self._delete_messages(entry.review_channel, [mid for mid in entry.review_messages if mid != keep])
        entry.review_messages = [keep] if keep is not None else []

    async def _delete_messages(self, channel_id: int, message_ids: list[int]) -> None:
        channel = self.get_partial_messageable(channel_id)
        for message_id in message_ids:
            try:
                await channel.get_partial_message(message_id).delete()
            except discord.HTTPException:
                log.warning("could not delete review message %s", message_id, exc_info=True)


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
