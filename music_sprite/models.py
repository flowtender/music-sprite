from dataclasses import asdict, dataclass, field

REPLAYGAIN_REFERENCE_LUFS = -18.0
R128_REFERENCE_LUFS = -23.0


class LookupUnavailable(Exception):
    """A metadata service could not be reached; the message names the service."""


@dataclass
class MusicBrainzIds:
    recording_id: str
    release_id: str | None = None
    artist_ids: list[str] = field(default_factory=list)
    length_seconds: float | None = None
    release_group_id: str | None = None
    release_track_id: str | None = None
    album_artist_ids: list[str] = field(default_factory=list)
    work_ids: list[str] = field(default_factory=list)


@dataclass
class ReleaseInfo:
    """What MusicBrainz knows about the recording and the release it was picked from."""

    artist_sort: str | None = None
    album_artist_sort: str | None = None
    release_date: str | None = None  # "YYYY", "YYYY-MM" or "YYYY-MM-DD"
    original_date: str | None = None
    track_number: int | None = None
    track_total: int | None = None
    disc_number: int | None = None
    disc_total: int | None = None
    media_format: str | None = None
    label: str | None = None
    catalog_number: str | None = None
    barcode: str | None = None
    country: str | None = None
    status: str | None = None
    types: list[str] = field(default_factory=list)  # lower case, e.g. ["album", "live"]
    language: str | None = None  # ISO 639-3
    script: str | None = None
    isrcs: list[str] = field(default_factory=list)
    iswcs: list[str] = field(default_factory=list)
    # Role -> names: composer, lyricist, writer, arranger, conductor, producer, engineer, mixer, remixer, and
    # performer, whose entries read "Name (instrument)".
    credits: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class AudioFeatures:
    """What analysing the audio itself says."""

    bpm: int
    key: str
    # Best Discogs style guesses first, as (style, probability).
    genres: list[tuple[str, float]] = field(default_factory=list)
    moods: list[str] = field(default_factory=list)
    instruments: list[str] = field(default_factory=list)
    vocals: str | None = None  # "instrumental", "female" or "male"
    # 0-1 scores by tag name: danceability, mood_happy, approachability, ...
    scores: dict[str, float] = field(default_factory=dict)
    loudness: float | None = None  # integrated, LUFS
    loudness_range: float | None = None  # LU
    peak: float | None = None  # true peak, linear

    @property
    def replaygain(self) -> float | None:
        """Gain in dB that brings the track to the ReplayGain 2.0 reference loudness."""
        return None if self.loudness is None else REPLAYGAIN_REFERENCE_LUFS - self.loudness

    @property
    def r128_gain(self) -> int | None:
        """Opus R128_TRACK_GAIN: Q7.8 fixed-point dB relative to -23 LUFS."""
        if self.loudness is None:
            return None
        return max(-32768, min(32767, round((R128_REFERENCE_LUFS - self.loudness) * 256)))


@dataclass
class TrackMetadata:
    title: str
    artist: str
    album: str | None = None
    album_artist: str | None = None
    year: int | None = None
    genres: list[str] = field(default_factory=list)
    confidence: float = 0.0
    origin: str = "heuristic"
    musicbrainz: MusicBrainzIds | None = None
    from_known_publisher: bool = False
    release: ReleaseInfo = field(default_factory=ReleaseInfo)
    audio: AudioFeatures | None = None

    @property
    def genre(self) -> str | None:
        """The main genre, used for display and the library path."""
        return self.genres[0] if self.genres else None

    @property
    def effective_album(self) -> str:
        return self.album or self.title

    @property
    def effective_album_artist(self) -> str:
        return self.album_artist or self.artist

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "TrackMetadata":
        data = dict(data)
        # Queue entries saved by earlier versions: a single genre and flat bpm/key.
        legacy_genre = data.pop("genre", None)
        if legacy_genre and not data.get("genres"):
            data["genres"] = [legacy_genre]
        bpm, key = data.pop("bpm", None), data.pop("key", None)
        if bpm and key and not data.get("audio"):
            data["audio"] = {"bpm": bpm, "key": key}
        musicbrainz = data.pop("musicbrainz", None)
        release = data.pop("release", None) or {}
        audio = data.pop("audio", None)
        return cls(
            **data,
            musicbrainz=MusicBrainzIds(**musicbrainz) if musicbrainz else None,
            release=ReleaseInfo(**release),
            audio=AudioFeatures(**audio) if audio else None,
        )
