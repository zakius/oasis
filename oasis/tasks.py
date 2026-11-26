"""The task queue in oasis's database, which the worker processes (worker.py) take their work from: the periodic jobs,
one row each that is due again after each run, and one-off tasks, some with someone waiting for their result.

A worker claims a task in one statement, so no two take the same one. The claim is a lease: the worker's heartbeat
renews it while the task runs, and a task whose worker died is claimed again once it lapses. The periodic jobs of a
lane stay with the worker that ran them while it lives, as they keep what they learn between runs in its memory."""
import json
import time

from .db import transaction

LEASE = 60        # seconds a claim holds unless its worker's heartbeat renews it
HEARTBEAT = 5     # seconds between a worker's heartbeats
ALIVE = 20        # seconds since its last heartbeat that a worker counts as alive
KEEP = 3600       # seconds a finished one-off task is kept, for whoever waits on it
ATTEMPTS = 3      # claims of a one-off task before it fails: one that kills its worker isn't run again and again
POLL = 0.002      # seconds between looks for a task's result, or for new tasks
INTERACTIVE, BACKGROUND = 1, 0
ERRORS = (TimeoutError, ValueError, LookupError, OSError)  # the errors a task's waiter gets as they were; others as RuntimeError

# Due, and not held by another live worker: a periodic job's lane is held by the worker that ran any job of it last.
CLAIMABLE = """kind in (select value from json_each(:kinds))
    and (state = 'queued' and due <= :now or state = 'running' and lease < :now)
    and (lane is null or not exists (select 1 from task o join worker w on w.id = o.worker
                                     where o.lane = task.lane and o.worker != :me and w.seen > :now - :alive))"""


def add(db, kind: str, args: dict | None = None, key: str | None = None, priority: int = BACKGROUND) -> int:
    """Queues a one-off task; its id. With key, a task of that key still queued or running is that task."""
    with transaction(db):
        if key and (row := db.execute("select id from task where key = ? and state in ('queued', 'running')", (key,)).fetchone()):
            return row[0]
        return db.execute("insert into task(kind, args, key, priority, due, created) values (?, ?, ?, ?, 0, ?)",
                          (kind, json.dumps(args or {}), key, priority, time.time())).lastrowid


def schedule(db, jobs):
    """Gives each periodic job (worker.Job) its row, due at once, unless it has one."""
    db.executemany("insert into task(kind, key, lane, due, created) select ?, ?, ?, ?, ? where not exists "
                   "(select 1 from task where key = ? and state in ('queued', 'running'))",
                   [(j.name, j.name, j.lane or j.name, 0 if j.every is not None else None, time.time(), j.name) for j in jobs])


def wake(db, job: str):
    """Makes the periodic job due now; one running now runs again once it ends."""
    db.execute("update task set due = ?, woken = 1 where key = ? and state in ('queued', 'running')", (time.time(), job))


def claim(db, me: str, kinds) -> dict | None:
    """Takes the next task this worker (me) can run, interactive ones first; None when none is due."""
    args = {"kinds": json.dumps(list(kinds)), "now": time.time(), "me": me, "alive": ALIVE}
    if not db.execute(f"select 1 from task where {CLAIMABLE} limit 1", args).fetchone():
        return None  # most looks find nothing: they don't take the write lock
    with transaction(db):
        row = db.execute(f"update task set state = 'running', worker = :me, lease = :now + {LEASE}, attempts = attempts + 1, woken = 0 "
                         f"where id = (select id from task where {CLAIMABLE} order by priority desc, due limit 1) returning *", args).fetchone()
    return dict(row) if row else None


def done(db, task: dict, result=None):
    db.execute("update task set state = 'done', result = ?, finished = ?, lease = null where id = ?", (json.dumps(result), time.time(), task["id"]))


def failed(db, task: dict, error: Exception):
    kind = next((e.__name__ for e in ERRORS if isinstance(error, e)), "RuntimeError")
    db.execute("update task set state = 'failed', error = ?, finished = ?, lease = null where id = ?",
               (json.dumps({"type": kind, "message": str(error)}), time.time(), task["id"]))


def again(db, task: dict, wait: float | None):
    """A periodic job's run ended: due in wait seconds (None: when woken), or at once when it was woken meanwhile."""
    now = time.time()
    db.execute("update task set state = 'queued', lease = null, finished = ?, due = case when woken then ? else ? end where id = ?",
               (now, now, None if wait is None else now + wait, task["id"]))


def beat(db, me: str, running: list[int]):
    """The worker's heartbeat: it is alive, and the claims of the tasks running in it hold on. Old finished tasks and
    long-gone workers go."""
    now = time.time()
    with transaction(db):
        db.execute("insert or replace into worker values (?, ?)", (me, now))
        db.execute("update task set lease = ? where worker = ? and state = 'running' and id in (select value from json_each(?))",
                   (now + LEASE, me, json.dumps(running)))
        db.execute("delete from task where state in ('done', 'failed') and finished < ?", (now - KEEP,))
        db.execute("delete from worker where seen < ?", (now - KEEP,))


def leave(db, me: str):
    """The worker stops: its periodic jobs' lanes are free at once."""
    db.execute("delete from worker where id = ?", (me,))


def alive(db) -> bool:
    """Whether a worker process runs."""
    return db.execute("select 1 from worker where seen > ?", (time.time() - ALIVE,)).fetchone() is not None


def running(db, job: str) -> bool:
    return db.execute("select 1 from task where key = ? and state = 'running'", (job,)).fetchone() is not None
