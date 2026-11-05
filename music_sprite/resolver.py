import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from . import heuristics
from .acoustid import AcoustIdClient
from .analysis import AnalysisError, Analyzer, describe
from .config import Config
from .llm import LlmClient
from .models import AudioFeatures, Findings, LlmVerdict, LookupUnavailable, TrackMetadata
from .musicbrainz import MusicBrainzClient
from .reconcile import ambiguous_fields

log = logging.getLogger(__name__)

MAX_LISTED_RELEASES = 3
MAX_LISTED_CREDITS = 3

# Called with the finished steps so far and what is running right now.
ProgressCallback = Callable[[list[str], list[str]], Awaitable[None]]


@dataclass
class Resolution:
    findings: Findings
    unavailable_services: list[str]
    steps: list[str] = field(default_factory=list)


class _Steps:
    """The lookup log: finished steps plus those running now, reported live so the user can follow along."""

    def __init__(self, notify: ProgressCallback | None):
        self.lines: list[str] = []
        self._running: list[str] = []
        self._notify = notify

    async def add(self, text: str) -> None:
        self.lines.append(text)
        await self._push()

    @asynccontextmanager
    async def working(self, text: str):
        self._running.append(text)
        await self._push()
        try:
            yield
        finally:
            self._running.remove(text)

    async def _push(self) -> None:
        if self._notify is None:
            return
        try:
            await self._notify(self.lines, list(self._running))
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


def _recordings(matches: list[TrackMetadata]) -> set[str]:
    return {match.musicbrainz.recording_id for match in matches if match.musicbrainz}


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
        """Runs every source on every track. They do not depend on one another, so they run side by side; the LLM
        comes last because it reads what the others found."""
        steps = _Steps(progress)
        unavailable: list[str] = []
        duration = info.get("duration")
        draft = heuristics.guess(info, self._config.known_publishers)
        await steps.add(
            f"Upload ({draft.origin}): {_detail(draft)} · confidence {draft.confidence:.0%}"
            + (" · known publisher" if draft.from_known_publisher else "")
        )

        async with asyncio.TaskGroup() as group:
            identified = group.create_task(self._identify_by_audio(audio_path, draft, unavailable, steps))
            searched = group.create_task(self._search_musicbrainz(draft, duration, unavailable, steps))
            analysis = group.create_task(self._analyze(audio_path, steps))

        verified, found = identified.result(), searched.result()
        if verified and found and not _recordings(found) <= _recordings(verified):
            await steps.add("MusicBrainz search: found other recordings; the AcoustID match is used instead")
        findings = Findings(
            upload=draft,
            # A fingerprint match is verified by the audio itself, so it outranks whatever a text search found.
            releases=self._rank(verified or found, duration),
            audio=analysis.result(),
        )
        findings.llm = await self._ask_llm(info, findings, unavailable, steps)
        return Resolution(findings, list(dict.fromkeys(unavailable)), steps.lines)

    async def _analyze(self, audio_path: Path, steps: _Steps) -> AudioFeatures | None:
        reason = self._analyzer.disabled_reason
        if reason:
            await steps.add(f"Analysis: skipped ({reason})")
            return None
        async with steps.working("Analyzing the audio (tempo, key, genre, mood, loudness)"):
            try:
                features = await self._analyzer.analyze(audio_path)
            except AnalysisError as error:
                await steps.add(f"Analysis: failed ({error})")
                return None
            except Exception:
                log.exception("audio analysis crashed for %s", audio_path)
                await steps.add("Analysis: crashed; see the service log")
                return None
        for line in describe(features):
            await steps.add(f"Analysis: {line}")
        return features

    async def _ask_llm(
        self, info: dict, findings: Findings, unavailable: list[str], steps: _Steps
    ) -> LlmVerdict | None:
        asked = ambiguous_fields(findings)
        if not self._llm.enabled:
            await steps.add("LLM: skipped (LLM_MODEL not set)")
            return None
        if not asked:
            await steps.add("LLM: skipped (the other sources leave nothing ambiguous)")
            return None
        async with steps.working(f"Asking the LLM about {', '.join(asked)}"):
            try:
                verdict = await self._llm.resolve(info, findings, asked)
            except LookupUnavailable as error:
                unavailable.append(str(error))
                await steps.add("LLM: unavailable")
                return None
        answers = " · ".join(f"{name} {value}" for name, value in verdict.answers.items())
        await steps.add(
            f"LLM ({verdict.model}, asked {', '.join(asked)}): {answers or 'nothing in the information answers it'}"
            f" · confidence {verdict.confidence:.0%}" + (f" · {verdict.reasoning}" if verdict.reasoning else "")
        )
        return verdict

    @staticmethod
    def _rank(matches: list[TrackMetadata], duration: float | None) -> list[TrackMetadata]:
        def rank(match: TrackMetadata) -> tuple[int, int]:
            # Closest in length first (to the second, so 1.0s vs 1.04s is a tie), then the earliest release.
            length = match.musicbrainz.length_seconds if match.musicbrainz else None
            gap = round(abs(length - duration)) if length and duration else 0
            return gap, match.year or 9999

        return sorted(_distinct(matches), key=rank)

    async def _identify_by_audio(
        self, audio_path: Path, draft: TrackMetadata, unavailable: list[str], steps: _Steps
    ) -> list[TrackMetadata]:
        reason = self._acoustid.disabled_reason
        if reason:
            await steps.add(f"AcoustID: skipped ({reason})")
            return []
        async with steps.working("Fingerprinting the audio (AcoustID)"):
            try:
                match = await self._acoustid.identify(audio_path)
            except LookupUnavailable as error:
                unavailable.append(str(error))
                await steps.add(f"AcoustID: {error} unavailable")
                return []
        if match is None:
            await steps.add("AcoustID: no confident match")
            return []

        identified: list[TrackMetadata] = []
        async with steps.working(f"Fetching {len(match.recording_ids)} matching recording(s) from MusicBrainz"):
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

    async def _search_musicbrainz(
        self, draft: TrackMetadata, duration: float | None, unavailable: list[str], steps: _Steps
    ) -> list[TrackMetadata]:
        if not draft.artist or not draft.title:
            await steps.add("MusicBrainz search: skipped (no artist or title to search for)")
            return []
        async with steps.working(f"Searching MusicBrainz for {_describe(draft)}"):
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
        await steps.add(f"{source}: {len(matches)} equally good matches{suffix} for {_describe(matches[0])}: {listed}{more}")
