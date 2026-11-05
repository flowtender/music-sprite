import asyncio
from unittest.mock import MagicMock

import discord

from music_sprite import emojis


class FakeEmoji:
    def __init__(self, name, emoji_id):
        self.name, self.id = name, emoji_id

    def __str__(self):
        return f"<:{self.name}:{self.id}>"


class FakeClient:
    def __init__(self, existing, refused=()):
        self.existing, self.refused, self.uploaded = existing, set(refused), []

    async def fetch_application_emojis(self):
        return self.existing

    async def create_application_emoji(self, *, name, image):
        if name in self.refused:
            raise discord.HTTPException(MagicMock(status=400, reason="x"), "image too big")
        self.uploaded.append(name)
        return FakeEmoji(name, len(self.uploaded))


def test_only_missing_images_are_uploaded_and_a_refused_one_falls_back_to_a_plain_emoji():
    assets = {path.stem for path in emojis.ASSETS.glob("*.png")}
    assert {"musicbrainz", "llm", "youtube"} <= assets
    client = FakeClient([FakeEmoji("musicbrainz", 99)], refused={"llm"})

    synced = asyncio.run(emojis.sync(client))

    assert sorted(client.uploaded) == sorted(assets - {"musicbrainz", "llm"})
    marks = synced.sources({"extractor_key": "Youtube"})
    assert marks["musicbrainz"] == "<:musicbrainz:99>"
    assert marks["llm"] == emojis.PLAIN["llm"]
    assert marks["upload"] == str(synced.markup["youtube"])
    assert synced.sources({"extractor_key": "Vimeo"})["upload"] == emojis.PLAIN["upload"]
