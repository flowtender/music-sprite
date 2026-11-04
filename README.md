# Why

I wanted a tool like this but haven't had time to make it myself, so I thought it would be a good candidate to experiment with "vibe coding" - giving specs to an agent (in this case omp+sonnet 5.5) and seeing what it comes up with.

# What

It's (in theory, supposed to be) a discord bot that I can use to add music to my collection by:
- downloading via yt-dlp
- tagging based on metadata, heuristics, musicbrainz (release, track and disc numbers, label, ISRC, credits, genres, ...), LLM, and finally by hand
- analyzing the audio itself (Essentia, ffmpeg) for tempo, key, genre guesses, moods, instruments, vocals, danceability and ReplayGain loudness, which go on the review card with the other tags
- putting it in my navidrome server, where it will sync with my music players (this part is already set up from before)

# License

Most of this is probably not copyrightable anyway because it's LLM-generated, but just in case:

if you want to use, copy, or fork it, you may do so under the MIT license.
