"""Plain-text descriptions of tracks, shared by the lookup log (resolver.py) and the review card (review.py)."""

from .models import AudioFeatures, TrackMetadata

MAX_LOGGED_CREDITS = 3


def artist_title(meta: TrackMetadata) -> str:
    return f"{meta.artist or '?'} – {meta.title}"


def duration(seconds: float) -> str:
    return f"{int(seconds // 60)}:{int(seconds % 60):02d}"


def track_position(meta: TrackMetadata) -> str | None:
    """"3/10 · disc 1/2"; the disc only when the release has several."""
    release = meta.release
    if not release.track_number:
        return None
    position = f"{release.track_number}/{release.track_total}" if release.track_total else str(release.track_number)
    if release.disc_total and release.disc_total > 1:
        position += f" · disc {release.disc_number}/{release.disc_total}"
    return position


def release_name(meta: TrackMetadata) -> str:
    return meta.album or f"{meta.title} (single)"


def label(meta: TrackMetadata) -> str | None:
    """The label with the catalog number, which means nothing without it."""
    release = meta.release
    if release.label and release.catalog_number:
        return f"{release.label} {release.catalog_number}"
    return release.label


def vocals(audio: AudioFeatures) -> str | None:
    if not audio.vocals:
        return None
    return "instrumental" if audio.vocals == "instrumental" else f"{audio.vocals} vocals"


def loudness(audio: AudioFeatures) -> str | None:
    if audio.loudness is None or audio.replaygain is None:
        return None
    return f"{audio.loudness:.1f} LUFS · ReplayGain {audio.replaygain:+.1f} dB"


# --- the lookup log: one line per fact, short enough to follow while it runs ------------------------------------


def track_detail(meta: TrackMetadata) -> str:
    """Everything known about a track on one line."""
    release = meta.release
    parts = [artist_title(meta)]
    if meta.album:
        parts.append(f"album {meta.album}")
    if release.release_date or meta.year:
        parts.append(release.release_date or str(meta.year))
    if position := track_position(meta):
        parts.append(f"track {position}")
    if catalog := label(meta):
        parts.append(catalog)
    if meta.genres:
        parts.append("genre " + ", ".join(meta.genres))
    if meta.length_seconds:
        parts.append(duration(meta.length_seconds))
    return " · ".join(parts)


def release_facts(meta: TrackMetadata) -> str:
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


def credit_summary(meta: TrackMetadata) -> str:
    parts = []
    for role, names in meta.release.credits.items():
        hidden = len(names) - MAX_LOGGED_CREDITS
        parts.append(f"{role} {', '.join(names[:MAX_LOGGED_CREDITS])}" + (f" +{hidden} more" if hidden > 0 else ""))
    return " · ".join(parts)


def audio_lines(features: AudioFeatures) -> list[str]:
    basics = [f"{features.bpm} BPM", features.key, vocals(features), loudness(features)]
    lines = [" · ".join(basic for basic in basics if basic)]
    if features.genres:
        lines.append("genre guesses: " + ", ".join(f"{name} {probability:.0%}" for name, probability in features.genres))
    extras = []
    if features.moods:
        extras.append("moods " + ", ".join(features.moods))
    if features.instruments:
        extras.append("instruments " + ", ".join(features.instruments))
    for tag in ("danceability", "approachability", "engagement"):
        if tag in features.scores:
            extras.append(f"{tag} {features.scores[tag]:.0%}")
    if extras:
        lines.append(" · ".join(extras))
    return lines
