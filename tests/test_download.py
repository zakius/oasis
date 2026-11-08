import json
import threading
import time

import pytest

from conftest import WANT, Prowlarr, finished, result, tb, torrent
from oasis import download, releases
from oasis.wants import Outcome


def crowd(downloads, db):
    """Every TorBox slot taken and an album waiting: the short waits apply."""
    downloads.max_active = 1
    db.execute("insert into want_album(rg, artist_mbid, title, reason, state) values ('waiting', 'a', 'Next', 'followed', 'missing')")


def offer(hash, quality="FLAC", **extra):
    return result(f"Artist - Album [{quality}]", hash, magnetUrl=f"magnet:?xt=urn:btih:{hash}", **extra)


def test_a_find_sends_the_pick_to_torbox_and_the_files_are_fetched_and_placed(downloads, torbox, db, tmp_path):
    downloads.prowlarr.results["Artist Album"] = [offer("abcd")]
    assert downloads.find(db, WANT, "mb-album") is Outcome.SENT
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
    assert db.execute("select state, seeding, mode from download").fetchone()[:] == ("done", 1, "torbox")  # TorBox's setting decides its seeding
    assert torbox.deleted == [] and not folder.exists() and downloads.groups(db) == set() and downloads.scans == [1]
    downloads.cycle()
    assert torbox.deleted == [71]  # TorBox isn't seeding it: it goes


def test_dead_torrents_get_another_try_after_two_weeks_after_all_others_but_wrong_content_never(downloads, db):
    old, fresh = time.time() - download.DEAD_AGAIN - 60, time.time()
    db.executemany("insert into release_block(hash, reason, at) values (?, ?, ?)", [
        ("dead-old", "dead torrent: no seeds, no progress", old), ("dead-new", "dead torrent: nobody shares it", fresh),
        ("user", "blocked by a user", old), ("empty", "no audio files in the torrent", old), ("bad", "import failed: no edition matches", old)])
    assert downloads.blocked(db) == {"dead-new", "user", "empty", "bad"} and downloads.slow(db) == {"dead-old"}
    downloads.block_release(db, {"hash": "dead-old", "guid": None}, "dead torrent: its peers have only 3%")  # dead again: two weeks more
    assert "dead-old" in downloads.blocked(db) and not downloads.slow(db)
    assert releases.rank([{"title": "A [FLAC]", "infoHash": "dead-old", "seeders": 90}, {"title": "A [MP3]", "infoHash": "x", "seeders": 1}],
                         set(), slow=frozenset({"dead-old"}))[0]["infoHash"] == "x"


class LoginProwlarr(Prowlarr):
    logins = names = {1: "Some-Tracker"}


def seeding(downloads, torbox, db, tmp_path, ratio, **more):
    """An album from a login tracker, downloaded and imported; TorBox then seeding it at ratio."""
    downloads.prowlarr = LoginProwlarr({"Artist Album": [offer("abcd")]})
    assert downloads.find(db, WANT, "mb-album") and torbox.seeded == ["seed"]
    torbox.list = [finished(71, "abcd", ["Album/01 Song.flac"], tmp_path)]
    downloads.cycle()
    row = db.execute("select state, seeding, tracker, finished from download").fetchone()
    assert tuple(row)[:3] == ("done", 1, "Some-Tracker") and row["finished"] and 71 not in torbox.deleted
    torbox.list = [{**torbox.list[0], "download_state": "seeding", "active": True, "ratio": ratio, "total_downloaded": 1000,
                    "total_uploaded": int(ratio * 1000), **more}]


def test_a_login_trackers_torrent_seeds_after_its_import_till_its_ratio_is_met_and_not_in_a_slot(downloads, torbox, db, tmp_path, caplog):
    caplog.set_level("INFO", "oasis.download")
    seeding(downloads, torbox, db, tmp_path, 0.5)
    downloads.cycle()
    assert 71 not in torbox.deleted and downloads.groups(db) == set()  # the album is done; the torrent seeds on
    downloads.max_active = 1
    assert downloads.room()  # seeding takes none of oasis's slots
    torbox.list[0].update(ratio=1.02, total_uploaded=1020)
    downloads.cycle()
    assert 71 in torbox.deleted and "stopped seeding Artist - Album [FLAC]: ratio 1.02" in caplog.text
    assert db.execute("select seeding, downloaded, uploaded from download").fetchone()[:] == (0, 1000, 1020)
    assert [tuple(r) for r in download.trackers(db)] == [("Some-Tracker", 1000, 1020, 0)]  # kept after it went


def test_seeding_ends_at_the_day_cap_unless_the_trackers_ratio_is_low_and_waits_its_minimum_time(downloads, torbox, db, tmp_path, caplog):
    caplog.set_level("INFO", "oasis.download")
    downloads.rules = {"Some-Tracker": {"min_hours": 72, "min_ratio": 0.6}}
    seeding(downloads, torbox, db, tmp_path, 1.5)
    downloads.cycle()
    assert 71 not in torbox.deleted  # its ratio is met, but not its 72 hours
    torbox.list[0].update(ratio=0.4, total_uploaded=400)
    db.execute("update download set finished = ?", (time.time() - 8 * 86400,))
    downloads.cycle()
    assert 71 not in torbox.deleted and "Some-Tracker: our ratio is 0.40, below its 0.60" in caplog.text  # low: past the cap
    downloads.rules = {}
    downloads.cycle()
    assert 71 in torbox.deleted and "stopped seeding Artist - Album [FLAC]: 7 days, ratio 0.40" in caplog.text


def test_seeding_stops_at_once_when_torbox_doesnt_seed_and_other_indexers_leave_it_to_torbox(downloads, torbox, db, tmp_path, caplog):
    caplog.set_level("INFO", "oasis.download")
    seeding(downloads, torbox, db, tmp_path, 0, active=False, download_state="completed")
    downloads.cycle()
    assert 71 in torbox.deleted and "TorBox isn't seeding it (completed)" in caplog.text
    downloads.prowlarr = Prowlarr({"Artist Album": [offer("bcde", indexerId=2)]})
    db.execute("delete from release_block")
    assert downloads.find(db, WANT, "mb-other") and torbox.seeded[-1] == "torbox"


def test_a_torbox_mode_torrent_seeds_as_torbox_likes_however_long_and_goes_when_torbox_stops(downloads, torbox, db, tmp_path, caplog):
    caplog.set_level("INFO", "oasis.download")
    downloads.rules = {"Some-Tracker": {"mode": "torbox"}}
    downloads.prowlarr = LoginProwlarr({"Artist Album": [offer("abcd")]})
    assert downloads.find(db, WANT, "mb-album") and torbox.seeded == ["torbox"]
    torbox.list = [{**finished(71, "abcd", ["Album/01 Song.flac"], tmp_path), "download_state": "seeding", "active": True, "ratio": 5.0}]
    downloads.cycle()
    db.execute("update download set finished = ?", (time.time() - 30 * 86400,))
    downloads.cycle()
    assert 71 not in torbox.deleted and db.execute("select seeding, mode from download").fetchone()[:] == (1, "torbox")  # neither ratio nor days end it
    torbox.list[0].update(active=False, download_state="completed")
    downloads.cycle()
    assert 71 in torbox.deleted and "TorBox isn't seeding it (completed)" in caplog.text


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
    crowd(downloads, db)
    downloads.prowlarr.results["Artist Album"] = [offer("abcd")]
    downloads.find(db, WANT, "mb-album")
    torbox.list = [tb(71, "abcd", progress=0.3, seeds=0)]
    downloads.cycle()
    torbox.list = []
    for _ in range(download.GONE_AFTER):
        downloads.cycle()  # gone from TorBox: not blocked
    assert db.execute("select state, error from download").fetchone()[:] == ("failed", "gone from TorBox")
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
    assert downloads.find(db, WANT, "mb-album") is Outcome.NO_SLOT  # acquire.py makes the album due again soon, a full gap on
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
    crowd(downloads, db)
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


def test_an_indexer_whose_torrents_were_often_dead_is_tried_last(downloads, torbox, db):
    for n in range(6):
        db.execute("insert into download(hash, guid, album_id, state, error, created, updated) values (?, ?, 0, ?, ?, 0, 0)",
                   (f"h{n}", f"https://lime.example/t/{n}", "failed" if n < 2 else "done", "dead torrent: no seeds, no progress" if n < 2 else None))
    downloads.prowlarr.results["Artist Album"] = [offer("aaaa", seeders=40, guid="https://lime.example/x"),
                                                  offer("bbbb", "MP3 320", guid="https://good.example/y")]
    downloads.find(db, WANT, "mb-album")
    assert torbox.added[-1][1] == "magnet:?xt=urn:btih:bbbb"
    torbox.have = {"aaaa"}  # cached: fine
    downloads.find(db, WANT, "mb-album")
    assert torbox.added[-1][1] == "magnet:?xt=urn:btih:aaaa"


def test_how_much_of_a_download_is_copied_here_counts_partly_fetched_files(downloads, torbox, db, tmp_path):
    downloads.prowlarr.results["Artist Album"] = [offer("abcd")]
    downloads.find(db, WANT, "mb-album")
    names = ["Album/01.flac", "Album/02.flac", "Album/03.flac"]
    torbox.list = [finished(71, "abcd", names, tmp_path)]
    sizes = [f["size"] for f in torbox.list[0]["files"]]
    folder = tmp_path / "dl" / "abcd"
    (folder / "Album").mkdir(parents=True)
    (folder / names[0]).write_bytes((tmp_path / "cdn" / "0").read_bytes())  # fetched before a restart
    (folder / "Album/02.flac.part").write_bytes(b"x" * (sizes[1] // 2))  # cut short
    seen, link = [], torbox.link
    torbox.link = lambda torrent_id, file_id: seen.append(downloads.copied("abcd")) or link(torrent_id, file_id)
    downloads.cycle()
    assert seen[0] == (sizes[0] + sizes[1] // 2) / sum(sizes)
    assert downloads.copied("other") is None


def test_a_trickling_torrent_makes_way_when_slots_are_full_and_albums_wait_and_is_tried_last_again(downloads, torbox, db, monkeypatch):
    now = [time.time()]
    monkeypatch.setattr(time, "time", lambda: now[0])
    downloads.prowlarr.results["Artist Album"] = [offer("abcd")]
    downloads.find(db, WANT, "mb-album")
    torbox.list = [tb(71, "abcd", progress=0.01, seeds=1)]
    downloads.cycle()
    crowd(downloads, db)
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
    downloads.prowlarr.results["Artist Album"] = [offer("abcd", seeders=90), offer("beef", seeders=1)]
    downloads.find(db, WANT, "mb-album")
    assert torbox.added[-1][1] == "magnet:?xt=urn:btih:beef"  # tried last
    downloads.prowlarr.results["Artist Album"] = [offer("abcd", seeders=90)]
    downloads.find(db, WANT, "mb-album")
    assert torbox.added[-1][1] == "magnet:?xt=urn:btih:abcd"  # not never


@pytest.mark.parametrize("swarm, why", [({"peers": 0}, "dead torrent: nobody shares it"),
                                        ({"peers": 3, "availability": 0.6}, "dead torrent: its peers have only 60% of it")])
def test_a_torrent_nobody_or_only_part_of_which_is_shared_goes_after_10_minutes(downloads, torbox, db, monkeypatch, swarm, why):
    now = [time.time()]
    monkeypatch.setattr(time, "time", lambda: now[0])
    crowd(downloads, db)
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


def test_while_slots_are_free_a_torrent_without_seeders_gets_half_an_hour(downloads, torbox, db, monkeypatch):
    now = [time.time()]
    monkeypatch.setattr(time, "time", lambda: now[0])
    downloads.prowlarr.results["Artist Album"] = [offer("abcd")]
    downloads.find(db, WANT, "mb-album")
    torbox.list = [tb(71, "abcd", progress=0, seeds=0, peers=0)]
    downloads.cycle()
    now[0] += 1500
    downloads.cycle()
    assert db.execute("select state from download").fetchone()[0] == "downloading"
    now[0] += 400
    downloads.cycle()
    assert db.execute("select state, error from download").fetchone()[:] == ("failed", "dead torrent: nobody shares it")


def test_a_slow_torrent_makes_way_for_the_searchs_next_pick_even_with_slots_free(downloads, torbox, db, monkeypatch):
    now = [time.time()]
    monkeypatch.setattr(time, "time", lambda: now[0])
    downloads.prowlarr.results["Artist Album"] = [offer("abcd", seeders=90), offer("beef", seeders=3)]
    downloads.find(db, WANT, "mb-album")
    now[0] += 3600
    torbox.list = [tb(71, "abcd", progress=0.01, seeds=1, speed=50e3, size=400e6)]  # 1 % in 1 h, but done in 2 h at its speed now
    downloads.cycle()
    assert len(torbox.added) == 1 and db.execute("select state from download").fetchone()[0] == "downloading"
    torbox.list = [tb(71, "abcd", progress=0.01, seeds=1, speed=1e3, size=400e6)]
    downloads.cycle()
    assert torbox.added[1][1] == "magnet:?xt=urn:btih:beef" and torbox.deleted == [71]
    assert [tuple(r) for r in db.execute("select hash, state, error from download order by created")] == [
        ("abcd", "failed", "too slow: 1% in 1 h"), ("beef", "added", None)]
    assert "abcd" in downloads.slow(db) and downloads.groups(db) == {"mb-album"}


def test_a_slow_torrent_keeps_going_when_its_search_found_nothing_else(downloads, torbox, db, monkeypatch):
    now = [time.time()]
    monkeypatch.setattr(time, "time", lambda: now[0])
    downloads.prowlarr.results["Artist Album"] = [offer("abcd")]
    downloads.find(db, WANT, "mb-album")
    now[0] += 7200
    torbox.list = [tb(71, "abcd", progress=0.02, seeds=1)]
    downloads.cycle()
    assert len(torbox.added) == 1 and torbox.deleted == [] and db.execute("select state from download").fetchone()[0] == "downloading"


def test_a_slow_torrent_is_not_judged_before_its_warm_up(downloads, torbox, db, monkeypatch):
    now = [time.time()]
    monkeypatch.setattr(time, "time", lambda: now[0])
    downloads.prowlarr.results["Artist Album"] = [offer("abcd", seeders=90), offer("beef", seeders=3)]
    downloads.find(db, WANT, "mb-album")
    now[0] += download.WARM_UP - 60
    torbox.list = [tb(71, "abcd", progress=0, seeds=1)]
    downloads.cycle()
    assert len(torbox.added) == 1 and db.execute("select state from download").fetchone()[0] == "downloading"


def test_a_slow_torrent_makes_way_for_a_cached_pick_first(downloads, torbox, db, monkeypatch):
    now = [time.time()]
    monkeypatch.setattr(time, "time", lambda: now[0])
    downloads.prowlarr.results["Artist Album"] = [offer("abcd", seeders=90), offer("beef", seeders=50), offer("cafe", seeders=2)]
    downloads.find(db, WANT, "mb-album")
    torbox.have = {"cafe"}  # cached since the search
    now[0] += 3600
    torbox.list = [tb(71, "abcd", progress=0.01, seeds=1)]
    downloads.cycle()
    assert torbox.added[1][1] == "magnet:?xt=urn:btih:cafe"
    assert [r["infoHash"] for r in json.loads(db.execute("select picks from download where hash = 'cafe'").fetchone()[0])] == ["beef"]


def test_a_download_torbox_leaves_out_of_its_list_is_gone_only_after_polls_in_a_row(downloads, torbox, db):
    downloads.prowlarr.results["Artist Album"] = [offer("abcd")]
    downloads.find(db, WANT, "mb-album")
    for listed in ([], [], [tb(71, "abcd", progress=0.5)], [], []):  # an empty or short list now and then
        torbox.list = listed
        downloads.cycle()
    assert db.execute("select state from download").fetchone()[0] == "downloading" and torbox.deleted == []
    torbox.list = [tb(80, "other", progress=0.5)]
    downloads.cycle()
    assert db.execute("select state, error from download").fetchone()[:] == ("failed", "gone from TorBox")
    assert db.execute("select count(*) from release_block").fetchone()[0] == 0


def test_a_slow_torrent_keeps_its_picks_while_torbox_has_no_free_slot_for_the_next(downloads, torbox, db, monkeypatch):
    now = [time.time()]
    monkeypatch.setattr(time, "time", lambda: now[0])
    downloads.prowlarr.results["Artist Album"] = [offer("abcd", seeders=90), offer("beef", seeders=3)]
    downloads.find(db, WANT, "mb-album")
    now[0] += 3600
    torbox.list, torbox.full = [tb(71, "abcd", progress=0.01, seeds=1)], True
    downloads.cycle()
    assert torbox.added[1][1] == "magnet:?xt=urn:btih:beef" and torbox.deleted == []
    assert db.execute("select state from download").fetchone()[0] == "downloading" and db.execute("select picks from download").fetchone()[0]
    torbox.full = False
    downloads.cycle()
    assert torbox.added[2][1] == "magnet:?xt=urn:btih:beef" and torbox.deleted == [71]


def keep(db, hash="abcd", groups=None, tried=()):
    """A remembered bundle (bundle_kept) holding the album 'g-album', TorBox having it cached."""
    groups = groups or {"g-album": ["Disco/Album/01.flac", "Disco/Album/02.flac"]}
    db.execute("insert into bundle_kept values (?, 'Artist - Discography', 'mb-artist', '[]', ?, ?, ?)", (hash, json.dumps(groups), time.time(), json.dumps(list(tried))))


def test_a_remembered_bundle_torbox_had_no_free_slot_for_still_serves_the_album(downloads, torbox, db):
    keep(db)
    torbox.have, torbox.full = {"abcd"}, True
    assert downloads.find(db, WANT, "g-album") is Outcome.NO_SLOT and downloads.prowlarr.asked == []
    assert db.execute("select tried from bundle_kept").fetchone()[0] == "[]"
    torbox.full = False
    assert downloads.find(db, WANT, "g-album") is Outcome.SENT
    assert json.loads(db.execute("select tried from bundle_kept").fetchone()[0]) == ["g-album"]


def test_a_remembered_bundle_whose_files_changed_is_forgotten_not_blocked(downloads, torbox, db, tmp_path):
    keep(db)
    torbox.have = {"abcd"}
    assert downloads.find(db, WANT, "g-album") is Outcome.SENT
    torbox.list = [finished(71, "abcd", ["Disco/Album (2001)/01.flac", "Disco/Album (2001)/02.flac"], tmp_path)]
    downloads.cycle()
    assert db.execute("select state from download").fetchone()[0] == "failed" and torbox.deleted == [71]
    assert db.execute("select count(*) from release_block").fetchone()[0] == db.execute("select count(*) from bundle_kept").fetchone()[0] == 0


def test_a_bundle_downloaded_again_keeps_what_it_was_tried_for_before(downloads, torbox, db, tmp_path):
    keep(db, tried=["g-old"])
    db.execute("insert into download(hash, album_id, title, torbox_id, state, created, updated, catalogue) values "
               "('abcd', 0, 'Artist - Discography', 71, 'fetching', 0, 0, ?)", (json.dumps({"want": WANT, "groups": []}),))
    db.execute("insert into download_target(hash, rg, files, state) values ('abcd', 'g-album', ?, 'pending')", (json.dumps(["Disco/Album/01.flac"]),))
    torbox.list = [finished(71, "abcd", ["Disco/Album/01.flac"], tmp_path)]
    downloads.cycle()
    [t.join(10) for t in threading.enumerate() if t.name == "remember bundle"]
    assert db.execute("select state from download").fetchone()[0] == "done"
    assert json.loads(db.execute("select tried from bundle_kept").fetchone()[0]) == ["g-album", "g-old"]


def test_an_indexer_set_off_is_never_seeded_and_rules_come_from_our_config_over_two_generic_defaults(downloads, torbox, db, tmp_path):
    downloads.prowlarr = LoginProwlarr({"Artist Album": [offer("abcd")]})
    downloads.rules = {"Some-Tracker": {"mode": "off"}}
    assert downloads.find(db, WANT, "mb-album") and torbox.seeded == ["off"]
    torbox.list = [finished(71, "abcd", ["Album/01 Song.flac"], tmp_path)]
    downloads.cycle()
    assert db.execute("select state, seeding, mode from download").fetchone()[:] == ("done", 0, "off") and 71 in torbox.deleted
    assert downloads.rule("Unknown Login", login=True) == {"mode": "seed", **download.SEEDING}
    assert downloads.rule("Unknown Open", login=False) == {"mode": "torbox", **download.SEEDING}
    downloads.rules = {"Tracker K": {"mode": "seed", "days": 14, "min_ratio": 0.7}}
    assert (downloads.rule("Tracker K", login=False)["mode"], downloads.rule("Tracker K")["days"], downloads.rule_source("Tracker K")) == ("seed", 14, "oasis.toml")
    downloads.save_rule(db, "Tracker K", {"mode": "seed", "ratio": 1.05, "days": 7, "min_hours": 0, "min_ratio": None})
    assert (downloads.rule("Tracker K")["ratio"], downloads.rule("Tracker K")["min_ratio"], downloads.rule_source("Tracker K")) == (1.05, None, "oasis database")


def test_downloads_sent_before_modes_keep_how_they_were_sent(downloads, db):
    db.executemany("insert into download(hash, album_id, title, state, created, updated, tracker) values (?, 0, ?, 'done', 0, 0, ?)",
                   [("a", "A", "Some-Tracker"), ("b", "B", None)])
    assert [download.mode_of(r) for r in db.execute("select * from download order by hash")] == ["seed", "off"]


def test_a_low_ratio_trackers_releases_come_after_other_indexers_offering_the_album():
    picks = [{"title": "A [FLAC]", "infoHash": "aa", "seeders": 90, "indexerId": 40, "downloadUrl": "u"},
             {"title": "A [FLAC]", "infoHash": "bb", "seeders": 5, "indexerId": 32, "downloadUrl": "u"}]
    assert [r["infoHash"] for r in releases.rank(picks, set())] == ["aa", "bb"]
    assert [r["infoHash"] for r in releases.rank(picks, set(), low=frozenset({40}))] == ["bb", "aa"]


def test_album_searches_count_each_indexers_results_and_downloads_note_the_indexer(downloads, torbox, db):
    downloads.prowlarr.results["Artist Album"] = [offer("abcd", indexerId=22), result("Other - Thing [FLAC]", "ffff", indexerId=22),
                                                  offer("bcde", indexerId=35, seeders=0)]
    assert downloads.find(db, WANT, "mb-album")
    assert [tuple(r) for r in db.execute("select indexer, results, album, usable from search_stat order by indexer")] == [(22, 2, 1, 1), (35, 1, 1, 0)]
    assert db.execute("select indexer from download").fetchone()[0] == 22
