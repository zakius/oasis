import multiprocessing
import threading
import time

from starlette.testclient import TestClient

from oasis import tasks
from oasis.config import Config
from oasis.db import connect
from oasis.server import create_app
from oasis.worker import Job, Worker


def wait_for(check, seconds=10):
    end = time.monotonic() + seconds
    while not (found := check()):
        assert time.monotonic() < end, "timed out"
        time.sleep(0.01)
    return found


def test_a_task_is_claimed_by_one_worker_and_interactive_ones_first(tmp_path):
    first, second = connect(tmp_path / "oasis.db"), connect(tmp_path / "oasis.db")
    background = tasks.add(first, "work", {"n": 1})
    interactive = tasks.add(first, "work", {"n": 2}, priority=tasks.INTERACTIVE)
    assert tasks.claim(first, "a", ["work"])["id"] == interactive
    assert tasks.claim(second, "b", ["work"])["id"] == background
    assert tasks.claim(first, "a", ["work"]) is None
    assert tasks.claim(first, "a", ["other"]) is None


def test_a_task_with_the_key_of_one_waiting_is_that_one(tmp_path):
    db = connect(tmp_path / "oasis.db")
    id = tasks.add(db, "work", {}, "same")
    assert tasks.add(db, "work", {}, "same") == id
    tasks.done(db, tasks.claim(db, "a", ["work"]))
    assert tasks.add(db, "work", {}, "same") != id


def test_a_task_whose_worker_died_is_claimed_again_once_its_lease_lapses(tmp_path, monkeypatch):
    db = connect(tmp_path / "oasis.db")
    tasks.add(db, "work")
    tasks.beat(db, "dead", [])
    assert tasks.claim(db, "dead", ["work"])["attempts"] == 1
    assert tasks.claim(db, "other", ["work"]) is None  # its lease holds
    later = time.time() + tasks.LEASE + 1
    monkeypatch.setattr(time, "time", lambda: later)
    task = tasks.claim(db, "other", ["work"])
    assert (task["worker"], task["attempts"]) == ("other", 2)


def test_a_heartbeat_keeps_a_long_task_claimed(tmp_path, monkeypatch):
    db = connect(tmp_path / "oasis.db")
    tasks.add(db, "work")
    task = tasks.claim(db, "busy", ["work"])
    now = time.time() + tasks.LEASE - 1
    monkeypatch.setattr(time, "time", lambda: now)
    tasks.beat(db, "busy", [task["id"]])
    now += 2
    assert tasks.claim(db, "other", ["work"]) is None


def test_a_task_that_keeps_killing_its_worker_fails(tmp_path):
    db = connect(tmp_path / "oasis.db")
    id = tasks.add(db, "work")
    db.execute("update task set attempts = ? where id = ?", (tasks.ATTEMPTS, id))
    ran = []
    worker = Worker(lambda: connect(tmp_path / "oasis.db"), [], {"work": lambda: ran.append(1)})
    worker.execute(tasks.claim(db, worker.id, ["work"]))
    row = db.execute("select state, error from task where id = ?", (id,)).fetchone()
    assert row["state"] == "failed" and "stopped" in row["error"] and not ran


def test_a_worker_runs_tasks_and_hands_back_their_results_and_errors(tmp_path):
    db = connect(tmp_path / "oasis.db")

    def fail(why):
        raise ValueError(why)
    worker = Worker(lambda: connect(tmp_path / "oasis.db"), [], {"add": lambda a, b: a + b, "fail": fail})
    worker.start()
    try:
        added, failed = tasks.add(db, "add", {"a": 1, "b": 2}), tasks.add(db, "fail", {"why": "no"})
        done = lambda id: db.execute("select * from task where id = ? and state in ('done', 'failed')", (id,)).fetchone()
        assert wait_for(lambda: done(added))["result"] == "3"
        assert wait_for(lambda: done(failed))["error"] == '{"type": "ValueError", "message": "no"}'
    finally:
        worker.stop()


def count_runs(path, name, runs, seconds):
    """A worker process with one job, woken now and then by the test: each run is logged to runs, with its start and end."""
    def step():
        start = time.time()
        time.sleep(0.05)
        with open(runs, "a") as f:
            f.write(f"{start} {time.time()}\n")
    worker = Worker(lambda: connect(path), [Job(name, step, None)], {})
    threading.Timer(seconds, worker.stop).start()
    worker.run()


def test_a_periodic_job_runs_on_one_worker_at_a_time_across_processes(tmp_path):
    path, runs = tmp_path / "oasis.db", tmp_path / "runs"
    db = connect(path)
    tasks.schedule(db, [Job("job", None, None)])
    spawn = multiprocessing.get_context("spawn")
    workers = [spawn.Process(target=count_runs, args=(path, "job", runs, 3)) for _ in range(2)]
    for w in workers:
        w.start()
    wait_for(lambda: db.execute("select count(*) from worker").fetchone()[0] == 2)
    for _ in range(20):
        tasks.wake(db, "job")
        time.sleep(0.05)
    for w in workers:
        w.join()
    spans = sorted(tuple(map(float, line.split())) for line in runs.read_text().splitlines())
    assert len(spans) >= 2
    assert all(end <= next_start for (_, end), (next_start, _) in zip(spans, spans[1:]))  # never two at once


def test_a_lane_stays_with_the_worker_that_ran_it_while_it_lives(tmp_path):
    db = connect(tmp_path / "oasis.db")
    tasks.schedule(db, [Job("one", None, 60, "lane"), Job("two", None, 60, "lane")])
    tasks.beat(db, "a", [])
    tasks.beat(db, "b", [])
    first = tasks.claim(db, "a", ["one", "two"])
    assert tasks.claim(db, "b", ["one", "two"]) is None
    assert tasks.claim(db, "a", ["one", "two"])["kind"] != first["kind"]
    tasks.leave(db, "a")
    db.execute("update task set state = 'queued', due = 0")
    assert tasks.claim(db, "b", ["one", "two"])


def test_a_job_woken_while_it_runs_runs_again_after(tmp_path):
    db = connect(tmp_path / "oasis.db")
    tasks.schedule(db, [Job("job", None, 600)])
    task = tasks.claim(db, "a", ["job"])
    tasks.wake(db, "job")
    tasks.again(db, task, 600)
    assert tasks.claim(db, "a", ["job"])
    tasks.again(db, task, 600)
    assert tasks.claim(db, "a", ["job"]) is None


def serving(library, tmp_path, worker: bool):
    config = Config(library=[library], users={"admin": "secret"}, database=tmp_path / "oasis.db")
    return TestClient(create_app(config, worker=worker))


def test_serve_runs_a_worker_of_its_own(library, tmp_path):
    with serving(library, tmp_path, True):
        db = connect(tmp_path / "oasis.db")
        wait_for(lambda: tasks.alive(db))
        wait_for(lambda: db.execute("select 1 from track").fetchone())  # its scan at start


def test_serve_with_no_worker_leaves_the_jobs_to_worker_processes(library, tmp_path):
    with serving(library, tmp_path, False) as client:
        db = connect(tmp_path / "oasis.db")
        assert client.get("/rest/ping.view").status_code == 200
        time.sleep(0.5)
        assert not tasks.alive(db) and not db.execute("select 1 from track").fetchone()
        assert db.execute("select due from task where key = 'scan'").fetchone()[0]  # its scan at start, due
        worker = client.app.state.oasis.worker()
        worker.start()
        try:
            wait_for(lambda: db.execute("select 1 from track").fetchone())
        finally:
            worker.stop()
