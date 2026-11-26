import pytest

from oasis import scan as scanning
from oasis.db import connect
from oasis.scan import scan

from conftest import make_track


@pytest.fixture
def albums(tmp_path):
    root = tmp_path / "music"
    for a in range(3):
        for n in range(3):
            make_track(root / f"Artist {a}/Album {a}/{n + 1}.flac", seconds=1, title=f"Song {a}{n}", artist=f"Artist {a}",
                       album=f"Album {a}", tracknumber=str(n + 1))
    return root


def tracks(db) -> list[tuple]:
    return db.execute("select id, ref, title, artist_id, album_id, track_no from track order by ref").fetchall()


def test_a_scan_stopped_midway_leaves_whole_albums_and_the_next_one_completes_it(albums, tmp_path, monkeypatch):
    monkeypatch.setattr(scanning, "WRITE_EVERY", 0)  # each folder written as soon as it is read
    real_read, reads = scanning.read, []

    def read_then_stop(*args):
        if len(reads) == 4:  # the second album's second file
            raise KeyboardInterrupt
        reads.append(args[0])
        return real_read(*args)
    monkeypatch.setattr(scanning, "read", read_then_stop)
    db = connect(tmp_path / "oasis.db")
    with pytest.raises(KeyboardInterrupt):
        scan(db, [albums])

    counts = db.execute("select album_id, count(*) from track group by album_id").fetchall()
    assert [n for _, n in counts] == [3]  # the first album whole, nothing of the second
    assert db.execute("select count(distinct track_id) from credit").fetchone()[0] == 3
    assert not db.in_transaction

    monkeypatch.setattr(scanning, "read", real_read)
    assert scan(db, [albums]) == 6
    whole = connect(tmp_path / "whole.db")
    scan(whole, [albums])
    assert tracks(db) == tracks(whole)
    assert len(tracks(db)) == 9
    credits = "select track_id, role, name, artist_id from credit order by track_id, role, position"
    assert db.execute(credits).fetchall() == whole.execute(credits).fetchall()


def test_a_scan_writes_in_batches_that_other_connections_see_as_it_goes(albums, tmp_path, monkeypatch):
    monkeypatch.setattr(scanning, "WRITE_EVERY", 0)
    db, other = connect(tmp_path / "oasis.db"), connect(tmp_path / "oasis.db")
    seen, real_write = [], scanning.write

    def write(db, rows):
        real_write(db, rows)
        seen.append(other.execute("select count(*) from track").fetchone()[0])
    monkeypatch.setattr(scanning, "write", write)
    scan(db, [albums])
    assert seen[:3] == [3, 6, 9]
