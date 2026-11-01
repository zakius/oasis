import os

import pytest

from oasis import musicbrainz, relocate, scan
from oasis.db import connect
from test_importer import audio

NAMES = {"00000000-0000-0000-0000-00000000000a": "O.N.A.", "00000000-0000-0000-0000-00000000000b": "Beta"}


def tagged(path, artist, album, title, track, disc="1", date="1995-03-01", release="rel-1"):
    return audio(path, 1, albumartist="whatever", musicbrainz_albumartistid=artist, album=album, title=title, tracknumber=track,
                 discnumber=disc, originaldate=date, date="2001", musicbrainz_albumid=release)


@pytest.fixture
def library(tmp_path, monkeypatch):
    monkeypatch.setattr(musicbrainz, "get", lambda url, db=None, keep=0: {"name": NAMES[url.split("/")[-1].split("?")[0]]})
    root = tmp_path / "music"
    a, b = list(NAMES)
    tagged(root / "o.n.a/Modlishka (1995)/01 - Kiedy powiem sobie dość.flac", a, "Modlishka", "Kiedy powiem sobie dość", "1")
    tagged(root / "o.n.a/Modlishka (1995)/02 - Niepotrzebny.mp3", a, "Modlishka", "Niepotrzebny: znowu", "2/12")
    (root / "o.n.a/Modlishka (1995)/cover.jpg").write_bytes(b"jpg")
    for disc in ("1", "2"):
        tagged(root / f"Beta/Two (2000)/CD 0{disc}/01 - Song.flac", b, "Two", "Song", "1", disc=f"{disc}/2", date="2000", release="rel-2")
    (root / "Beta/Two (2000)/cover.png").write_bytes(b"png")
    audio(root / "Beta/Loose/untagged.flac", 1, title="No MusicBrainz")
    (root / "Beta/Loose/cover.jpg").write_bytes(b"jpg")
    taken = tagged(root / "Beta/Taken (2003)/01 - One.flac", b, "Taken", "One", "1", date="2003", release="rel-3")
    tagged(root / "Beta/2003 - Taken/01 - One.flac", b, "Taken", "One", "1", date="2003", release="rel-3")  # already in place
    tagged(root / ".replaced/o.n.a/Modlishka (1995)/02 - Niepotrzebny.mp3", a, "Modlishka", "Niepotrzebny", "2")
    db = connect(tmp_path / "oasis.db")
    scan.scan(db, [root])
    return root, db, taken


def ids(db):
    return {r["id"]: r["ref"] for r in db.execute("select id, ref from track")}


def test_a_dry_run_plans_and_checks_but_changes_nothing(library, tmp_path):
    root, db, _ = library
    files, before = sorted(root.rglob("*")), ids(db)
    result = relocate.run(db, root, tmp_path / "plan.tsv", apply=False)
    assert sorted(root.rglob("*")) == files and ids(db) == before
    lines = (tmp_path / "plan.tsv").read_text().splitlines()
    assert lines[0] == "old\tnew" and "o.n.a/Modlishka (1995)/cover.jpg\tO.N.A/1995 - Modlishka/cover.jpg" in lines
    assert len(result.moves) == 6 and result.in_place == 1


def test_files_move_to_their_musicbrainz_names_and_keep_their_ids_and_stats(library, tmp_path):
    root, db, taken = library
    one = db.execute("select id from track where ref like '%Kiedy%'").fetchone()[0]
    two = db.execute("select id from track where ref like '%Niepotrzebny%'").fetchone()[0]
    old_one, old_two = str(root / "o.n.a/Modlishka (1995)/01 - Kiedy powiem sobie dość.flac"), str(root / "o.n.a/Modlishka (1995)/02 - Niepotrzebny.mp3")
    db.execute("insert into stat(id, starred, play_count) values (?, 1, 3)", (one,))
    db.execute("insert into audio_pair values (?, ?, 1, 2, 1)", sorted((old_one, old_two)))
    db.execute("insert into audio_shared values (?, ?, 2, 1)", (old_two, old_one))
    result = relocate.run(db, root, root / ".oasis-renames.tsv", apply=True)

    album = root / "O.N.A/1995 - Modlishka"
    assert sorted(p.name for p in album.iterdir()) == ["01 - Kiedy powiem sobie dość.flac", "02 - Niepotrzebny - znowu.mp3", "cover.jpg"]
    assert sorted(p.name for p in (root / "Beta/2000 - Two").iterdir()) == ["1-01 - Song.flac", "2-01 - Song.flac", "cover.png"]
    assert not (root / "o.n.a").exists() and not (root / "Beta/Two (2000)").exists()  # emptied folders go
    assert (root / "Beta/Loose/untagged.flac").exists() and (root / "Beta/Loose/cover.jpg").exists()
    assert taken.exists() and "collision" in result.left[taken] and "no artist" in result.left[root / "Beta/Loose/untagged.flac"]
    assert (root / ".replaced/o.n.a/Modlishka (1995)/02 - Niepotrzebny.mp3").exists()

    assert db.execute("select ref, cover from track where id = ?", (one,)).fetchone()[0] == str(album / "01 - Kiedy powiem sobie dość.flac")
    assert db.execute("select starred, play_count from stat where id = ?", (one,)).fetchone()[:] == (1, 3)
    new_one, new_two = str(album / "01 - Kiedy powiem sobie dość.flac"), str(album / "02 - Niepotrzebny - znowu.mp3")
    assert db.execute("select a, b, level from audio_pair").fetchall()[0][:] == (*sorted((new_one, new_two)), 1)
    assert db.execute("select path, kept from audio_shared").fetchone()[:] == (new_two, new_one)
    assert db.execute("select count(*) from track where cover glob ?", (f"{root}/o.n.a/*",)).fetchone()[0] == 0
    assert all(os.path.exists(ref) for ref in ids(db).values())
    assert "o.n.a/Modlishka (1995)/02 - Niepotrzebny.mp3\tO.N.A/1995 - Modlishka/02 - Niepotrzebny - znowu.mp3" in \
           (root / ".oasis-renames.tsv").read_text().splitlines()

    # The next scan finds nothing new, and a file that changes later keeps its id.
    before = ids(db)
    assert scan.scan(db, [root]) == 0
    os.utime(new_one, (1, 1))
    scan.scan(db, [root])
    assert ids(db) == before
