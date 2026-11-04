import asyncio
import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import Config
from .models import LookupUnavailable

log = logging.getLogger(__name__)

API_URL = "https://api.acoustid.org/v2/lookup"
MIN_SCORE = 0.9
FINGERPRINT_SECONDS = 120
FPCALC_TIMEOUT_SECONDS = 60


@dataclass
class AcoustIdMatch:
    recording_id: str
    score: float


class AcoustIdClient:
    def __init__(self, http: httpx.AsyncClient, config: Config):
        self._http = http
        self._config = config

    @property
    def disabled_reason(self) -> str | None:
        if not self._config.acoustid_api_key:
            return "ACOUSTID_API_KEY not set"
        if shutil.which("fpcalc") is None:
            return "fpcalc not installed (chromaprint package)"
        return None

    async def identify(self, audio_path: Path) -> AcoustIdMatch | None:
        fingerprint, duration = await self._fingerprint(audio_path)
        try:
            response = await self._http.post(
                API_URL,
                data={
                    "client": self._config.acoustid_api_key,
                    "meta": "recordings",
                    "duration": str(round(duration)),
                    "fingerprint": fingerprint,
                },
                timeout=20,
            )
            if response.is_error:
                log.warning("AcoustID lookup failed: HTTP %s %s", response.status_code, response.text[:200])
                raise LookupUnavailable("acoustid")
            payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            log.warning("AcoustID lookup failed: %s", error)
            raise LookupUnavailable("acoustid") from error
        return self._best_match(payload.get("results") or [], duration)

    @staticmethod
    def _best_match(results: list[dict], duration: float) -> AcoustIdMatch | None:
        for result in sorted(results, key=lambda item: item.get("score", 0), reverse=True):
            score = result.get("score", 0)
            recordings = result.get("recordings") or []
            if score < MIN_SCORE:
                return None
            if recordings:
                closest = min(recordings, key=lambda item: abs((item.get("duration") or duration) - duration))
                return AcoustIdMatch(closest["id"], score)
        return None

    @staticmethod
    async def _fingerprint(audio_path: Path) -> tuple[str, float]:
        process = await asyncio.create_subprocess_exec(
            "fpcalc", "-json", "-length", str(FINGERPRINT_SECONDS), str(audio_path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(FPCALC_TIMEOUT_SECONDS):
                stdout, stderr = await process.communicate()
        except TimeoutError:
            process.kill()
            await process.wait()
            log.warning("fpcalc timed out on %s", audio_path)
            raise LookupUnavailable("fpcalc")
        if process.returncode != 0:
            log.warning("fpcalc failed on %s: %s", audio_path, stderr.decode(errors="replace").strip()[-300:])
            raise LookupUnavailable("fpcalc")
        try:
            data = json.loads(stdout)
            return data["fingerprint"], float(data["duration"])
        except (ValueError, KeyError) as error:
            log.warning("unreadable fpcalc output for %s: %s", audio_path, error)
            raise LookupUnavailable("fpcalc") from error
