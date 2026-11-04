import asyncio
import logging
from dataclasses import replace
from difflib import SequenceMatcher

import httpx

from .models import LookupUnavailable, MusicBrainzIds, TrackMetadata

log = logging.getLogger(__name__)

API_URL = "https://musicbrainz.org/ws/2/recording"
MIN_SEARCH_SCORE = 90
MIN_TEXT_SIMILARITY = 0.85
DURATION_TOLERANCE_SECONDS = 15
REQUEST_SPACING_SECONDS = 1.1


def _similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, left.casefold(), right.casefold()).ratio()


def _escape(term: str) -> str:
    return term.replace("\\", "\\\\").replace('"', '\\"')


class MusicBrainzClient:
    def __init__(self, http: httpx.AsyncClient, contact: str):
        self._http = http
        self._user_agent = f"music-sprite/0.1 ({contact or 'no-contact'})"
        self._lock = asyncio.Lock()

    async def enrich(self, draft: TrackMetadata, duration_seconds: float | None) -> TrackMetadata | None:
        if not draft.artist or not draft.title:
            return None
        query = f'recording:"{_escape(draft.title)}" AND artist:"{_escape(draft.artist)}"'
        recordings = await self._search(query)
        for recording in recordings:
            if self._matches(recording, draft, duration_seconds):
                return self._merge(draft, recording)
        return None

    async def _search(self, query: str) -> list[dict]:
        async with self._lock:
            try:
                response = await self._http.get(
                    API_URL,
                    params={"query": query, "fmt": "json", "limit": 5},
                    headers={"User-Agent": self._user_agent},
                    timeout=15,
                )
                response.raise_for_status()
                return response.json().get("recordings", [])
            except (httpx.HTTPError, ValueError) as error:
                log.warning("MusicBrainz lookup failed: %s", error)
                raise LookupUnavailable("musicbrainz") from error
            finally:
                await asyncio.sleep(REQUEST_SPACING_SECONDS)

    def _matches(self, recording: dict, draft: TrackMetadata, duration_seconds: float | None) -> bool:
        if recording.get("score", 0) < MIN_SEARCH_SCORE:
            return False
        if _similarity(recording.get("title", ""), draft.title) < MIN_TEXT_SIMILARITY:
            return False
        if _similarity(self._credited_artist(recording), draft.artist) < MIN_TEXT_SIMILARITY:
            return False
        length_ms = recording.get("length")
        if length_ms and duration_seconds:
            return abs(length_ms / 1000 - duration_seconds) <= DURATION_TOLERANCE_SECONDS
        return True

    @staticmethod
    def _credited_artist(recording: dict) -> str:
        return "".join(
            credit.get("name", "") + credit.get("joinphrase", "")
            for credit in recording.get("artist-credit", [])
        )

    def _merge(self, draft: TrackMetadata, recording: dict) -> TrackMetadata:
        release = (recording.get("releases") or [{}])[0]
        release_year = str(release.get("date", ""))[:4]
        ids = MusicBrainzIds(
            recording_id=recording["id"],
            release_id=release.get("id"),
            artist_ids=[
                credit["artist"]["id"]
                for credit in recording.get("artist-credit", [])
                if "artist" in credit
            ],
        )
        return replace(
            draft,
            title=recording.get("title", draft.title),
            artist=self._credited_artist(recording) or draft.artist,
            album=release.get("title") or draft.album,
            year=int(release_year) if release_year.isdigit() else draft.year,
            confidence=max(draft.confidence, recording["score"] / 100),
            origin="musicbrainz",
            musicbrainz=ids,
        )
