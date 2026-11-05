"""The emojis that mark where each value on the review card came from, uploaded from assets/ as application emojis."""

import logging
from dataclasses import dataclass, field
from pathlib import Path

import discord

log = logging.getLogger(__name__)

ASSETS = Path(__file__).with_name("assets")
# Stand-ins, by source (models.SOURCES), for the emojis the application doesn't have.
PLAIN = {"essentia": "🎚️", "musicbrainz": "💿", "llm": "✨", "upload": "🌐"}


@dataclass(frozen=True)
class Emojis:
    """The application's emojis by name, as message markup ("<:name:id>")."""

    markup: dict[str, str] = field(default_factory=dict)

    def sources(self, info: dict) -> dict[str, str]:
        """The emoji of every source for one track. The upload's is that of its site, named after the yt-dlp
        extractor (assets/youtube.png for "Youtube"); sites without one get the "upload" emoji."""
        marks = {source: self.markup.get(source, plain) for source, plain in PLAIN.items()}
        site = str(info.get("extractor_key") or "").lower()
        if site in self.markup:
            marks["upload"] = self.markup[site]
        return marks


async def sync(client: discord.Client) -> Emojis:
    """Uploads every image in assets/ that the application has no emoji of that name for. Emojis are matched by
    name only: to replace an image, delete its emoji in the Discord Developer Portal and restart."""
    uploaded = {emoji.name: emoji for emoji in await client.fetch_application_emojis()}
    for path in sorted(ASSETS.glob("*.png")):
        if path.stem in uploaded:
            continue
        try:
            uploaded[path.stem] = await client.create_application_emoji(name=path.stem, image=path.read_bytes())
        except discord.HTTPException:
            log.warning("could not upload the emoji %s", path.name, exc_info=True)
            continue
        log.info("uploaded the emoji %s", path.stem)
    return Emojis({name: str(emoji) for name, emoji in uploaded.items()})
