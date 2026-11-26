"""The workers: processes (`oasis worker`, or threads of `oasis serve`) that run what the task queue (tasks.py) holds.
Each runs the periodic jobs, every job on one worker at a time, and the one-off tasks, each task in a thread of its
own. A periodic run that fails is logged and the next one comes as usual."""
import json
import logging
import os
import socket
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable

from . import tasks

log = logging.getLogger(__name__)

THREADS = 32  # tasks a worker runs at once
ASK_WAIT = 120  # seconds an interactive task is waited for at most: each of its calls has its own, shorter timeout
CLAIM_WAIT = 2  # seconds an interactive task waits for a worker to take it before the process that asked runs it
here = ThreadPoolExecutor(THREADS, "task")  # interactive tasks run in the process that asked for them


@dataclass
class Job:
    """A periodic job: step() runs once, then again what it returned in seconds later, or every seconds when it
    returned None or failed; every None: only when woken (tasks.wake). Jobs of one lane share memory: one worker
    process runs them all."""
    name: str
    step: Callable
    every: float | None
    lane: str | None = None


class Worker:
    def __init__(self, connect, jobs: list[Job], kinds: dict[str, Callable], threads: int = THREADS):
        """kinds: the one-off tasks' functions by kind, each called with the task's arguments and returning its
        result (JSON)."""
        self.connect, self.jobs, self.kinds, self.threads = connect, {j.name: j for j in jobs}, kinds, threads
        self.id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.running: set[int] = set()  # the ids of the tasks running here, whose claims the heartbeat renews
        self.ended = threading.Event()  # a task ended: room for another
        self.stopped = threading.Event()
        self.thread = None

    def start(self):
        """Runs in threads of this process; alive on return."""
        self.begin()
        self.thread = threading.Thread(target=self.claim, name="worker", daemon=True)
        self.thread.start()

    def stop(self):
        """Claims no more tasks; those running go on."""
        self.stopped.set()
        if self.thread:
            self.thread.join()

    def run(self):
        self.begin()
        self.claim()

    def begin(self):
        """The periodic jobs' rows, and the heartbeat."""
        db = self.connect()
        try:
            tasks.schedule(db, self.jobs.values())
            tasks.beat(db, self.id, [])
        finally:
            db.close()
        threading.Thread(target=self.beat, name="heartbeat", daemon=True).start()
        log.info("worker %s runs %d periodic jobs and %d kinds of tasks", self.id, len(self.jobs), len(self.kinds))

    def claim(self):
        """Claims tasks and runs each in a thread, till stopped."""
        db = self.connect()
        kinds = [*self.jobs, *self.kinds]
        try:
            while not self.stopped.is_set():
                try:
                    task = tasks.claim(db, self.id, kinds) if len(self.running) < self.threads else None
                except sqlite3.OperationalError as e:  # locked for longer than the connection waits
                    log.warning("no task claimed: %s", e)
                    task = None
                if task:
                    self.running.add(task["id"])
                    threading.Thread(target=self.execute, args=(task,), name=task["kind"], daemon=True).start()
                else:
                    self.idle(db)
        finally:
            tasks.leave(db, self.id)
            db.close()

    def idle(self, db):
        """Waits for something to claim: another connection's commit (a task added or woken), one of this worker's
        tasks ending, or a second passing, for periodic jobs coming due."""
        seen, until = db.execute("pragma data_version").fetchone()[0], time.monotonic() + 1
        while time.monotonic() < until and not self.stopped.is_set():
            if self.ended.wait(tasks.POLL):
                self.ended.clear()
                return
            if db.execute("pragma data_version").fetchone()[0] != seen:
                return

    def beat(self):
        db = self.connect()
        try:
            while not self.stopped.wait(tasks.HEARTBEAT):
                try:
                    tasks.beat(db, self.id, list(self.running))
                except Exception:
                    log.exception("heartbeat failed")
        finally:
            db.close()

    def execute(self, task: dict):
        db = self.connect()
        try:
            if job := self.jobs.get(task["kind"]):
                wait = job.every
                try:
                    found = job.step()
                    if found is not None:
                        wait = found
                except Exception:
                    log.exception("%s failed", job.name)
                tasks.again(db, task, wait)
            elif task["attempts"] > tasks.ATTEMPTS:
                tasks.failed(db, task, RuntimeError(f"its worker stopped {tasks.ATTEMPTS} times"))
            else:
                try:
                    tasks.done(db, task, self.kinds[task["kind"]](**json.loads(task["args"])))
                except Exception as e:
                    if not isinstance(e, tasks.ERRORS):
                        log.exception("%s failed", task["kind"])
                    tasks.failed(db, task, e)
        finally:
            db.close()
            self.running.discard(task["id"])
            self.ended.set()


class Answer:
    """The result of an interactive task to come. A worker runs it; or this process does, when no worker is alive or
    none takes it within CLAIM_WAIT, as all of oasis runs in one process when there is no worker."""

    def __init__(self, connect, kind: str, args: dict, run: Callable, key: str | None = None):
        """run: the task's function, for running it here."""
        self.connect, self.args, self.run = connect, args, run
        db = connect()
        try:
            self.id = tasks.add(db, kind, args, key, tasks.INTERACTIVE) if tasks.alive(db) else None
        finally:
            db.close()
        self.asked, self.future = time.monotonic(), None if self.id else here.submit(run, **args)

    def result(self, timeout: float):
        """The task's result, or the error it raised; TimeoutError when it isn't done in timeout seconds."""
        end = time.monotonic() + timeout
        if self.future is None:
            db = self.connect()
            try:
                while not (row := db.execute("select * from task where id = ?", (self.id,)).fetchone()) or row["state"] not in ("done", "failed"):
                    if not row or row["state"] == "queued" and time.monotonic() > self.asked + CLAIM_WAIT and \
                            db.execute("delete from task where id = ? and state = 'queued'", (self.id,)).rowcount:
                        log.info("no worker took a %s task: run here", row and row["kind"])
                        self.future = here.submit(self.run, **self.args)
                        break
                    if time.monotonic() > end:
                        raise TimeoutError(f"{row['kind']} took over {timeout} s")
                    time.sleep(tasks.POLL)
            finally:
                db.close()
        if self.future is not None:
            return self.future.result(max(0.0, end - time.monotonic()))
        if row["state"] == "failed":
            error = json.loads(row["error"])
            raise {e.__name__: e for e in tasks.ERRORS}.get(error["type"], RuntimeError)(error["message"])
        return json.loads(row["result"])
