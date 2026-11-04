from dataclasses import dataclass, replace

from . import heuristics
from .config import Config
from .llm import LlmClient
from .models import LookupUnavailable, TrackMetadata
from .musicbrainz import MusicBrainzClient

TRUSTED_SOURCE_CONFIDENCE = 0.9


@dataclass
class Resolution:
    metadata: TrackMetadata
    unavailable_services: list[str]


class MetadataResolver:
    def __init__(self, config: Config, musicbrainz: MusicBrainzClient, llm: LlmClient):
        self._config = config
        self._musicbrainz = musicbrainz
        self._llm = llm

    async def resolve(self, info: dict) -> Resolution:
        duration = info.get("duration")
        unavailable: list[str] = []
        draft = heuristics.guess(info, self._config.known_publishers)

        matched = await self._enrich(draft, duration, unavailable)
        if matched:
            return Resolution(self._keep_source_genre(matched, draft), unavailable)
        if draft.confidence >= TRUSTED_SOURCE_CONFIDENCE or not self._llm.enabled:
            return Resolution(draft, unavailable)

        try:
            refined = await self._llm.refine(info, draft)
        except LookupUnavailable as error:
            return Resolution(draft, [*unavailable, str(error)])
        matched = await self._enrich(refined, duration, unavailable)
        if matched:
            return Resolution(self._keep_source_genre(matched, refined), unavailable)
        return Resolution(refined, unavailable)

    async def _enrich(
        self, draft: TrackMetadata, duration: float | None, unavailable: list[str]
    ) -> TrackMetadata | None:
        if "musicbrainz" in unavailable:
            return None
        try:
            return await self._musicbrainz.enrich(draft, duration)
        except LookupUnavailable as error:
            unavailable.append(str(error))
            return None

    @staticmethod
    def _keep_source_genre(matched: TrackMetadata, earlier: TrackMetadata) -> TrackMetadata:
        return replace(matched, genre=matched.genre or earlier.genre)
