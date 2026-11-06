"""The queue's work, without Discord: download into the queue, look tracks up, and import them into the library."""

import asyncio
import logging
import shutil
import uuid
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx

from . import downloader, library
from .config import LibraryConfig, QueueConfig
from .models import TrackMetadata
from .resolver import MetadataResolver, ProgressCallback
from .staging import EntryStatus, QueueEntry, Staging

log = logging.getLogger(__name__)


@dataclass
class Batch:
    """What one download command brought into the queue."""

    entries: list[QueueEntry]
    failed_items: int


@dataclass
class ImportResult:
    meta: TrackMetadata
    path: Path | None = None  # where the track went, relative to the music root
    error: str | None = None  # why it was not added; shown to the user

    @property
    def added(self) -> bool:
        return self.path is not None


def summarize(batch: Batch, waiting: list[QueueEntry], imported: int) -> str:
    incomplete = sum(entry.status != EntryStatus.RESOLVED for entry in waiting)
    needs_choice = sum(entry.needs_choice for entry in waiting)
    parts = [f"Downloaded {len(batch.entries)} track(s)."]
    if imported:
        parts.append(f"{imported} added to the library automatically.")
    if waiting:
        parts.append(f"{len(waiting)} waiting in the queue ({incomplete} with incomplete lookup).")
    if needs_choice:
        parts.append(f"{needs_choice} need a release choice.")
    if batch.failed_items:
        parts.append(f"{batch.failed_items} item(s) failed to download.")
    return " ".join(parts)


class Pipeline:
    def __init__(
        self,
        queue: QueueConfig,
        library_config: LibraryConfig,
        staging: Staging,
        resolver: MetadataResolver,
        http: httpx.AsyncClient,
    ):
        self._queue = queue
        self._library = library_config
        self._staging = staging
        self._resolver = resolver
        self._http = http
        self._download_slots = asyncio.Semaphore(queue.max_concurrent_downloads)
        self._importing: set[str] = set()
        # Entries something is looking up or about to show the card of, each with an event set once it is done.
        self._claims: dict[str, asyncio.Event] = {}

    async def download(
        self, url: str, extra_args: list[str], requested_by: int, on_track: Callable[[int], Awaitable[None]]
    ) -> Batch:
        """Downloads into the queue, calling on_track with the count so far as each track arrives. Raises
        DownloadError when nothing could be downloaded.

        The batch's entries come claimed (see claiming): the caller looks them up and releases each when done."""
        batch_id = uuid.uuid4().hex[:8]
        incoming = self._staging.incoming_dir(batch_id)
        entries: list[QueueEntry] = []

        async def adopt(track: downloader.DownloadedTrack) -> None:
            entry = self._staging.adopt(track, url, requested_by, batch_id)
            self._claims[entry.id] = asyncio.Event()
            entries.append(entry)
            await on_track(len(entries))

        try:
            async with self._download_slots:
                failed = await downloader.download(url, extra_args, incoming, self._queue.max_playlist_items, adopt)
        except BaseException:
            self.release(*entries)
            raise
        finally:
            shutil.rmtree(incoming, ignore_errors=True)
        return Batch(entries, len(failed))

    def claimed(self, entry_id: str) -> bool:
        return entry_id in self._claims

    async def until_released(self, entry_id: str) -> None:
        if (claim := self._claims.get(entry_id)) is not None:
            await claim.wait()

    def release(self, *entries: QueueEntry) -> None:
        for entry in entries:
            if (claim := self._claims.pop(entry.id, None)) is not None:
                claim.set()

    @contextmanager
    def claiming(self, entry: QueueEntry) -> Iterator[None]:
        """Claims the entry for the block unless something else already has it. A claim says the entry's lookup
        progress, and then its card, are on their way in some message, so nothing else should start another."""
        if entry.id in self._claims:
            yield
            return
        self._claims[entry.id] = asyncio.Event()
        try:
            yield
        finally:
            self.release(entry)

    async def resolve_entry(
        self, entry: QueueEntry, overwrite_edits: bool = False, progress: ProgressCallback | None = None
    ) -> None:
        if entry.edited and not overwrite_edits:
            return
        try:
            resolution = await self._resolver.resolve(entry.info, entry.audio_path, progress)
        except Exception:
            log.exception("metadata resolution crashed for %s", entry.id)
            entry.status = EntryStatus.PENDING
            entry.resolve_error = "Lookup crashed; see the service log."
        else:
            entry.set_findings(resolution.findings)
            unavailable = resolution.unavailable_services
            entry.status = EntryStatus.PARTIAL if unavailable else EntryStatus.RESOLVED
            entry.resolve_error = f"{', '.join(unavailable)} unavailable; use Re-lookup later" if unavailable else None
        self._staging.save(entry)

    async def resolve_unfinished(self) -> int:
        unfinished = [
            entry for entry in self._staging.list() if entry.status != EntryStatus.RESOLVED and not entry.edited
        ]
        for entry in unfinished:
            await self.resolve_entry(entry)
        return len(unfinished)

    def qualifies_for_auto_import(self, entry: QueueEntry) -> bool:
        threshold = self._queue.auto_import_confidence
        return bool(
            threshold is not None
            and entry.status == EntryStatus.RESOLVED
            and entry.meta
            and entry.meta.artist
            and entry.meta.confidence >= threshold
            and not entry.needs_choice
        )

    async def auto_import(self, entry: QueueEntry) -> bool:
        """Imports the entry if it is sure enough to skip review; returns whether it was added."""
        return self.qualifies_for_auto_import(entry) and (await self.import_entry(entry)).added

    async def import_entry(self, entry: QueueEntry) -> ImportResult:
        meta = entry.require_meta()
        if entry.id in self._importing:
            return ImportResult(meta, error="This track is already being imported.")
        self._importing.add(entry.id)
        try:
            target = await asyncio.to_thread(library.add_to_library, self._library, entry, meta)
        except Exception:
            log.exception("failed to add %s to library", entry.id)
            return ImportResult(
                meta, error=f"Tagging or moving failed; `{entry.id}` is still in the queue. See the service log."
            )
        finally:
            self._importing.discard(entry.id)
        await library.trigger_navidrome_scan(self._http, self._library)
        return ImportResult(meta, path=target.relative_to(self._library.music_root))
