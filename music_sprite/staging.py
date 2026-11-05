"""The import queue on disk: one folder per downloaded track with its files and an entry.json."""

import json
import shutil
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from . import heuristics
from .downloader import DownloadedTrack
from .models import Choices, Findings, TrackMetadata
from .reconcile import FIELDS, RELEASE_FIELDS, Reconciled, reconcile, valid_index

ENTRY_FILE = "entry.json"
INCOMING_DIR = ".incoming"


class EntryStatus(StrEnum):
    PENDING = "pending"  # not looked up yet, or the lookup crashed
    PARTIAL = "partial"  # looked up, but a service was unavailable
    RESOLVED = "resolved"


@dataclass
class QueueEntry:
    id: str
    directory: Path
    audio_file: str
    info_file: str
    cover_file: str | None
    source_url: str
    requested_by: int
    batch_id: str
    created_at: str
    status: EntryStatus = EntryStatus.PENDING
    resolve_error: str | None = None
    resolve_steps: list[str] = field(default_factory=list)
    findings: Findings | None = None
    choices: Choices = field(default_factory=Choices)
    meta: TrackMetadata | None = None
    # Where the review card is shown: it can span several messages, first to last.
    review_channel: int | None = None
    review_messages: list[int] = field(default_factory=list)
    review_note: str | None = None
    _info: dict | None = field(default=None, repr=False, compare=False)

    @property
    def audio_path(self) -> Path:
        return self.directory / self.audio_file

    @property
    def info_path(self) -> Path:
        return self.directory / self.info_file

    @property
    def cover_path(self) -> Path | None:
        return self.directory / self.cover_file if self.cover_file else None

    @property
    def info(self) -> dict:
        if self._info is None:
            self._info = json.loads(self.info_path.read_text(encoding="utf-8"))
        return self._info

    @property
    def edited(self) -> bool:
        return bool(self.choices.manual)

    @property
    def needs_choice(self) -> bool:
        """True when equally good MusicBrainz releases tie, so a person should pick one."""
        return bool(self.findings and len(self.findings.releases) > 1)

    def reconcile(self) -> Reconciled | None:
        return reconcile(self.findings, self.choices) if self.findings else None

    def refresh_meta(self) -> None:
        """Re-derives the metadata that gets tagged from the findings, the user's choices and hand edits."""
        if reconciled := self.reconcile():
            self.meta = reconciled.metadata

    def set_findings(self, findings: Findings) -> None:
        self.findings = findings
        self.choices = Choices()
        self.refresh_meta()

    def ensure_findings(self, known_publishers: frozenset[str]) -> None:
        """A track that was never looked up (or whose lookup crashed) still gets a card, from its own tags."""
        if self.findings is None:
            self.findings = Findings(upload=heuristics.guess(self.info, known_publishers))
            self.refresh_meta()

    def pick_field(self, name: str, source: str) -> bool:
        """Takes the field from that source, replacing a hand edit of it. False for a field that cannot be picked."""
        if name not in FIELDS:
            return False
        self.choices.fields[name] = source
        self.choices.manual.pop(name, None)
        self.refresh_meta()
        return True

    def pick_release(self, index: int) -> bool:
        """False when there is no such release (the card was older than the last lookup)."""
        if self.findings is None or not valid_index(index, len(self.findings.releases)):
            return False
        self.choices.release = index
        # Album, album artist and year belong to the release that was just replaced.
        for name in RELEASE_FIELDS:
            self.choices.manual.pop(name, None)
        self.refresh_meta()
        return True

    def apply_edits(self, entered: dict[str, object]) -> None:
        """Records what differs from what the sources would give as hand edits, so choices made later still apply."""
        assert self.findings is not None
        base = reconcile(self.findings, replace(self.choices, manual={})).metadata
        manual = {name: value for name, value in entered.items() if value != getattr(base, name)}
        # The album artist belongs to the album and artist that were looked up; it is only dropped once those change.
        if ("artist" in manual or "album" in manual) and base.album_artist is not None:
            manual["album_artist"] = None
        self.choices.manual = manual
        self.refresh_meta()

    def require_meta(self) -> TrackMetadata:
        if self.meta is None:
            raise ValueError(f"queue entry {self.id} has no metadata yet")
        return self.meta

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "audio_file": self.audio_file,
            "info_file": self.info_file,
            "cover_file": self.cover_file,
            "source_url": self.source_url,
            "requested_by": self.requested_by,
            "batch_id": self.batch_id,
            "created_at": self.created_at,
            "status": self.status,
            "resolve_error": self.resolve_error,
            "resolve_steps": self.resolve_steps,
            "findings": self.findings.to_dict() if self.findings else None,
            "choices": self.choices.to_dict(),
            "meta": self.meta.to_dict() if self.meta else None,
            "review_channel": self.review_channel,
            "review_messages": self.review_messages,
            "review_note": self.review_note,
        }

    @classmethod
    def from_json(cls, directory: Path, data: dict) -> "QueueEntry":
        meta = data.pop("meta", None)
        meta = TrackMetadata.from_dict(meta) if meta else None
        findings = data.pop("findings", None)
        choices = data.pop("choices", None)
        status = EntryStatus(data.pop("status", EntryStatus.PENDING))
        # Entries saved before lookups kept their findings: the tags they had become the one source they now have.
        for legacy_key in ("edited", "candidates", "candidate_index"):
            data.pop(legacy_key, None)
        return cls(
            directory=directory,
            status=status,
            meta=meta,
            findings=Findings.from_dict(findings) if findings else Findings(upload=meta, audio=meta.audio) if meta else None,
            choices=Choices.from_dict(choices) if choices else Choices(),
            **data,
        )


class Staging:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def incoming_dir(self, batch_id: str) -> Path:
        return self.root / INCOMING_DIR / batch_id

    def adopt(self, track: DownloadedTrack, source_url: str, requested_by: int, batch_id: str) -> QueueEntry:
        entry_id = uuid.uuid4().hex[:8]
        directory = self.root / entry_id
        directory.mkdir()
        audio_file = self._move_in(track.audio_path, directory)
        info_file = self._move_in(track.info_path, directory)
        cover_file = self._move_in(track.cover_path, directory) if track.cover_path else None
        entry = QueueEntry(
            id=entry_id,
            directory=directory,
            audio_file=audio_file,
            info_file=info_file,
            cover_file=cover_file,
            source_url=source_url,
            requested_by=requested_by,
            batch_id=batch_id,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self.save(entry)
        return entry

    def save(self, entry: QueueEntry) -> None:
        temporary = entry.directory / f"{ENTRY_FILE}.tmp"
        temporary.write_text(json.dumps(entry.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(entry.directory / ENTRY_FILE)

    def get(self, entry_id: str) -> QueueEntry | None:
        return self._load(self.root / entry_id)

    def list(self) -> list[QueueEntry]:
        entries = [
            entry
            for directory in self.root.iterdir()
            if directory.is_dir() and not directory.name.startswith(".")
            if (entry := self._load(directory))
        ]
        return sorted(entries, key=lambda entry: (entry.created_at, entry.id))

    def remove(self, entry: QueueEntry) -> None:
        shutil.rmtree(entry.directory, ignore_errors=True)

    @staticmethod
    def _load(directory: Path) -> QueueEntry | None:
        try:
            data = json.loads((directory / ENTRY_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return QueueEntry.from_json(directory, data)

    @staticmethod
    def _move_in(source: Path, directory: Path) -> str:
        shutil.move(source, directory / source.name)
        return source.name
