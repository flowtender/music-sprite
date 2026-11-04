import os
from dataclasses import dataclass
from pathlib import Path

from .layout import DEFAULT_TEMPLATE, validate_template


def _id_set(name: str) -> frozenset[int]:
    return frozenset(int(part) for part in os.environ.get(name, "").replace(",", " ").split())


def _name_set(name: str, default: str) -> frozenset[str]:
    raw = os.environ.get(name, default)
    return frozenset(part.strip().casefold() for part in raw.split(",") if part.strip())


@dataclass(frozen=True)
class Config:
    discord_token: str
    guild_id: int | None
    channel_ids: frozenset[int]
    allowed_user_ids: frozenset[int]

    music_root: Path
    library_path_template: str
    queue_dir: Path
    archive_dir: Path
    max_concurrent_downloads: int
    max_playlist_items: int
    auto_import_confidence: float | None

    known_publishers: frozenset[str]
    musicbrainz_contact: str

    llm_model: str
    llm_base_url: str
    llm_api_key: str
    llm_json_mode: str
    llm_timeout_seconds: float
    llm_description_chars: int
    llm_call_log: Path | None

    navidrome_url: str
    navidrome_user: str
    navidrome_password: str

    @classmethod
    def from_env(cls) -> "Config":
        state_dir = Path(os.environ.get("STATE_DIRECTORY", "/var/lib/music-sprite"))
        guild_id = os.environ.get("DISCORD_GUILD_ID", "")
        auto_import = os.environ.get("AUTO_IMPORT_CONFIDENCE", "")
        call_log = os.environ.get("LLM_CALL_LOG", str(state_dir / "llm_calls.jsonl"))
        template = os.environ.get("LIBRARY_PATH_TEMPLATE", DEFAULT_TEMPLATE)
        validate_template(template)
        return cls(
            discord_token=os.environ["DISCORD_TOKEN"],
            guild_id=int(guild_id) if guild_id else None,
            channel_ids=_id_set("DISCORD_CHANNEL_IDS"),
            allowed_user_ids=_id_set("DISCORD_ALLOWED_USER_IDS"),
            music_root=Path(os.environ.get("MUSIC_ROOT", "/srv/music")),
            library_path_template=template,
            queue_dir=Path(os.environ.get("QUEUE_DIR", state_dir / "queue")),
            archive_dir=Path(os.environ.get("ARCHIVE_DIR", state_dir / "source-metadata")),
            max_concurrent_downloads=int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "2")),
            max_playlist_items=int(os.environ.get("MAX_PLAYLIST_ITEMS", "100")),
            auto_import_confidence=float(auto_import) if auto_import else None,
            known_publishers=_name_set("KNOWN_PUBLISHERS", "Epic Network,Xkito Music"),
            musicbrainz_contact=os.environ.get("MUSICBRAINZ_CONTACT", ""),
            llm_model=os.environ.get("LLM_MODEL", ""),
            llm_base_url=os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1").rstrip("/"),
            llm_api_key=os.environ.get("LLM_API_KEY", ""),
            llm_json_mode=os.environ.get("LLM_JSON_MODE", "schema"),
            llm_timeout_seconds=float(os.environ.get("LLM_TIMEOUT_SECONDS", "60")),
            llm_description_chars=int(os.environ.get("LLM_DESCRIPTION_CHARS", "2000")),
            llm_call_log=Path(call_log) if call_log else None,
            navidrome_url=os.environ.get("NAVIDROME_URL", "").rstrip("/"),
            navidrome_user=os.environ.get("NAVIDROME_USER", ""),
            navidrome_password=os.environ.get("NAVIDROME_PASSWORD", ""),
        )
