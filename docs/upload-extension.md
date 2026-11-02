# OpenSubsonic extension `upload`, version 1

Status: agreed with the user on 2026-11-01. This is the canonical copy; mirage links to it. The changes made
while implementing it in oasis are listed at the end.

Goal: a desktop client sends a ripped CD, an album or single files to the server, which tags, names, files and
scans them. Servers without the extension and clients without it behave as plain OpenSubsonic. caravan won't use
it.

## Rules

- The server advertises `{"name": "upload", "versions": [1]}` in `getOpenSubsonicExtensions`.
- Only users with the standard Subsonic `uploadRole` (`getUser`) may upload; in oasis, the admins. Others get error 50
  from every endpoint below. Clients hide the feature for them.
- An upload belongs to the user who created it; for anyone else it doesn't exist (70).
- Clients decide by the extension list and `uploadRole` only, never by `subsonic-response.type`.
- Nothing appears in the library until `commitUpload` succeeds. An abandoned upload is deleted 24 h after its last
  request.
- Errors use the standard Subsonic error codes (10 missing parameter, 50 not authorized, 70 not found) or 0
  with a message.

## Endpoints

### `lookupDisc`

Params: `discId` (MusicBrainz disc id), and optionally `toc` (the MusicBrainz TOC string: first track, last track,
lead-out offset, track offsets, separated by spaces or `+`) so the server can fall back to a fuzzy TOC lookup.

Response `discLookup.release[]`, best match first, possibly empty:
`id` (MusicBrainz release id), `title`, `artist`, `year`, `date`, `country`, `label`, `catalogNumber`,
`barcode`, `discCount`, `discNumber` (the disc this TOC matched: the medium holding the disc id, else the first
with the TOC's track count), `coverArt` (an id usable with `getCoverArt` when the release is already in the
library with a cover, else absent), `track[]` of `number`, `title`, `artist`, `duration` (seconds),
`musicBrainzId`. Names come with `…Latin` fields when the client also sends `romanization=both`.

### `createUpload`

Either:
- `releaseId` (a MusicBrainz release id) and `disc` (number, default 1): the server takes all tags from
  MusicBrainz;
- or `tags`: a JSON object `{"album", "albumArtist", "year", "genre", "track": [{"disc", "number", "title",
  "artist", "artists"}]}` for uploads with no MusicBrainz release (single files go in as an album of one or more
  tracks; `album` may be empty for loose singles: each track then becomes its own single, so several singles go in
  one upload). `artists` is an array, preferred over the joined `artist`. `year` is a number; a numeric string is
  accepted too. `albumArtist` may be absent when every track has an `artist` or `artists`.

With neither, error 10. An unknown `releaseId`, or a `disc` the release doesn't have, is error 70.

Response `upload`: `id`, `expires` (ISO 8601; each request about the upload moves it to 24 h later), `track[]` of
the `disc`/`number` slots it expects.

### `uploadTrack` (POST)

Query params: `uploadId`, `disc`, `number`, `offset` (bytes already sent, 0 for a new file), `size` (the whole
file's size), `suffix` (`flac`, `opus`, `mp3`, `m4a`, `ogg`).
Body: the next bytes of the file, `Content-Type: application/octet-stream`. Auth params go in the query.
Response `uploadTrack`: `received` (bytes stored so far). A client resumes from `received` after a failure.
Before resuming after any error, call `getUpload`: a file the server rejected (format check) is dropped and its
`received` returns to 0.

An `offset` below `received` sends the file again from there; one above it is refused (0). A resumed file keeps its
`suffix` and `size`; to change them, start again at offset 0. Bytes beyond `size` are refused and dropped.
The server rejects a suffix it doesn't accept (0) and a file whose content isn't that format: checked once the last
byte arrives, refused (0) and dropped, so `received` is 0 again.

### `getUpload`

Param `id`. Response `upload` as from `createUpload`, each `track` with `received` and `size`.

### `uploadCover` (POST, optional)

Query `uploadId`; body a JPEG or PNG (anything else is refused, 0). Without it the server uses the release's cover
from MusicBrainz / the Cover Art Archive; an upload with `tags` has none then. An album folder that already has a
cover keeps it.

### `commitUpload`

Param `uploadId`, optional `replace=true`.
- Checks every expected track is complete and decodes. FLACs then go through the checks downloaded albums do: one
  made from a lossy file is refused (0), and fake hi-res or badly compressed ones are converted or recompressed.
- Tags each file (from MusicBrainz or `tags`), names and places it by the server's library scheme, adds cover
  art, scans it in. Atomic: on any failure nothing is added and the upload stays for a retry.
- An album already in the library: refused with code 0, message "already in the library" and `"duplicate": true`
  in the error object (match on that, not the text), unless the upload is lossless
  and the library copy lossy, or `replace=true`. Already in the library means library tracks of the same album
  (album artist and album name, or MusicBrainz release group) on the uploaded discs, or files of the same names
  where the upload goes; another disc of an album isn't. The library tracks the upload replaces (same disc and
  number, or same file name) are deleted once the upload is in; the album's other tracks stay.
- Response `album`: the new `AlbumID3` (with `song[]`). Loose singles make several albums: `album` is the first,
  and `albums` lists them all.

### `cancelUpload`

Param `uploadId`. Deletes the upload and its files.

## Client (mirage)

- Shown only for a server listing `upload` v1 and a user with `uploadRole`.
- Rip CD to server: read the TOC and disc id (cdda plugin), `lookupDisc`, let the user pick a release or edit
  tags, rip each track to FLAC locally with fooyin's converter (AccurateRip check when available), upload, commit.
- Upload album or files: pick local files (this is the only place mirage reads local media), read their tags as
  the starting `tags`, let the user edit, upload, commit.
- Temp files live in mirage's cache and are deleted after commit or cancel.

## Changes from the 2026-11-01 draft

1. `uploadRole` is true for admins only: oasis has no per-user roles beyond admin. Other users get 50.
2. An upload belongs to the user who created it; for other users it is not found (70).
3. `toc` is optional in `lookupDisc`; without it only the disc id's own releases are found. It may be separated by
   spaces or `+`.
4. `discNumber` falls back to the first medium with the TOC's track count when no medium lists the disc id.
5. `coverArt` in `lookupDisc` is the library album's when the release is already in the library with a cover; oasis
   fetches nothing for a lookup.
6. `albumArtist` may be left out when every track has an `artist` or `artists`.
7. `createUpload` errors: 10 with neither `releaseId` nor `tags`, 70 for an unknown release or disc.
8. `expires` is 24 h after the upload's last request, not its creation, so a long upload isn't deleted midway.
9. `uploadTrack`: an `offset` below `received` resends from there; above it is refused. A resumed file keeps its
   `suffix` and `size`. The format check happens when the last byte arrives, and a file that fails it is dropped.
10. `uploadCover` refuses what isn't JPEG or PNG; an album folder's existing cover stays; an upload with `tags`
    gets no Cover Art Archive cover.
11. `commitUpload` runs downloads' FLAC checks (`quality.py`): a FLAC made from a lossy file is refused, fake hi-res
    and badly compressed FLACs are converted or recompressed.
12. "Already in the library" is defined: the same album on the uploaded discs, or files of the same names at the
    target. Another disc of an album already there is accepted. Only the replaced tracks (same disc and number) are
    deleted; others, such as a deluxe edition's extra tracks, stay.
13. With loose singles, `commitUpload` returns `albums` (each single) besides `album` (the first).
