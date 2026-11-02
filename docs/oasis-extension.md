# OpenSubsonic extension `oasis`, version 1

Status: written on 2026-11-02 from what oasis already sent every client; this is the canonical copy. caravan and roma
link to it.

Goal: oasis's own additions (acquisition state, YouTube Music ids, home mixes, MusicBrainz-only albums) reach the
clients that know them, and other clients get plain OpenSubsonic.

## Rules

- The server advertises `{"name": "oasis", "versions": [1]}` in `getOpenSubsonicExtensions`.
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
  library song once oasis found one (for share links). `getSong` looks for one the first time it is asked.
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
