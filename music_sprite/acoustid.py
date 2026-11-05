"""Identifies a recording from its audio fingerprint (fpcalc + the AcoustID web service)."""

import asyncio
import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

import httpx

from .models import LookupUnavailable

log = logging.getLogger(__name__)

API_URL = "https://api.acoustid.org/v2/lookup"
MIN_SCORE = 0.9
FINGERPRINT_SECONDS = 120
FPCALC_TIMEOUT_SECONDS = 60
DURATION_TOLERANCE_SECONDS = 15
MAX_CANDIDATES = 5


@dataclass
class AcoustIdMatch:
    recording_ids: list[str]
    score: float


class AcoustIdClient:
    def __init__(self, http: httpx.AsyncClient, api_key: str):
        self._http = http
        self._api_key = api_key

    @property
    def disabled_reason(self) -> str | None:
        if not self._api_key:
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
                    "client": self._api_key,
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
        """Collects the recordings of every result tied at the best score that are close to our length."""
        scored = [result for result in results if result.get("recordings")]
        if not scored:
            return None
        best = max(result.get("score", 0) for result in scored)
        if best < MIN_SCORE:
            return None
        seen: dict[str, float] = {}
        for result in scored:
            if result.get("score", 0) == best:
                for recording in result["recordings"]:
                    seen.setdefault(recording["id"], abs((recording.get("duration") or duration) - duration))
        by_closeness = sorted(seen, key=seen.get)
        near = [recording_id for recording_id in by_closeness if seen[recording_id] <= DURATION_TOLERANCE_SECONDS]
        return AcoustIdMatch((near or by_closeness[:1])[:MAX_CANDIDATES], best)

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
