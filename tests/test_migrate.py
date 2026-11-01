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
