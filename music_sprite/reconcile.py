"""Turns what the independent sources found into one TrackMetadata, and says where they disagree.

Sources rank Essentia > MusicBrainz > LLM > upload (models.SOURCES). Most fields only have a few sources: Essentia
knows nothing about titles, and only MusicBrainz knows about releases. Fields are settled like this:

- every field in FIELDS (title, artist, album, year, track number, label, composer, ...): one value per source; the
  strongest source with a value wins unless the user picked another. Wherever the sources disagree the user can pick.
- the MusicBrainz release: its values for those fields, and everything only it knows (release date, ids, ISRCs, the
  other credits, ...), come from one release, because mixing releases would give inconsistent tags. The user picks a
  whole release, which resets the picks for its fields (RELEASE_FIELDS); a single field can still be taken from
  another source afterwards. A year that differs from the release's drops its release date, which is tagged first.
- genres: never a conflict; every source's genres are combined, MusicBrainz first.
- audio features: Essentia only.
"""

import re
from dataclasses import dataclass, replace

from .models import Choices, Findings, TrackMetadata

# Fields that identify the recording; they stay with the track when the user switches to another release.
TRACK_FIELDS = ("title", "artist")
# Where TrackMetadata keeps the release's fields: on itself, on its ReleaseInfo, or as names in ReleaseInfo.credits.
ALBUM_FIELDS = ("album", "album_artist", "year")
RELEASE_INFO_FIELDS = ("track_number", "track_total", "label", "catalog_number")
CREDIT_FIELDS = ("composer", "lyricist", "producer")
RELEASE_FIELDS = (*ALBUM_FIELDS, *RELEASE_INFO_FIELDS, *CREDIT_FIELDS)
FIELDS = (*TRACK_FIELDS, *RELEASE_FIELDS)
NUMBER_FIELDS = ("year", "track_number", "track_total")
# What the LLM answers besides FIELDS: which MusicBrainz release fits best, and genres for the combined list.
RELEASE = "release"
GENRES = "genres"


@dataclass
class Candidate:
    value: str | int | list[str]
    sources: list[str]  # every source that reported this value, strongest first


@dataclass
class FieldReport:
    name: str
    candidates: list[Candidate]  # distinct values, strongest source first
    chosen: int | None  # index into candidates; None when nothing was found
    manual: bool = False  # a hand-typed value overrides the candidates

    @property
    def conflicted(self) -> bool:
        return len(self.candidates) > 1

    @property
    def chosen_candidate(self) -> Candidate | None:
        return None if self.chosen is None else self.candidates[self.chosen]


@dataclass
class Reconciled:
    metadata: TrackMetadata
    fields: dict[str, FieldReport]
    releases: list[TrackMetadata]
    release_index: int | None
    genres: list[Candidate]  # every source's genres combined, strongest source first, each with its sources

    @property
    def release_conflicted(self) -> bool:
        return len(self.releases) > 1


def field_value(meta: TrackMetadata, name: str) -> str | int | list[str] | None:
    if name in CREDIT_FIELDS:
        return meta.release.credits.get(name) or None
    if name in RELEASE_INFO_FIELDS:
        return getattr(meta.release, name)
    return getattr(meta, name)


def _found(value: object) -> bool:
    return value not in (None, "", [])


def _fold(value: object) -> str:
    """Two spellings that differ only in case, spacing or punctuation are the same value."""
    text = ", ".join(value) if isinstance(value, list) else str(value)
    return re.sub(r"[\W_]+", "", text.casefold()) or text


def _group(pairs: list[tuple[str, object]]) -> list[Candidate]:
    groups: dict[str, Candidate] = {}
    for source, value in pairs:
        if not _found(value):
            continue
        candidate = groups.setdefault(_fold(value), Candidate(value, []))
        candidate.sources.append(source)
    return list(groups.values())


def valid_index(value: object, count: int) -> bool:
    """Whether a pick (the user's, the LLM's, or one read back from Discord) points at one of `count` releases."""
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value < count


def release_index(findings: Findings, choices: Choices) -> int | None:
    """The user's pick, else the LLM's, else the best-ranked match."""
    count = len(findings.releases)
    if not count:
        return None
    llm_pick = findings.llm.answers.get(RELEASE) if findings.llm else None
    return next((pick for pick in (choices.release, llm_pick) if valid_index(pick, count)), 0)


def _pairs(name: str, selected: TrackMetadata | None, findings: Findings) -> list[tuple[str, object]]:
    """One value per source, in precedence order (models.SOURCES; Essentia has none of these fields)."""
    answers = findings.llm.answers if findings.llm else {}
    return [
        ("musicbrainz", field_value(selected, name) if selected else None),
        ("llm", answers.get(name)),
        ("upload", field_value(findings.upload, name)),
    ]


def _report(name: str, pairs: list[tuple[str, object]], choices: Choices) -> FieldReport:
    candidates = _group(pairs)
    chosen = 0 if candidates else None
    wanted = choices.fields.get(name)
    if wanted:
        # A pick that no longer applies (say, the MusicBrainz title after switching to the upload's release) is ignored.
        chosen = next((index for index, candidate in enumerate(candidates) if wanted in candidate.sources), chosen)
    return FieldReport(name, candidates, chosen, name in choices.manual)


def _genres(selected: TrackMetadata | None, findings: Findings) -> list[Candidate]:
    answers = findings.llm.answers if findings.llm else {}
    return _group(
        [
            *(("musicbrainz", genre) for genre in (selected.genres if selected else [])),
            *(("essentia", genre) for genre in (findings.audio.confident_genres if findings.audio else [])),
            *(("llm", genre) for genre in answers.get(GENRES, [])),
            *(("upload", genre) for genre in findings.upload.genres),
        ]
    )


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
    reports = {name: _report(name, _pairs(name, selected, findings), choices) for name in FIELDS}

    def value(name: str):
        candidate = reports[name].chosen_candidate
        return candidate.value if candidate else None

    # The release's details that no other source knows come along with it.
    base = selected or findings.upload
    credits = {role: names for role, names in base.release.credits.items() if role not in CREDIT_FIELDS}
    for role in CREDIT_FIELDS:
        if names := value(role):
            credits[role] = names
    release = replace(base.release, credits=credits, **{name: value(name) for name in RELEASE_INFO_FIELDS})

    confidence = min(
        (
            _candidate_confidence(candidate, selected, findings)
            for name in TRACK_FIELDS
            if (candidate := reports[name].chosen_candidate)
        ),
        default=0.0,
    )
    first_sources = {name: candidate.sources[0] for name in TRACK_FIELDS if (candidate := reports[name].chosen_candidate)}
    if selected is not None and "musicbrainz" in first_sources.values():
        origin = selected.origin
    elif "llm" in first_sources.values() and findings.llm:
        origin = f"llm:{findings.llm.model}"
    else:
        origin = findings.upload.origin

    genres = _genres(selected, findings)
    metadata = replace(
        base,
        title=value("title") or "",
        artist=value("artist") or "",
        album=value("album"),
        album_artist=value("album_artist"),
        year=value("year"),
        release=release,
        genres=[candidate.value for candidate in genres],
        confidence=confidence,
        origin=origin,
        from_known_publisher=findings.upload.from_known_publisher,
        audio=findings.audio,
    )
    if choices.manual:
        metadata = replace(metadata, origin=f"{origin}+edited", **choices.manual)
    if selected is not None and metadata.year != selected.year:
        # The date tag is the release date if there is one, so a year from another source would never be tagged.
        metadata = replace(metadata, release=replace(metadata.release, release_date=None))
    return Reconciled(metadata, reports, findings.releases, index, genres)
