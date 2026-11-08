import logging
import os
import sqlite3
import time
from pathlib import Path

import mutagen

from . import pcm
from .credits import store
from .tracks import adopt_local_copies, album_id, artist_id, name_id, track_id
from .formats import AUDIO, PCM, is_cover, suffix
from .relocate import repoint
from .tags import first, labels, number, pictures, tag_credits, values

log = logging.getLogger(__name__)


def folder_cover(directory: Path) -> str | None:
    for entry in sorted(directory.iterdir()):
        if is_cover(entry):
            return str(entry)
    return None


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
    kind = suffix(path)
    joined = lambda key: "; ".join(values(tags, key)) or None
    return {
        "id": track_id(str(path)), "source": "local", "ref": str(path), "root": str(root),
        "title": title, "artist": artist, "album_artist": album_artist, "album": album,
        "artist_id": name_id(album_artist), "album_id": album_id(album_artist, album),
        "track_no": number(first(tags, "tracknumber"), 4), "disc_no": number(first(tags, "discnumber"), 4),
        "year": number(first(tags, "date") or first(tags, "originaldate"), 4), "genre": first(tags, "genre"),
        "duration": round(length), "bitrate": round(bitrate / 1000), "size": stat.st_size,
        "suffix": kind, "content_type": AUDIO.get(kind, "application/octet-stream"),
        "cover": "embedded" if pictures(audio) else covers[path.parent],
        "mtime": stat.st_mtime, "created": time.time(),
        "mb_release": first(tags, "musicbrainz_albumid"), "mb_track": first(tags, "musicbrainz_releasetrackid"),
        "mb_recording": first(tags, "musicbrainz_trackid"), "mb_release_group": first(tags, "musicbrainz_releasegroupid"), "isrc": joined("isrc"), "label": "; ".join(labels(tags)) or None,
        "release_types": joined("releasetype"), "original_date": first(tags, "originaldate"),
        "_credits": tag_credits(tags), "_albumartist_mbids": tags.get("musicbrainz_albumartistid") if tags else None,
    }


def scan(db: sqlite3.Connection, roots: list[Path]) -> int:
    # A file keeps the id it was first scanned with when relocate.py renames it: stars, plays and playlists hang on it.
    known = {r["ref"]: r for r in db.execute("select ref, id, mtime from track where source = 'local'")}
    seen, covers, changed = set(), {}, 0
    for root in roots:
        for directory, folders, files in os.walk(root):
            for name in files:
                path = Path(directory, name)
                if suffix(path) not in AUDIO:
                    continue
                if suffix(path) in PCM and (new := pcm.convert(path)):  # a WAV slipped in: FLAC, keeping its id
                    repoint(db, str(path), str(new))
                    if old := known.pop(str(path), None):
                        known[str(new)] = old
                    path = new
                    db.commit()
                seen.add(str(path))
                if (had := known.get(str(path))) and had["mtime"] == path.stat().st_mtime:
                    continue
                try:
                    row = read(path, root, covers)
                except Exception as e:
                    log.warning("skipping %s: %s", path, e)
                    continue
                if row:
                    row["id"] = had["id"] if had else row["id"]
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
    # Only the tracks go. What is known of their files (dedupe's and quality's records, lyrics, credits) waits for them:
    # a library folder that is unmounted for a scan comes back whole.
    db.executemany("delete from track where ref = ?", [(ref,) for ref in gone])
    adopted = adopt_local_copies(db) if changed else 0
    log.info("scan: %d updated, %d removed, %d streams now local", changed, len(gone), adopted)
    return changed + len(gone)
