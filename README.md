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

Recommendations mix several methods, taking turns so library songs come throughout: YouTube Music's radio for the song, Last.fm's similar tracks and the library songs of Last.fm's similar artists (with `[lastfm] api_key`), the artist's other library songs, songs played within an hour of it, and library songs of the same genre, closest in year first. Songs that aren't on YouTube Music still get a full list from the library. Local copies stand in for streaming tracks everywhere, other uploads of a song already known fold into it, and a stream that gains a local copy is moved onto it (stars, plays and playlist entries) at the next scan.

- Clients' "instant mix" or "similar songs" (`getSimilarSongs2`) returns YouTube Music's radio for the song, album or artist.
- `getArtist` lists the library's albums by the artist, then the rest of their YouTube Music discography (albums, singles and EPs, newest first, with `releaseTypes`); opening one fetches its tracks. The artist's YouTube Music page is fetched on first open and kept for a day. These albums don't join the library lists.
- `getTopSongs` lists an artist's most played library tracks, then their YouTube Music top songs.
- `getArtistInfo2` adds YouTube Music's description and photo, with Last.fm's biography taking over when there is one.
- `getArtistInfo2` returns the Last.fm biography and the similar artists that are in the library.
- Opening an album known only from streaming search or radio fetches the whole album.
- Radio playlists grow while you listen and keep 30 unplayed tracks queued. Each user gets a "Radio" playlist that follows everything they play, mixing YouTube Music radio and Last.fm picks for the last few songs. Any playlist whose name starts with "Radio" grows the same way from the songs played in it, so a playlist named "Radio" with one song is a station for that song. Radio tracks don't join the library.

With YouTube Music linked, each user's personal mixes (My Supermix, the themed My Mixes, Discover Mix, Replay Mix, Archive Mix and others) and library playlists (Liked Music and the playlists they made or saved) appear as read-only playlists with owner `YouTube Music` and their cover art. Mixes also carry `oasisMix: true`, and every mirrored playlist carries its YouTube playlist id as `youtubeId`. `getPlaylists` lists the mixes first in YouTube Music's order (My Supermix, My Mix 1–7, Discover, New Release, Replay, then others by title), then Liked Music, then the rest by name. oasis refreshes them every 6 hours. Their tracks, albums and artists are in the library lists while the mix holds them, but they only download when played, starred or added to your own playlist.

Lyrics (`getLyricsBySongId`, OpenSubsonic `songLyrics`, and `getLyrics`) come from a `.lrc` file next to the track or lyrics tags in the file. Otherwise oasis asks the sources roma uses at once: YouTube Music, LRCLIB, NetEase, KuGou and LyricsPlus. It picks by roma's rules, with YouTube Music's text as the reference: word-timed lyrics that match it, then YouTube Music's line-timed lyrics, then other synced lyrics that match, then YouTube Music's plain text. Credit lines are dropped. Results are cached, and a miss is retried after a day. NetEase and KuGou are unofficial APIs and may change.

`stream` transcodes when a client asks for `format=opus` (in WebM), `mp3` or `aac`, or for a `maxBitRate` below the file's bitrate; `timeOffset` starts partway in. Transcoding needs `ffmpeg` on `PATH`, which the container includes. Without it, oasis sends the original file.

## Getting music

With `[torbox]` (with `downloads`) and `[prowlarr]`, starring an album or artist (`star` with `albumId` or `artistId`, as clients do for "add to library") makes oasis get it: a starred album is downloaded, a starred artist followed, its missing albums and new releases downloaded. `unstar` lets it go; files stay. Clients need nothing beyond standard Subsonic. oasis searches Prowlarr's indexers itself, downloads through TorBox, and places the files in the library itself; nothing on the host seeds or uploads.

```toml
[torbox]
rdt_db = "/rdt/rdtclient.db"   # rdtclient's database, read from a copy for the TorBox key and its torrents
downloads = "/downloads/oasis" # oasis's staging folder
# max_active = 8               # TorBox torrents active at which no download starts: Pro has 10, two stay free for anime
# stall_hours = 1              # no progress this long: the torrent is dead
# dry_run = true               # the doctor logs what it would do and changes nothing

[prowlarr]
url = "http://prowlarr:9696"
api_key = "…"                  # Prowlarr's config.xml
# tag = 2                      # the music indexers' tag

[acquire]
secondary_types = ["Soundtrack"] # besides albums with none
# bundles = false                # no bundle searches
```

oasis keeps the catalogue from MusicBrainz (see `oasis/acquire.py`), and a worker reconciles it with the stars after every star or unstar and every minute. A starred album resolves to its release group: from the library's tags, else by an exact title match among the artist's release groups (the artist from tags, else by name or alias; collaboration credits are tried artist by artist). A followed artist's albums, EPs and singles are read once a day, with no secondary type or one of `secondary_types`, and only those with an official release. Left out are repackages (best-ofs, deluxe and anniversary editions, re-recordings, albums 60 % of whose songs are on earlier ones) and singles whose songs are all on the artist's albums and EPs; starred albums, albums with files and the albums and EPs Lidarr was looking for when oasis took over from it never are. An album is here once the library holds as many of its tracks as its smallest edition has (video discs aside). One missing album is searched a minute, while TorBox has a slot: starred ones first, then albums and EPs, then singles, those never searched first, newest first. One that finds nothing is searched again after 6 hours, then twice as long each time up to a week; singles after a week, then up to four. An album here only lossy gets a lossless search weekly. Unstarring or unfollowing drops the albums from the catalogue; files stay. Names with no MusicBrainz match are looked up again after 30 minutes, errors and timeouts after 5. The Status panel counts the albums still to find.

Albums and artists carry `oasisWanted`: `wanted`, `downloading` or `failed`, with the reason in `oasisWantedError` (for example "searched, no release found yet"), and nothing once an album is here, an artist is followed, or nobody wants it.

The search (see `oasis/releases.py`) asks Prowlarr's music indexers for "artist album", then the album alone when the title is distinctive, then the album with the artist's names in other scripts (MusicBrainz aliases), with brackets and quotes dropped, and stops at the first query that finds a usable release. A name must hold the artist (under any of their names) and the album title as whole words, case, accents, punctuation and full-width forms aside; kana, kanji and Hangul count character by character, so `[2022.09.07] Creepy Nuts 3rdアルバム「アンサンブル・プレイ」[FLAC]` matches. Names with variant words the title lacks (live, instrumentals, acapellas, karaoke, remixes, deluxe, anniversary…), bundles (discographies, or the titles of two of the artist's albums), videos, disc images (`image+.cue`: the import takes a file per track), and sizes outside 20 MB–5 GB per disc are left out; a self-titled album or a title under 4 characters also needs the year, the title right after the artist, or the title in quotes. Lossless comes first, then what TorBox has cached (whatever its seeders), then MP3 320, V0, then seeders; a release TorBox hasn't cached needs a seeder. An album already here lossy only takes lossless.

An artist with two albums or EPs missing, or one a search found nothing for, also gets a bundle search weekly, in an album search's place: "Artist discography", "Artist" and the artist's names in other scripts. A release is a bundle when its name holds the artist and a bundle word (discography, complete, box set...) or two of the artist's album titles. Only bundles TorBox has cached count, read by their file lists: a folder whose name holds a wanted album's title (and its year, when the name has one), no variant word, and file names covering one of its editions is that album. The bundle with the most such albums goes to TorBox; only those albums' music and images are fetched, each is imported on its own, and the torrent is deleted from TorBox. A bundle none of whose albums imports is blocked. Disc images, partial albums and albums already here or downloading are left out.

The pick goes to TorBox with no seeding (see `oasis/download.py`); oasis fetches the music, cue sheets and cover art one file at a time (paced, two at once) into `downloads/<hash>/` at their paths inside the torrent, and imports them. It starts a download only while fewer than `max_active` torrents are active on the whole TorBox account, anime's included. A dead torrent, a TorBox error or a failed import blocks the release and searches the album again; a failed import's files stay 7 days.

The import (see `oasis/importer.py`) finds the MusicBrainz edition of the album the files are: every track of it must have a file with its title (or, failing that, its disc and track number) and a length within 5 s or 3 %; the edition with the most tracks wins, then the one with the closest lengths. It writes that edition's tags the way Picard names them, overwriting what the download had, and copies the files into the first library folder as `Artist/Year - Album/NN - Title.ext`: the MusicBrainz name of the release's first artist, the release group's first year, and the edition's titles. An album of several discs stays in one folder, its files named `1-01 - Title.ext`. It never overwrites a file: a track the library has already stays, unless the library's copy is lossy and the new one lossless; then the old file moves to `.replaced/` at the same path, which the scan skips. Then oasis scans, and streams of the same songs move onto the files. All MusicBrainz requests of oasis, the credits' too, go one at a time, 1.1 s apart.

With `[torbox]`, a worker also keeps TorBox's active slots free every 5 minutes (see `oasis/doctor.py`), for oasis and for anime (Sonarr through rdtclient): it deletes the account's queued items (TorBox queues adds beyond the plan's slots, and rdtclient reports them failed), active torrents neither rdtclient nor oasis tracks, and torrents with no seeds and no progress for `stall_hours`, which rdtclient reports to Sonarr as failed. oasis's own downloads are left to it.

Any signed-in user can also find music on the dashboard: `/music` searches MusicBrainz for artists (by any name or alias, in any script: "Kenshi Yonezu" finds 米津玄師) or albums, and shows whether each is in the library, wanted or followed. An artist's page (`/music/artist/<MusicBrainz id>`) follows or unfollows it and lists its albums, EPs and singles, each with Want or Unwant. Wanting and following are starring: the dashboard stars the library's id, or `ar-mb-<id>` / `al-mb-<release group id>` for what only MusicBrainz has. `/music/wanted` lists every album oasis is looking for with its state (waiting for its search, not found yet, downloading with its progress, importing, failed and why), and can search one next, block the release downloading and try another, or give up on it (unwanted, and left out of its artist's catalogue; files stay). In Subsonic clients, `search3` adds MusicBrainz artists and albums that neither the library nor YouTube Music has, for queries of 3 or more characters, after the other results; searches are kept a week. Such an artist lists its release groups, an album has no songs until it arrives, and starring them wants or follows them as for any other. Once one arrives, its stars and wants move to the library's id, and the old id keeps working.

Lidarr searched and imported before oasis did. Its tables `lidarr_rescue` and `catalogue_search` stay in the database. Usenet isn't supported.

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

Point a Subsonic client at `http://<host>:4533`. `oasis scan` runs a scan without serving. `oasis relocate` moves files placed under an older scheme (Lidarr's `Artist/Album (Year)`) to the import's names by their MusicBrainz tags; it only plans and checks the moves until given `--apply`, and should run while the server is stopped. Tracks keep their ids, so stars, plays and playlists stay, and the moves are written to `.oasis-renames-<date>.tsv` in the library. The server rescans when a client calls `startScan`.

## Supported API

Browsing (`getArtists`, `getArtist`, `getAlbum`, `getSong`, `getIndexes`, `getMusicDirectory`), album lists, random songs, genres, `search2`/`search3` (an empty query lists everything, for clients that sync the whole library), stars, ratings, scrobbles, playlists, cover art (embedded or `cover`/`folder`/`front` images), and `stream`/`download` with HTTP range requests. Responses are XML or JSON (`f=json`), over GET or form POST.

Songs carry `youtubeId`, the YouTube video of the same song, when oasis knows it: always for YouTube Music tracks, and for local files once they're matched. `getSong` looks a local song up on YouTube Music the first time it's asked. Clients can build share links from it (`https://music.youtube.com/watch?v=<id>`, `https://youtu.be/<id>`).

Methods for podcasts, shares, bookmarks and internet radio return empty results.

## Tests

```sh
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

The tests generate audio with `ffmpeg`, so it must be on `PATH`.

## License

GPL-3.0-or-later.
