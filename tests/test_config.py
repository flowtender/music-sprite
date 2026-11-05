from pathlib import Path

import pytest

from music_sprite.config import Config

VARIABLES = (
    "STATE_DIRECTORY", "DISCORD_TOKEN", "DISCORD_GUILD_ID", "DISCORD_GLOBAL_COMMANDS", "DISCORD_COMMAND_NAME",
    "DISCORD_ALLOWED_USER_IDS", "MUSIC_ROOT", "LIBRARY_PATH_TEMPLATE", "QUEUE_DIR", "ARCHIVE_DIR",
    "MAX_CONCURRENT_DOWNLOADS", "MAX_PLAYLIST_ITEMS", "AUTO_IMPORT_CONFIDENCE", "KNOWN_PUBLISHERS",
    "MUSICBRAINZ_CONTACT", "ACOUSTID_API_KEY", "ANALYSIS_ENABLED", "ANALYSIS_DEVICE", "ANALYSIS_MODEL_DIR",
    "LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY", "LLM_JSON_MODE", "LLM_TIMEOUT_SECONDS", "LLM_DESCRIPTION_CHARS",
    "LLM_CALL_LOG", "NAVIDROME_URL", "NAVIDROME_USER", "NAVIDROME_PASSWORD",
)


@pytest.fixture
def env(monkeypatch, tmp_path):
    for name in VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path))
    monkeypatch.setenv("DISCORD_TOKEN", "token")

    def load(**values) -> Config:
        for name, value in values.items():
            monkeypatch.setenv(name, value)
        return Config.from_env()

    return load


def test_defaults_live_under_the_state_directory(env, tmp_path):
    config = env()
    discord = config.discord
    assert (discord.token, discord.command_name, discord.guild_id, discord.global_commands) == (
        "token", "music_sprite", None, True
    )
    assert (config.queue.directory, config.library.archive_dir) == (tmp_path / "queue", tmp_path / "source-metadata")
    assert (config.analysis.model_dir, config.llm.call_log) == (tmp_path / "models", tmp_path / "llm_calls.jsonl")
    assert (config.library.music_root, config.library.path_template) == (Path("/srv/music"), "{album_artist}/{album}/{title}")
    assert config.lookup.known_publishers == {"epic network", "xkito music"}
    assert (config.queue.auto_import_confidence, config.analysis.enabled, config.analysis.device) == (None, True, "auto")
    assert (config.llm.model, config.llm.base_url, config.llm.json_mode) == ("", "http://localhost:11434/v1", "schema")
    assert (config.queue.max_concurrent_downloads, config.queue.max_playlist_items) == (2, 100)


def test_values_are_parsed(env):
    config = env(
        DISCORD_GUILD_ID="42",
        DISCORD_GLOBAL_COMMANDS="off",
        DISCORD_ALLOWED_USER_IDS="1, 2 3",
        KNOWN_PUBLISHERS="Some Records, ,Other",
        AUTO_IMPORT_CONFIDENCE="0.9",
        LLM_BASE_URL="http://llm/v1/",
        LLM_CALL_LOG="",
        LLM_TIMEOUT_SECONDS="5",
        NAVIDROME_URL="http://navidrome/",
        ANALYSIS_DEVICE="CPU",
    )
    assert (config.discord.guild_id, config.discord.global_commands, config.discord.allowed_user_ids) == (42, False, {1, 2, 3})
    assert config.lookup.known_publishers == {"some records", "other"}
    assert (config.queue.auto_import_confidence, config.llm.timeout_seconds) == (0.9, 5.0)
    assert (config.llm.base_url, config.library.navidrome_url, config.llm.call_log) == ("http://llm/v1", "http://navidrome", None)
    assert config.analysis.device == "cpu"


@pytest.mark.parametrize(
    "values, message",
    [
        ({"DISCORD_COMMAND_NAME": "Music"}, "DISCORD_COMMAND_NAME"),
        ({"DISCORD_GLOBAL_COMMANDS": "false"}, "DISCORD_GUILD_ID"),
        ({"DISCORD_GLOBAL_COMMANDS": "maybe"}, "true or false"),
        ({"ANALYSIS_DEVICE": "gpu"}, "ANALYSIS_DEVICE"),
        ({"LIBRARY_PATH_TEMPLATE": "{artist}/{album}"}, "must contain {title}"),
        ({"LIBRARY_PATH_TEMPLATE": "../{title}"}, "relative path"),
    ],
)
def test_invalid_settings_are_rejected_at_startup(env, values, message):
    with pytest.raises(ValueError, match=message):
        env(**values)
