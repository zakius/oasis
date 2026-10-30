# oasis

An OpenSubsonic music server that merges your local library with streaming services into one library. Clients see ordinary tracks and never learn where a track comes from.

It pairs with **mirage**, a fooyin fork that shows the server's library as if it were on your machine, and **roma**, an Android client.

## Status

Early. oasis serves a local music library over the Subsonic API, so any Subsonic client works: Feishin, Symfonium, DSub, Supersonic, and others.

With `[ytmusic] enabled = true`, searches also return YouTube Music songs after the local matches. A YouTube Music result that matches a local track (same artist and title, length within 5 seconds) shows up as the local track. YouTube Music tracks join the library while they are starred or in a playlist, and download into the cache when they join. Other tracks download when first played. Uncached tracks start in about 0.2 seconds: oasis gets the audio URL with one visionOS player request, plays straight from YouTube, and fills the cache in the background. If that client stops working, oasis falls back to yt-dlp, whose URLs only work after about 5 seconds. No account is needed yet. Running from source needs `node` or `bun` on `PATH`; the container image includes node.

## Accounts

Open `http://<host>:4533/` and sign in with a Subsonic user and password. The account page links each user's own services:

- **YouTube Music**: paste the "Copy as cURL" of a `browse` request (or its request headers, or the `Cookie` value) from a signed-in music.youtube.com tab, as the page explains step by step. oasis checks the sign-in and shows the account name. Clients can also link it themselves: `oasisLinkYouTubeMusic` takes `cookie` (the Cookie header) and optional `authUser`, which is how roma reuses its own Google sign-in. `oasisUnlinkYouTubeMusic` removes it.
- **Last.fm**: "Sign in with Last.fm" goes through Last.fm's own page. The server needs `[lastfm] api_key` and `secret`.
- **ListenBrainz**: paste the user token. oasis checks it.

Users in `oasis.toml` are admins, and their passwords are set there. Admins add and remove other users on `/admin` or with Subsonic's `createUser`, `updateUser`, `deleteUser` and `getUsers`; anyone changes their own password on the account page or with `changePassword`. Passwords are stored as given, because Subsonic's token login needs them.

Each play a client reports (`scrobble`) goes to the services its user linked. oasis keeps each play until the service takes it, so plays made offline or during an outage arrive later. Plays of local tracks reach YouTube Music history too: oasis finds the same song there (same title, length within 5 seconds) and remembers the match. Clients should not scrobble to Last.fm themselves, or plays count twice.

Recommendations come from YouTube Music radio, plus Last.fm's similar tracks when `[lastfm] api_key` is set. Local copies stand in for streaming tracks everywhere, and other uploads of a song already known fold into it.

- Clients' "instant mix" or "similar songs" (`getSimilarSongs2`) returns YouTube Music's radio for the song, album or artist.
- `getTopSongs` lists an artist's most played library tracks, then their YouTube Music songs.
- `getArtistInfo2` returns the Last.fm biography and the similar artists that are in the library.
- Opening an album known only from streaming search or radio fetches the whole album.
- Radio playlists grow while you listen and keep 30 unplayed tracks queued. Each user gets a "Radio" playlist that follows everything they play, mixing YouTube Music radio and Last.fm picks for the last few songs. Any playlist whose name starts with "Radio" grows the same way from the songs played in it, so a playlist named "Radio" with one song is a station for that song. Radio tracks don't join the library.

With YouTube Music linked, each user's personal mixes (My Supermix, the themed My Mixes, Discover Mix, Replay Mix, Archive Mix and others) and library playlists (Liked Music and the playlists they made or saved) appear as read-only playlists with owner `YouTube Music` and their cover art. Mixes also carry `oasisMix: true`. oasis refreshes them every 6 hours. Their tracks, albums and artists are in the library lists while the mix holds them, but they only download when played, starred or added to your own playlist.

Lyrics (`getLyricsBySongId`, OpenSubsonic `songLyrics`, and `getLyrics`) come from a `.lrc` file next to the track or lyrics tags in the file. Otherwise oasis asks the sources roma uses at once: YouTube Music, LRCLIB, NetEase, KuGou and LyricsPlus. It picks by roma's rules, with YouTube Music's text as the reference: word-timed lyrics that match it, then YouTube Music's line-timed lyrics, then other synced lyrics that match, then YouTube Music's plain text. Credit lines are dropped. Results are cached, and a miss is retried after a day. NetEase and KuGou are unofficial APIs and may change.

`stream` transcodes when a client asks for `format=opus` (in WebM), `mp3` or `aac`, or for a `maxBitRate` below the file's bitrate; `timeOffset` starts partway in. Transcoding needs `ffmpeg` on `PATH`, which the container includes. Without it, oasis sends the original file.

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

Methods for podcasts, shares, bookmarks and internet radio return empty results.

## Tests

```sh
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

The tests generate audio with `ffmpeg`, so it must be on `PATH`.

## License

GPL-3.0-or-later.
