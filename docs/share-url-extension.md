# OpenSubsonic extension `shareUrl`, version 1

Goal: a client can share a song as a public link without knowing where the link points or how to build it.
Servers without the extension and clients without it behave as plain OpenSubsonic.

## Rules

- The server advertises `{"name": "shareUrl", "versions": [1]}` in `getOpenSubsonicExtensions`.
- The field is sent to every client. No request parameter turns it on or off.
- The field adds to the response and changes nothing else. Clients that don't know it ignore it.

## Fields

### Song (`Child`)

| Field | Type | Req. | Details |
|---|---|---|---|
| `shareUrl` | `string` | No | A public URL of the song, for anyone the user shares it with. Absent when the server has none. |

Wherever the server returns a `Child` song (`getSong`, `getAlbum`, `getPlaylist`, `search3`, `getStarred2`,
`getRandomSongs` and the others), it carries `shareUrl` when there is one.

## Semantics

- The URL is opaque. Clients share it as is: they must not build one themselves, parse it, or change it.
- The URL may point to any service, and the server may change where it points in later responses.
- A song without `shareUrl` has no public link. Clients hide or disable sharing for it; they don't fall back to a
  link of their own.
- The URL needs no credentials: it never carries the user's name, password, token or API key.

## In oasis

`shareUrl` is the song's YouTube Music link, `https://music.youtube.com/watch?v=<id>`: for YouTube Music songs, and
for library songs once oasis has matched them to a YouTube Music video (`getSong` looks for a match the first time
it is asked). Songs with no match have no `shareUrl`.
