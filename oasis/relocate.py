"""Moves the library's files to the names importer.py gives an import, "Artist/Year - Album/NN - Title.ext", by
their MusicBrainz tags: once, for the files placed under an older scheme. Files only move (os.rename), nothing is
deleted but folders left empty; a file that lacks the tags, or whose new name is taken, stays where it is. Other files
in an album's folder (covers) move with it. A track keeps its id: the database follows each move in the same
transaction, so stars, plays, playlists, credits and dedupe's verdicts stay. Without apply nothing changes on disk or
in the database; the plan is checked against the database and written out all the same."""
import logging
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import mutagen

from . import musicbrainz
from .importer import album_folder, track_file
from .scan import CONTENT_TYPES, REPLACED

log = logging.getLogger(__name__)

ARTIST = "https://musicbrainz.org/ws/2/artist/{}?fmt=json"
KEEP = 30 * 86400  # seconds an artist's name stays cached
MBID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@dataclass
class Plan:
    moves: dict[Path, Path] = field(default_factory=dict)  # old path -> new path, audio first
    left: dict[Path, str] = field(default_factory=dict)    # files that stay -> why
    in_place: int = 0


def numbers(value) -> tuple[int | None, int | None]:
    """'3/12' -> (3, 12); '3' -> (3, None)."""
    m = re.match(r"\s*(\d+)\s*(?:/\s*(\d+))?", str(value or ""))
    return (int(m[1]), int(m[2]) if m[2] else None) if m else (None, None)


def tags_of(path: Path) -> dict | str:
    """What a file's new name needs from its tags, or what it lacks."""
    audio = mutagen.File(path, easy=True)
    tags = audio.tags if audio is not None and audio.tags is not None else {}
    get = lambda name: str((tags.get(name) or [""])[0]).strip() if name in tags else ""
    artist = MBID.search(get("musicbrainz_albumartistid"))
    year = (get("originaldate") or get("originalyear") or get("date"))[:4]
    (track, tracks), (disc, discs) = numbers(get("tracknumber")), numbers(get("discnumber"))
    found = {"artist": artist and artist[0], "album": get("album"), "title": get("title"), "track": track,
             "year": year if year.isdigit() else None}
    if missing := [k for k, v in found.items() if not v]:
        return "no " + ", ".join(missing)
    return found | {"tracks": max(track, tracks or 0, numbers(get("totaltracks") or get("tracktotal"))[0] or 0), "disc": disc or 1,
                    "discs": max(disc or 1, discs or 0, numbers(get("totaldiscs") or get("disctotal"))[0] or 0),
                    "release": get("musicbrainz_albumid") or (artist[0], get("album"), year)}


def album_of(path: Path, root: Path) -> Path:
    """The folder a file's album has: Artist/Album, whatever lies below it (a disc's folder)."""
    parts = path.relative_to(root).parts
    return root.joinpath(*parts[:2]) if len(parts) > 2 else path.parent


def plan(db, root: Path) -> Plan:
    result, found, names, files = Plan(), {}, {}, []
    for directory, folders, names_here in os.walk(root):
        folders[:] = [f for f in folders if f != REPLACED]
        files += [Path(directory, f) for f in names_here if not f.startswith(".")]  # hidden: half-written files, this tool's mapping
    files.sort()
    audio = [p for p in files if p.suffix.lower().lstrip(".") in CONTENT_TYPES]
    for path in audio:
        try:
            info = tags_of(path)
        except Exception as e:
            info = f"unreadable: {e}"
        if isinstance(info, str):
            result.left[path] = info
            continue
        found[path] = info
        if info["artist"] not in names:
            try:
                names[info["artist"]] = musicbrainz.get(ARTIST.format(info["artist"]), db, KEEP)["name"]
            except (LookupError, OSError) as e:
                names[info["artist"]] = None
                log.warning("no MusicBrainz name for artist %s: %s", info["artist"], e)
        if names[info["artist"]] is None:
            del found[path]
            result.left[path] = f"MusicBrainz has no name for artist {info['artist']}"
    # A release's numbers are as wide as its widest, over every file of it the library has.
    widest = defaultdict(lambda: [0, 0])
    for info in found.values():
        w = widest[info["release"]]
        w[0], w[1] = max(w[0], info["discs"]), max(w[1], info["tracks"])
    targets, albums = {}, defaultdict(set)  # new path -> old; an old album folder -> its new ones
    for path, info in found.items():
        discs, tracks = widest[info["release"]]
        folder = root / album_folder(names[info["artist"]], info["year"], info["album"])
        target = folder / track_file(info["disc"], discs, info["track"], tracks, info["title"], path.suffix)
        albums[album_of(path, root)].add(folder)
        if target == path:
            result.in_place += 1
        elif target in targets:
            result.left[path] = f"collision: {targets[target].relative_to(root)} takes {target.relative_to(root)}"
        elif target.exists():
            result.left[path] = f"collision: {target.relative_to(root)} exists"
        else:
            targets[target] = path
            result.moves[path] = target
    split = {album_of(p, root) for p in result.left}
    for path in sorted(set(files) - set(audio)):
        folders = albums.get(album_of(path, root), set())
        if len(folders) != 1 or album_of(path, root) in split:
            result.left[path] = "some of its album's audio stays" if album_of(path, root) in split else \
                "no audio beside it" if not folders else "its album's audio moves to several folders"
            continue
        target = next(iter(folders)) / path.name
        if target == path:
            result.in_place += 1
        elif target in targets or target.exists():
            result.left[path] = f"collision: {target.relative_to(root)} is taken"
        else:
            targets[target] = path
            result.moves[path] = target
    return result


def repoint(db, old: str, new: str):
    """The database follows one file's move: its track (whose id stays), covers, and dedupe's records."""
    db.execute("update track set ref = ? where ref = ? and source = 'local'", (new, old))
    db.execute("update track set cover = ? where cover = ?", (new, old))
    db.execute("update audio_shared set path = ? where path = ?", (new, old))
    db.execute("update audio_shared set kept = ? where kept = ?", (new, old))
    for r in db.execute("select * from audio_pair where ? in (a, b)", (old,)).fetchall():
        (a, a_mtime), (b, b_mtime) = sorted([(new if r["a"] == old else r["a"], r["a_mtime"]), (new if r["b"] == old else r["b"], r["b_mtime"])])
        db.execute("delete from audio_pair where a = ? and b = ?", (r["a"], r["b"]))
        db.execute("insert or replace into audio_pair values (?, ?, ?, ?, ?)", (a, b, a_mtime, b_mtime, r["level"]))


def counts(db) -> dict:
    return {"tracks": db.execute("select count(*) from track where source = 'local'").fetchone()[0],
            "starred": db.execute("select count(*) from stat s join track t on t.id = s.id where s.starred is not null").fetchone()[0],
            "stats": db.execute("select count(*) from stat s join track t on t.id = s.id").fetchone()[0],
            "playlist entries": db.execute("select count(*) from playlist_entry p join track t on t.id = p.track_id").fetchone()[0],
            "credits": db.execute("select count(*) from credit c join track t on t.id = c.track_id").fetchone()[0],
            "dedupe pairs": db.execute("select count(*) from audio_pair").fetchone()[0],
            "shared": db.execute("select count(*) from audio_shared").fetchone()[0]}


def missing(db, exists) -> list[str]:
    """Local tracks and covers the database points at that aren't there."""
    return [r[0] for r in db.execute("select ref from track where source = 'local' union "
                                     "select cover from track where cover like '/%'") if not exists(r[0])]


def run(db, root: Path, out: Path, apply: bool) -> Plan:
    root = Path(root)
    result = plan(db, root)
    before = counts(db)
    if not apply:  # the moves' database changes, checked and rolled back
        db.execute("begin")
        for old, new in result.moves.items():
            repoint(db, str(old), str(new))
        after = counts(db)
        sources, targets = {str(p) for p in result.moves}, {str(p) for p in result.moves.values()}
        lost = missing(db, lambda p: p in targets or p not in sources and os.path.exists(p))
        db.execute("rollback")
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as tsv:
        tsv.write("old\tnew\n")  # relative to the library
        for old, new in result.moves.items():
            if apply:
                try:
                    move(db, old, new)
                except OSError as e:
                    result.left[old] = f"move failed: {e}"
                    continue
            tsv.write(f"{old.relative_to(root)}\t{new.relative_to(root)}\n")
            tsv.flush()
    if apply:
        for old in [p for p in result.moves if p not in result.left]:
            prune(old.parent, root)
        after, lost = counts(db), missing(db, os.path.exists)
    moved = [p for p in result.moves if p not in result.left]
    print(f"{'moved' if apply else 'would move'}: {sum(p.suffix.lower().lstrip('.') in CONTENT_TYPES for p in moved)} audio files, "
          f"{sum(p.suffix.lower().lstrip('.') not in CONTENT_TYPES for p in moved)} others; already in place: {result.in_place}; "
          f"staying: {len(result.left)}; mapping: {out}")
    for reason, n in sorted(count_reasons(result.left).items()):
        print(f"  {n} staying: {reason}")
    for path, reason in sorted(result.left.items()):
        print(f"  stays: {path.relative_to(root)}: {reason}")
    print(f"database before: {before}\ndatabase after:  {after}")
    print(f"database paths not on disk after the moves: {len(lost)}", *lost[:20], sep="\n  ")
    return result


def count_reasons(left: dict[Path, str]) -> dict[str, int]:
    found = defaultdict(int)
    for reason in left.values():
        found[reason.split(":")[0]] += 1
    return found


def move(db, old: Path, new: Path):
    """One file's move with its database rows, in one transaction; never over an existing file."""
    new.parent.mkdir(parents=True, exist_ok=True)
    if new.exists():
        raise FileExistsError(f"{new} exists")
    db.execute("begin")
    try:
        repoint(db, str(old), str(new))
        os.rename(old, new)
    except BaseException:
        db.execute("rollback")
        raise
    try:
        db.execute("commit")
    except BaseException:
        os.rename(new, old)
        raise


def prune(folder: Path, root: Path):
    """Removes folder and the folders above it while they are empty, up to the library."""
    while folder != root and root in folder.parents:
        try:
            folder.rmdir()
        except OSError:  # not empty
            return
        folder = folder.parent
