"""Reading and writing a file's tags through mutagen. Importing this registers Picard's names for the tags mutagen's
MP3 and M4A interfaces lack, so those files read and take the tags FLAC's Vorbis comments do. What write writes in a
format's own way (a track's totals, the label) is read back here too, along with other taggers' names for it. Credits
(composers, performers and the rest) are read from and added to files under Picard's names too."""
import base64
import json
import logging
import re
from pathlib import Path

import mutagen
from mutagen._vorbis import VCommentDict
from mutagen.easyid3 import EasyID3
from mutagen.easymp4 import EasyMP4Tags
from mutagen.flac import Picture
from mutagen.id3 import ID3, PictureType
from mutagen.mp4 import MP4Cover

log = logging.getLogger(__name__)

for key, name in {"artists": "ARTISTS", "releasetype": "MusicBrainz Album Type", "releasestatus": "MusicBrainz Album Status"}.items():
    EasyID3.RegisterTXXXKey(key, name)
EasyID3.RegisterTextKey("label", "TPUB")
for key, name in {"artists": "ARTISTS", "originaldate": "ORIGINALDATE", "media": "MEDIA", "label": "LABEL", "catalognumber": "CATALOGNUMBER",
                  "barcode": "BARCODE", "isrc": "ISRC", "releasetype": "MusicBrainz Album Type", "releasestatus": "MusicBrainz Album Status",
                  "musicbrainz_releasetrackid": "MusicBrainz Release Track Id", "musicbrainz_releasegroupid": "MusicBrainz Release Group Id"}.items():
    EasyMP4Tags.RegisterFreeformKey(key, name)

# Role -> tag written into files (Picard's names); performers go to PERFORMER as "Name (instrument)".
TAGS = {"composer": "COMPOSER", "lyricist": "LYRICIST", "writer": "WRITER", "arranger": "ARRANGER", "conductor": "CONDUCTOR",
        "producer": "PRODUCER", "mixer": "MIXER", "engineer": "ENGINEER", "remixer": "REMIXER", "performer": "PERFORMER"}
PERFORMER = re.compile(r"(.+?)\s*\(([^()]+)\)")  # "Name (instrument)", as write_tags writes PERFORMER


def first(tags, key: str) -> str | None:
    """A tag's first value, stripped; None when it is missing or blank. tags: a file opened with easy=True, its tags, or None."""
    value = tags.get(key) if tags else None
    return str(value[0]).strip() or None if value else None


def values(tags, key: str) -> list[str]:
    """All of a tag's values; [] when it is missing."""
    return [str(v) for v in (tags.get(key) or [])] if tags else []


def number(text, digits: int | None = None) -> int | None:
    """The number before a '/': '3/12' -> 3; None unless all of it is digits. With digits, only its first so many
    count, as a year leading a date: '2001-05-02' -> 2001."""
    found = str(text or "").split("/")[0].strip()[:digits]
    return int(found) if found.isdigit() else None


def numbers(text) -> tuple[int | None, int | None]:
    """'3/12' -> (3, 12); '3' -> (3, None)."""
    m = re.match(r"\s*(\d+)\s*(?:/\s*(\d+))?", str(text or ""))
    return (int(m[1]), int(m[2]) if m[2] else None) if m else (None, None)


def numbering(tags) -> dict[str, int | None]:
    """A track's track and disc number and their totals, as write writes them and other taggers do: ID3 and MP4 keep the
    totals in the numbers ('3/12'), Vorbis comments in totaltracks and totaldiscs or tracktotal and disctotal. The
    larger is taken when both say. A number that isn't there is None."""
    track, in_track = numbers(first(tags, "tracknumber"))
    disc, in_disc = numbers(first(tags, "discnumber"))
    tracks = numbers(first(tags, "totaltracks") or first(tags, "tracktotal"))[0]
    discs = numbers(first(tags, "totaldiscs") or first(tags, "disctotal"))[0]
    return {"track": track, "tracks": max(in_track or 0, tracks or 0) or None,
            "disc": disc, "discs": max(in_disc or 0, discs or 0) or None}


def position(track: int, tracks: int, disc: int, discs: int) -> dict[str, list[str]]:
    """The tags of a track's place on its album, which write needs among the tags it is given."""
    return {"tracknumber": [str(track)], "totaltracks": [str(tracks)], "discnumber": [str(disc)], "totaldiscs": [str(discs)]}


def labels(tags) -> list[str]:
    """The record labels: label, else organization, the name Vorbis comments had for it before Picard's."""
    return values(tags, "label") or values(tags, "organization")


def original_year(tags) -> str | None:
    """The year the album first came out: of originaldate (Picard's), originalyear (other taggers'), else of the date."""
    found = (first(tags, "originaldate") or first(tags, "originalyear") or first(tags, "date") or "")[:4]
    return found if found.isdigit() else None


def pictures(audio) -> list[Picture]:
    """The pictures a file (opened without easy) embeds, as FLAC pictures: FLAC's own, ID3's APIC frames, MP4's covr
    atoms and Vorbis comments' METADATA_BLOCK_PICTURE."""
    if audio is None:
        return []
    if getattr(audio, "pictures", None):
        return list(audio.pictures)
    if audio.tags is None:
        return []
    found = []
    if isinstance(audio.tags, ID3):
        for frame in audio.tags.getall("APIC"):
            picture = Picture()
            picture.data, picture.mime, picture.type, picture.desc = frame.data, frame.mime, frame.type, frame.desc
            found.append(picture)
        return found
    for cover in audio.tags.get("covr") or []:
        picture = Picture()
        picture.data, picture.type = bytes(cover), PictureType.COVER_FRONT
        picture.mime = "image/png" if cover.imageformat == MP4Cover.FORMAT_PNG else "image/jpeg"
        found.append(picture)
    for value in audio.tags.get("metadata_block_picture") or []:
        try:
            found.append(Picture(base64.b64decode(value)))
        except (ValueError, mutagen.MutagenError):  # a broken picture block: the file still plays and the rest is read
            continue
    return found


def write(path: Path, tags: dict[str, list[str]]):
    """Replaces these tags with the edition's (an empty one is removed: it described another release); others stay.
    The tags include a track's position(). ID3 and MP4 hold the totals in the track and disc numbers."""
    audio = mutagen.File(path, easy=True)
    if audio is None:
        return
    if audio.tags is None:
        audio.add_tags()
    if isinstance(audio.tags, ID3):  # WAV and AIFF: mutagen names no tags in their ID3, which takes only frames
        log.info("can't tag %s: no named tags for its ID3", path.name)
        return
    if isinstance(audio.tags, VCommentDict):  # the other names Vorbis comments use for the totals
        tags = {**tags, "tracktotal": tags["totaltracks"], "disctotal": tags["totaldiscs"]}
    else:
        tags = {k: v for k, v in tags.items() if k not in ("totaltracks", "totaldiscs")} | \
               {"tracknumber": [f"{tags['tracknumber'][0]}/{tags['totaltracks'][0]}"], "discnumber": [f"{tags['discnumber'][0]}/{tags['totaldiscs'][0]}"]}
    try:
        for name, given in tags.items():
            if given := [v for v in given if v]:
                audio[name] = given
            else:
                audio.pop(name, None)
    except ValueError as e:  # a value the format can't hold, such as a MusicBrainz id that isn't ASCII in ID3: the file stays as it came
        log.info("can't tag %s: %s", path.name, e)
        return
    audio.save()


def tag_credits(tags) -> list[dict]:
    """Credits from a file's own tags, for files MusicBrainz knows nothing about: as credits.py stores them, with no
    MusicBrainz artist; one per role, sub-role and name."""
    named = [("artist", name, i, None) for i, name in enumerate(values(tags, "artists") or values(tags, "artist"))]
    named += [("albumartist", name, i, None) for i, name in enumerate(values(tags, "albumartist"))]
    for role, tag in TAGS.items():
        if role != "performer":
            named += [(role, name, i, None) for i, name in enumerate(values(tags, tag.lower()))]
    for i, value in enumerate(values(tags, "performer")):
        if m := PERFORMER.fullmatch(value):
            named.append(("performer", m[1], i, m[2]))
        else:
            named.append(("performer", value, i, None))
    found, seen = [], set()
    for role, name, position, sub_role in named:
        if (role, sub_role, name.casefold()) in seen:
            continue
        seen.add((role, sub_role, name.casefold()))
        found.append({"role": role, "sub_role": sub_role, "name": name, "artist_name": name, "mbid": None, "artist_type": None,
                      "position": position, "joinphrase": None})
    return found


def write_tags(path: str, fields: dict, credits: list[dict]) -> bool:
    """Adds a track's credits and MusicBrainz fields (isrc, label, genres, original_date) where the file lacks them
    (Vorbis comments: FLAC, Ogg), never removing a value; returns whether it changed."""
    audio = mutagen.File(path)
    if audio is None or not isinstance(audio.tags, VCommentDict):
        return False
    tags = audio.tags
    by_role = {}
    for c in credits:
        by_role.setdefault(c["role"], []).append(c)
    wanted = {"ARTISTS": [c["name"] for c in by_role.get("artist", [])],
              "ISRC": (fields.get("isrc") or "").split("; ") if fields.get("isrc") else [],
              "LABEL": fields["label"].split("; ") if fields.get("label") else [],
              "GENRE": json.loads(fields.get("genres") or "[]"),
              "ORIGINALDATE": [fields["original_date"]] if fields.get("original_date") else []}
    for role, tag in TAGS.items():
        people = by_role.get(role, [])
        wanted[tag] = [f"{c['name']} ({c['sub_role']})" if role == "performer" and c["sub_role"] else c["name"] for c in people]
    changed = False
    for tag, given in wanted.items():
        given = list(dict.fromkeys(v for v in given if v))
        have = tags.get(tag) or []
        # Only adds: an empty tag is filled, and one holding nothing but values from here gains the rest; a tag with
        # anything else in it (someone's own edit) stays as it is.
        if set(have) < set(given):
            tags[tag] = [*have, *(v for v in given if v not in have)]
            changed = True
    if not changed:
        return False
    audio.save()
    return True
