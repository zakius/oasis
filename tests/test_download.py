import time

import pytest

from oasis.db import connect
from oasis.doctor import Doctor
from oasis import download
from oasis.download import Downloads, choose
from oasis.lidarr import Library
from test_doctor import FakeLidarr, FakeTorBox, proposal, torrent


class TorBox(FakeTorBox):
    def __init__(self, tmp_path):
        super().__init__()
        self.tmp, self.added, self.full, self.links = tmp_path, [], False, []

    def add(self, name, magnet=None, torrent=None):
        self.added.append((name, magnet, torrent))
        hash = magnet.split("btih:")[1][:4] if magnet else "file"
        if self.full:
            self.waiting.append({"id": 90, "hash": hash})
            return {"queued_id": 90, "hash": hash}
        return {"torrent_id": 70 + len(self.added), "hash": hash.upper()}

    def link(self, torrent_id, file_id):
        self.links.append(file_id)
        return (self.tmp / "cdn" / str(file_id)).as_uri()


def tb(id, hash, **extra):
    return {"download_state": "downloading", "download_finished": False, "download_present": False, **torrent(id, hash, **extra)}


def offer(guid, hash, weight, *rejections, **extra):
    return {"guid": guid, "indexerId": 1, "title": f"Artist - Album {guid}", "infoHash": hash, "qualityWeight": weight, "size": 300e6,
            "rejections": list(rejections), "quality": {"quality": {"name": "FLAC"}}, "protocol": "torrent", **extra}


def files(tmp_path, *names):
    (tmp_path / "cdn").mkdir(exist_ok=True)
    for id, name in enumerate(names):
        (tmp_path / "cdn" / str(id)).write_bytes(name.encode() * (id + 1))
    return [{"id": id, "name": name, "size": len(name.encode()) * (id + 1)} for id, name in enumerate(names)]


@pytest.fixture
def db(tmp_path):
    return connect(tmp_path / "oasis.db")


@pytest.fixture
def lidarr():
    fake = FakeLidarr()
    fake.albums[5] = {"id": 5, "artistId": 9, "title": "Album", "foreignAlbumId": "mb-album", "artist": {"artistName": "Artist"},
                      "releases": [], "statistics": {"trackFileCount": 0, "trackCount": 2}}
    return fake


@pytest.fixture
def torbox(tmp_path):
    return TorBox(tmp_path)


@pytest.fixture
def downloads(tmp_path, lidarr, torbox, monkeypatch):
    monkeypatch.setattr(download, "PACE", 0)
    made = Downloads(lambda: connect(tmp_path / "oasis.db"), lidarr, Doctor(None, lidarr, "unused"), str(tmp_path / "dl"), stall_hours=1)
    made.client = torbox
    return made


def test_cached_releases_win_at_equal_or_better_quality_whatever_their_seeders():
    releases = [offer("best", "aa", 10), offer("worse", "bb", 5), offer("seedless", "cc", 10, "Not enough seeders: 0"),
                offer("wrong", "dd", 20, "Wrong format"), offer("blocked", "ee", 10)]
    picks = choose(releases, "Artist", "Album", {"ee"}, lambda hashes: {"bb", "cc", "dd", "ee"} & set(hashes))
    assert [r["guid"] for r in picks] == ["seedless", "best", "worse"]
    # The best accepted release is cached itself: no lesser cached one jumps it.
    picks = choose(releases[:3], "Artist", "Album", set(), lambda hashes: {"aa", "bb"})
    assert [r["guid"] for r in picks] == ["best", "worse"]
    # A seedless release nobody has cached isn't eligible, and doesn't raise the bar for the cached ones.
    picks = choose([offer("dead", "ff", 20, "Not enough seeders: 0"), *releases[:2]], "Artist", "Album", set(), lambda hashes: {"bb"})
    assert [r["guid"] for r in picks] == ["best", "worse"]


def test_an_unparsed_match_comes_last_and_blocked_guids_are_skipped():
    releases = [offer("odd", "", 1, "Unable to parse release"), offer("g1", None, 10), offer("ok", "aa", 10)]
    picks = choose(releases, "Artist", "Album", {"g1"}, lambda hashes: set())
    assert [r["guid"] for r in picks] == ["ok", "odd"]


def test_a_search_sends_the_pick_to_torbox_and_the_files_are_fetched_and_imported(downloads, lidarr, torbox, db, tmp_path):
    lidarr.offers[5] = [offer("a", "abcd", 10, magnetUrl="magnet:?xt=urn:btih:abcd&dn=x")]
    assert downloads.search(db, 5)
    assert torbox.added == [("Artist - Album a", "magnet:?xt=urn:btih:abcd&dn=x", None)]
    row = db.execute("select * from download").fetchone()
    assert (row["hash"], row["album_id"], row["album_mbid"], row["torbox_id"], row["state"]) == ("abcd", 5, "mb-album", 71, "added")
    assert downloads.albums(db) == {5}

    torbox.list = [tb(71, "abcd", progress=0.5)]
    downloads.cycle()
    assert db.execute("select state from download").fetchone()[0] == "downloading"
    torbox.list = [{**tb(71, "abcd", progress=1), "download_state": "checking", "download_finished": True, "download_present": True, "files": []}]
    downloads.cycle()
    assert db.execute("select state from download").fetchone()[0] == "downloading"

    folder = tmp_path / "dl" / "abcd"
    lidarr.proposed[str(folder)] = [proposal(str(folder / "Album/01.flac"), tracks=[1]), proposal(str(folder / "Album/02.flac"), tracks=[2])]
    torbox.list = [{**tb(71, "abcd", progress=1, active=False), "download_state": "completed", "download_finished": True,
                    "download_present": True, "files": files(tmp_path, "Album/01.flac", "Album/02.flac", "Album/rip.log", "Album/cover.jpg")}]
    downloads.cycle()
    assert sorted(torbox.links) == [0, 1, 3]  # no log
    assert [f["path"] for f in lidarr.imported[0]] == [str(folder / "Album/01.flac"), str(folder / "Album/02.flac")]
    assert lidarr.mode == "move" and lidarr.imported[0][0]["downloadId"] is None
    assert db.execute("select state from download").fetchone()[0] == "done"
    assert torbox.deleted == [71] and not folder.exists() and downloads.albums(db) == set()


def test_two_same_named_files_in_different_folders_both_arrive_and_a_restart_mid_fetch_resumes(downloads, lidarr, torbox, db, tmp_path):
    lidarr.offers[5] = [offer("a", "abcd", 10)]
    downloads.search(db, 5)
    names = ["Album/CD1/01 Intro.flac", "Album/CD2/01 Intro.flac", "Album/CD1/folder.jpg", "Album/CD2/folder.jpg"]
    torbox.list = [{**tb(71, "abcd", progress=1, active=False), "download_state": "completed", "download_finished": True,
                    "download_present": True, "files": files(tmp_path, *names)}]
    folder = tmp_path / "dl" / "abcd"
    (folder / "Album/CD1").mkdir(parents=True)
    (folder / "Album/CD2").mkdir()
    (folder / names[0]).write_bytes((tmp_path / "cdn" / "0").read_bytes())  # fetched before the restart
    (folder / names[1]).write_bytes(b"cut short")
    db.execute("update download set state = 'fetching'")
    lidarr.imports_land = False  # import fails: the files stay for inspection
    downloads.cycle()
    assert sorted(torbox.links) == [1, 2, 3]
    assert [(folder / n).read_bytes() for n in names] == [(tmp_path / "cdn" / str(i)).read_bytes() for i in range(4)]
    row = db.execute("select state, error from download").fetchone()
    assert tuple(row) == ("failed", "import failed: the files match no edition of the album") and folder.exists()
    assert {r[0] for r in db.execute("select hash from release_block")} == {"a", "abcd"}


def test_a_dead_torrent_is_deleted_blocked_and_its_album_searched_again(downloads, lidarr, torbox, db, tmp_path):
    lidarr.offers[5] = [offer("a", "abcd", 10), offer("b", "bcde", 9)]
    db.execute("insert into lidarr_item(kind, ref, state, lidarr_id, searched, updated) values ('album', 'al-1', 'downloading', 5, 1, 0)")
    downloads.search(db, 5)
    db.execute("update download set created = ?", (time.time() - 7200,))
    torbox.list = [tb(71, "abcd", progress=0, seeds=0)]
    downloads.cycle()
    assert torbox.deleted == [71] and db.execute("select state from download").fetchone()[0] == "failed"
    assert db.execute("select searched from lidarr_item").fetchone()[0] is None
    downloads.search(db, 5)
    assert [a[1] for a in torbox.added][-1] == "magnet:?xt=urn:btih:bcde"


def test_a_torrent_still_moving_or_seeded_is_not_dead(downloads, lidarr, torbox, db):
    lidarr.offers[5] = [offer("a", "abcd", 10)]
    downloads.search(db, 5)
    db.execute("update download set created = ?", (time.time() - 7200,))
    torbox.list = [tb(71, "abcd", progress=0, seeds=2)]
    downloads.cycle()
    assert torbox.deleted == [] and db.execute("select state from download").fetchone()[0] == "downloading"


def test_a_queued_add_is_dropped_without_blocking_and_the_album_comes_back_soon(downloads, lidarr, torbox, db, tmp_path):
    lidarr.offers[5] = [offer("a", "abcd", 10)]
    torbox.full = True
    library = Library(lambda: connect(tmp_path / "oasis.db"), lidarr, downloads)
    before = time.time()
    searched = library.search(db, 5)
    assert torbox.dropped == [90] and db.execute("select count(*) from release_block").fetchone()[0] == 0
    assert db.execute("select count(*) from download").fetchone()[0] == 0
    assert before - 6 * 3600 < searched < before - 6 * 3600 + 700  # due again after RETRY_FULL, not RESEARCH


def test_the_slot_rule_counts_every_active_torbox_torrent_anime_included(downloads, torbox):
    torbox.list = [torrent(i, f"anime{i}") for i in range(7)] + [torrent(9, "done", progress=1, active=False)]
    assert downloads.room()
    torbox.list.append(torrent(8, "seeding", progress=1))
    assert not downloads.room()


def test_with_downloads_the_library_searches_through_them_and_shows_them_downloading(downloads, lidarr, torbox, db, tmp_path):
    from test_lidarr import FakeLidarr as LibraryLidarr
    fake = LibraryLidarr()
    fake.releases, fake.album_by_id = lidarr.releases, lidarr.album_by_id
    lidarr.albums[1], lidarr.offers[1] = {**lidarr.albums[5], "id": 1}, [offer("a", "abcd", 10)]  # the fake's id for the album
    downloads.lidarr = fake
    library = Library(lambda: connect(tmp_path / "oasis.db"), fake, downloads)
    db.execute("insert into track(id, source, ref, album_artist, album, album_id) values ('t', 'local', '/x', 'Artist', 'Album', 'al-1')")
    db.execute("insert into user(name, password, created) values ('u', 'p', 0)")
    db.execute("insert into wanted values ('u', 'album', 'al-1')")
    torbox.list = [torrent(i, f"anime{i}") for i in range(8)]
    library.reconcile()
    assert torbox.added == [] and ("search", 1) not in fake.calls  # TorBox full: no search
    torbox.list = []
    library.reconcile()
    assert len(torbox.added) == 1 and ("search", 1) not in fake.calls  # not through Lidarr's grab
    library.reconcile()
    assert db.execute("select state from lidarr_item").fetchone()[0] == "downloading"
