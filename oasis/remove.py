"""Albums taken out of the library. An admin removes one at once (Oasis.remove_album); a user who unwants an album here
that nobody else wants asks for its removal, and admins remove or keep it from the wanted page."""
import shutil
import time
from pathlib import Path

from . import acquire, audiorecords
from .formats import AUDIO, suffix


def files(db, rg: str) -> list[str]:
    """The album's files in the library."""
    return [r[0] for r in db.execute("select ref from track where source = 'local' and mb_release_group = ?", (rg,))]


def delete(db, rg: str, roots: list[Path]) -> int:
    """Deletes the album's files and forgets their tracks; the number of files."""
    paths = files(db, rg)
    for path in paths:
        Path(path).unlink(missing_ok=True)
    for folder in {Path(p).parent for p in paths}:
        tidy(folder, roots)
    audiorecords.gone(db, paths)
    keep(db, rg)
    return len(paths)


def tidy(folder: Path, roots: list[Path]):
    """An album's folder goes once no audio is left in it, with its cover, cue sheets and logs, and then its artist's
    folder if that is empty. A library folder itself always stays."""
    inside = lambda f: any(f != root and f.is_relative_to(root) for root in roots)
    if not (inside(folder) and folder.is_dir()) or any(p.is_file() and suffix(p) in AUDIO for p in folder.rglob("*")):
        return
    shutil.rmtree(folder)
    if inside(folder.parent) and not any(folder.parent.iterdir()):
        folder.parent.rmdir()


def ask(db, user: str, rg: str):
    db.execute("insert or replace into removal values (?, ?, ?)", (rg, user, time.time()))


def keep(db, rg: str):
    db.execute("delete from removal where rg = ?", (rg,))


def asked(db) -> list[dict]:
    """The removals users asked for, oldest first, each with its album's artist and title. One whose album somebody
    wants again, or that left the library, is dropped."""
    found = []
    for r in db.execute("select * from removal order by asked").fetchall():
        album = db.execute("select album_artist, album from track where source = 'local' and mb_release_group = ? limit 1", (r["rg"],)).fetchone()
        if album is None or acquire.wanted_by(db, "album", r["rg"]):
            keep(db, r["rg"])
            continue
        found.append({**r, "artist": album["album_artist"], "title": album["album"]})
    return found
