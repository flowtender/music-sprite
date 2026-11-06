# Why

I wanted a tool like this but haven't had time to make it myself, so I thought it would be a good candidate to experiment with "vibe coding" - giving specs to an agent (in this case omp+sonnet 5.5) and seeing what it comes up with.

# What

It's (in theory, supposed to be) a discord bot that I can use to add music to my collection by:
- downloading via yt-dlp; an album or mix uploaded as one video can be split into its songs along its chapters (the timestamps in its description) with the `--split-chapters` option, each song then queued and tagged on its own
- tagging from every source at once: the upload's own metadata, AcoustID/MusicBrainz (release, track and disc numbers, label, ISRC, credits, genres, ...), Essentia audio analysis, and an LLM that reads all of that and answers only the fields the others leave ambiguous
- a review card that lists every field with the value it will be tagged with, followed by the emoji of each source that gave it (MusicBrainz, Essentia, the LLM, and the upload's site: YouTube, SoundCloud, Bandcamp, ...) and then any other value a source gave. Where sources disagree you pick one with a button. The default is MusicBrainz, then the LLM, then the upload's own tags; a MusicBrainz release is chosen as a whole (from a drop-down when several fit equally well) and genres from all sources are combined. When the LLM was asked, the card shows all of the upload description it read so a prompt injection can't hide there. Anything can also be edited by hand. The card spans several messages when it needs to
- analyzing the audio itself (Essentia, ffmpeg) for tempo, key, genre guesses, moods, instruments, vocals, danceability and ReplayGain loudness, which go on the review card with the other tags
- putting it in my navidrome server, where it will sync with my music players (this part is already set up from before)

# License

Most of this is probably not copyrightable anyway because it's LLM-generated, but just in case:

if you want to use, copy, or fork it, you may do so under the MIT license.
