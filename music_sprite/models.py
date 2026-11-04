from dataclasses import asdict, dataclass, field


class LookupUnavailable(Exception):
    """A metadata service could not be reached; the message names the service."""


@dataclass
class MusicBrainzIds:
    recording_id: str
    release_id: str | None = None
    artist_ids: list[str] = field(default_factory=list)
    length_seconds: float | None = None


@dataclass
class TrackMetadata:
    title: str
    artist: str
    album: str | None = None
    album_artist: str | None = None
    year: int | None = None
    genre: str | None = None
    confidence: float = 0.0
    origin: str = "heuristic"
    musicbrainz: MusicBrainzIds | None = None
    from_known_publisher: bool = False

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
        musicbrainz = data.get("musicbrainz")
        return cls(**{**data, "musicbrainz": MusicBrainzIds(**musicbrainz) if musicbrainz else None})
