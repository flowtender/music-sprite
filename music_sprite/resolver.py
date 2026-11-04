from dataclasses import dataclass, field, replace
from pathlib import Path

from . import heuristics
from .acoustid import AcoustIdClient
from .config import Config
from .llm import LlmClient
from .models import LookupUnavailable, TrackMetadata
from .musicbrainz import MusicBrainzClient

TRUSTED_SOURCE_CONFIDENCE = 0.9


@dataclass
class Resolution:
    metadata: TrackMetadata
    unavailable_services: list[str]
    steps: list[str] = field(default_factory=list)
    # Two or more equally good matches for the user to choose between; empty when the match is unambiguous.
    candidates: list[TrackMetadata] = field(default_factory=list)


def _describe(meta: TrackMetadata) -> str:
    return f"{meta.artist or '?'} – {meta.title}"


def _distinct(matches: list[TrackMetadata]) -> list[TrackMetadata]:
    """Drops matches that would look identical to the user (different recordings of the same release)."""
    seen: set[tuple] = set()
    unique = []
    for match in matches:
        key = (match.artist.casefold(), match.title.casefold(), (match.album or "").casefold(), match.year)
        if key not in seen:
            seen.add(key)
            unique.append(match)
    return unique


class MetadataResolver:
    def __init__(self, config: Config, acoustid: AcoustIdClient, musicbrainz: MusicBrainzClient, llm: LlmClient):
        self._config = config
        self._acoustid = acoustid
        self._musicbrainz = musicbrainz
        self._llm = llm

    async def resolve(self, info: dict, audio_path: Path) -> Resolution:
        duration = info.get("duration")
        unavailable: list[str] = []
        draft = heuristics.guess(info, self._config.known_publishers)
        steps = [f"Heuristic ({draft.origin}): {_describe(draft)}"]

        matches = await self._identify_by_audio(audio_path, draft, unavailable, steps)
        if matches:
            return self._resolution(matches, draft, duration, unavailable, steps)

        matches = await self._enrich(draft, duration, unavailable, steps)
        if matches:
            return self._resolution(matches, draft, duration, unavailable, steps)

        if draft.confidence >= TRUSTED_SOURCE_CONFIDENCE:
            steps.append("LLM: skipped (source tags already trusted)")
            return Resolution(draft, unavailable, steps)
        if not self._llm.enabled:
            steps.append("LLM: skipped (LLM_MODEL not set)")
            return Resolution(draft, unavailable, steps)

        try:
            refined = await self._llm.refine(info, draft)
        except LookupUnavailable as error:
            steps.append("LLM: unavailable")
            return Resolution(draft, [*unavailable, str(error)], steps)
        steps.append(f"LLM ({refined.origin}): {_describe(refined)} (confidence {refined.confidence:.0%})")
        matches = await self._enrich(refined, duration, unavailable, steps)
        if matches:
            return self._resolution(matches, refined, duration, unavailable, steps)
        return Resolution(refined, unavailable, steps)

    def _resolution(
        self,
        matches: list[TrackMetadata],
        earlier: TrackMetadata,
        duration: float | None,
        unavailable: list[str],
        steps: list[str],
    ) -> Resolution:
        def rank(match: TrackMetadata) -> tuple[int, int]:
            # Closest in length first (to the second, so 1.0s vs 1.04s is a tie), then the earliest release.
            length = match.musicbrainz.length_seconds if match.musicbrainz else None
            gap = round(abs(length - duration)) if length and duration else 0
            return gap, match.year or 9999

        ranked = [self._keep_source_genre(match, earlier) for match in sorted(_distinct(matches), key=rank)]
        return Resolution(ranked[0], unavailable, steps, candidates=ranked if len(ranked) > 1 else [])

    async def _identify_by_audio(
        self, audio_path: Path, draft: TrackMetadata, unavailable: list[str], steps: list[str]
    ) -> list[TrackMetadata]:
        reason = self._acoustid.disabled_reason
        if reason:
            steps.append(f"AcoustID: skipped ({reason})")
            return []
        try:
            match = await self._acoustid.identify(audio_path)
        except LookupUnavailable as error:
            unavailable.append(str(error))
            steps.append(f"AcoustID: {error} unavailable")
            return []
        if match is None:
            steps.append("AcoustID: no confident match")
            return []

        identified: list[TrackMetadata] = []
        for recording_id in match.recording_ids:
            try:
                identified.append(
                    await self._musicbrainz.fetch_recording(recording_id, draft, match.score, "acoustid+musicbrainz")
                )
            except LookupUnavailable as error:
                unavailable.append(str(error))
                break
        if not identified:
            steps.append("AcoustID: matched a recording but MusicBrainz is unavailable")
            return []
        self._note_matches(steps, "AcoustID", _distinct(identified), f"{match.score:.0%}")
        return identified

    async def _enrich(
        self, draft: TrackMetadata, duration: float | None, unavailable: list[str], steps: list[str]
    ) -> list[TrackMetadata]:
        if "musicbrainz" in unavailable:
            steps.append("MusicBrainz search: skipped (unavailable)")
            return []
        if not draft.artist or not draft.title:
            steps.append("MusicBrainz search: skipped (no artist or title to search for)")
            return []
        try:
            matches = await self._musicbrainz.enrich(draft, duration)
        except LookupUnavailable as error:
            unavailable.append(str(error))
            steps.append("MusicBrainz search: unavailable")
            return []
        if not matches:
            steps.append(f"MusicBrainz search: no match for {_describe(draft)}")
            return []
        self._note_matches(steps, "MusicBrainz search", _distinct(matches), None)
        return matches

    @staticmethod
    def _note_matches(steps: list[str], source: str, matches: list[TrackMetadata], score: str | None) -> None:
        suffix = f" ({score})" if score else ""
        if len(matches) == 1:
            steps.append(f"{source}: matched {_describe(matches[0])}{suffix}")
        else:
            steps.append(f"{source}: {len(matches)} equally good matches{suffix}; pick one in the review")

    @staticmethod
    def _keep_source_genre(matched: TrackMetadata, earlier: TrackMetadata) -> TrackMetadata:
        return replace(matched, genre=matched.genre or earlier.genre)
