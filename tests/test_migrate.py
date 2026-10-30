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
