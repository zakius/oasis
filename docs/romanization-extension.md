# OpenSubsonic extension `romanization`, version 1

Status: agreed with the user on 2026-11-01. This is the canonical copy; roma and mirage link to it. The changes made
while implementing it in oasis are listed at the end.

Goal: the server does the romanization and sends it in a form any client can ignore. Clients that know the
extension show original, romanized or both names and lyrics without romanizing anything themselves. A server
without it, and a client without it, behave as plain OpenSubsonic.

## Rules

- The server advertises `{"name": "romanization", "versions": [1]}` in `getOpenSubsonicExtensions`. It also
  advertises `songLyrics` version 2, which the lyrics below use.
- Clients decide by that list only, never by `subsonic-response.type`.
- Without the request parameters below, the server answers exactly as before: no extra fields. Other clients pay
  nothing.

## Request parameter `romanization` (any endpoint returning names)

| Value | Response |
|---|---|
| absent, `original`, anything else | Plain OpenSubsonic. No extra fields. |
| `both` | Standard name fields keep the original. Each item adds the `…Latin` fields below where the romanized form differs from the original. |
| `latin` | Standard name fields carry the romanized form; no `…Latin` fields. For clients showing romanized names only. |

With `latin`, `getArtists` and `getIndexes` group the artists by the first letter of their romanized names.

Search (`search3`) matches the original and the romanized form whatever the value. Matching the romanized form
ignores case, accents (ō = o), spaces and punctuation: `qing tian`, `Qíng` and `qingtian` all find 晴天.

## Fields (only present with `romanization=both`, only where the romanized form differs)

- Song (`Child`): `titleLatin`, `albumLatin`, `artistLatin`, `displayArtistLatin`, `displayAlbumArtistLatin`, `displayComposerLatin`.
- Album (`AlbumID3`): `nameLatin`, `artistLatin`, `displayArtistLatin`. With `latin`, the album's `title` and `album`
  fields (copies of `name`) carry the romanized form too.
- Artist (`ArtistID3`, also inside `artists`, `albumArtists`, `contributors[].artist` and the `getArtists` index): `nameLatin`.
  In a list, each entry's `nameLatin` belongs to that entry's own `name`.
- Playlist: `nameLatin`.
- `getMusicDirectory`'s `directory` (an artist or an album): `nameLatin`.

A missing `…Latin` field means the original is already in Latin script, or the server has no romanization for it;
the client shows the original.

A song's title is romanized together with its artist and album, so the title of a Chinese singer's song reads as
Chinese. An artist on its own (an index, a search result) has no such context, so its `nameLatin` can differ from
the `artistLatin` of its songs.

## Lyrics

Standard `songLyrics` v2, no new fields: with `enhanced=true` the server returns the `main` lyrics, a
`pronunciation` entry (the romanization, `lang` such as `ja-Latn`) and `translation` entries where it has them,
each with `cueLine` word or syllable timing when the server has it. Without `enhanced=true` the response is
songLyrics v1, `main` only, as before.

The `pronunciation` entry's `lang` is the language romanized from plus `-Latn`: `ja`, `zh`, `ko`, `el`, a Cyrillic
language (`ru`, `uk`, `be`, `kk`, `ky`, `sr`, `mk`, `bg`, `mn`) or `und`. Lyrics already in Latin script have no
`pronunciation` entry.

Alignment, so clients can pair the layers: the `pronunciation` (and `translation`) entry has the same number of
lines as its `main` entry, in the same order, with the same `start` times when synced; its cues fall inside the
matching main line's time span. If a server breaks this and the counts differ, clients pair by line index.

Translations: Chinese providers (NetEase, KuGou, QQ through LyricsPlus) and some LRCLIB uploads put a translation
under each line, with the line's start time. oasis takes such lines out of the lyrics:

- A line with the same start as the line before, written in a script other than both that line's and the song's, is
  a translation of that line. A backing line in the song's own script stays.
- Latin lines that read as their line (romaji, pinyin) are dropped, as `pronunciation` gives oasis's own reading.
- NetEase's separate translation (`tlyric`) and a KuGou KRC file's `[language:]` translation are translations too.

Each one becomes a `translation` entry with `lang` from its script: `zh` for Han without kana, `ja`, `ko`, `en` for
Latin, a Cyrillic language, `el`, `ar`, `he`, `th`, `hi`, else `und`. It has one line per `main` line, in order, with
the same `start`; `value` is `""` where the provider has no translation for that line. With songLyrics v1 (no
`enhanced=true`), `getLyrics` and `kinds` without `translation`, clients get the song's lines only.

`offset` has the OpenSubsonic meaning: positive shows the lyrics sooner, so a line displays at `start - offset`.
oasis applies an LRC file's `[offset:]` tag to the start times and always sends `offset: 0`.

Extension parameter on `getLyricsBySongId` with `enhanced=true`, to save bandwidth:

- `kinds`: comma-separated subset of `main,pronunciation,translation`. Default: all. A client showing romanized
  lyrics only asks `kinds=pronunciation`; one showing original and romanized asks `kinds=main,pronunciation`.
  When the lyrics need no romanization, `main` stands in for `pronunciation`, so `kinds=pronunciation` still gets
  lyrics.

Servers that don't know `kinds` ignore it and send everything, which clients must handle.

## What each side does

- oasis: implements and advertises the extension; romanizes names and lyrics with a port of roma's romanizer
  (`oasis/romanize.py`), kept identical by the golden cases in `tests/romanization-cases.tsv`, which roma's own
  romanizer generates. It sends `cueLine` on `main` and `pronunciation` for lines with karaoke words (enhanced LRC,
  NetEase, KuGou, LyricsPlus): a pronunciation line is its words' romaji joined, each word keeping the main word's
  timing, as roma's romanizeLyrics does. A romaji word that went to the word before is left out, so a pronunciation
  line can have fewer cues than its main line. `translation` entries hold the translations providers send (see
  Translations below); `main` is only the song's own lines, and only those are romanized.
- roma (Subsonic app): with the extension, never downloads or asks for the Japanese dictionary; syncs its index
  with `romanization=both` so switching the display is instant; asks lyrics with `enhanced=true` and `kinds`
  matching the display. Without it, on-device romanization with the dictionary, as today.
- mirage: same display options (original, romanized, both) for names and lyrics, fed by the extension. Without it,
  originals only for now; on-device romanization is a later question.

## Changes from the 2026-11-01 draft

1. `songLyrics` is advertised as versions `[1, 2]`. `enhanced` and `kind` belong to songLyrics v2, and clients send
   `enhanced=true` only to a server that advertises v2.
2. `kinds` applies with `enhanced=true` only. Without `enhanced`, the response stays songLyrics v1, unchanged.
3. Lyrics already in Latin script have no `pronunciation` entry, and `main` stands in for it when `kinds` asks for
   `pronunciation`.
4. `cueLine` comes "when the server has word timing", not "when synced": lyrics with line timing only stay line-level.
5. The `pronunciation` entry's `lang` values are listed.
6. With `latin`, albums' `title` and `album` fields change with `name`, and `getArtists` and `getIndexes` regroup
   their index by the romanized names.
7. `getMusicDirectory`'s `directory.name` gets `nameLatin` too.
8. Search's matching of romanized forms ignores case, accents and punctuation; each search word must fall inside a single
   word of a name, never across a space ("runika" doesn't find Yoru ni Kakeru).
9. A `romanization` value other than `both` or `latin` counts as `original`.
10. Siblings: titles are romanized with their artist and album, and lone artists without context (see Fields).
