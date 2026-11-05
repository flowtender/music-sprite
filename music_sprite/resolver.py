"""Asks every source about one downloaded track and keeps a live log of what each one found."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from . import describe, heuristics
from .acoustid import AcoustIdClient
from .analysis import AnalysisError, Analyzer
from .llm import LlmClient
from .models import AudioFeatures, Findings, LlmVerdict, LookupUnavailable, TrackMetadata
from .musicbrainz import MusicBrainzClient
from .reconcile import ambiguous_fields

log = logging.getLogger(__name__)

MAX_LOGGED_RELEASES = 3
ORIGIN = "acoustid+musicbrainz"

# Called with the finished steps so far and what is running right now.
ProgressCallback = Callable[[list[str], list[str]], Awaitable[None]]


@dataclass
class Resolution:
    findings: Findings
    unavailable_services: list[str]
    steps: list[str] = field(default_factory=list)


class _Steps:
    """The lookup log: finished steps plus those running now, reported live so the user can follow along. It also
    collects the services that could not be reached, which make the lookup worth repeating later."""

    def __init__(self, notify: ProgressCallback | None):
        self.lines: list[str] = []
        self.unreachable: list[str] = []
        self._running: list[str] = []
        self._notify = notify

    async def add(self, text: str) -> None:
        self.lines.append(text)
        await self._push()

    async def unavailable(self, error: LookupUnavailable, text: str | None = None) -> None:
        if str(error) not in self.unreachable:
            self.unreachable.append(str(error))
        if text:
            await self.add(text)

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


def _rank(matches: list[TrackMetadata], duration: float | None) -> list[TrackMetadata]:
    def rank(match: TrackMetadata) -> tuple[int, int]:
        # Closest in length first (to the second, so 1.0s vs 1.04s is a tie), then the earliest release.
        length = match.length_seconds
        gap = round(abs(length - duration)) if length and duration else 0
        return gap, match.year or 9999

    return sorted(_distinct(matches), key=rank)


def _release_label(match: TrackMetadata) -> str:
    return describe.release_name(match) + (f" ({match.year})" if match.year else "")


class MetadataResolver:
    def __init__(
        self,
        known_publishers: frozenset[str],
        acoustid: AcoustIdClient,
        musicbrainz: MusicBrainzClient,
        llm: LlmClient,
        analyzer: Analyzer,
    ):
        self._known_publishers = known_publishers
        self._acoustid = acoustid
        self._musicbrainz = musicbrainz
        self._llm = llm
        self._analyzer = analyzer

    async def resolve(self, info: dict, audio_path: Path, progress: ProgressCallback | None = None) -> Resolution:
        """Runs every source on the track. Identification and analysis do not depend on each other, so they run side
        by side; the LLM comes last because it reads what the others found."""
        steps = _Steps(progress)
        draft = heuristics.guess(info, self._known_publishers)
        await steps.add(
            f"Upload ({draft.origin}): {describe.track_detail(draft)} · confidence {draft.confidence:.0%}"
            + (" · known publisher" if draft.from_known_publisher else "")
        )

        async with asyncio.TaskGroup() as group:
            identified = group.create_task(self._identify(audio_path, draft, steps))
            analysis = group.create_task(self._analyze(audio_path, steps))

        findings = Findings(
            upload=draft, releases=_rank(identified.result(), info.get("duration")), audio=analysis.result()
        )
        findings.llm = await self._ask_llm(info, findings, steps)
        return Resolution(findings, steps.unreachable, steps.lines)

    async def _identify(self, audio_path: Path, draft: TrackMetadata, steps: _Steps) -> list[TrackMetadata]:
        """The recording by its fingerprint, then everything MusicBrainz knows about it."""
        reason = self._acoustid.disabled_reason
        if reason:
            await steps.add(f"AcoustID and MusicBrainz: skipped ({reason})")
            return []
        async with steps.working("Fingerprinting the audio (AcoustID)"):
            try:
                match = await self._acoustid.identify(audio_path)
            except LookupUnavailable as error:
                await steps.unavailable(error, f"AcoustID: {error} unavailable")
                return []
        if match is None:
            await steps.add("AcoustID: no confident match")
            return []

        identified: list[TrackMetadata] = []
        async with steps.working(f"Fetching {len(match.recording_ids)} matching recording(s) from MusicBrainz"):
            for recording_id in match.recording_ids:
                try:
                    identified.append(await self._musicbrainz.fetch_recording(recording_id, draft, match.score, ORIGIN))
                except LookupUnavailable as error:
                    await steps.unavailable(error)
                    break
        if not identified:
            await steps.add("AcoustID: matched a recording but MusicBrainz is unavailable")
            return []
        await self._note_matches(steps, _distinct(identified), match.score)
        return identified

    @staticmethod
    async def _note_matches(steps: _Steps, matches: list[TrackMetadata], score: float) -> None:
        if len(matches) == 1:
            await steps.add(f"AcoustID: matched {describe.track_detail(matches[0])} ({score:.0%})")
            for label, text in (
                ("release", describe.release_facts(matches[0])),
                ("credits", describe.credit_summary(matches[0])),
            ):
                if text:
                    await steps.add(f"MusicBrainz {label}: {text}")
            return
        listed = " / ".join(_release_label(match) for match in matches[:MAX_LOGGED_RELEASES])
        hidden = len(matches) - MAX_LOGGED_RELEASES
        more = f" / +{hidden} more" if hidden > 0 else ""
        await steps.add(
            f"AcoustID: {len(matches)} equally good matches ({score:.0%}) for "
            f"{describe.artist_title(matches[0])}: {listed}{more}"
        )

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
        for line in describe.audio_lines(features):
            await steps.add(f"Analysis: {line}")
        return features

    async def _ask_llm(self, info: dict, findings: Findings, steps: _Steps) -> LlmVerdict | None:
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
                await steps.unavailable(error, "LLM: unavailable")
                return None
        answers = " · ".join(f"{name} {value}" for name, value in verdict.answers.items())
        await steps.add(
            f"LLM ({verdict.model}, asked {', '.join(asked)}): {answers or 'nothing in the information answers it'}"
            f" · confidence {verdict.confidence:.0%}" + (f" · {verdict.reasoning}" if verdict.reasoning else "")
        )
        return verdict
