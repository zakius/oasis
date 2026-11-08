"""What oasis has found out about each library file's audio, in rows keyed by the file's path: dedupe.py's verdicts
on pairs of files (audio_pair) and the clones it made (audio_shared), and quality.py's findings (audio_quality). The
rows follow a file as it moves, has its tags rewritten or leaves the library; the modules that find these things out
read and write them here."""
import os
import time
from pathlib import Path

from .tracks import forget

LOSSY, UPSAMPLED, PADDED = "lossy", "upsampled", "padded"  # quality's verdicts, and borderline's kinds
RESAMPLED, CONVERTED = "resampled", "converted"            # what is done about upsampled and padded files


def retagged(db, path: str):
    """Tags of a file changed, its audio didn't: dedupe's verdicts and clone records for it stay valid."""
    mtime = os.path.getmtime(path)
    db.execute("update audio_shared set mtime = ? where path = ?", (mtime, path))
    db.execute("update audio_shared set kept_mtime = ? where kept = ?", (mtime, path))
    db.execute("update audio_pair set a_mtime = ? where a = ?", (mtime, path))
    db.execute("update audio_pair set b_mtime = ? where b = ?", (mtime, path))


def moved(db, old: str, new: str):
    """The records follow a file's move: dedupe's verdicts and clone records (as a clone or as the kept copy), and
    quality's findings."""
    db.execute("update audio_shared set path = ? where path = ?", (new, old))
    db.execute("update audio_shared set kept = ? where kept = ?", (new, old))
    for r in db.execute("select * from audio_pair where ? in (a, b)", (old,)).fetchall():
        (a, a_mtime), (b, b_mtime) = sorted([(new if r["a"] == old else r["a"], r["a_mtime"]), (new if r["b"] == old else r["b"], r["b_mtime"])])
        db.execute("delete from audio_pair where a = ? and b = ?", (r["a"], r["b"]))
        db.execute("insert or replace into audio_pair values (?, ?, ?, ?, ?)", (a, b, a_mtime, b_mtime, r["level"]))
    db.execute("update audio_quality set path = ? where path = ?", (new, old))


def gone(db, paths: list[str]):
    """Files no longer in the library: their tracks, with the rows about them alone, and every record of them here."""
    ids = []
    for path in paths:
        ids += [r[0] for r in db.execute("select id from track where source = 'local' and ref = ?", (path,))]
    forget(db, ids)
    rows = [(p,) for p in paths]
    for table, column in (("audio_shared", "path"), ("audio_shared", "kept"), ("audio_pair", "a"), ("audio_pair", "b"),
                          ("audio_quality", "path")):
        db.executemany(f"delete from {table} where {column} = ?", rows)


def pair_level(db, a: str, b: str, a_mtime: float, b_mtime: float) -> int | None:
    """dedupe's verdict on two files (a < b) as they are now: the level they match at, 0 for none; None when not
    compared since either changed."""
    found = db.execute("select level from audio_pair where a = ? and b = ? and a_mtime = ? and b_mtime = ?",
                       (a, b, a_mtime, b_mtime)).fetchone()
    return found and found["level"]


def store_pair(db, a: str, b: str, a_mtime: float, b_mtime: float, level: int):
    db.execute("insert or replace into audio_pair values (?, ?, ?, ?, ?)", (a, b, a_mtime, b_mtime, level))


def same_audio(db, path: str) -> list[str]:
    """Other files dedupe found to hold the same stream."""
    return [r[0] for r in db.execute("select b from audio_pair where a = ? and level > 0 union select a from audio_pair where b = ? and level > 0",
                                     (path, path))]


def clones(db, paths: list[str]) -> dict:
    """The clone records (path, kept, mtime, kept_mtime) of those of these files that are clones, by path."""
    return {r["path"]: r for r in db.execute(f"select * from audio_shared where path in ({','.join('?' * len(paths))})", paths)}


def store_clone(db, old: str, path: str, kept: str, mtime: float, kept_mtime: float):
    """old became path, a clone of kept."""
    db.execute("delete from audio_shared where path = ?", (old,))
    db.execute("insert or replace into audio_shared values (?, ?, ?, ?)", (path, kept, mtime, kept_mtime))


def shared_paths(db) -> set[str]:
    """The files that share their audio with another: the clones and the copies they were made of."""
    return {p for row in db.execute("select path, kept from audio_shared") for p in row}


def kept_copy(db, path: str) -> str | None:
    """The file a clone shares its audio with; None when path isn't one."""
    row = db.execute("select kept from audio_shared where path = ?", (path,)).fetchone()
    return row and row[0]


def savings(db) -> tuple[int, int]:
    """How many library files are clones, and the bytes they would take as files of their own."""
    return tuple(db.execute("select count(*), coalesce(sum(t.size), 0) from audio_shared a join track t on t.ref = a.path").fetchone())


def store_finding(db, path: Path, found: dict):
    """quality's finding on a file as it is now (its mtime)."""
    db.execute("insert or replace into audio_quality(path, mtime, checked, rate, bits, bits_used, low_share, cutoff, drop_db, sustained, "
               "verdict, borderline, source_rate, action, was, saved) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
               (str(path), path.stat().st_mtime, time.time(), found["rate"], found["bits"], found["bits_used"], found["low_share"],
                found["cutoff"], found["drop"], int(found["sustained"]), found["verdict"], found["borderline"], found["source_rate"],
                found.get("action"), found.get("was"), found.get("saved", 0)))


def checked(db) -> dict[str, float]:
    """The files quality has findings on, with their mtime when checked."""
    return dict(db.execute("select path, mtime from audio_quality").fetchall())


def with_verdict(db, verdict: str) -> set[str]:
    return {r[0] for r in db.execute("select path from audio_quality where verdict = ?", (verdict,))}


def findings(db, paths: list[str]) -> list:
    """The stored findings (audio_quality rows) of those of these files that were checked."""
    return db.execute(f"select * from audio_quality where path in ({','.join('?' * len(paths))})", paths).fetchall()
