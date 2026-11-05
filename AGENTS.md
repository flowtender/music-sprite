# music-sprite

Discord bot: `/<name> download <url>` pulls audio with yt-dlp, tags it from several sources, shows a review card in Discord, then moves the tagged file into a Navidrome library. Python ≥3.11, discord.py, httpx, mutagen. See `README.md` for the user-facing pitch, `INSTALL.md` for deployment.

## Commands

```bash
python -m venv .venv && .venv/bin/pip install -e '.[test]'   # the full `analysis` extra is ~300 MB and not needed for tests
.venv/bin/python -m pytest -q                                      # baseline: 57 pass (the ffmpeg-dependent test skips without ffmpeg)
```

- No linter or formatter is configured. Match the surrounding style; don't reformat code you aren't changing.
- Running the bot needs a real Discord token plus an env file (`music-sprite.env.example`). Don't try; verify through tests and small scripts.
- Production is a systemd service under `/opt/music-sprite`. `restart.sh` and `INSTALL.md` use `sudo`. NEVER run them from an agent session.

## Pipeline

```
bot.py  download (downloader.py, yt-dlp) → staging.adopt → QueueEntry on disk
        → resolver.resolve: three steps run concurrently
             upload tags (heuristics.py) | AcoustID+MusicBrainz (acoustid.py, musicbrainz.py) | Essentia (analysis.py)
           then llm.py asks about ONLY the fields those leave ambiguous
        → Findings (per-source results) stored on the entry
        → reconcile.py: Findings + Choices (user picks/hand edits) → TrackMetadata + per-field conflict report
        → review.py / panels.py render it as Discord components
        → import_entry → library.add_to_library: layout.py picks the path, tagger.py writes tags, Navidrome rescan
```

| Module | Role |
|---|---|
| `models.py` | Dataclasses shared everywhere: `TrackMetadata`, `Findings`, `Choices`, `AudioFeatures`, `SOURCES` |
| `reconcile.py` | Pure logic that turns findings into one result and reports disagreements. Its docstring is the spec; read it before changing precedence |
| `staging.py` | `QueueEntry` (one JSON file per entry in `queue_dir/<id>/entry.json`) and `Staging` |
| `review.py`, `panels.py` | Discord UI: review card and queue list. Both are 100% `DynamicItem`s |
| `bot.py` | Slash commands, download→resolve→import orchestration, review message lifecycle |
| `config.py` | `Config.from_env()`; every setting is an env var |

## Invariants that are easy to break

- **UI survives restarts.** Every interactive item is a `DynamicItem` whose `custom_id` (`ms:r:…` review, `ms:q:…` queue) carries its state, and everything else is read from the `QueueEntry` on disk. Changing a `custom_id` template or its regex breaks every message already posted in Discord. Don't keep state in memory.
- **On-disk compatibility.** `QueueEntry.from_json` reads entries written by older versions (see `legacy_key` handling). New fields need defaults; `Staging.save` writes atomically via a temp file.
- **Source ranking** is Essentia > MusicBrainz > LLM > upload (`models.SOURCES`). A MusicBrainz release is chosen as a whole, never field by field. Genres are merged, not conflicted. The LLM is only asked about ambiguous fields.
- **Analysis runs in a spawned single-worker `ProcessPoolExecutor`** (`analysis.py`), so TensorFlow can't block the event loop or fork a threaded process. Don't switch to fork or threads. The `analysis` extra is optional, so everything must degrade to "Analysis: skipped".
- **Discord limits** shape `review.py`: 40 components (30 interactive) and 4000 characters per message, so a card is a list of pages. Check the limits when adding fields.
- **External services fail.** MusicBrainz, AcoustID and the LLM raise `LookupUnavailable`. The entry becomes `partial` and can be re-looked-up. Lookup failures must not crash a batch.
- **Config changes touch three places:** `config.py`, `music-sprite.env.example`, and `INSTALL.md` or `README.md` if user-visible.

## Tests

`tests/conftest.py` has the builders: `release()`, `upload()` (`TrackMetadata`), and the `staging`, `bot` (a `SimpleNamespace` fake) and `make_entry` fixtures. Use them; tests build plain objects and don't mock discord.py.

Covered: analysis worker, staging, reconcile, resolver, review card, llm. **Not covered:** `bot.py`, `panels.py`, `tagger.py`, `library.py`, `layout.py`, `downloader.py`, `heuristics.py`, `musicbrainz.py`, `acoustid.py`, `config.py`. Before refactoring one of these, write characterization tests for it first.

## Conventions

- Type-hinted, dataclass-heavy, small functions; `asyncio.to_thread` for blocking work.
- Comments explain *why* (constraints, past failures), not what. Most modules have no docstring; add a one-line module docstring when you touch one.
- Delete dead code instead of keeping shims. The repo is a single-user deployment with no external API consumers, so clean cutovers beat compatibility layers, except for on-disk queue entries and Discord `custom_id`s (above).
