# OpenSubsonic extension `unavailableSongs`, version 1

Status: written on 2026-11-26 for oasis; meant to be proposed to OpenSubsonic, so nothing in it is specific to oasis.

Goal: a server can show songs that it knows but cannot play, such as a recommendation that no source has, or a
track of an album that isn't in the library yet. A client can then show such a song with its album and artist, and
the user can star it. Clients that don't know the extension never get these songs, because a plain Subsonic client
expects every song to stream.

## Rules

- The server advertises `{"name": "unavailableSongs", "versions": [1]}` in `getOpenSubsonicExtensions`.
- A client opts in per request with the parameter `unavailableSongs=1`. Without it (absent, `0`, anything else) the
  response holds no unplayable songs, and the counts and lengths leave them out.
- The server may also leave them out for a user who chose not to see them, even when the client asks.
- The extension combines with the others.

## Request parameter `unavailableSongs` (any endpoint)

| Value | Response |
|---|---|
| absent, anything but `1` | Only songs that play. Playlists' `songCount` and `duration` count only those. |
| `1` | Unplayable songs too, where the server has them, each with `playable: false`. |

## Fields

### Song (`Child`)

| Field | Type | Req. | Details |
|---|---|---|---|
| `playable` | `boolean` | No | `false` for a song the server cannot stream. Absent for songs that play: there is no `playable: true`. |
| `preview` | `integer` | No | Only with `playable: false`: the server can stream a clip of the song this many seconds long, such as a store's 30 s preview. |

An unplayable song carries the usual fields where the server knows them: `title`, `artist`, `artistId`, `album`,
`albumId`, `coverArt`, `duration`, `musicBrainzId`. Its `albumId` and `artistId` can be opened with `getAlbum` and
`getArtist` like any others.

## Semantics

- `stream` and `download` on an unplayable song fail with error 70 and a message for the user, unless it has a
  `preview`: then they return the clip.
- `star` on an unplayable song stars it like any song. A server that gets music may take that as a request to get it.
  oasis does: see below.
- Unplayable songs are never in library lists (`getAlbumList2`, `getRandomSongs`, `search3`'s library results,
  `getIndexes`). They may appear where a server suggests or lists songs, such as `getSimilarSongs`,
  `getSimilarSongs2`, `getPlaylist`, `getStarred`, `getStarred2`, `getSong`, and `getAlbum` of an album the server
  knows but doesn't have.
- A song that becomes playable keeps working under its old id. The server may answer with a new id and without
  `playable`.

### What clients should do

- Show an unplayable song in place, dimmed, with its names and links to its album and artist.
- Never put it in the play queue as a song, and never call `stream` for it unless it has a `preview`. Playing a list
  that holds one plays the songs around it.
- A song with a `preview` may be played as a clip when the user asks for it, marked as a preview, and is never counted
  as played (no `scrobble`).
- Tapping it offers starring, its album and its artist, not playback.

## In oasis

- Unplayable songs are the picks of Last.fm and ListenBrainz (similar songs, radios) that neither the library, YouTube
  Music nor another streaming service the user has on has, and the tracks of albums only MusicBrainz has
  (`getAlbum` on an `al-mb-` id). Their ids start with `ms-`.
- A streaming song (YouTube Music, Qobuz, Tidal, Bandcamp, SoundCloud) is unplayable for a user who doesn't have its
  service on, until its audio is kept on the server (someone who has the service played it). For that user it keeps
  its id, has `playable: false` and isn't in library lists. Apps that don't ask for unplayable songs still get it as
  usual, and its `stream` fails with a message.
- Each user chooses on the dashboard (Songs nothing can play) whether apps that ask get them. The default is yes.
- Their `albumId` is the MusicBrainz release group's `al-mb-` id: the earliest official studio album that has the
  song, else an EP, else a single, else any official release. oasis looks it up on MusicBrainz in the background, so a
  song that was just recommended may have no album yet. Its `artistId` is the library's artist, or the `ar-mb-` id of
  an artist the library doesn't have, which can be followed.
- Starring one wants its album, as starring the album would (see the README's "Getting music"). When MusicBrainz
  doesn't know the album yet, it is wanted once it does. Unstarring the song takes back the album's star unless
  another starred song of the same album holds it, or the user starred the album itself.
- When the song arrives (its album is downloaded, or a streaming service has it), its stars and playlist entries move
  to the playable song, and the `ms-` id keeps working.
- A radio counts only playable songs toward the 30 it keeps queued.
- With previews on (the user's, on the dashboard's Services), a song that Deezer's public API or Apple's iTunes Search API has
  gets `preview: 30`. oasis asks for the clip's link when it is first played, and keeps the clip.
