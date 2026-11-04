import asyncio
import logging
from dataclasses import replace
from difflib import SequenceMatcher

import httpx

from .models import LookupUnavailable, MusicBrainzIds, ReleaseInfo, TrackMetadata

log = logging.getLogger(__name__)

API_ROOT = "https://musicbrainz.org/ws/2"
RECORDING_URL = f"{API_ROOT}/recording"
RELEASE_URL = f"{API_ROOT}/release"
# One recording request carries the ISRCs, genres, performer credits and the writers of the underlying work; one
# release request adds the track position, label, catalog number, barcode, album artist and release-group genres.
RECORDING_INC = "artists+artist-credits+releases+release-groups+isrcs+genres+work-rels+work-level-rels+artist-rels"
RELEASE_INC = "artist-credits+labels+recordings+release-groups+genres"
MIN_SEARCH_SCORE = 90
MIN_TEXT_SIMILARITY = 0.85
DURATION_TOLERANCE_SECONDS = 15
REQUEST_SPACING_SECONDS = 1.1
MAX_GENRES = 5
MAX_CREDITS_PER_ROLE = 50

# Relationship type -> credit role. Performers carry their instrument or vocal part in the entry.
RECORDING_ROLES = {
    "producer": "producer",
    "engineer": "engineer",
    "recording": "engineer",
    "mastering": "engineer",
    "sound": "engineer",
    "mix": "mixer",
    "remixer": "remixer",
    "conductor": "conductor",
    "arranger": "arranger",
    "orchestrator": "arranger",
    "instrument": "performer",
    "vocal": "performer",
    "performer": "performer",
    "programming": "performer",
}
WORK_ROLES = {
    "composer": "composer",
    "lyricist": "lyricist",
    "writer": "writer",
    "arranger": "arranger",
    "orchestrator": "arranger",
}


def _similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, left.casefold(), right.casefold()).ratio()


def _escape(term: str) -> str:
    return term.replace("\\", "\\\\").replace('"', '\\"')


def _credited(credits: list[dict], *, sort: bool = False) -> str:
    """Joins an artist credit the way it is printed: names with their join phrases ("A feat. B")."""
    parts = []
    for credit in credits:
        sort_name = (credit.get("artist") or {}).get("sort-name") if sort else None
        parts.append((sort_name or credit.get("name", "")) + credit.get("joinphrase", ""))
    return "".join(parts)


def _artist_ids(credits: list[dict]) -> list[str]:
    return [credit["artist"]["id"] for credit in credits if "artist" in credit]


def _genres(*sources: dict | None) -> list[str]:
    """The most voted genres of the first source that has any: recording, then release group, release, artist."""
    for source in sources:
        genres = (source or {}).get("genres") or []
        if genres:
            ranked = sorted(genres, key=lambda genre: (-genre.get("count", 0), genre["name"]))
            return [genre["name"].title() for genre in ranked[:MAX_GENRES]]
    return []


def _add_credit(credits: dict[str, list[str]], role: str, entry: str) -> None:
    entries = credits.setdefault(role, [])
    if entry not in entries and len(entries) < MAX_CREDITS_PER_ROLE:
        entries.append(entry)


def _performer(relation: dict) -> str:
    name = relation["artist"]["name"]
    parts = relation.get("attributes") or ([] if relation["type"] != "vocal" else ["vocals"])
    if relation["type"] == "programming":
        parts = ["programming", *parts]
    return f"{name} ({', '.join(parts)})" if parts else name


def _credits(recording: dict, works: list[dict]) -> dict[str, list[str]]:
    credits: dict[str, list[str]] = {}
    for relation in recording.get("relations", []):
        role = RECORDING_ROLES.get(relation["type"])
        if relation.get("target-type") == "artist" and role:
            _add_credit(credits, role, _performer(relation) if role == "performer" else relation["artist"]["name"])
    for work in works:
        for relation in work.get("relations", []):
            role = WORK_ROLES.get(relation["type"])
            if relation.get("target-type") == "artist" and role:
                _add_credit(credits, role, relation["artist"]["name"])
    if "writer" in credits and "composer" not in credits:
        # Popular songs credit "writers" only, and players show the composer tag.
        credits["composer"] = list(credits["writer"])
    return credits


def _find_track(release: dict, recording_id: str) -> tuple[dict, dict]:
    for medium in release.get("media", []):
        for track in medium.get("tracks", []):
            if (track.get("recording") or {}).get("id") == recording_id:
                return track, medium
    return {}, {}


def _release_label(release: dict) -> tuple[str | None, str | None]:
    entries = release.get("label-info") or []
    named = next((entry for entry in entries if (entry.get("label") or {}).get("name")), None)
    label = named["label"]["name"] if named else None
    catalog = next(
        (entry["catalog-number"] for entry in [named, *entries] if entry and entry.get("catalog-number")), None
    )
    return label, catalog


class MusicBrainzClient:
    def __init__(self, http: httpx.AsyncClient, contact: str):
        self._http = http
        self._user_agent = f"music-sprite/0.1 ({contact or 'no-contact'})"
        self._lock = asyncio.Lock()

    async def enrich(self, draft: TrackMetadata, duration_seconds: float | None) -> list[TrackMetadata]:
        """Returns every recording that matches at the best search score, with full details."""
        if not draft.artist or not draft.title:
            return []
        query = f'recording:"{_escape(draft.title)}" AND artist:"{_escape(draft.artist)}"'
        matching = [
            recording
            for recording in await self._search(query)
            if self._matches(recording, draft, duration_seconds)
        ]
        if not matching:
            return []
        best_score = max(recording["score"] for recording in matching)
        tied = [recording for recording in matching if recording["score"] == best_score]
        return [await self.fetch_recording(recording["id"], draft, best_score / 100, "musicbrainz") for recording in tied]

    async def fetch_recording(
        self, recording_id: str, draft: TrackMetadata, confidence: float, origin: str
    ) -> TrackMetadata:
        recording = await self._get(f"{RECORDING_URL}/{recording_id}", {"inc": RECORDING_INC})
        stub = self._pick_release(recording.get("releases") or [])
        release = await self._get(f"{RELEASE_URL}/{stub['id']}", {"inc": RELEASE_INC}) if stub else {}
        return self._merge(draft, recording, release, confidence, origin)

    async def _search(self, query: str) -> list[dict]:
        return (await self._get(RECORDING_URL, {"query": query, "limit": 5})).get("recordings", [])

    async def _get(self, url: str, params: dict) -> dict:
        async with self._lock:
            try:
                response = await self._http.get(
                    url,
                    params={**params, "fmt": "json"},
                    headers={"User-Agent": self._user_agent},
                    timeout=15,
                )
                response.raise_for_status()
                return response.json()
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
        if _similarity(_credited(recording.get("artist-credit", [])), draft.artist) < MIN_TEXT_SIMILARITY:
            return False
        length_ms = recording.get("length")
        if length_ms and duration_seconds:
            return abs(length_ms / 1000 - duration_seconds) <= DURATION_TOLERANCE_SECONDS
        return True

    @staticmethod
    def _pick_release(releases: list[dict]) -> dict:
        """Prefers the earliest official studio album, then any official release, then anything."""

        def rank(release: dict) -> tuple[bool, bool, str]:
            group = release.get("release-group") or {}
            studio_album = group.get("primary-type") == "Album" and not group.get("secondary-types")
            return (release.get("status") != "Official", not studio_album, release.get("date") or "9999")

        return min(releases, key=rank) if releases else {}

    def _merge(
        self, draft: TrackMetadata, recording: dict, release: dict, confidence: float, origin: str
    ) -> TrackMetadata:
        group = release.get("release-group") or {}
        artist_credit = recording.get("artist-credit", [])
        album_credit = release.get("artist-credit", [])
        track, medium = _find_track(release, recording["id"])
        works = [
            relation["work"]
            for relation in recording.get("relations", [])
            if relation.get("target-type") == "work" and relation.get("work")
        ]
        label, catalog_number = _release_label(release)
        text = release.get("text-representation") or {}
        release_date = release.get("date") or None
        length_ms = recording.get("length")

        details = ReleaseInfo(
            artist_sort=_credited(artist_credit, sort=True) or None,
            album_artist_sort=_credited(album_credit, sort=True) or None,
            release_date=release_date,
            original_date=group.get("first-release-date") or recording.get("first-release-date") or None,
            track_number=track.get("position"),
            track_total=medium.get("track-count"),
            disc_number=medium.get("position"),
            disc_total=len(release.get("media", [])) or None,
            media_format=medium.get("format"),
            label=label,
            catalog_number=catalog_number,
            barcode=release.get("barcode") or None,
            country=release.get("country"),
            status=(release.get("status") or "").lower() or None,
            types=[
                value.lower()
                for value in [group.get("primary-type"), *(group.get("secondary-types") or [])]
                if value
            ],
            language=text.get("language") or next((work["language"] for work in works if work.get("language")), None),
            script=text.get("script"),
            isrcs=recording.get("isrcs") or [],
            iswcs=[iswc for work in works for iswc in work.get("iswcs") or []],
            credits=_credits(recording, works),
        )
        ids = MusicBrainzIds(
            recording_id=recording["id"],
            release_id=release.get("id"),
            artist_ids=_artist_ids(artist_credit),
            length_seconds=length_ms / 1000 if length_ms else None,
            release_group_id=group.get("id"),
            release_track_id=track.get("id"),
            album_artist_ids=_artist_ids(album_credit),
            work_ids=[work["id"] for work in works],
        )
        genres = _genres(
            recording, group, release, (album_credit[0].get("artist") if album_credit else None)
        )
        year = (release_date or "")[:4]
        return replace(
            draft,
            title=recording.get("title", draft.title),
            artist=_credited(artist_credit) or draft.artist,
            album=release.get("title") or draft.album,
            album_artist=_credited(album_credit) or draft.album_artist,
            year=int(year) if year.isdigit() else draft.year,
            genres=genres or draft.genres,
            confidence=max(draft.confidence, confidence),
            origin=origin,
            musicbrainz=ids,
            release=details,
        )
