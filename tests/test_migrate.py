import sqlite3
import subprocess

from oasis import db as database


def test_database_from_an_older_oasis_gains_new_columns(tmp_path):
    old = subprocess.run(["git", "show", "18b9e5e:oasis/db.py"], capture_output=True, text=True, check=True).stdout
    namespace = {}
    exec(compile(old, "old_db.py", "exec"), namespace)
    path = tmp_path / "old.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(namespace["SCHEMA"])
    legacy.execute("insert into track(id, source, ref, title) values ('tr-1', 'local', '/a.mp3', 'Old')")
    legacy.execute("insert into playlist(name, owner, created, changed) values ('Mine', 'admin', 0, 0)")
    legacy.commit()
    legacy.close()

    db = database.connect(path)
    track = db.execute("select title, library, ytm_album from track").fetchone()
    assert tuple(track) == ("Old", 1, None)
    assert tuple(db.execute("select radio, played_pos, mix from playlist").fetchone()) == (0, -1, None)
    db.execute("insert into outbox(user, service, track_id, played, next_try) values ('admin', 'lastfm', 'tr-1', 1, 0)")


def test_stars_and_follows_keep_their_rows_under_the_new_table_name(tmp_path):
    old = subprocess.run(["git", "show", "c581826:oasis/db.py"], capture_output=True, text=True, check=True).stdout
    namespace = {}
    exec(compile(old, "old_db.py", "exec"), namespace)
    path = tmp_path / "old.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(namespace["SCHEMA"])
    legacy.execute("insert into lidarr_item(kind, ref, state, mbid, lidarr_id, updated) values ('artist', 'ar-1', 'followed', 'mb-1', 7, 0)")
    legacy.commit()
    legacy.close()

    db = database.connect(path)
    assert tuple(db.execute("select kind, ref, state, mbid from want_item").fetchone()) == ("artist", "ar-1", "followed", "mb-1")
    assert not db.execute("select 1 from sqlite_master where name = 'lidarr_item'").fetchone()
    assert "lidarr_id" not in {row[1] for row in db.execute("pragma table_info(want_item)")}


def test_global_stats_go_to_every_admin_once(tmp_path):
    old = subprocess.run(["git", "show", "8e15d09:oasis/db.py"], capture_output=True, text=True, check=True).stdout
    namespace = {}
    exec(compile(old, "old_db.py", "exec"), namespace)
    path = tmp_path / "old.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(namespace["SCHEMA"])
    legacy.executemany("insert into user(name, password, admin, created) values (?, 'pw', ?, 0)", [("ann", 1), ("bob", 0), ("cat", 1)])
    legacy.execute("insert into stat(id, starred, rating, play_count, played) values ('tr-1', 5, 4, 3, 6)")
    legacy.commit()
    legacy.close()

    db = database.connect(path)
    assert [tuple(r) for r in db.execute("select * from stat order by user")] == [("ann", "tr-1", 5, 4, 3, 6), ("cat", "tr-1", 5, 4, 3, 6)]
    assert not db.execute("select 1 from sqlite_master where name = 'stat_global'").fetchone()
    database.migrated.clear()
    assert len(database.connect(path).execute("select * from stat").fetchall()) == 2  # once only


def test_global_stats_wait_for_a_user(tmp_path):
    path = tmp_path / "old.db"
    legacy = sqlite3.connect(path)
    legacy.execute("create table stat(id text primary key, starred real, rating int, play_count int not null default 0, played real)")
    legacy.execute("insert into stat(id, play_count) values ('tr-1', 2)")
    legacy.commit()
    legacy.close()

    db = database.connect(path)
    assert db.execute("select play_count from stat_global").fetchone()[0] == 2 and not db.execute("select * from stat").fetchall()
    db.execute("insert into user(name, password, created) values ('solo', 'pw', 0)")
    database.migrated.clear()
    assert [tuple(r) for r in database.connect(path).execute("select user, id, play_count from stat")] == [("solo", "tr-1", 2)]
