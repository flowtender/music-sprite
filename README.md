# Why

I wanted a tool like this but haven't had time to make it myself, so I thought it would be a good candidate to experiment with "vibe coding" - giving specs to an agent (in this case omp+sonnet 5.5) and seeing what it comes up with.

# What

It's (in theory, supposed to be) a discord bot that I can use to add music to my collection by:
- downloading via yt-dlp; an album or mix uploaded as one video can be split into its songs along its chapters (the timestamps in its description) with the `--split-chapters` option, each song then queued and tagged on its own
- tagging from every source at once: the upload's own metadata, AcoustID/MusicBrainz (release, track and disc numbers, label, ISRC, credits, genres, ...), Essentia audio analysis, and an LLM that reads all of that and gives its own answer for every field it can glean (title, artist, album, album artist, year, track number and count, label, catalog number, composer, lyricist, producer, genres, and which MusicBrainz release fits)
- a review card that lists every field with the value it will be tagged with, followed by the emoji of each source that gave it (MusicBrainz, Essentia, the LLM, and the upload's site: YouTube, SoundCloud, Bandcamp, ...) and then any other value a source gave. Wherever sources disagree, including the LLM, you pick one with a button. The default is MusicBrainz, then the LLM, then the upload's own tags; when several MusicBrainz releases fit equally well each gets a button, and genres from all sources are combined. The card shows all of the upload description the LLM read so a prompt injection can't hide there. Anything can also be edited by hand, and a track can be discarded from its card. Approving or discarding a track opens the card of the next one in the queue, looking it up first (with its progress showing) if it never was. The card spans several messages when it needs to
- a queue list with a Review button beside each track (Look up for one that was never looked up); no drop-downs anywhere
- analyzing the audio itself (Essentia, ffmpeg) for tempo, key, genre guesses, moods, instruments, vocals, danceability and ReplayGain loudness, which go on the review card with the other tags
- putting it in my navidrome server, where it will sync with my music players (this part is already set up from before)

# License

Most of this is probably not copyrightable anyway because it's LLM-generated, but just in case:

if you want to use, copy, or fork it, you may do so under the MIT license.
