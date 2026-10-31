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

## Lidarr

With a `[lidarr]` section, starring an album or artist (`star` with `albumId` or `artistId`, as clients do for "add to library") makes oasis get it through Lidarr; `unstar` lets it go. Clients need nothing beyond standard Subsonic.

```toml
[lidarr]
url = "http://lidarr:8686"
api_key = "..."            # Lidarr → Settings → General
# root_folder = "/music"   # defaults: the first root folder and its default profiles
# quality_profile = 4
# metadata_profile = 1
```

oasis keeps who wants what, and a worker reconciles Lidarr with it after every star or unstar and every minute. A wanted album is looked up on MusicBrainz through Lidarr by artist and title (punctuation and case aside; collaboration credits are tried artist by artist), added with only that album monitored, and searched: one album search every 3 minutes, so a burst of stars doesn't trip a debrid service's rate limit, and again after 6 hours if nothing came. A wanted artist is followed: new releases get monitored, the back catalogue doesn't, and nothing is searched. Once no user wants an item, oasis only unmonitors it; downloaded files stay. Lookups with no match are retried after 30 minutes, errors and timeouts after 5. An album known only by a romanized YouTube Music title may not match MusicBrainz's original-script title. Lidarr downloads into the music folder, the scan picks the files up, and streams of the same songs move onto them.

Albums and artists carry `oasisWanted`: `wanted`, `downloading` (only while Lidarr's queue or oasis's own downloads hold it) or `failed`, with the reason in `oasisWantedError` (for example "searched, nothing grabbed yet"), and nothing once an album is complete, an artist is followed, or nobody wants it.

`oasis adopt-lidarr --user NAME` makes NAME follow, as if starred, every artist followed in Lidarr itself that oasis doesn't follow yet, by MusicBrainz id; `--dry-run` only lists them. It first saves their Lidarr monitoring to `adopt-lidarr-<date>.json` next to the database, since the next reconcile monitors their albums and EPs. Unstarring one undoes it.

With a `[torbox]` section as well, a worker fixes what Lidarr and rdtclient leave stuck, every 5 minutes (see `oasis/doctor.py`): failed imports go in against whichever edition of the album the files cover, or the release is blocklisted and searched again; downloads rdtclient finished but never completed get imported; TorBox's queued items, untracked and dead torrents are deleted so its active slots stay free; and slow downloads are swapped for a copy TorBox has cached. It needs the downloads folder at the same path Lidarr sees it.

With `downloads` set, oasis downloads its searches' finds through TorBox itself instead of having Lidarr grab them through rdtclient (see `oasis/download.py`). Of the releases Lidarr's search lists, it picks one TorBox has cached, whatever its seeders, when that is as good as the best Lidarr accepts, else Lidarr's best; it adds it to TorBox with no seeding, fetches the music, cue sheets and cover art one file at a time (paced, two at once) into `downloads/<hash>/` at their paths inside the torrent, and imports them itself. It starts a download only while fewer than `max_active` torrents are active on the whole TorBox account, anime's included. A dead torrent, a TorBox error or a failed import blocks the release in oasis (not Lidarr's blocklist) and searches the album again; a failed import's files stay 7 days. These downloads don't show in Lidarr's queue. Lidarr's own RSS grabs still go through rdtclient, and the doctor's rules still cover them. Removing `downloads` and restarting goes back to Lidarr grabbing; downloads already running finish.

oasis's import (see `oasis/importer.py`) finds the MusicBrainz edition of the album the files are: every track of it must have a file with its title (or, failing that, its disc and track number) and a length within 5 s or 3 %; the edition with the most tracks wins, then the one with the closest lengths. It writes that edition's tags the way Picard names them, overwriting what the download had, and copies the files into the first library folder as Lidarr names them, `Artist/Album (Year)/NN - Title.ext` with `CD 01`-style folders for several discs, into the artist's existing folder if the library has their files. It never overwrites a file: a track the library has already stays, unless the library's copy is lossy and the new one lossless; then the old file moves to `.replaced/` at the same path, which the scan skips. Then oasis scans, and Lidarr rescans the album's folder (at the same path: Lidarr must see the library where oasis does) so it counts the album present. A download no edition fits fails like a failed import. `import = "lidarr"` has Lidarr import it instead, with the doctor's matcher. All MusicBrainz requests of oasis, the credits' too, go one at a time, 1.1 s apart.

```toml
[torbox]
rdt_db = "/rdt/rdtclient.db"   # rdtclient's database, read from a copy for the TorBox key and its downloads
# stall_hours = 1              # no progress this long: the download is dead
# dry_run = true               # log what it would do, change nothing
# downloads = "/downloads/oasis"  # oasis's own staging folder, at the same path Lidarr sees it
# import = "lidarr"            # Lidarr imports oasis's downloads instead of oasis
# max_active = 8               # TorBox torrents active at which no download starts: Pro has 10, two stay free for anime
```

With a `[prowlarr]` section as well, oasis searches Prowlarr's music indexers itself instead of Lidarr's search (see `oasis/releases.py`), so Lidarr's parser no longer decides which names are the album. It asks for "artist album", then the album alone when the title is distinctive, then the album with the artist's names in other scripts (MusicBrainz aliases), with brackets and quotes dropped, and stops at the first query that finds a usable release. A name must hold the artist (under any of their names) and the album title as whole words, case, accents, punctuation and full-width forms aside; kana, kanji and Hangul count character by character, so `[2022.09.07] Creepy Nuts 3rdアルバム「アンサンブル・プレイ」[FLAC]` matches. Names with variant words the title lacks (live, instrumentals, acapellas, karaoke, remixes, deluxe, anniversary…), bundles (discographies, or the titles of two of the artist's albums), videos, disc images (`image+.cue`: the import takes a file per track), and sizes outside 20 MB–5 GB per disc are left out; a self-titled album or a title under 4 characters also needs the year, the title right after the artist, or the title in quotes. Lossless comes first, then what TorBox has cached (whatever its seeders), then MP3 320, V0, then seeders; a release TorBox hasn't cached needs a seeder. An album already here lossy only takes lossless. Removing `[prowlarr]` goes back to Lidarr's search.

```toml
[prowlarr]
url = "http://prowlarr:9696"
api_key = "…"                  # Prowlarr's config.xml
# tag = 2                      # the music indexers' tag
```

With `[prowlarr]`, `downloads` and oasis's own import, oasis also keeps the catalogue itself, from MusicBrainz (see `oasis/acquire.py`), and Lidarr is only asked what its own RSS grabs are downloading, so oasis doesn't search those albums too. A starred album resolves to its release group: from the library's tags, else by an exact title match among the artist's release groups (the artist from tags, else by name or alias). A followed artist's albums, EPs and singles are read once a day, with no secondary type or one of `secondary_types`, and only those with an official release. Left out are repackages (best-ofs, deluxe and anniversary editions, re-recordings, albums 60 % of whose songs are on earlier ones) and singles whose songs are all on the artist's albums and EPs; starred albums, albums with files and the albums and EPs Lidarr was looking for when oasis took over never are. An album is here once the library holds as many of its tracks as its smallest edition has (video discs aside). One missing album is searched a minute, while TorBox has a slot: starred ones first, then albums and EPs, then singles, those never searched first, newest first. One that finds nothing is searched again after 6 hours, then twice as long each time up to a week; singles after a week, then up to four. An album here only lossy gets a lossless search weekly. Unstarring or unfollowing drops the albums from the catalogue; files stay. The Status panel counts the albums still to find. Lidarr's catalogue searches carry over, by release group, on the first run.

An artist with two albums or EPs missing, or one a search found nothing for, also gets a bundle search weekly, in an album search's place: "Artist discography", "Artist" and the artist's names in other scripts. A release is a bundle when its name holds the artist and a bundle word (discography, complete, box set...) or two of the artist's album titles. Only bundles TorBox has cached count, read by their file lists: a folder whose name holds a wanted album's title (and its year, when the name has one), no variant word, and file names covering one of its editions is that album. The bundle with the most such albums goes to TorBox; only those albums' music and images are fetched, each is imported on its own, and the torrent is deleted from TorBox. A bundle none of whose albums imports is blocked. Disc images, partial albums and albums already here or downloading are left out.

```toml
[acquire]
# catalogue = "lidarr"           # Lidarr's catalogue and wanted lists instead, as above
secondary_types = ["Soundtrack"] # besides albums with none; as in Lidarr's metadata profile
# bundles = false                # no bundle searches
```

Never point a Lidarr SABnzbd download client at rdtclient's categories: rdtclient's SABnzbd emulation deletes torrents it doesn't own. Usenet isn't supported.

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
