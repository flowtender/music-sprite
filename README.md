# Why

I wanted a tool like this but haven't had time to make it myself, so I thought it would be a good candidate to experiment with "vibe coding" - giving specs to an agent (in this case omp+sonnet 5.5) and seeing what it comes up with.

# What

It's (in theory, supposed to be) a discord bot that I can use to add music to my collection by:
- downloading via yt-dlp
- tagging from every source at once: the upload's own metadata, AcoustID/MusicBrainz (release, track and disc numbers, label, ISRC, credits, genres, ...), Essentia audio analysis, and an LLM that reads all of that and answers only the fields the others leave ambiguous
- a review card that lists every field with the value each source gave. Where sources disagree you pick one from a drop-down. The default is MusicBrainz, then the LLM, then the upload's own tags; a MusicBrainz release is chosen as a whole and genres from all sources are combined. Anything can also be edited by hand. The card spans several messages when it needs to
- analyzing the audio itself (Essentia, ffmpeg) for tempo, key, genre guesses, moods, instruments, vocals, danceability and ReplayGain loudness, which go on the review card with the other tags
- putting it in my navidrome server, where it will sync with my music players (this part is already set up from before)

# License

Most of this is probably not copyrightable anyway because it's LLM-generated, but just in case:

if you want to use, copy, or fork it, you may do so under the MIT license.
