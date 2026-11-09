# OpenSubsonic extension `oasis`, versions 1 to 3

Status: written on 2026-11-02 from what oasis already sent every client; this is the canonical copy. caravanserai (caravan)
link to it.

Goal: oasis's own additions (acquisition state, YouTube Music ids, home mixes, MusicBrainz-only albums) reach the
clients that know them, and other clients get plain OpenSubsonic.

## Rules

- The server advertises `{"name": "oasis", "versions": [1, 2, 3]}` in `getOpenSubsonicExtensions`. Version 2 adds
  radio sources and stations ([Radio](#radio-version-2)), version 3 lyrics translation
  ([Lyrics translation](#lyrics-translation-version-3)). A client offers each only when the list has its version.
- Clients decide by that list only, never by `subsonic-response.type`.
- A client opts in per request with the parameter `oasis=1`. Without it (absent, `0`, anything else) the response
  has none of the fields and none of the items below.
- The extension combines with the others: `oasis=1&romanization=both` gets both.

## Request parameter `oasis` (any endpoint)

| Value | Response |
|---|---|
| absent, anything but `1` | Plain OpenSubsonic: no fields below, no MusicBrainz-only artists or albums in lists. |
| `1` | The fields and items below where they apply. |

## Fields (only with `oasis=1`, only where they have a value)

- Album (`AlbumID3`) and artist (`ArtistID3`):
  - `oasisWanted`: acquisition state of a starred album or artist: `wanted`, `downloading` or `failed`. Absent for
    albums in the library and for followed artists, which need no badge.
  - `oasisWantedError`: with `oasisWanted`, why it isn't downloading (for `failed`, the last search's result).
- Song (`Child`): `youtubeId`: the YouTube Music video id of a streaming song, or of the YouTube Music copy of a
  library song once oasis found one. `getSong` looks for one the first time it is asked. The link to share is
  `shareUrl`, from the separate extension `shareUrl` ([share-url-extension.md](share-url-extension.md)), sent to every
  client.
- Playlist:
  - `oasisMix`: `true` for a YouTube Music home mix (My Supermix, My Mix 1-7, Discover Mix, New Release Mix,
    Replay Mix). Absent for other playlists.
  - `youtubeId`: the YouTube Music playlist id of a mix or library playlist mirrored from YouTube Music.
- Artist in `getArtistInfo`/`getArtistInfo2` `similarArtist`: `oasisLink`: how the artist relates to the one asked
  about: `persona` (one person's other names or projects), `related` (grouped by the server's admin) or `member`
  (a band and its members). These come first in the list; artists without it are similar by Last.fm.

## MusicBrainz-only artists and albums (only with `oasis=1`)

Artists and albums that only MusicBrainz knows, so they can be starred and the server acquires them. Ids start with
`ar-mb-` and `al-mb-` followed by the MusicBrainz artist or release group id. An album has `songCount: 0`,
`duration: 0`, no `song` entries, and a `coverArt` from the Cover Art Archive.

- `search3` and `search2` add them after the library's and YouTube Music's results, for queries of 3 characters or
  more, when the server acquires music.
- `getStarred` and `getStarred2` list the starred ones.
- `getArtist` and `getAlbum` answer for these ids with or without `oasis=1`, as a client only has them from the two
  above. `getArtist` on an `ar-mb-` id lists the artist's MusicBrainz albums.

Once one arrives in the library, its `ar-mb-`/`al-mb-` id keeps working and answers with the library's artist or
album.

## Endpoints (answer with or without `oasis=1`)

- `oasisLinkYouTubeMusic`: params `cookie` (the music.youtube.com `Cookie` header, or the request headers) and
  optional `authUser` (the `X-Goog-AuthUser` index, default 0). Links the user's YouTube Music account and loads
  their mixes. Response `youtubeMusic.account`: the account name. Error 0 with a message when YouTube Music rejects
  the sign-in or is off on the server.
- `oasisUnlinkYouTubeMusic`: no params. Unlinks it and removes the mirrored playlists.

## Radio (version 2)

oasis suggests songs like a seed song (`getSimilarSongs`, `getSimilarSongs2`) and keeps radio playlists that grow as
they are played. Both take their songs from a **source**:

| Source id | Songs from | Available when |
|---|---|---|
| `all` | Every source below that is available, taking turns (oasis's behaviour before version 2) | always |
| `lastfm` | Last.fm's similar tracks, and library songs by Last.fm's similar artists | the server has a Last.fm API key |
| `ytm` | YouTube Music's radio for the song | the server has YouTube Music on |
| `library` | The library alone: the artist's linked artists, the same artist, songs played around it, the same genre | always |
| `listenbrainz` | ListenBrainz's LB Radio for the song's artist and artists ListenBrainz finds similar | the user linked a ListenBrainz token |

Last.fm's and ListenBrainz's picks become library songs where the library has them, else YouTube Music streaming
songs (`yt-…`). A source that finds nothing, or fails, gives nothing: no other source stands in, so an empty list
means that source had nothing for this song.

Each user has a **default source**, set on the dashboard (Radio, on the home page) and `all` until set. It serves every
request that names no source, from any client, and radios started without one.

### `oasisRadio` (answers with or without `oasis=1`)

No params. What this user can choose from now; ask again after the user links or unlinks an account.

```json
"oasisRadio": {
  "defaultSource": "all",
  "source": [{"id": "all", "name": "Everything mixed"}, {"id": "lastfm", "name": "Last.fm"},
             {"id": "ytm", "name": "YouTube Music"}, {"id": "library", "name": "Your library"},
             {"id": "listenbrainz", "name": "ListenBrainz"}],
  "station": [{"id": "lastfm-top", "name": "Last.fm top tracks", "defaultPeriod": "3month",
               "period": ["7day", "1month", "3month", "6month", "12month", "overall"]},
              {"id": "lastfm-loved", "name": "Last.fm loved tracks"}]
}
```

- `source` lists only the available sources, in this order; `name` is for display.
- `station` lists only the available stations: both need the server's Last.fm API key and the user's linked Last.fm.
  It is an empty list otherwise. Only `lastfm-top` has periods.
- `defaultSource` can name a source that is no longer available (the user unlinked it); requests that rely on it then
  get nothing until the user picks another.

In XML, `source` and `station` are repeated elements with attributes, and each `period` is a child element.

### Choosing the source of similar songs

`getSimilarSongs` and `getSimilarSongs2` take `oasisSource=<source id>`, read only with `oasis=1`. Without
`oasis=1` the parameter is ignored and the user's default applies. With `oasis=1`:

- `similarSongs`/`similarSongs2` has `oasisSource`: the source the songs came from (the one asked for, else the
  default).
- An unknown id is error 0, `oasisSource must be one of all, lastfm, ytm, library, listenbrainz`.
- A known but unavailable source gives an empty `song` list.

### Starting a radio playlist

`createPlaylist` with `oasis=1` and one or both of these makes a radio playlist whatever its name:

| Param | Value |
|---|---|
| `oasisSource` | A source id from `oasisRadio`. Default: the user's default source. |
| `oasisStation` | `lastfm-top` or `lastfm-loved`: a station seeded from the user's Last.fm instead of from its own songs. |
| `oasisPeriod` | With `lastfm-top`: one of its periods. Default `3month`. Ignored for `lastfm-loved`. |

- `name` is optional for a station (default: the station's name) and required otherwise.
- `songId` (repeatable) gives the first songs. A radio with a source and no station grows from its last played songs,
  so give it at least one; a station needs none.
- oasis fills the radio before answering, so the response's `entry` already lists the first 30 or so songs. This can
  take several seconds while oasis asks Last.fm, YouTube Music or ListenBrainz.
- Errors (code 0): `oasisSource <id> isn't available: …` (unknown ids too), `oasisStation <id> isn't available: …`,
  or `oasisPeriod must be one of …`.

Examples:

```
createPlaylist?oasis=1&name=Radio: Song&songId=<id>&oasisSource=listenbrainz
createPlaylist?oasis=1&oasisStation=lastfm-top&oasisPeriod=1month
createPlaylist?oasis=1&oasisStation=lastfm-loved&oasisSource=library
```

A station takes the user's top tracks over the period (or loved tracks) from Last.fm, shuffled. Each one the library
or YouTube Music has is added, followed by up to 4 songs like it from the station's source. The source of a station
is only for the similar songs; `all` is fine.

How radios grow, for every radio:

- oasis keeps about 30 unplayed songs queued after the last song played from the radio (plays reach oasis by
  `scrobble`). It adds songs in the background. A client reloads the playlist (`getPlaylist`) to see them.
- It skips songs already in the radio and the user's 100 most recently played.
- A radio keeps the source it was started with. A radio started by name (`createPlaylist?name=Radio …`, without
  the params above) takes the user's default at that moment. The automatic `Radio` playlist (created at a user's first
  play, growing from everything they play) always follows the current default.
- Radios are `readonly: true`. A radio whose name doesn't start with `Radio` (made with the params above) stays a
  radio when renamed. One named `Radio …` becomes a plain playlist when renamed to something else, as before.

Playlist fields (only with `oasis=1`, only on radios):

- `oasisSource`: the source the radio grows from.
- `oasisStation`: `lastfm-top` or `lastfm-loved`, for a station.
- `oasisPeriod`: a `lastfm-top` station's period.

## Lyrics translation (version 3)

Lyrics come as songLyrics v2 entries (`getLyricsBySongId` with `enhanced=true`; see the romanization extension's
Lyrics section for `kinds` and alignment). `main` holds only the song's own lines. Translations come as
`translation` entries, each with a `lang` and one line per `main` line (`value` `""` where a line has none):

- **Provided translations**, always: the ones lyrics providers send (NetEase, KuGou, QQ: mostly Chinese, `lang`
  `zh`). They need no parameter; clients choose whether to show them.
- **Machine translations**, only on request: oasis never translates on its own.

### Asking for a machine translation

`getLyricsBySongId` with `enhanced=true`, `oasis=1` and `oasisTranslate`:

| `oasisTranslate` | Translation to |
|---|---|
| absent | none: no machine translation, and no request to the translator |
| `1` | the user's language, set on the dashboard (Lyrics translation); none if the user has not set one |
| a language code (`en`, `pl`, `zh-CN`, `zh-TW`, `sr-Latn`; pattern `[a-z]{2,3}(-[A-Z]{2}\|-[A-Z][a-z]{3})?`) | that language |

- A provided translation in the language asked (same primary subtag: `zh` serves `zh-CN` and `zh-TW`) wins, and
  no machine translation is made.
- Otherwise the response gets one more `translation` entry with `lang` set to the code asked and
  `oasisMachineTranslation: true`, so a client can label it.
- Lyrics already in that language (the translator's own detection) get no machine translation.
- oasis keeps each translation per song and language, and translates again only when the song's lyrics change. The
  first request for a song waits for the translator (about a second); later ones don't.
- If the translator fails or blocks oasis, the response has no machine translation and the lyrics are otherwise as
  usual. oasis then leaves the translator alone for 10 minutes. A client sees only the missing entry.
- `oasisTranslate` is ignored without `oasis=1`, without `enhanced=true` (songLyrics v1 has the song's lines only),
  and when `kinds` leaves out `translation`.
- An invalid code is error 0, `oasisTranslate must be 1 or a language code such as en, pl or zh-CN`.

The translator is Google Translate's unofficial keyless endpoint, sent the song's lines in one request.

### `oasisTranslation` (answers with or without `oasis=1`)

No params.

```json
"oasisTranslation": {
  "available": true,
  "language": "pl",
  "languages": [{"id": "en", "name": "English"}, {"id": "pl", "name": "Polish"}, {"id": "zh-CN", "name": "Chinese (Simplified)"}]
}
```

- `available`: whether this server makes machine translations. A client offers translation when the server lists
  oasis version 3 and `available` is true.
- `language`: the user's language from the dashboard, which `oasisTranslate=1` uses. Absent when not set.
- `languages`: the languages the dashboard offers, for a client's own picker. Requests may name others.

## oasis behaviour every client gets

These use standard fields only, so they are not part of the parameter:

- Streaming songs from YouTube Music (ids `yt-…`) in `search3`, `getTopSongs`, `getSimilarSongs` and playlists;
  they stream like library songs.
- YouTube Music mixes and library playlists of a linked account are read-only playlists (`readonly: true`) owned by
  `YouTube Music`.
- `getArtist` lists the artist's YouTube Music albums after the library's; such an album has `songCount: 0` until
  `getAlbum` opens it.
- `star`/`unstar` with `albumId` or `artistId` asks the server to acquire the album or the artist's albums, or takes
  that back. Stars, ratings and play counts are each user's.
- Playlists named `Radio …` add similar songs as they are played.
- Ids a client keeps (a stream that became a library file, an `ar-mb-`/`al-mb-` id that arrived, another name of an
  artist) keep working.
