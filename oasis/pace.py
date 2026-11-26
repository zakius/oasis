"""The rate limits of the services oasis asks, shared by all its processes: the API and every worker ask MusicBrainz,
TorBox and Apple Music from one address, so they keep to one limit. Each service's next free turn is a row of the pace
table, taken in one transaction: a turn is `every` seconds after the one before, waited for (MusicBrainz, TorBox), or
up to `burst` turns are taken at once and none past those (Apple Music: a token bucket, GCRA). A service that pushed
back is held: no turns till then.

A process that has no database (tests, one-off commands) keeps its limits in memory."""
import sqlite3
import threading
import time
from contextlib import contextmanager

path = None  # oasis's database, once the server or a worker opened it (server.Oasis)
mine = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
mine.execute("create table pace(service text primary key, next real not null, held real not null)")
lock = threading.Lock()


@contextmanager
def store():
    """The pace table, in a transaction."""
    if path is None:
        with lock:
            yield mine
        return
    db = sqlite3.connect(path, timeout=30, isolation_level=None)
    try:
        db.execute("begin immediate")
        yield db
        db.execute("commit")
    finally:
        db.close()


def wait_turn(service: str, every: float):
    """Waits for the service's next turn, every seconds after the one taken before. A turn is taken when it comes, not
    ahead: one held meanwhile waits on."""
    while True:
        now = time.time()
        with store() as db:
            next, held = db.execute("select next, held from pace where service = ?", (service,)).fetchone() or (0.0, 0.0)
            if max(next, held) <= now:
                db.execute("insert or replace into pace values (?, ?, ?)", (service, now + every, held))
                return
        time.sleep(max(next, held) - now)


def take_turn(service: str, every: float, burst: int):
    """Takes one of the service's turns, up to burst of them at once and one every seconds after; OSError at once when
    none is free."""
    now = time.time()
    with store() as db:
        next, held = db.execute("select next, held from pace where service = ?", (service,)).fetchone() or (0.0, 0.0)
        if held > now or max(next, now) + every - now > burst * every:
            raise OSError(f"skipped: {service} takes {burst} requests in {burst * every:g} s")
        db.execute("insert or replace into pace values (?, ?, ?)", (service, max(next, now) + every, held))


def hold(service: str, seconds: float):
    """The service pushed back (busy, too many requests): no turns for seconds."""
    with store() as db:
        db.execute("insert into pace values (?, 0, ?) on conflict(service) do update set held = max(held, excluded.held)",
                   (service, time.time() + seconds))


def held(service: str) -> bool:
    with store() as db:
        row = db.execute("select held from pace where service = ?", (service,)).fetchone()
    return bool(row and row[0] > time.time())


def forget():
    """This process's own limits start afresh (tests)."""
    with lock:
        mine.execute("delete from pace")
