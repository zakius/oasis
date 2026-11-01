# Backlog

Planned work, not built yet.

## OpenSubsonic extension `upload` v1

CD rips and album or file uploads from desktop clients (mirage). caravan won't use it.

- Advertised in `getOpenSubsonicExtensions`. Servers without it are unaffected, and clients hide the feature.
- `lookupDisc`: a MusicBrainz disc id or TOC in; matching releases out (album, artists, tracks, cover).
- `createUpload`: a chosen release id or client-supplied tags in; an upload id out.
- `uploadTrack`: per track, a FLAC body via the `formPost` extension, resumable.
- `commitUpload`: oasis checks completeness, tags, names by its library scheme, adds cover art, scans, and returns
  the new album id. Atomic: nothing appears half-done.
- Reuses oasis's existing MusicBrainz lookups, tagging, naming and scanning.
- Later: AccurateRip verification on the client.
