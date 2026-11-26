import multiprocessing
import time

import pytest

from oasis import pace
from oasis.db import connect


def take_turns(path, every, count, out):
    """A process of its own taking turns of one service, each logged to out when taken."""
    pace.path = path
    for _ in range(count):
        pace.wait_turn("service", every)
        with open(out, "a") as f:
            f.write(f"{time.time()}\n")


def take_bursts(path, out):
    pace.path = path
    taken = 0
    for _ in range(10):
        try:
            pace.take_turn("burst", 10, 5)
            taken += 1
        except OSError:
            pass
    with open(out, "a") as f:
        f.write(f"{taken}\n")


def in_processes(target, *args):
    spawn = multiprocessing.get_context("spawn")
    processes = [spawn.Process(target=target, args=args) for _ in range(2)]
    for p in processes:
        p.start()
    for p in processes:
        p.join()


def test_two_processes_keep_to_one_pace(tmp_path):
    path, out = tmp_path / "oasis.db", tmp_path / "turns"
    connect(path).close()
    in_processes(take_turns, path, 0.2, 4, out)
    turns = sorted(float(t) for t in out.read_text().split())
    assert len(turns) == 8 and min(b - a for a, b in zip(turns, turns[1:])) > 0.18


def test_two_processes_share_a_burst(tmp_path):
    path, out = tmp_path / "oasis.db", tmp_path / "taken"
    connect(path).close()
    in_processes(take_bursts, path, out)
    assert sum(int(n) for n in out.read_text().split()) == 5


def test_a_service_held_by_one_process_holds_every_process(tmp_path):
    path, out = tmp_path / "oasis.db", tmp_path / "turns"
    connect(path).close()
    pace.path = path
    pace.hold("service", 1.5)
    start = time.time()
    in_processes(take_turns, path, 0.1, 1, out)
    assert min(float(t) for t in out.read_text().split()) - start > 1.4
    with pytest.raises(OSError, match="skipped"):
        pace.hold("burst", 60)
        pace.take_turn("burst", 1, 5)
