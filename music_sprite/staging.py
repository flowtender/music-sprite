import json
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .downloader import DownloadedTrack
from .models import TrackMetadata

ENTRY_FILE = "entry.json"
INCOMING_DIR = ".incoming"


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
    status: str = "pending"
    resolve_error: str | None = None
    edited: bool = False
    meta: TrackMetadata | None = None
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
            "edited": self.edited,
            "meta": self.meta.to_dict() if self.meta else None,
        }

    @classmethod
    def from_json(cls, directory: Path, data: dict) -> "QueueEntry":
        meta = data.pop("meta")
        return cls(directory=directory, meta=TrackMetadata.from_dict(meta) if meta else None, **data)


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
