"""Settings, all from environment variables (see music-sprite.env.example), grouped by the part of the bot that uses them."""

import os
import re
from dataclasses import dataclass
from pathlib import Path

from .layout import DEFAULT_TEMPLATE, validate_template


def _id_set(name: str) -> frozenset[int]:
    return frozenset(int(part) for part in os.environ.get(name, "").replace(",", " ").split())


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{name} must be true or false, got {raw!r}")


def _name_set(name: str, default: str) -> frozenset[str]:
    raw = os.environ.get(name, default)
    return frozenset(part.strip().casefold() for part in raw.split(",") if part.strip())


@dataclass(frozen=True)
class DiscordConfig:
    token: str
    guild_id: int | None
    global_commands: bool
    command_name: str
    allowed_user_ids: frozenset[int]

    @classmethod
    def from_env(cls) -> "DiscordConfig":
        guild_id = int(os.environ.get("DISCORD_GUILD_ID") or 0) or None
        global_commands = _flag("DISCORD_GLOBAL_COMMANDS", True)
        command_name = os.environ.get("DISCORD_COMMAND_NAME", "").strip() or "music_sprite"
        if command_name != command_name.lower() or not re.fullmatch(r"[\w-]{1,32}", command_name):
            raise ValueError(
                f"DISCORD_COMMAND_NAME must be 1-32 lowercase letters, digits, '-' or '_', got {command_name!r}"
            )
        if guild_id is None and not global_commands:
            raise ValueError("Set DISCORD_GUILD_ID or enable DISCORD_GLOBAL_COMMANDS; otherwise no commands would be registered")
        return cls(
            token=os.environ["DISCORD_TOKEN"],
            guild_id=guild_id,
            global_commands=global_commands,
            command_name=command_name,
            allowed_user_ids=_id_set("DISCORD_ALLOWED_USER_IDS"),
        )


@dataclass(frozen=True)
class QueueConfig:
    directory: Path
    max_concurrent_downloads: int
    max_playlist_items: int
    auto_import_confidence: float | None

    @classmethod
    def from_env(cls, state_dir: Path) -> "QueueConfig":
        auto_import = os.environ.get("AUTO_IMPORT_CONFIDENCE", "")
        return cls(
            directory=Path(os.environ.get("QUEUE_DIR", state_dir / "queue")),
            max_concurrent_downloads=int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "2")),
            max_playlist_items=int(os.environ.get("MAX_PLAYLIST_ITEMS", "100")),
            auto_import_confidence=float(auto_import) if auto_import else None,
        )


@dataclass(frozen=True)
class LibraryConfig:
    """Where approved tracks go, and the Navidrome server that serves them."""

    music_root: Path
    path_template: str
    archive_dir: Path
    navidrome_url: str
    navidrome_user: str
    navidrome_password: str

    @classmethod
    def from_env(cls, state_dir: Path) -> "LibraryConfig":
        template = os.environ.get("LIBRARY_PATH_TEMPLATE", DEFAULT_TEMPLATE)
        validate_template(template)
        return cls(
            music_root=Path(os.environ.get("MUSIC_ROOT", "/srv/music")),
            path_template=template,
            archive_dir=Path(os.environ.get("ARCHIVE_DIR", state_dir / "source-metadata")),
            navidrome_url=os.environ.get("NAVIDROME_URL", "").rstrip("/"),
            navidrome_user=os.environ.get("NAVIDROME_USER", ""),
            navidrome_password=os.environ.get("NAVIDROME_PASSWORD", ""),
        )


@dataclass(frozen=True)
class LookupConfig:
    known_publishers: frozenset[str]
    musicbrainz_contact: str
    acoustid_api_key: str

    @classmethod
    def from_env(cls) -> "LookupConfig":
        return cls(
            known_publishers=_name_set("KNOWN_PUBLISHERS", "Epic Network,Xkito Music"),
            musicbrainz_contact=os.environ.get("MUSICBRAINZ_CONTACT", ""),
            acoustid_api_key=os.environ.get("ACOUSTID_API_KEY", ""),
        )


@dataclass(frozen=True)
class AnalysisConfig:
    enabled: bool
    device: str  # "auto" or "cpu"
    model_dir: Path

    @classmethod
    def from_env(cls, state_dir: Path) -> "AnalysisConfig":
        device = os.environ.get("ANALYSIS_DEVICE", "").strip().lower() or "auto"
        if device not in ("auto", "cpu"):
            raise ValueError(f"ANALYSIS_DEVICE must be auto or cpu, got {device!r}")
        return cls(
            enabled=_flag("ANALYSIS_ENABLED", True),
            device=device,
            model_dir=Path(os.environ.get("ANALYSIS_MODEL_DIR", state_dir / "models")),
        )


@dataclass(frozen=True)
class LlmConfig:
    model: str  # empty disables the LLM
    base_url: str
    api_key: str
    json_mode: str
    timeout_seconds: float
    description_chars: int
    call_log: Path | None

    @classmethod
    def from_env(cls, state_dir: Path) -> "LlmConfig":
        call_log = os.environ.get("LLM_CALL_LOG", str(state_dir / "llm_calls.jsonl"))
        return cls(
            model=os.environ.get("LLM_MODEL", ""),
            base_url=os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1").rstrip("/"),
            api_key=os.environ.get("LLM_API_KEY", ""),
            json_mode=os.environ.get("LLM_JSON_MODE", "schema"),
            timeout_seconds=float(os.environ.get("LLM_TIMEOUT_SECONDS", "60")),
            description_chars=int(os.environ.get("LLM_DESCRIPTION_CHARS", "5000")),
            call_log=Path(call_log) if call_log else None,
        )


@dataclass(frozen=True)
class Config:
    discord: DiscordConfig
    queue: QueueConfig
    library: LibraryConfig
    lookup: LookupConfig
    analysis: AnalysisConfig
    llm: LlmConfig

    @classmethod
    def from_env(cls) -> "Config":
        state_dir = Path(os.environ.get("STATE_DIRECTORY", "/var/lib/music-sprite"))
        return cls(
            discord=DiscordConfig.from_env(),
            queue=QueueConfig.from_env(state_dir),
            library=LibraryConfig.from_env(state_dir),
            lookup=LookupConfig.from_env(),
            analysis=AnalysisConfig.from_env(state_dir),
            llm=LlmConfig.from_env(state_dir),
        )
