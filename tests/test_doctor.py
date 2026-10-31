import os
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from oasis.db import connect
from oasis.doctor import Doctor
from oasis.lidarr import Lidarr

NOW = datetime.now(timezone.utc)


def ago(**delta) -> str:
    return (NOW - timedelta(**delta)).isoformat()


class FakeLidarr(Lidarr):
    def __init__(self):
        self.items, self.proposed, self.albums, self.tracks, self.offers, self.files = [], {}, {}, {}, {}, {}
        self.removed, self.grabbed, self.imported = [], [], []
        self.status = {"status": "completed"}
        self.imports_land = True

    def queue(self):
        return [dict(i) for i in self.items]

    def remove(self, queue_id, blocklist, search):
        self.removed.append((queue_id, blocklist, search))

    def releases(self, album_id):
        return self.offers.get(album_id, [])

    def grab(self, release, **ids):
        self.grabbed.append(release["guid"])

    def album_by_id(self, album_id):
        return self.albums[album_id]

    def release_tracks(self, release_id):
        return self.tracks[release_id]

    def proposals(self, **where):
        return self.proposed.get(where.get("downloadId") or where.get("folder"), [])

    def manual_import(self, files, mode="auto"):
        self.imported.append(files)
        self.mode = mode
        if self.imports_land:
            for f in files:
                self.files.setdefault(f["albumId"], []).append({"dateAdded": "2099-01-01T00:00:00Z"})
        return {"id": 1, "queued": NOW.isoformat()}

    def command(self, command_id):
        return self.status

    def track_files(self, album_id):
        return self.files.get(album_id, [])

    def rescan(self, folders):
        self.rescanned = folders


class FakeTorBox:
    def __init__(self):
        self.waiting, self.list, self.have, self.deleted, self.dropped = [], [], set(), [], []

    def queued(self):
        return self.waiting

    def delete_queued(self, queued_id):
        self.dropped.append(queued_id)

    def torrents(self):
        return self.list

    def cached(self, hashes):
        return self.have & set(hashes)

    def delete_torrent(self, torrent_id):
        self.deleted.append(torrent_id)


@pytest.fixture
def rdt(tmp_path):
    path = tmp_path / "rdtclient.db"
    db = sqlite3.connect(path)
    db.executescript("create table Settings(SettingId, Value); create table Torrents(TorrentId, Hash, Completed, Category, RdName);"
                     "create table Downloads(TorrentId, FileName, DownloadFinished);"
                     "insert into Settings values ('Provider:ApiKey', 'tb-key');")
    db.commit()
    return db


@pytest.fixture
def lidarr():
    fake = FakeLidarr()
    fake.albums[5] = {"id": 5, "artistId": 9, "artist": {"artistName": "Artist"}, "releases": [],
                      "statistics": {"trackFileCount": 0, "trackCount": 3}}
    return fake


@pytest.fixture
def torbox():
    return FakeTorBox()


@pytest.fixture
def doctor(tmp_path, rdt, lidarr, torbox):
    return Doctor(lambda: connect(tmp_path / "oasis.db"), lidarr, str(tmp_path / "rdtclient.db"), stall_hours=1,
                  torbox=lambda key: torbox if key == "tb-key" else None)


def item(id=1, hash="HASH1", album=5, state="importFailed", status="completed", **extra):
    return {"id": id, "downloadId": hash, "title": f"Artist - Album {id}", "albumId": album, "status": status,
            "trackedDownloadState": state, "trackedDownloadStatus": "warning", "statusMessages": [], **extra}


def proposal(path, release=50, tracks=(1,), rejections=()):
    return {"path": path, "artist": {"id": 9}, "album": {"id": 5}, "albumReleaseId": release, "tracks": [{"id": t} for t in tracks],
            "rejections": list(rejections), "quality": {"quality": {"name": "FLAC"}}}


def torrent(id, hash, progress=0.5, seeds=3, created=None, active=True):
    return {"id": id, "hash": hash, "name": f"torrent {id}", "progress": progress, "seeds": seeds, "active": active,
            "created_at": created or ago(hours=3)}


def track(rdt, hash, *downloads, category="music", name="Album"):
    rdt.execute("insert into Torrents values (?, ?, null, ?, ?)", (hash, hash, category, name))
    rdt.executemany("insert into Downloads values (?, ?, ?)", [(hash, *d) for d in downloads])
    rdt.commit()


def test_rule1_imports_lidarrs_clean_proposal(doctor, lidarr):
    lidarr.items = [item()]
    lidarr.proposed["HASH1"] = [proposal(f"/dl/Album/0{n}.flac", tracks=[n]) for n in (1, 2, 3)]
    doctor.cycle()
    assert [(f["albumReleaseId"], f["trackIds"], f["downloadId"]) for f in lidarr.imported[0]] == [(50, [1], "HASH1"), (50, [2], "HASH1"), (50, [3], "HASH1")]
    assert lidarr.removed == []


def test_rule1_imports_against_the_largest_edition_the_files_cover(doctor, lidarr):
    lidarr.items = [item()]
    names = ["01 Artist - Ein (Remastered).flac", "02. Zwei.flac", "03 Drei.flac"]
    lidarr.proposed["HASH1"] = [proposal(f"/dl/Album/{n}", tracks=[], rejections=["Has missing tracks"]) for n in names]
    lidarr.albums[5]["releases"] = [{"id": 61, "trackCount": 12}, {"id": 62, "trackCount": 2}, {"id": 63, "trackCount": 3}]
    lidarr.tracks[62] = [{"id": 21, "title": "Ein", "mediumNumber": 1, "absoluteTrackNumber": 1},
                         {"id": 22, "title": "Zwei", "mediumNumber": 1, "absoluteTrackNumber": 2}]
    lidarr.tracks[63] = [{"id": 31, "title": "Ein", "mediumNumber": 1, "absoluteTrackNumber": 1},
                         {"id": 32, "title": "Zwei (Live)", "mediumNumber": 1, "absoluteTrackNumber": 2},
                         {"id": 33, "title": "Three", "mediumNumber": 1, "absoluteTrackNumber": 3}]  # by number
    doctor.cycle()
    assert {f["trackIds"][0]: (os.path.basename(f["path"]), f["albumReleaseId"]) for f in lidarr.imported[0]} == \
        {31: (names[0], 63), 32: (names[1], 63), 33: (names[2], 63)}
    assert lidarr.removed == []


def test_rule1_replaces_what_matches_no_edition(doctor, lidarr):
    lidarr.items = [item()]
    lidarr.proposed["HASH1"] = [proposal("/dl/Album/01 Other.flac", tracks=[], rejections=["Album match is not close enough"])]
    lidarr.albums[5]["releases"] = [{"id": 63, "trackCount": 1}]
    lidarr.tracks[63] = [{"id": 31, "title": "Ein", "mediumNumber": 1, "absoluteTrackNumber": 2}]
    doctor.cycle()
    assert lidarr.imported == [] and lidarr.removed == [(1, True, True)]


def test_rule2_an_import_lidarr_fails_after_the_files_are_in_counts(doctor, lidarr):
    lidarr.items = [item()]
    lidarr.proposed["HASH1"] = [proposal("/dl/Album/01.flac")]
    lidarr.status = {"status": "failed", "message": "Object reference not set to an instance of an object"}
    doctor.cycle()
    assert lidarr.removed == []
    lidarr.imports_land, lidarr.files = False, {}
    doctor.cycle()
    assert lidarr.removed == [(1, True, True)]


def test_rule3_extensionless_names_get_a_hardlink_before_proposals_are_read(doctor, lidarr, tmp_path):
    folder = tmp_path / "dl" / "Album"
    folder.mkdir(parents=True)
    (folder / "13. P.L.U.C.Kflac").write_bytes(b"audio")
    lidarr.items = [item()]
    lidarr.proposed["HASH1"] = [proposal(str(folder / "13. P.L.U.C.Kflac"))]
    doctor.cycle()
    assert os.path.samefile(folder / "13. P.L.U.C.Kflac", folder / "13. P.L.U.C.K.flac")


def test_rule4_imports_downloads_rdtclient_finished_but_never_completed(doctor, lidarr, rdt):
    track(rdt, "HASH1", ("01.flac", ago(hours=1)), ("02.flac", ago(minutes=40)), ("folder.jpg", None))
    track(rdt, "HASH2", ("01.flac", ago(minutes=5)), ("folder.jpg", None))  # too recent
    lidarr.items = [item(state="downloading", status="downloading"), item(2, "HASH2", state="downloading", status="downloading")]
    lidarr.proposed["/downloads/music/Album"] = [proposal("/downloads/music/Album/01.flac")]
    doctor.cycle()
    assert len(lidarr.imported) == 1 and lidarr.removed == [(1, False, False)]


def test_rule5_an_error_while_torbox_holds_it_in_its_queue_drops_it_without_blocklisting(doctor, lidarr, torbox):
    lidarr.items = [item(state="downloading", status="warning", errorMessage="qBittorrent is reporting an error")]
    torbox.waiting = [{"id": 70, "hash": "hash1", "name": "Album"}]
    doctor.cycle()
    assert lidarr.removed == [(1, False, False)]


def test_rule6_other_errors_and_long_stalls_replace_and_search_once_per_album(doctor, lidarr):
    error = dict(state="downloading", status="warning", errorMessage="qBittorrent is reporting an error")
    stalled = dict(state="downloading", status="warning", errorMessage="The download is stalled with no connections")
    lidarr.albums[6] = {**lidarr.albums[5], "id": 6, "statistics": {"trackFileCount": 3, "trackCount": 3}}
    lidarr.albums[7] = {**lidarr.albums[5], "id": 7}
    lidarr.items = [item(1, "H1", **error), item(2, "H2", **stalled, added=ago(hours=2)),  # same album: one search
                    item(3, "H3", album=6, **error),  # album complete
                    item(4, "H4", album=7, **error), item(5, "H5", album=7, state="downloading", status="downloading",
                                                          trackedDownloadStatus="ok"),  # another copy coming
                    item(6, "H6", album=8, **stalled, added=ago(minutes=30))]  # not stalled long enough
    doctor.cycle()
    assert lidarr.removed == [(1, True, True), (2, True, False), (3, True, False), (4, True, False)]


def test_rule7_torbox_queued_items_are_deleted(doctor, torbox):
    torbox.waiting = [{"id": 70, "hash": "abc", "name": "Album"}]
    doctor.cycle()
    assert torbox.dropped == [70]


def test_rule8_unfinished_torrents_rdtclient_does_not_track_are_deleted(doctor, torbox, rdt):
    track(rdt, "tracked")
    torbox.list = [torrent(1, "untracked"), torrent(2, "young", created=ago(minutes=5)), torrent(3, "tracked"),
                   torrent(4, "finished", progress=1), torrent(5, "inactive", active=False)]
    doctor.cycle()
    assert torbox.deleted == [1]


def test_rules8_and_9_leave_oasiss_own_downloads_alone(doctor, torbox, tmp_path):
    connect(tmp_path / "oasis.db").execute("insert into download(hash, album_id, state, created, updated) values ('ours', 5, 'downloading', 0, 0)")
    torbox.list = [torrent(1, "OURS", progress=0, seeds=0)]  # untracked by rdtclient, and dead
    doctor.cycle()
    assert torbox.deleted == []


def test_rule9_dead_torrents_are_replaced_in_lidarr_or_deleted(doctor, lidarr, torbox, rdt):
    for h in ("lidarr", "sonarr", "fresh", "moving", "seeded"):
        track(rdt, h)
    lidarr.items = [item(1, "LIDARR", state="downloading", status="downloading", trackedDownloadStatus="ok")]
    torbox.list = [torrent(1, "lidarr", progress=0, seeds=0), torrent(2, "sonarr", progress=0, seeds=0),
                   torrent(3, "fresh", progress=0, seeds=0, created=ago(minutes=30)),
                   torrent(4, "moving", progress=0.4, seeds=0), torrent(5, "seeded", progress=0, seeds=2)]
    doctor.cycle()
    assert lidarr.removed == [(1, True, True)] and torbox.deleted == [2]
    doctor.progress_seen["moving"] = (0.4, NOW - timedelta(hours=2))  # no progress since
    doctor.cycle()
    assert torbox.deleted == [2, 2, 4]


def test_rule10_slow_downloads_are_swapped_for_a_cached_copy(doctor, lidarr, torbox, rdt):
    track(rdt, "slow")
    lidarr.items = [item(1, "SLOW", state="downloading", status="downloading", trackedDownloadStatus="ok")]
    offer = lambda guid, hash, weight, *rejections: {"guid": guid, "indexerId": 1, "title": guid, "infoHash": hash, "qualityWeight": weight,
                                                     "rejections": list(rejections), "quality": {"quality": {"name": "FLAC"}}}
    lidarr.offers[5] = [offer("worse", "w", 5), offer("wrong", "x", 15, "Wrong format"), offer("uncached", "u", 12, "Not enough seeders: 0"),
                        offer("current", "SLOW", 10, "Release in queue"), offer("cached", "c", 10, "Release in queue")]
    torbox.have = {"w", "x", "c"}
    torbox.list = [torrent(1, "slow", created=ago(minutes=20))]
    doctor.cycle()
    assert lidarr.grabbed == ["cached"] and lidarr.removed == [(1, False, False)]
    lidarr.offers[5] = []
    doctor.cycle()  # checked within the hour: not again
    assert lidarr.grabbed == ["cached"]


def test_rule11_downloads_lidarr_ties_to_no_album_get_it_from_the_rescue_table(doctor, lidarr, tmp_path):
    connect(tmp_path / "oasis.db").execute("insert into lidarr_rescue values ('hash1', 5, 0)")
    lidarr.items = [item(album=None)]
    lidarr.proposed["HASH1"] = [proposal("/dl/Album/01.flac")]
    doctor.cycle()
    assert len(lidarr.imported) == 1


def test_dry_run_changes_nothing_and_logs_each_action(doctor, lidarr, torbox, rdt, caplog):
    doctor.dry = True
    lidarr.items = [item(), item(2, "H2", state="downloading", status="warning", errorMessage="error")]
    lidarr.proposed["HASH1"] = [proposal("/dl/Album/01.flac")]
    torbox.waiting = [{"id": 70, "hash": "abc", "name": "Queued"}]
    torbox.list = [torrent(1, "untracked")]
    with caplog.at_level("INFO", "oasis.doctor"):
        doctor.cycle()
    assert lidarr.imported == lidarr.removed == torbox.deleted == torbox.dropped == []
    lines = [r.getMessage() for r in caplog.records]
    assert [m.split(" ")[1] for m in lines if m.startswith("[dry]")] == ["import", "replace", "drop", "delete"]
    assert lines[-1] == "doctor: 2 downloads in Lidarr's queue, 1 TorBox torrents"
