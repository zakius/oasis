# oasis

An OpenSubsonic music server that merges your local library with streaming services into one library. Clients see ordinary tracks and never learn where a track comes from.

It pairs with **mirage**, a fooyin fork that shows the server's library as if it were on your machine, and **roma**, an Android client.

## Status

Early. oasis serves a local music library over the Subsonic API, so any Subsonic client works: Feishin, Symfonium, DSub, Supersonic, and others.

With `[ytmusic] enabled = true`, searches also return YouTube Music songs after the local matches. A YouTube Music result that matches a local track (same artist and title, length within 5 seconds) shows up as the local track. YouTube Music tracks join the library while they are starred or in a playlist, and download into the cache when they join. Other tracks download when first played. Uncached tracks start in about 0.2 seconds: oasis gets the audio URL with one visionOS player request, plays straight from YouTube, and fills the cache in the background. If that client stops working, oasis falls back to yt-dlp, whose URLs only work after about 5 seconds. No account is needed yet. Running from source needs `node` or `bun` on `PATH`; the container image includes node.

Planned:

- Play forwarding to Last.fm, ListenBrainz and YouTube Music history, also for local copies.
- Recommendation radios as playlists that grow while you listen.

## Run

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
cp oasis.example.toml oasis.toml   # set library paths and a password
.venv/bin/oasis -c oasis.toml       # scans in the background, serves on port 4533
```

Or in a container. Put `oasis.toml` in `./config`, set its library path to `/music`, and run:

```sh
docker compose up -d --build
```

`compose.yaml` mounts `~/Music` read-only at `/music`. The container runs as UID 1000, so `./config` must be writable by that user; set `user:` in `compose.yaml` otherwise.

Point a Subsonic client at `http://<host>:4533`. `oasis scan` runs a scan without serving. The server rescans when a client calls `startScan`.

## Supported API

Browsing (`getArtists`, `getArtist`, `getAlbum`, `getSong`, `getIndexes`, `getMusicDirectory`), album lists, random songs, genres, `search2`/`search3` (an empty query lists everything, for clients that sync the whole library), stars, ratings, scrobbles, playlists, cover art (embedded or `cover`/`folder`/`front` images), and `stream`/`download` with HTTP range requests. Responses are XML or JSON (`f=json`), over GET or form POST.

Methods for artist info, similar songs, top songs, podcasts, shares and bookmarks return empty results for now.

## Tests

```sh
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

The tests generate audio with `ffmpeg`, so it must be on `PATH`.

## License

GPL-3.0-or-later.
