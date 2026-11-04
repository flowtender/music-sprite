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


def _describe(meta: TrackMetadata) -> str:
    return f"{meta.artist or '?'} – {meta.title}"


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

        identified = await self._identify_by_audio(audio_path, draft, unavailable, steps)
        if identified:
            return Resolution(self._keep_source_genre(identified, draft), unavailable, steps)

        matched = await self._enrich(draft, duration, unavailable, steps)
        if matched:
            return Resolution(self._keep_source_genre(matched, draft), unavailable, steps)

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
        matched = await self._enrich(refined, duration, unavailable, steps)
        if matched:
            return Resolution(self._keep_source_genre(matched, refined), unavailable, steps)
        return Resolution(refined, unavailable, steps)

    async def _identify_by_audio(
        self, audio_path: Path, draft: TrackMetadata, unavailable: list[str], steps: list[str]
    ) -> TrackMetadata | None:
        reason = self._acoustid.disabled_reason
        if reason:
            steps.append(f"AcoustID: skipped ({reason})")
            return None
        try:
            match = await self._acoustid.identify(audio_path)
        except LookupUnavailable as error:
            unavailable.append(str(error))
            steps.append(f"AcoustID: {error} unavailable")
            return None
        if match is None:
            steps.append("AcoustID: no confident match")
            return None
        try:
            identified = await self._musicbrainz.fetch_recording(
                match.recording_id, draft, match.score, "acoustid+musicbrainz"
            )
        except LookupUnavailable as error:
            unavailable.append(str(error))
            steps.append("AcoustID: matched a recording but MusicBrainz is unavailable")
            return None
        steps.append(f"AcoustID: matched {_describe(identified)} ({match.score:.0%})")
        return identified

    async def _enrich(
        self, draft: TrackMetadata, duration: float | None, unavailable: list[str], steps: list[str]
    ) -> TrackMetadata | None:
        if "musicbrainz" in unavailable:
            steps.append("MusicBrainz search: skipped (unavailable)")
            return None
        if not draft.artist or not draft.title:
            steps.append("MusicBrainz search: skipped (no artist or title to search for)")
            return None
        try:
            matched = await self._musicbrainz.enrich(draft, duration)
        except LookupUnavailable as error:
            unavailable.append(str(error))
            steps.append("MusicBrainz search: unavailable")
            return None
        if matched is None:
            steps.append(f"MusicBrainz search: no match for {_describe(draft)}")
        else:
            steps.append(f"MusicBrainz search: matched {_describe(matched)}")
        return matched

    @staticmethod
    def _keep_source_genre(matched: TrackMetadata, earlier: TrackMetadata) -> TrackMetadata:
        return replace(matched, genre=matched.genre or earlier.genre)
