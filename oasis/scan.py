import logging
import os
import sqlite3
import time
from pathlib import Path

import mutagen
from mutagen.easyid3 import EasyID3
from mutagen.easymp4 import EasyMP4Tags

from .credits import store, tag_credits
from .db import adopt_local_copies, artist_id, make_id

log = logging.getLogger(__name__)

# Picard's frames for the tags read below that mutagen's MP3 and M4A interfaces lack, so those files carry what FLAC does.
for key, name in {"artists": "ARTISTS", "releasetype": "MusicBrainz Album Type", "releasestatus": "MusicBrainz Album Status"}.items():
    EasyID3.RegisterTXXXKey(key, name)
EasyID3.RegisterTextKey("label", "TPUB")
for key, name in {"artists": "ARTISTS", "originaldate": "ORIGINALDATE", "media": "MEDIA", "label": "LABEL", "catalognumber": "CATALOGNUMBER",
                  "barcode": "BARCODE", "isrc": "ISRC", "releasetype": "MusicBrainz Album Type", "releasestatus": "MusicBrainz Album Status",
                  "musicbrainz_releasetrackid": "MusicBrainz Release Track Id", "musicbrainz_releasegroupid": "MusicBrainz Release Group Id"}.items():
    EasyMP4Tags.RegisterFreeformKey(key, name)

CONTENT_TYPES = {
    "mp3": "audio/mpeg", "flac": "audio/flac", "ogg": "audio/ogg", "oga": "audio/ogg", "opus": "audio/ogg",
    "m4a": "audio/mp4", "aac": "audio/aac", "wav": "audio/wav", "wv": "audio/x-wavpack",
    "ape": "audio/x-ape", "mpc": "audio/x-musepack", "aiff": "audio/aiff", "aif": "audio/aiff",
}
REPLACED = ".replaced"  # library files a better copy replaced, moved aside (see importer.py)
COVER_NAMES = ("cover", "folder", "front", "album")
COVER_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")


def first(tags, key: str) -> str | None:
    value = tags.get(key) if tags else None
    return str(value[0]).strip() or None if value else None


def number(text: str | None) -> int | None:
    digits = (text or "").split("/")[0].strip()[:4]
    return int(digits) if digits.isdigit() else None


def folder_cover(directory: Path) -> str | None:
    for entry in sorted(directory.iterdir()):
        if entry.suffix.lower() in COVER_SUFFIXES and entry.stem.lower() in COVER_NAMES:
            return str(entry)
    return None


def has_embedded_cover(audio) -> bool:
    if getattr(audio, "pictures", None):
        return True
    tags = audio.tags or {}
    return any(k.startswith("APIC") or k.lower() in ("covr", "metadata_block_picture") for k in tags.keys())


def read(path: Path, root: Path, covers: dict[Path, str | None]) -> dict | None:
    audio = mutagen.File(path)
    if audio is None:
        return None
    easy = mutagen.File(path, easy=True)
    tags = easy.tags if easy else None
    title = first(tags, "title") or path.stem
    artist = first(tags, "artist") or "Unknown Artist"
    album_artist = first(tags, "albumartist") or artist
    album = first(tags, "album") or path.parent.name
    if path.parent not in covers:
        covers[path.parent] = folder_cover(path.parent)
    stat = path.stat()
    length = getattr(audio.info, "length", 0) or 0
    bitrate = getattr(audio.info, "bitrate", 0) or (stat.st_size * 8 / length if length else 0)
    suffix = path.suffix.lower().lstrip(".")
    joined = lambda key: "; ".join(str(v) for v in tags.get(key) or []) or None if tags is not None and key in tags else None
    return {
        "id": make_id("tr-", str(path)), "source": "local", "ref": str(path), "root": str(root),
        "title": title, "artist": artist, "album_artist": album_artist, "album": album,
        "artist_id": make_id("ar-", album_artist), "album_id": make_id("al-", album_artist, album),
        "track_no": number(first(tags, "tracknumber")), "disc_no": number(first(tags, "discnumber")),
        "year": number(first(tags, "date") or first(tags, "originaldate")), "genre": first(tags, "genre"),
        "duration": round(length), "bitrate": round(bitrate / 1000), "size": stat.st_size,
        "suffix": suffix, "content_type": CONTENT_TYPES.get(suffix, "application/octet-stream"),
        "cover": "embedded" if has_embedded_cover(audio) else covers[path.parent],
        "mtime": stat.st_mtime, "created": time.time(),
        "mb_release": first(tags, "musicbrainz_albumid"), "mb_track": first(tags, "musicbrainz_releasetrackid"),
        "mb_recording": first(tags, "musicbrainz_trackid"), "mb_release_group": first(tags, "musicbrainz_releasegroupid"), "isrc": joined("isrc"), "label": joined("label") or joined("organization"),
        "release_types": joined("releasetype"), "original_date": first(tags, "originaldate"),
        "_credits": tag_credits(tags), "_albumartist_mbids": tags.get("musicbrainz_albumartistid") if tags else None,
    }


def scan(db: sqlite3.Connection, roots: list[Path]) -> int:
    known = {r["ref"]: r["mtime"] for r in db.execute("select ref, mtime from track where source = 'local'")}
    seen, covers, changed = set(), {}, 0
    for root in roots:
        for directory, folders, files in os.walk(root):
            folders[:] = [f for f in folders if f != REPLACED]
            for name in files:
                path = Path(directory, name)
                if path.suffix.lower().lstrip(".") not in CONTENT_TYPES:
                    continue
                seen.add(str(path))
                if known.get(str(path)) == path.stat().st_mtime:
                    continue
                try:
                    row = read(path, root, covers)
                except Exception as e:
                    log.warning("skipping %s: %s", path, e)
                    continue
                if row:
                    credits, mbids = row.pop("_credits"), row.pop("_albumartist_mbids") or []
                    row["artist_id"] = artist_id(db, row["album_artist"], mbids[0] if len(mbids) == 1 else None)
                    columns = ", ".join(row)
                    updates = ", ".join(f"{k} = excluded.{k}" for k in row if k not in ("id", "created"))
                    # MusicBrainz credits stay while the file is still the same release track.
                    db.execute(f"insert into track({columns}) values ({', '.join('?' * len(row))}) "
                               f"on conflict(id) do update set {updates}, credits_at = case when track.mb_track is excluded.mb_track "
                               "and track.mb_release is excluded.mb_release then track.credits_at end", list(row.values()))
                    store(db, row["id"], credits, "tags")
                    changed += 1
    gone = [ref for ref in known if ref not in seen]
    db.executemany("delete from track where ref = ?", [(ref,) for ref in gone])
    adopted = adopt_local_copies(db) if changed else 0
    log.info("scan: %d updated, %d removed, %d streams now local", changed, len(gone), adopted)
    return changed + len(gone)
