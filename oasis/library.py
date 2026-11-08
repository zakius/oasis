"""Upkeep of the library as a whole, from the command line or the server: work that spans the modules owning each
part (the scan, dedupe's merges, search's romanized names, quality's checks, acquisition's wants)."""
import threading

from . import acquire, dedupe, latin, quality, scan
from .tracks import move_track, track_id


def update(db, roots, merge_copies: bool):
    """Reads the files new or changed since the last scan; with merge_copies, dedupe's pass after it (files it replaced
    are read again, and a copy that took another format keeps its id); then the names search finds romanized."""
    scan.scan(db, roots)
    if merge_copies:
        merge(db, roots)
    latin.index(db)


def merge(db, roots):
    replaced = dedupe.run(db)
    if not replaced:
        return
    ids = dict(db.execute("select ref, id from track where source = 'local'").fetchall())  # the scan drops the old paths' rows
    scan.scan(db, roots)
    renamed = [(old, new) for old, new in replaced if old != new]  # took the kept copy's format, so a new path and id
    for old, new in renamed:
        move_track(db, ids.get(old, track_id(old)), track_id(new))
    db.commit()


class Scanner:
    """The server's scans, one at a time. scanned() is called after each, for the work that follows the files."""

    def __init__(self, connect, roots, merge_copies: bool, scanned=None):
        self.connect, self.roots, self.merge_copies, self.scanned = connect, roots, merge_copies, scanned
        self.scanning = self.again = False
        self.lock = threading.Lock()

    def scan_once(self, db):
        update(db, self.roots, self.merge_copies)
        if self.scanned:
            self.scanned()

    def start(self):
        """A scan in the background. One asked for while one runs follows it: files an import placed meanwhile may be
        past it."""
        if not self.lock.acquire(blocking=False):
            self.again = True
            return
        self.scanning = True

        def run():
            db = self.connect()
            try:
                while True:
                    self.again = False
                    self.scan_once(db)
                    if not self.again:
                        break
            finally:
                db.close()
                self.scanning = False
                self.lock.release()
        threading.Thread(target=run, daemon=True).start()

    def now(self, db):
        """A scan that is done on return, after the one running if any; one asked for meanwhile follows it."""
        with self.lock:
            self.again = False
            self.scan_once(db)
        if self.again:
            self.start()


def check_quality(db, roots, jobs: int = quality.JOBS, delete_lossy: bool = False) -> dict:
    """quality.backlog; every album it deleted files of is wanted again, and the library scanned."""
    result = quality.backlog(db, jobs, delete_lossy, acquire.want_again)
    if any(r["deleted"] for r in result["purged"]):
        scan.scan(db, roots)
    return result
