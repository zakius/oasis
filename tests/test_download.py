import time

import pytest

from oasis.db import connect
from oasis import download, releases
from oasis.download import Downloads
from test_doctor import FakeTorBox, torrent


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


def tb(id, hash, peers=1, availability=1.0, **extra):
    return {"download_state": "downloading", "download_finished": False, "download_present": False, "peers": peers,
            "availability": availability, **torrent(id, hash, **extra)}


def result(title, hash="", seeders=5, size=400e6, **extra):
    return {"title": title, "infoHash": hash, "seeders": seeders, "size": size, "guid": f"guid-{title}", "indexerId": 1, **extra}


class Prowlarr:
    def __init__(self, results):
        self.results, self.asked = results, []

    def search(self, query):
        self.asked.append(query)
        return self.results.get(query, [])


class Importer:
    """importer.py's place: records what it got, and fails for the release groups in failing."""
    def __init__(self):
        self.placed, self.failing = [], set()

    def place(self, folder, group):
        self.placed.append((folder, group))
        if group in self.failing:
            raise LookupError("no edition matches")


WANT = {"artist": "Artist", "names": ["Artist"], "title": "Album", "year": "", "others": [], "discs": 1, "upgrade": False, "native": [], "other_titles": []}


def offer(hash, quality="FLAC", **extra):
    return result(f"Artist - Album [{quality}]", hash, magnetUrl=f"magnet:?xt=urn:btih:{hash}", **extra)


def files(tmp_path, *names):
    (tmp_path / "cdn").mkdir(exist_ok=True)
    for id, name in enumerate(names):
        (tmp_path / "cdn" / str(id)).write_bytes(name.encode() * (id + 1))
    return [{"id": id, "name": name, "size": len(name.encode()) * (id + 1)} for id, name in enumerate(names)]


@pytest.fixture
def db(tmp_path):
    return connect(tmp_path / "oasis.db")


@pytest.fixture
def torbox(tmp_path):
    return TorBox(tmp_path)


@pytest.fixture
def downloads(tmp_path, torbox, monkeypatch):
    monkeypatch.setattr(download, "PACE", 0)
    made = Downloads(lambda: connect(tmp_path / "oasis.db"), lambda: torbox, Importer(), Prowlarr({}), str(tmp_path / "dl"), stall_hours=1,
                     imported=lambda: made.scans.append(1))
    made.scans = []
    return made


def finished(id, hash, names, tmp_path):
    return {**tb(id, hash, progress=1, active=False), "download_state": "completed", "download_finished": True, "download_present": True,
            "files": files(tmp_path, *names)}


def test_a_find_sends_the_pick_to_torbox_and_the_files_are_fetched_and_placed(downloads, torbox, db, tmp_path):
    downloads.prowlarr.results["Artist Album"] = [offer("abcd")]
    assert downloads.find(db, WANT, "mb-album")
    assert torbox.added == [("Artist - Album [FLAC]", "magnet:?xt=urn:btih:abcd", None)]
    row = db.execute("select * from download").fetchone()
    assert (row["hash"], row["album_id"], row["album_mbid"], row["torbox_id"], row["state"]) == ("abcd", 0, "mb-album", 71, "added")
    assert downloads.groups(db) == {"mb-album"}

    torbox.list = [tb(71, "abcd", progress=0.5)]
    downloads.cycle()
    assert db.execute("select state from download").fetchone()[0] == "downloading"
    torbox.list = [{**tb(71, "abcd", progress=1), "download_state": "checking", "download_finished": True, "download_present": True, "files": []}]
    downloads.cycle()
    assert db.execute("select state from download").fetchone()[0] == "downloading"

    folder = tmp_path / "dl" / "abcd"
    torbox.list = [finished(71, "abcd", ["Album/01.flac", "Album/02.flac", "Album/rip.log", "Album/cover.jpg"], tmp_path)]
    downloads.cycle()
    assert sorted(torbox.links) == [0, 1, 3]  # no log
    assert downloads.own.placed == [(folder, "mb-album")]
    assert db.execute("select state from download").fetchone()[0] == "done"
    assert torbox.deleted == [71] and not folder.exists() and downloads.groups(db) == set() and downloads.scans == [1]


def test_two_same_named_files_in_different_folders_both_arrive_and_a_restart_mid_fetch_resumes(downloads, torbox, db, tmp_path):
    downloads.prowlarr.results["Artist Album"] = [offer("abcd")]
    downloads.find(db, WANT, "mb-album")
    names = ["Album/CD1/01 Intro.flac", "Album/CD2/01 Intro.flac", "Album/CD1/folder.jpg", "Album/CD2/folder.jpg"]
    torbox.list = [finished(71, "abcd", names, tmp_path)]
    folder = tmp_path / "dl" / "abcd"
    (folder / "Album/CD1").mkdir(parents=True)
    (folder / "Album/CD2").mkdir()
    (folder / names[0]).write_bytes((tmp_path / "cdn" / "0").read_bytes())  # fetched before the restart
    (folder / names[1]).write_bytes(b"cut short")
    db.execute("update download set state = 'fetching'")
    downloads.own.failing.add("mb-album")  # import fails: the files stay for inspection
    downloads.cycle()
    assert sorted(torbox.links) == [1, 2, 3]
    assert [(folder / n).read_bytes() for n in names] == [(tmp_path / "cdn" / str(i)).read_bytes() for i in range(4)]
    row = db.execute("select state, error from download").fetchone()
    assert tuple(row) == ("failed", "import failed: no edition matches") and folder.exists()
    assert {r[0] for r in db.execute("select hash from release_block")} == {"guid-Artist - Album [FLAC]", "abcd"}


def test_a_dead_torrent_is_deleted_blocked_and_the_next_find_takes_another(downloads, torbox, db):
    downloads.prowlarr.results["Artist Album"] = [offer("abcd"), offer("bcde", "MP3 320")]
    downloads.find(db, WANT, "mb-album")
    db.execute("update download set created = ?", (time.time() - 7200,))
    torbox.list = [tb(71, "abcd", progress=0, seeds=0)]
    downloads.cycle()
    assert torbox.deleted == [71] and db.execute("select state from download").fetchone()[0] == "failed"
    downloads.find(db, WANT, "mb-album")
    assert [a[1] for a in torbox.added][-1] == "magnet:?xt=urn:btih:bcde"


def test_a_torrent_still_moving_or_seeded_is_not_dead(downloads, torbox, db):
    downloads.prowlarr.results["Artist Album"] = [offer("abcd")]
    downloads.find(db, WANT, "mb-album")
    db.execute("update download set created = ?", (time.time() - 1800,))
    torbox.list = [tb(71, "abcd", progress=0, seeds=2)]
    downloads.cycle()
    assert torbox.deleted == [] and db.execute("select state from download").fetchone()[0] == "downloading"


def test_a_torrent_added_again_after_a_failure_gets_a_new_stall_timer(downloads, torbox, db, monkeypatch):
    now = [time.time()]
    monkeypatch.setattr(time, "time", lambda: now[0])
    downloads.prowlarr.results["Artist Album"] = [offer("abcd")]
    downloads.find(db, WANT, "mb-album")
    torbox.list = [tb(71, "abcd", progress=0.3, seeds=0)]
    downloads.cycle()
    torbox.list = []
    downloads.cycle()  # gone from TorBox: not blocked
    now[0] += 7200
    downloads.find(db, WANT, "mb-album")
    torbox.list = [tb(72, "abcd", progress=0.3, seeds=0)]
    downloads.cycle()
    assert db.execute("select state from download").fetchone()[0] == "downloading"
    now[0] += 3601
    downloads.cycle()
    assert db.execute("select state from download").fetchone()[0] == "failed"


def test_a_queued_add_is_dropped_without_blocking(downloads, torbox, db):
    downloads.prowlarr.results["Artist Album"] = [offer("abcd")]
    torbox.full = True
    assert downloads.find(db, WANT, "mb-album") is False  # acquire.py makes the album due again soon
    assert torbox.dropped == [90] and db.execute("select count(*) from release_block").fetchone()[0] == 0
    assert db.execute("select count(*) from download").fetchone()[0] == 0


def test_the_slot_rule_counts_every_active_torbox_torrent_anime_included(downloads, torbox):
    torbox.list = [torrent(i, f"anime{i}") for i in range(7)] + [torrent(9, "done", progress=1, active=False)]
    assert downloads.room()
    torbox.list.append(torrent(8, "seeding", progress=1))
    assert not downloads.room()


def test_a_torrent_without_seeders_at_0_percent_is_dead_after_20_minutes(downloads, torbox, db, monkeypatch):
    now = [time.time()]
    monkeypatch.setattr(time, "time", lambda: now[0])
    downloads.prowlarr.results["Artist Album"] = [offer("abcd")]
    downloads.find(db, WANT, "mb-album")
    torbox.list = [tb(71, "abcd", progress=0, seeds=0, peers=2, availability=1.5)]
    downloads.cycle()
    now[0] += 1000
    downloads.cycle()
    assert db.execute("select state from download").fetchone()[0] == "downloading"
    now[0] += 300
    downloads.cycle()
    assert db.execute("select state, error from download").fetchone()[:] == ("failed", "dead torrent: no seeds, no progress")


def test_an_indexer_whose_torrents_were_often_dead_is_tried_last(downloads, db):
    for n in range(6):
        db.execute("insert into download(hash, guid, album_id, state, error, created, updated) values (?, ?, 0, ?, ?, 0, 0)",
                   (f"h{n}", f"https://lime.example/t/{n}", "failed" if n < 2 else "done", "dead torrent: no seeds, no progress" if n < 2 else None))
    assert downloads.unreliable(db) == {"lime.example"}
    lime, good = result("Artist - Album [FLAC]", "aa", seeders=40, guid="https://lime.example/x"), result("Artist - Album [MP3 320]", "bb", guid="https://good.example/y")
    assert [r["infoHash"] for r in releases.rank([lime, good], set(), unreliable=downloads.unreliable(db))] == ["bb", "aa"]
    assert [r["infoHash"] for r in releases.rank([lime, good], {"aa"}, unreliable=downloads.unreliable(db))] == ["aa", "bb"]  # cached: fine


def test_how_much_of_a_download_is_copied_here_counts_partly_fetched_files(downloads, tmp_path):
    done, partial, missing = tmp_path / "a.flac", tmp_path / "b.flac", tmp_path / "c.flac"
    done.write_bytes(b"x" * 50)
    (tmp_path / "b.flac.part").write_bytes(b"x" * 25)
    downloads.copying["h"] = [(done, 50), (partial, 50), (missing, 100)]
    assert downloads.copied("h") == 0.375 and downloads.copied("other") is None


def test_a_trickling_torrent_makes_way_when_slots_are_full_and_albums_wait_and_is_tried_last_again(downloads, torbox, db, monkeypatch):
    now = [time.time()]
    monkeypatch.setattr(time, "time", lambda: now[0])
    downloads.prowlarr.results["Artist Album"] = [offer("abcd")]
    downloads.find(db, WANT, "mb-album")
    torbox.list = [tb(71, "abcd", progress=0.01, seeds=1)]
    downloads.cycle()
    downloads.max_active = 1
    db.execute("insert into want_album(rg, artist_mbid, title, reason, state) values ('waiting', 'a', 'Next', 'followed', 'missing')")
    now[0] += 1800
    downloads.cycle()
    assert db.execute("select state from download").fetchone()[0] == "downloading"  # within the usual wait
    now[0] += 3600
    torbox.list = [tb(71, "abcd", progress=0.2, seeds=1)]  # 20 % in 1.5 h: done in 7.5 h
    downloads.cycle()
    assert db.execute("select state from download").fetchone()[0] == "downloading"
    torbox.list = [tb(71, "abcd", progress=0.1, seeds=1, active=False)]  # a slot free: slow is fine
    downloads.cycle()
    assert db.execute("select state from download").fetchone()[0] == "downloading"
    torbox.list = [tb(71, "abcd", progress=0.1, seeds=1)]  # every slot taken and an album waits: it makes way
    downloads.cycle()
    assert db.execute("select state, error from download").fetchone()[:] == ("failed", "too slow: 10% in 2 h")
    assert "abcd" in downloads.slow(db) and "abcd" not in downloads.blocked(db)
    other = result("Artist - Album [FLAC]", "beef", seeders=1)
    assert [r["infoHash"] for r in releases.rank([offer("abcd", seeders=90), other], set(), slow=downloads.slow(db))] == ["beef", "abcd"]


@pytest.mark.parametrize("swarm, why", [({"peers": 0}, "dead torrent: nobody shares it"),
                                        ({"peers": 3, "availability": 0.6}, "dead torrent: its peers have only 60% of it")])
def test_a_torrent_nobody_or_only_part_of_which_is_shared_goes_after_10_minutes(downloads, torbox, db, monkeypatch, swarm, why):
    now = [time.time()]
    monkeypatch.setattr(time, "time", lambda: now[0])
    downloads.prowlarr.results["Artist Album"] = [offer("abcd")]
    downloads.find(db, WANT, "mb-album")
    torbox.list = [tb(71, "abcd", progress=0.1, seeds=0, **swarm)]
    downloads.cycle()
    now[0] += 500
    downloads.cycle()
    assert db.execute("select state from download").fetchone()[0] == "downloading"
    now[0] += 200
    downloads.cycle()
    assert db.execute("select state, error from download").fetchone()[:] == ("failed", why)
