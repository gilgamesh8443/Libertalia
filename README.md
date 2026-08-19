# Libertalia
A decentralized media server with a web interface for sharing media libraries with friends.

## Adding Media
Media can be added in the `./media` folder. Every piece of media needs its own folder and a `data.json` file. By including `{ "tmdb_id": int, "media_type": "movie" | "tv" }` in the json, data will automatically be gathered for a movie/series.

> [!TIP]
> Many common video and audio codecs are not supported on certain browsers (hevc, eac3, etc.), using [FFmpeg](https://www.ffmpeg.org/) you can convert videos to codecs more commonly compatible with browsers (av1, h264, aac, etc.).

> [!NOTE]
> We cannot condone piracy; Libertalia is media server code capable of hosting non-pirated content. Ripping videos from discs for personal backup is a legally gray area that may or may not be legal depending on your country of residence.
