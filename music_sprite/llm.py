import json
import logging
import re
import time
from dataclasses import replace

import httpx

from .config import Config
from .models import LookupUnavailable, TrackMetadata

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You extract music metadata from video/audio upload information.

Rules:
- "artist" is the performing musician or group, not the uploader. Independent artists often upload to their own channel, so the channel name is the artist. Publishers and labels (channels listed in "publisher_channels", or names such as "Network", "Records", "Music", "Promotions") re-upload other people's music: find the real artist in the title or description instead.
- "title" is only the track name. Remove "(Official Video)", "[Free Download]", "ft./feat." credits, promotional text and similar. Keep remix/version info that is part of the track name.
- If the artist appears with featured artists, put all of them in "artist" separated by ", ".
- "album" only if the information explicitly names an album, EP or release; otherwise null.
- "year" only if the release year is stated or clearly implied; otherwise null. Do not guess from upload date alone.
- "genre" only if stated in tags or description; otherwise null.
- "confidence" is 0.0 to 1.0: how sure you are that artist and title are correct.
- "reasoning" is one short sentence explaining where the artist was found.
Never invent information that is not in the input."""

_NULLABLE_STRING = {"anyOf": [{"type": "string"}, {"type": "null"}]}
_NULLABLE_INTEGER = {"anyOf": [{"type": "integer"}, {"type": "null"}]}

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "reasoning": {"type": "string"},
        "artist": {"type": "string"},
        "title": {"type": "string"},
        "album": _NULLABLE_STRING,
        "album_artist": _NULLABLE_STRING,
        "year": _NULLABLE_INTEGER,
        "genre": _NULLABLE_STRING,
        "confidence": {"type": "number"},
    },
    "required": ["reasoning", "artist", "title", "album", "album_artist", "year", "genre", "confidence"],
    "additionalProperties": False,
}

THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)


class LlmClient:
    def __init__(self, http: httpx.AsyncClient, config: Config):
        self._http = http
        self._config = config

    @property
    def enabled(self) -> bool:
        return bool(self._config.llm_model)

    async def refine(self, info: dict, draft: TrackMetadata) -> TrackMetadata:
        facts = self._facts(info, draft)
        started = time.monotonic()
        try:
            response = await self._http.post(
                f"{self._config.llm_base_url}/chat/completions",
                json=self._request_body(facts),
                headers=self._headers(),
                timeout=self._config.llm_timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
            parsed = self._parse(payload["choices"][0]["message"]["content"])
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as error:
            log.warning("LLM metadata lookup failed: %s", error)
            self._log_call(info, started, None, None, str(error))
            raise LookupUnavailable("llm") from error

        self._log_call(info, started, payload.get("usage"), parsed, None)
        return replace(
            draft,
            title=(parsed.get("title") or draft.title).strip(),
            artist=(parsed.get("artist") or "").strip(),
            album=parsed.get("album"),
            album_artist=parsed.get("album_artist"),
            year=parsed.get("year") if isinstance(parsed.get("year"), int) else draft.year,
            genre=parsed.get("genre") or draft.genre,
            confidence=min(max(float(parsed.get("confidence", 0.0)), 0.0), 1.0),
            origin=f"llm:{self._config.llm_model}",
        )

    def _facts(self, info: dict, draft: TrackMetadata) -> dict:
        description = (info.get("description") or "")[: self._config.llm_description_chars]
        return {
            "publisher_channels": sorted(self._config.known_publishers),
            "source": info.get("extractor_key"),
            "uploader_or_channel": info.get("channel") or info.get("uploader"),
            "upload_title": info.get("title"),
            "description": description,
            "tags": (info.get("tags") or [])[:20],
            "upload_date": info.get("upload_date"),
            "duration_seconds": info.get("duration"),
            "playlist_title": info.get("playlist_title"),
            "position_in_playlist": info.get("playlist_index"),
            "heuristic_guess": {"artist": draft.artist, "title": draft.title},
        }

    def _request_body(self, facts: dict) -> dict:
        body = {
            "model": self._config.llm_model,
            "temperature": 0,
            "max_tokens": 500,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(facts, ensure_ascii=False)},
            ],
        }
        if self._config.llm_json_mode == "schema":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "track_metadata", "strict": True, "schema": RESPONSE_SCHEMA},
            }
        elif self._config.llm_json_mode == "object":
            body["response_format"] = {"type": "json_object"}
        return body

    def _headers(self) -> dict:
        if self._config.llm_api_key:
            return {"Authorization": f"Bearer {self._config.llm_api_key}"}
        return {}

    @staticmethod
    def _parse(content: str) -> dict:
        content = THINK_BLOCK.sub("", content)
        start, end = content.find("{"), content.rfind("}")
        if start < 0 or end < start:
            raise ValueError("no JSON object in model output")
        return json.loads(content[start : end + 1])

    def _log_call(self, info: dict, started: float, usage: dict | None, parsed: dict | None, error: str | None):
        if not self._config.llm_call_log:
            return
        record = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "model": self._config.llm_model,
            "base_url": self._config.llm_base_url,
            "seconds": round(time.monotonic() - started, 2),
            "usage": usage,
            "url": info.get("webpage_url"),
            "result": parsed,
            "error": error,
        }
        try:
            self._config.llm_call_log.parent.mkdir(parents=True, exist_ok=True)
            with self._config.llm_call_log.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as os_error:
            log.warning("could not write LLM call log: %s", os_error)
