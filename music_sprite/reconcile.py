"""Turns what the independent sources found into one TrackMetadata, and says where they disagree.

Sources rank Essentia > MusicBrainz > LLM > upload (models.SOURCES). Most fields only have a few sources: Essentia
knows nothing about titles, and only MusicBrainz knows about releases. Fields are settled like this:

- title and artist: one value per source; the strongest source with a value wins unless the user picked another.
- release (album, album artist, year, track and disc position, label, ids, credits, ...): bundled, because they
  describe one MusicBrainz release and mixing releases would give inconsistent tags. The user picks a whole release.
  Without a MusicBrainz match, album, album artist and year are settled per field like title and artist.
- genres: never a conflict; every source's genres are combined, MusicBrainz first.
- audio features: Essentia only.
"""

import re
from dataclasses import dataclass, replace

from .analysis import guessed_genres
from .models import Choices, Findings, TrackMetadata

# A guess at artist and title from the upload alone is trusted without asking the LLM at this confidence.
TRUSTED_SOURCE_CONFIDENCE = 0.9

CHOOSABLE_FIELDS = ("title", "artist")
RELEASE_FIELDS = ("album", "album_artist", "year")
FIELDS = (*CHOOSABLE_FIELDS, *RELEASE_FIELDS)
RELEASE = "release"


@dataclass
class Candidate:
    value: str | int
    sources: list[str]  # every source that reported this value, strongest first


@dataclass
class FieldReport:
    name: str
    candidates: list[Candidate]  # distinct values, strongest source first
    chosen: int | None  # index into candidates; None when nothing was found or a hand-typed value overrides
    # The user can pick between the candidates; false when there is only one, or when the release decides the field.
    selectable: bool
    manual: bool = False

    @property
    def conflicted(self) -> bool:
        return self.selectable and len(self.candidates) > 1

    @property
    def chosen_candidate(self) -> Candidate | None:
        return None if self.chosen is None else self.candidates[self.chosen]


@dataclass
class Reconciled:
    metadata: TrackMetadata
    fields: dict[str, FieldReport]
    releases: list[TrackMetadata]
    release_index: int | None

    @property
    def release_conflicted(self) -> bool:
        return len(self.releases) > 1

    @property
    def conflicts(self) -> list[str]:
        """The fields the user may want to look at, release first."""
        names = [RELEASE] if self.release_conflicted else []
        return names + [name for name, report in self.fields.items() if report.conflicted]


def _fold(value: object) -> str:
    """Two spellings that differ only in case, spacing or punctuation are the same value."""
    text = str(value)
    return re.sub(r"[\W_]+", "", text.casefold()) or text


def _group(pairs: list[tuple[str, object]]) -> list[Candidate]:
    groups: dict[str, Candidate] = {}
    for source, value in pairs:
        if value in (None, ""):
            continue
        candidate = groups.setdefault(_fold(value), Candidate(value, []))
        candidate.sources.append(source)
    return list(groups.values())


def release_index(findings: Findings, choices: Choices) -> int | None:
    """The user's pick, else the LLM's (it only answers when releases tie), else the best-ranked match."""
    count = len(findings.releases)
    if not count:
        return None
    llm_pick = findings.llm.answers.get(RELEASE) if findings.llm else None
    for pick in (choices.release, llm_pick):
        if isinstance(pick, int) and not isinstance(pick, bool) and 0 <= pick < count:
            return pick
    return 0


def _pairs(name: str, selected: TrackMetadata | None, findings: Findings) -> list[tuple[str, object]]:
    """One value per source, in precedence order (models.SOURCES; Essentia has none of these fields)."""
    answers = findings.llm.answers if findings.llm else {}
    return [
        ("musicbrainz", getattr(selected, name) if selected else None),
        ("llm", answers.get(name)),
        ("upload", getattr(findings.upload, name)),
    ]


def _report(name: str, pairs: list[tuple[str, object]], choices: Choices, *, selectable: bool) -> FieldReport:
    candidates = _group(pairs)
    chosen = 0 if candidates else None
    wanted = choices.fields.get(name)
    if wanted:
        # A pick that no longer applies (say, the MusicBrainz title after switching to the upload's release) is ignored.
        chosen = next((index for index, candidate in enumerate(candidates) if wanted in candidate.sources), chosen)
    return FieldReport(name, candidates, chosen, selectable and len(candidates) > 1, name in choices.manual)


def _genres(selected: TrackMetadata | None, findings: Findings) -> list[str]:
    combined: dict[str, str] = {}
    sources = [
        selected.genres if selected else [],
        guessed_genres(findings.audio) if findings.audio else [],
        findings.upload.genres,
    ]
    for genres in sources:
        for genre in genres:
            combined.setdefault(_fold(genre), genre)
    return list(combined.values())


def _candidate_confidence(candidate: Candidate, selected: TrackMetadata | None, findings: Findings) -> float:
    confidences = {
        "musicbrainz": selected.confidence if selected else 0.0,
        "llm": findings.llm.confidence if findings.llm else 0.0,
        "upload": findings.upload.confidence,
    }
    return max(confidences.get(source, 0.0) for source in candidate.sources)


def reconcile(findings: Findings, choices: Choices) -> Reconciled:
    index = release_index(findings, choices)
    selected = findings.releases[index] if index is not None else None

    reports = {
        name: _report(name, _pairs(name, selected, findings), choices, selectable=name in CHOOSABLE_FIELDS or selected is None)
        for name in FIELDS
    }

    def value(name: str):
        if selected is not None and name in RELEASE_FIELDS:
            return getattr(selected, name)
        candidate = reports[name].chosen_candidate
        return candidate.value if candidate else None

    # With a release selected the album, album artist and year it carries are shown for comparison but not choosable.
    base = selected or findings.upload
    title, artist = value("title") or "", value("artist") or ""

    confidence = min(
        (
            _candidate_confidence(candidate, selected, findings)
            for name in CHOOSABLE_FIELDS
            if (candidate := reports[name].chosen_candidate)
        ),
        default=0.0,
    )
    first_sources = {
        name: candidate.sources[0] for name in CHOOSABLE_FIELDS if (candidate := reports[name].chosen_candidate)
    }
    if selected is not None and "musicbrainz" in first_sources.values():
        origin = selected.origin
    elif "llm" in first_sources.values() and findings.llm:
        origin = f"llm:{findings.llm.model}"
    else:
        origin = findings.upload.origin

    metadata = replace(
        base,
        title=title,
        artist=artist,
        album=value("album"),
        album_artist=value("album_artist"),
        year=value("year") if isinstance(value("year"), int) else None,
        genres=_genres(selected, findings),
        confidence=confidence,
        origin=origin,
        from_known_publisher=findings.upload.from_known_publisher,
        audio=findings.audio,
    )
    if choices.manual:
        metadata = replace(metadata, origin=f"{origin}+edited", **choices.manual)
    return Reconciled(metadata, reports, findings.releases, index)


def ambiguous_fields(findings: Findings) -> list[str]:
    """The fields the sources cannot settle among themselves, for the LLM to answer. Its own answers are ignored."""
    reconciled = reconcile(replace(findings, llm=None), Choices())
    asked = []
    for name in CHOOSABLE_FIELDS:
        candidates = reconciled.fields[name].candidates
        only_upload = {source for candidate in candidates for source in candidate.sources} == {"upload"}
        # A single value that only the upload's own tags back is a guess unless the site stated artist and title.
        if len(candidates) != 1 or (only_upload and findings.upload.confidence < TRUSTED_SOURCE_CONFIDENCE):
            asked.append(name)
    if reconciled.release_conflicted:
        asked.append(RELEASE)
    if not findings.releases:
        for name in ("album", "year"):
            if len(reconciled.fields[name].candidates) != 1:
                asked.append(name)
        if len(reconciled.fields["album_artist"].candidates) > 1:
            asked.append("album_artist")
    return asked
