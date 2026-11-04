import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import heuristics
from .acoustid import AcoustIdClient
from .analysis import AnalysisError, Analyzer, describe, guessed_genres
from .config import Config
from .llm import LlmClient
from .models import LookupUnavailable, TrackMetadata
from .musicbrainz import MusicBrainzClient

log = logging.getLogger(__name__)

TRUSTED_SOURCE_CONFIDENCE = 0.9
MAX_LISTED_RELEASES = 3
MAX_LISTED_CREDITS = 3

# Called with the finished steps so far and what is running right now (None between steps).
ProgressCallback = Callable[[list[str], str | None], Awaitable[None]]


@dataclass
class Resolution:
    metadata: TrackMetadata
    unavailable_services: list[str]
    steps: list[str] = field(default_factory=list)
    # Two or more equally good matches for the user to choose between; empty when the match is unambiguous.
    candidates: list[TrackMetadata] = field(default_factory=list)


class _Steps:
    """The lookup log: finished steps plus the one running now, reported live so the user can follow along."""

    def __init__(self, notify: ProgressCallback | None):
        self.lines: list[str] = []
        self._notify = notify

    async def add(self, text: str) -> None:
        self.lines.append(text)
        await self._push(None)

    async def working(self, text: str) -> None:
        await self._push(text)

    async def _push(self, working: str | None) -> None:
        if self._notify is None:
            return
        try:
            await self._notify(self.lines, working)
        except Exception:
            # A failed progress display (say, the message was deleted) must not abort the lookup itself.
            log.warning("progress update failed; continuing without live progress", exc_info=True)
            self._notify = None


def _describe(meta: TrackMetadata) -> str:
    return f"{meta.artist or '?'} – {meta.title}"


def _detail(meta: TrackMetadata) -> str:
    """Everything known about a track on one line, for the lookup log."""
    release = meta.release
    parts = [_describe(meta)]
    if meta.album:
        parts.append(f"album {meta.album}")
    if release.release_date or meta.year:
        parts.append(release.release_date or str(meta.year))
    if release.track_number:
        position = f"track {release.track_number}" + (f"/{release.track_total}" if release.track_total else "")
        if release.disc_total and release.disc_total > 1:
            position += f" (disc {release.disc_number}/{release.disc_total})"
        parts.append(position)
    if release.label:
        parts.append(release.label + (f" {release.catalog_number}" if release.catalog_number else ""))
    if meta.genres:
        parts.append("genre " + ", ".join(meta.genres))
    length = meta.musicbrainz.length_seconds if meta.musicbrainz else None
    if length:
        parts.append(f"{int(length // 60)}:{int(length % 60):02d}")
    return " · ".join(parts)


def _release_facts(meta: TrackMetadata) -> str:
    release = meta.release
    facts = ["/".join(release.types), release.status, release.country, release.media_format]
    facts = [fact for fact in facts if fact]
    if release.barcode:
        facts.append(f"barcode {release.barcode}")
    if release.isrcs:
        facts.append("ISRC " + ", ".join(release.isrcs[:2]))
    if release.language:
        facts.append(f"language {release.language}")
    return " · ".join(facts)


def _credit_summary(meta: TrackMetadata) -> str:
    parts = []
    for role, names in meta.release.credits.items():
        hidden = len(names) - MAX_LISTED_CREDITS
        parts.append(f"{role} {', '.join(names[:MAX_LISTED_CREDITS])}" + (f" +{hidden} more" if hidden > 0 else ""))
    return " · ".join(parts)


def _release_label(meta: TrackMetadata) -> str:
    return f"{meta.album or 'single'}" + (f" ({meta.year})" if meta.year else "")


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
    def __init__(
        self,
        config: Config,
        acoustid: AcoustIdClient,
        musicbrainz: MusicBrainzClient,
        llm: LlmClient,
        analyzer: Analyzer,
    ):
        self._config = config
        self._acoustid = acoustid
        self._musicbrainz = musicbrainz
        self._llm = llm
        self._analyzer = analyzer

    async def resolve(self, info: dict, audio_path: Path, progress: ProgressCallback | None = None) -> Resolution:
        steps = _Steps(progress)
        resolution = await self._resolve(info, audio_path, steps)
        await self._analyze(resolution, audio_path, steps)
        return resolution

    async def _analyze(self, resolution: Resolution, audio_path: Path, steps: _Steps) -> None:
        reason = self._analyzer.disabled_reason
        if reason:
            await steps.add(f"Analysis: skipped ({reason})")
            return
        await steps.working("Analyzing the audio (tempo, key, genre, mood, loudness)")
        try:
            features = await self._analyzer.analyze(audio_path)
        except AnalysisError as error:
            await steps.add(f"Analysis: failed ({error})")
            return
        except Exception:
            log.exception("audio analysis crashed for %s", audio_path)
            await steps.add("Analysis: crashed; see the service log")
            return
        for line in describe(features):
            await steps.add(f"Analysis: {line}")

        def with_analysis(meta: TrackMetadata) -> TrackMetadata:
            return replace(meta, audio=features, genres=meta.genres or guessed_genres(features))

        resolution.metadata = with_analysis(resolution.metadata)
        resolution.candidates = [with_analysis(candidate) for candidate in resolution.candidates]

    async def _resolve(self, info: dict, audio_path: Path, steps: _Steps) -> Resolution:
        duration = info.get("duration")
        unavailable: list[str] = []
        draft = heuristics.guess(info, self._config.known_publishers)
        await steps.add(
            f"Heuristic ({draft.origin}): {_detail(draft)} · confidence {draft.confidence:.0%}"
            + (" · known publisher" if draft.from_known_publisher else "")
        )

        matches = await self._identify_by_audio(audio_path, draft, unavailable, steps)
        if matches:
            return self._resolution(matches, draft, duration, unavailable, steps)

        matches = await self._enrich(draft, duration, unavailable, steps)
        if matches:
            return self._resolution(matches, draft, duration, unavailable, steps)

        if draft.confidence >= TRUSTED_SOURCE_CONFIDENCE:
            await steps.add("LLM: skipped (source tags already trusted)")
            return Resolution(draft, unavailable, steps.lines)
        if not self._llm.enabled:
            await steps.add("LLM: skipped (LLM_MODEL not set)")
            return Resolution(draft, unavailable, steps.lines)

        await steps.working("Asking the LLM")
        try:
            refined = await self._llm.refine(info, draft)
        except LookupUnavailable as error:
            await steps.add("LLM: unavailable")
            return Resolution(draft, [*unavailable, str(error)], steps.lines)
        await steps.add(f"LLM ({refined.origin}): {_detail(refined)} · confidence {refined.confidence:.0%}")
        matches = await self._enrich(refined, duration, unavailable, steps)
        if matches:
            return self._resolution(matches, refined, duration, unavailable, steps)
        return Resolution(refined, unavailable, steps.lines)

    def _resolution(
        self,
        matches: list[TrackMetadata],
        earlier: TrackMetadata,
        duration: float | None,
        unavailable: list[str],
        steps: _Steps,
    ) -> Resolution:
        def rank(match: TrackMetadata) -> tuple[int, int]:
            # Closest in length first (to the second, so 1.0s vs 1.04s is a tie), then the earliest release.
            length = match.musicbrainz.length_seconds if match.musicbrainz else None
            gap = round(abs(length - duration)) if length and duration else 0
            return gap, match.year or 9999

        ranked = [self._keep_source_genre(match, earlier) for match in sorted(_distinct(matches), key=rank)]
        return Resolution(ranked[0], unavailable, steps.lines, candidates=ranked if len(ranked) > 1 else [])

    async def _identify_by_audio(
        self, audio_path: Path, draft: TrackMetadata, unavailable: list[str], steps: _Steps
    ) -> list[TrackMetadata]:
        reason = self._acoustid.disabled_reason
        if reason:
            await steps.add(f"AcoustID: skipped ({reason})")
            return []
        await steps.working("Fingerprinting the audio (AcoustID)")
        try:
            match = await self._acoustid.identify(audio_path)
        except LookupUnavailable as error:
            unavailable.append(str(error))
            await steps.add(f"AcoustID: {error} unavailable")
            return []
        if match is None:
            await steps.add("AcoustID: no confident match")
            return []

        await steps.working(f"Fetching {len(match.recording_ids)} matching recording(s) from MusicBrainz")
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
            await steps.add("AcoustID: matched a recording but MusicBrainz is unavailable")
            return []
        await self._note_matches(steps, "AcoustID", _distinct(identified), f"{match.score:.0%}")
        return identified

    async def _enrich(
        self, draft: TrackMetadata, duration: float | None, unavailable: list[str], steps: _Steps
    ) -> list[TrackMetadata]:
        if "musicbrainz" in unavailable:
            await steps.add("MusicBrainz search: skipped (unavailable)")
            return []
        if not draft.artist or not draft.title:
            await steps.add("MusicBrainz search: skipped (no artist or title to search for)")
            return []
        await steps.working(f"Searching MusicBrainz for {_describe(draft)}")
        try:
            matches = await self._musicbrainz.enrich(draft, duration)
        except LookupUnavailable as error:
            unavailable.append(str(error))
            await steps.add("MusicBrainz search: unavailable")
            return []
        if not matches:
            await steps.add(f"MusicBrainz search: no match for {_describe(draft)}")
            return []
        await self._note_matches(steps, "MusicBrainz search", _distinct(matches), None)
        return matches

    @staticmethod
    async def _note_matches(steps: _Steps, source: str, matches: list[TrackMetadata], score: str | None) -> None:
        suffix = f" ({score})" if score else ""
        if len(matches) == 1:
            await steps.add(f"{source}: matched {_detail(matches[0])}{suffix}")
            for label, text in (("release", _release_facts(matches[0])), ("credits", _credit_summary(matches[0]))):
                if text:
                    await steps.add(f"MusicBrainz {label}: {text}")
            return
        listed = " / ".join(_release_label(match) for match in matches[:MAX_LISTED_RELEASES])
        hidden = len(matches) - MAX_LISTED_RELEASES
        more = f" / +{hidden} more" if hidden > 0 else ""
        await steps.add(
            f"{source}: {len(matches)} equally good matches{suffix} for {_describe(matches[0])}: {listed}{more};"
            " pick one in the review"
        )

    @staticmethod
    def _keep_source_genre(matched: TrackMetadata, earlier: TrackMetadata) -> TrackMetadata:
        return replace(matched, genres=matched.genres or earlier.genres)
