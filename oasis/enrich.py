"""The background worker that reads what MusicBrainz knows about the library, one request at a time: each new
release's credits (credits.py), then each credited artist's other names and links (artists.py), then Latin names
(latin.py). Each step reads one item and says whether there was one; while MusicBrainz is away (503 when busy), the
worker waits longer each time."""
import logging
import threading

from . import artists, credits, latin, worker

log = logging.getLogger(__name__)

RETRY = 30, 600  # seconds before retrying after MusicBrainz errors: first, doubling up to the last
IDLE = 300       # seconds between looks for new releases when nothing woke the worker


class Enrichment:
    def __init__(self, connect):
        self.connect = connect
        self.wake = threading.Event()
        self.retry = RETRY[0]

    def start(self):
        worker.start("musicbrainz", self.fill, IDLE, self.wake)

    def fill(self) -> float:
        """Steps until all is done; the seconds to wait before looking again, longer each time MusicBrainz is away."""
        try:
            while self.step():
                pass
            self.unify()
        except OSError as e:
            log.warning("MusicBrainz: %s; again in %d s", e, self.retry)
            wait, self.retry = self.retry, min(self.retry * 2, RETRY[1])
            return wait
        return IDLE

    def unify(self):
        """Spellings of artist names that came in since: one artist each (artists.unify)."""
        db = self.connect()
        try:
            artists.unify(db)
        finally:
            db.close()

    def step(self) -> bool:
        """Runs the first step that has something to do; whether one had."""
        db = self.connect()
        try:
            done = credits.step(db) or artists.step(db) or latin.step(db)
            if done:
                self.retry = RETRY[0]
                db.commit()
            return done
        finally:
            db.close()
