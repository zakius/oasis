import shutil
import sqlite3
from pathlib import Path

from oasis import db as database

FIXTURES = Path(__file__).parent / "fixtures"


def older(path, commit, *rows):
    """A database made by the oasis of that commit (its SCHEMA, kept in fixtures/), holding rows: SQL statements."""
    legacy = sqlite3.connect(path)
    legacy.executescript((FIXTURES / f"schema-{commit}.sql").read_text())
    for row in rows:
        legacy.execute(row)
    legacy.commit()
    legacy.close()
    return path


def reopened(db, path):
    """The migrated database opened as a new server would: its file copied to a path not opened yet."""
    db.close()
    again = path.with_name(f"again-{path.name}")
    shutil.copy(path, again)
    return database.connect(again)


def test_database_from_an_older_oasis_gains_new_columns(tmp_path):
    path = older(tmp_path / "old.db", "18b9e5e", "insert into track(id, source, ref, title) values ('tr-1', 'local', '/a.mp3', 'Old')",
                 "insert into playlist(name, owner, created, changed) values ('Mine', 'admin', 0, 0)")
    db = database.connect(path)
    track = db.execute("select title, library, ytm_album from track").fetchone()
    assert tuple(track) == ("Old", 1, None)
    assert tuple(db.execute("select radio, played_pos, mix from playlist").fetchone()) == (0, -1, None)
    db.execute("insert into outbox(user, service, track_id, played, next_try) values ('admin', 'lastfm', 'tr-1', 1, 0)")


def test_stars_and_follows_keep_their_rows_under_the_new_table_name(tmp_path):
    path = older(tmp_path / "old.db", "c581826",
                 "insert into lidarr_item(kind, ref, state, mbid, lidarr_id, updated) values ('artist', 'ar-1', 'followed', 'mb-1', 7, 0)")
    db = database.connect(path)
    assert tuple(db.execute("select kind, ref, state, mbid from want_item").fetchone()) == ("artist", "ar-1", "followed", "mb-1")
    assert not db.execute("select 1 from sqlite_master where name = 'lidarr_item'").fetchone()
    assert "lidarr_id" not in {row[1] for row in db.execute("pragma table_info(want_item)")}


def test_global_stats_go_to_every_admin_once(tmp_path):
    path = older(tmp_path / "old.db", "8e15d09",
                 "insert into user(name, password, admin, created) values ('ann', 'pw', 1, 0), ('bob', 'pw', 0, 0), ('cat', 'pw', 1, 0)",
                 "insert into stat(id, starred, rating, play_count, played) values ('tr-1', 5, 4, 3, 6)")
    db = database.connect(path)
    assert [tuple(r) for r in db.execute("select * from stat order by user")] == [("ann", "tr-1", 5, 4, 3, 6), ("cat", "tr-1", 5, 4, 3, 6)]
    assert not db.execute("select 1 from sqlite_master where name = 'stat_global'").fetchone()
    assert len(reopened(db, path).execute("select * from stat").fetchall()) == 2  # once only


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
    assert [tuple(r) for r in reopened(db, path).execute("select user, id, play_count from stat")] == [("solo", "tr-1", 2)]


def test_albums_an_older_oasis_left_out_lose_their_rows(tmp_path):
    path = older(tmp_path / "old.db", "8e15d09",
                 "insert into want_album(rg, artist_mbid, title, reason, state) values ('g-1', 'mb-1', 'Kept', 'followed', 'missing'), "
                 "('g-2', 'mb-1', 'Hits', 'followed', 'skipped')")
    db = database.connect(path)
    assert [r[0] for r in db.execute("select rg from want_album")] == ["g-1"]
