import sqlite3
from datetime import timedelta

import pytest

from conftest import TORBOX_NOW, FakeTorBox, ago, torrent
from oasis.config import TorBox as TorBoxSettings
from oasis.db import connect
from oasis.doctor import Doctor
from oasis.download import tracked


@pytest.fixture
def rdt(tmp_path):
    path = tmp_path / "rdtclient.db"
    db = sqlite3.connect(path)
    db.executescript("create table Settings(SettingId, Value); create table Torrents(TorrentId, Hash, Completed, Category, RdName);"
                     "insert into Settings values ('Provider:ApiKey', 'tb-key');")
    db.commit()
    return db


@pytest.fixture
def torbox():
    return FakeTorBox()


@pytest.fixture
def now():
    return [TORBOX_NOW]


@pytest.fixture
def doctor(tmp_path, rdt, torbox, now):
    return Doctor(lambda: connect(tmp_path / "oasis.db"), TorBoxSettings(str(tmp_path / "rdtclient.db"), stall_hours=1), tracked,
                  torbox=lambda key: torbox if key == "tb-key" else None, clock=lambda: now[0])


def track(rdt, hash, category="sonarr", name="Show"):
    rdt.execute("insert into Torrents values (?, ?, null, ?, ?)", (hash, hash, category, name))
    rdt.commit()


def test_torbox_queued_items_are_deleted(doctor, torbox):
    torbox.waiting = [{"id": 70, "hash": "abc", "name": "Album"}]
    doctor.cycle()
    assert torbox.dropped == [70]


def test_unfinished_torrents_neither_rdtclient_nor_oasis_tracks_are_deleted(doctor, torbox, rdt, tmp_path):
    track(rdt, "tracked")
    connect(tmp_path / "oasis.db").execute("insert into download(hash, album_id, state, created, updated) values ('ours', 0, 'downloading', 0, 0)")
    torbox.list = [torrent(1, "untracked"), torrent(2, "young", created=ago(minutes=5)), torrent(3, "tracked"),
                   torrent(4, "finished", progress=1), torrent(5, "inactive", active=False), torrent(6, "OURS")]
    doctor.cycle()
    assert torbox.deleted == [1]


def test_dead_torrents_are_deleted_but_oasiss_own_are_left_to_it(doctor, torbox, rdt, tmp_path, now):
    for h in ("sonarr", "fresh", "moving", "seeded"):
        track(rdt, h)
    connect(tmp_path / "oasis.db").execute("insert into download(hash, album_id, state, created, updated) values ('ours', 0, 'downloading', 0, 0)")
    torbox.list = [torrent(1, "ours", progress=0, seeds=0), torrent(2, "sonarr", progress=0, seeds=0),
                   torrent(3, "fresh", progress=0, seeds=0, created=ago(minutes=30)),
                   torrent(4, "moving", progress=0.4, seeds=0), torrent(5, "seeded", progress=0, seeds=2)]
    doctor.cycle()
    assert torbox.deleted == [2]
    now[0] += timedelta(hours=2)  # no progress since
    doctor.cycle()
    assert torbox.deleted == [2, 2, 3, 4]  # and fresh is past the wait by now


def test_dry_run_changes_nothing_and_logs_each_action(doctor, torbox, rdt, caplog):
    doctor.dry = True
    torbox.waiting = [{"id": 70, "hash": "abc", "name": "Queued"}]
    torbox.list = [torrent(1, "untracked")]
    with caplog.at_level("INFO", "oasis.doctor"):
        doctor.cycle()
    assert torbox.deleted == torbox.dropped == []
    lines = [r.getMessage() for r in caplog.records]
    assert [m.split(" ")[1] for m in lines if m.startswith("[dry]")] == ["drop", "delete"]
    assert lines[-1] == "doctor: 1 TorBox torrents"
