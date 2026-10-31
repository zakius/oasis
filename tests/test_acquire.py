import io
import json
import threading
import time
import urllib.parse

import pytest

from conftest import call, make_client
from oasis import acquire, musicbrainz
from oasis.acquire import Acquire
from oasis.db import connect
from oasis.download import Downloads


def group(id, title, kind="Album", date="2001", secondary=(), artist=("mb-beta", "Beta")):
    return {"id": id, "title": title, "primary-type": kind, "secondary-types": list(secondary), "first-release-date": date,
            "artist-credit": [{"name": artist[1], "artist": {"id": artist[0], "name": artist[1]}}]}


class MusicBrainz:
    """Answers acquire's requests like MusicBrainz: catalogues by artist, editions' songs by release group (None: no
    official edition), searches by query."""

    def __init__(self):
        self.catalogues, self.songs, self.artists, self.found, self.releases, self.asked = {}, {}, [], [], {}, []

    def __call__(self, url, db=None, keep=0):
        self.asked.append(url)
        path, query = url.removeprefix(acquire.MB).split("?", 1)
        q = dict(urllib.parse.parse_qsl(query))
        if path == "release-group" and "artist" in q:
            groups = self.catalogues.get(q["artist"], [])
            return {"release-groups": groups[int(q["offset"]):][:100], "release-group-count": len(groups)}
        if path == "release":
            songs = self.songs.get(q["release-group"])
            return {"releases": [] if songs is None else [{"media": [{"tracks": [{"title": s} for s in songs]}]},
                                                          {"media": [{"tracks": [{"title": s} for s in songs + ["Bonus"]]}] * 2}]}
        if path.startswith("release-group/"):
            return next(g for gs in self.catalogues.values() for g in gs if g["id"] == path.split("/")[1])
        if path.startswith("release/"):
            return {"release-group": {"id": self.releases[path.split("/")[1]]}}
        if path == "artist":
            return {"artists": self.artists}
        if path == "release-group":
            return {"release-groups": self.found}
        if path.startswith("artist/"):  # aliases, for the search
            return {"name": "Beta", "aliases": []}
        raise AssertionError(url)


class FakeDownloads:
    def __init__(self):
        self.active, self.found, self.result, self.free = set(), [], None, True

    def groups(self, db):
        return set(self.active)

    def room(self):
        return self.free

    def find(self, db, want, group):
        self.found.append(group)
        self.wants = getattr(self, "wants", []) + [want]
        return self.result


class FakeLidarr:
    def __init__(self):
        self.calls, self.queue, self.albums = [], set(), []

    def queued_groups(self):
        self.calls.append("queue")
        return self.queue

    def all_albums(self):
        self.calls.append("albums")
        return self.albums

    def rescan(self, folders):
        self.calls.append("rescan")


@pytest.fixture
def mb(monkeypatch):
    fake = MusicBrainz()
    monkeypatch.setattr(musicbrainz, "get", fake)
    return fake


@pytest.fixture
def lidarr():
    return FakeLidarr()


@pytest.fixture
def client(library, tmp_path, lidarr, mb):
    with make_client(library, tmp_path / "oasis.db") as c:
        c.app.state.api.library = Acquire(c.app.state.api._db, FakeDownloads(), lidarr)
        yield c


def sync(client):
    client.app.state.api.library.reconcile()


def artist_id(client, name):
    return next(a for i in call(client, "getArtists")["artists"]["index"] for a in i["artist"] if a["name"] == name)["id"]


def album_of(client, title):
    return next(a for a in call(client, "getAlbumList2", type="alphabeticalByName", size=50)["albumList2"]["album"] if a["name"] == title)


def wants(client) -> dict:
    db = client.app.state.api._db()
    return {r["title"]: (r["state"], r["reason"], r["error"]) for r in db.execute("select * from want_album")}


def follow_beta(client, mb):
    db = client.app.state.api._db()
    db.execute("update track set mb_release_group = 'g-first' where album = 'First'")
    db.execute("insert into mb_artist(mbid, artist_id, name) values ('mb-beta', ?, 'Beta')", (artist_id(client, "Beta"),))
    mb.catalogues["mb-beta"] = [
        group("g-first", "First", date="2001"), group("g-second", "Second", date="2003"),
        group("g-hits", "Greatest Hits", date="2005"), group("g-again", "Again", date="2006"),
        group("g-live", "Live at Home", date="2007", secondary=["Live"]), group("g-ost", "Film", date="2008", secondary=["Soundtrack"]),
        group("g-one", "One", "Single", "2001"), group("g-new", "New Song", "Single", "2009"), group("g-ep", "Promo", "EP", "2010"),
        group("g-deluxe", "Second (Deluxe)", date="2011")]
    mb.songs.update({"g-first": ["One", "Two"], "g-second": ["Three", "Four", "Five"], "g-hits": ["New"], "g-again": ["One", "Three", "Four", "Six"],
                     "g-ost": ["Theme"], "g-one": ["One (Radio Edit)"], "g-new": ["New", "One - Live"], "g-ep": None, "g-deluxe": ["Three"]})
    call(client, "star", artistId=artist_id(client, "Beta"))
    sync(client)


def test_a_followed_artists_catalogue_leaves_out_repackages_and_singles_with_nothing_new(client, mb):
    follow_beta(client, mb)
    assert wants(client) == {
        "First": ("have", "followed", None),  # its 2 tracks are here, as many as the smallest edition has
        "Second": ("missing", "followed", None), "Film": ("missing", "followed", "searched, no release found yet"),  # newest first
        "Greatest Hits": ("skipped", "followed", "a repackage"),
        "Again": ("skipped", "followed", "most of its songs are on earlier releases"),  # 3 of 4
        "One": ("skipped", "followed", "its songs are on the artist's albums"),
        "New Song": ("missing", "followed", None),  # a song on no album or EP kept
        "Promo": ("skipped", "followed", "no official release"),
        "Second (Deluxe)": ("skipped", "followed", "a repackage")}  # live albums aren't in the catalogue
    db = client.app.state.api._db()
    assert db.execute("select tracks, discs from mb_group where rg = 'g-second'").fetchone()[:] == (3, 2)
    assert db.execute("select state, catalogued is not null from lidarr_item where kind = 'artist'").fetchone()[:] == ("followed", 1)

    db.execute("update track set mb_release_group = 'g-hits' where album = 'Second'")  # a repackage with files isn't left out
    mb.catalogues["mb-beta"] = mb.catalogues["mb-beta"][1:]  # nor is what the catalogue lost kept
    db.execute("update lidarr_item set catalogued = 0")
    sync(client)
    assert wants(client)["Greatest Hits"] == ("have", "followed", None) and "First" not in wants(client)


def test_have_counts_library_tracks_and_maps_files_tagged_without_their_release_group(client, mb):
    follow_beta(client, mb)
    db = client.app.state.api._db()
    db.execute("update track set mb_release_group = null, mb_release = 'r-first' where album = 'First'")
    db.execute("update track set mb_release_group = 'g-second', mb_track = 'same' where album = 'Second'")  # 2 files of one track
    mb.releases["r-first"] = "g-first"
    sync(client)
    assert wants(client)["First"][0] == "have" and wants(client)["Second"][0] == "missing"
    assert {r[0] for r in db.execute("select mb_release_group from track where album = 'First'")} == {"g-first"}


def test_missing_albums_are_searched_in_order_and_again_ever_further_apart(client, mb, lidarr, monkeypatch):
    follow_beta(client, mb)
    acq, now = client.app.state.api.library, [time.time()]
    monkeypatch.setattr(time, "time", lambda: now[0])

    def search():
        now[0] += acquire.SEARCH_GAP
        sync(client)
        return acq.downloads.found[-1] if acq.downloads.found else None
    assert acq.downloads.found == ["g-ost"]  # newest album first; singles after albums and EPs
    lidarr.queue.add("g-second")  # Lidarr's RSS grab has it
    assert search() == "g-new"
    lidarr.queue.clear()
    assert search() == "g-second"
    assert search() == "g-second" and len(acq.downloads.found) == 3  # nothing due: no search
    db = client.app.state.api._db()
    row = db.execute("select * from want_album where rg = 'g-ost'").fetchone()
    assert (row["tries"], row["next_search"] - row["searched"], row["error"]) == (1, 6 * 3600, "searched, no release found yet")
    assert db.execute("select next_search - searched from want_album where rg = 'g-new'").fetchone()[0] == 7 * 86400
    now[0] += 6 * 3600
    assert search() == "g-ost"
    assert db.execute("select next_search - searched from want_album where rg = 'g-ost'").fetchone()[0] == 12 * 3600
    acq.downloads.free = False
    now[0] += 7 * 86400
    assert search() == "g-ost" and len(acq.downloads.found) == 4  # TorBox full: nothing starts
    acq.downloads.free, acq.downloads.result = True, False
    search()
    assert db.execute("select tries, next_search - ? from want_album where rg = ?", (now[0], acq.downloads.found[-1])).fetchone()[:] \
        in [(1, acquire.RETRY_FULL), (2, acquire.RETRY_FULL)]  # no free slot: soon again, not counted as a try
    acq.downloads.result = True
    sent = search()
    acq.downloads.active.add(sent)
    sync(client)
    assert wants(client)[db.execute("select title from want_album where rg = ?", (sent,)).fetchone()[0]][0] == "downloading"
    want = acq.downloads.wants[-1]
    assert want["artist"] == "Beta" and want["names"] == ["Beta"] and "Greatest Hits" in want["others"] and want["upgrade"] is False


def test_a_new_star_resolves_through_musicbrainz_search_exact_matches_only(client, mb):
    db = client.app.state.api._db()
    db.execute("insert into ytm_artist(artist_id, name, fetched) values ('ar-yt', 'Kenshi Yonezu', 0)")
    db.execute("insert into ytm_release values ('al-yt', 'ar-yt', 'MPRE', 'STRAY SHEEP', 2020, 'Album', null)")
    mb.artists = [{"id": "mb-other", "name": "Kenshi Yonezu Tribute", "aliases": []},
                  {"id": "mb-kenshi", "name": "米津玄師", "aliases": [{"name": "Kenshi Yonezu"}]}]
    mb.found = [group("g-live", "STRAY SHEEP (Live)", artist=("mb-kenshi", "米津玄師")), group("g-ss", "Stray Sheep", artist=("mb-kenshi", "米津玄師"))]
    mb.catalogues["mb-kenshi"] = mb.found
    mb.songs["g-ss"] = ["感電", "Lemon"]
    call(client, "star", albumId="al-yt")
    sync(client)
    assert any("arid%3Amb-kenshi" in u for u in mb.asked) and not any("arid%3Amb-other" in u for u in mb.asked)
    assert wants(client) == {"Stray Sheep": ("missing", "starred", "searched, no release found yet")}  # searched at once: starred first
    item = db.execute("select state, mbid, error from lidarr_item").fetchone()
    assert item[:] == ("wanted", "g-ss", "waiting for its search")
    sync(client)
    assert db.execute("select error from lidarr_item").fetchone()[0] == "searched, no release found yet"
    assert db.execute("select artist from want_album").fetchone()[0] == "米津玄師"

    mb.artists, mb.found = [], []  # an artist MusicBrainz doesn't know: failed, looked up again later
    call(client, "star", artistId=artist_id(client, "The Alpha"))
    sync(client)
    row = db.execute("select state, error, retry_at > ? from lidarr_item where kind = 'artist'", (time.time(),)).fetchone()
    assert row[:] == ("failed", "no MusicBrainz artist named The Alpha", 1)

    call(client, "unstar", albumId="al-yt")
    sync(client)
    assert wants(client) == {}


def test_unfollowing_drops_the_catalogue_but_keeps_starred_albums_and_unstarring_gives_them_back(client, mb):
    follow_beta(client, mb)
    db = client.app.state.api._db()
    db.execute("insert into lidarr_item(kind, ref, state, mbid, updated) values ('album', 'al-x', 'wanted', 'g-hits', 0)")
    db.execute("insert into wanted values ('admin', 'album', 'al-x')")
    sync(client)
    assert wants(client)["Greatest Hits"] == ("missing", "starred", None)  # starred: never left out
    call(client, "unstar", artistId=artist_id(client, "Beta"))
    sync(client)
    assert list(wants(client)) == ["Greatest Hits"]
    call(client, "star", artistId=artist_id(client, "Beta"))
    sync(client)
    db.execute("delete from wanted where ref = 'al-x'")
    sync(client)
    sync(client)
    assert wants(client)["Greatest Hits"] == ("skipped", "followed", "a repackage")  # back in the catalogue, left out again


def test_lidarrs_searches_carry_over_and_lidarr_is_asked_nothing_else(client, mb, lidarr):
    db = client.app.state.api._db()
    searched = time.time() - 3600
    db.execute("insert into catalogue_search(album_id, searched) values (12, ?)", (searched,))
    lidarr.albums = [{"id": 12, "foreignAlbumId": "g-second"}, {"id": 13, "foreignAlbumId": "g-ost"}]
    follow_beta(client, mb)
    row = db.execute("select * from want_album where rg = 'g-second'").fetchone()
    assert (row["searched"], row["tries"], row["next_search"]) == (searched, 1, searched + acquire.RESEARCH)
    assert db.execute("select searched from want_album where rg = 'g-ost'").fetchone()[0] is not None  # never searched: first
    assert set(lidarr.calls) == {"albums", "queue"}  # no lookups, no monitoring


def test_catalogue_reads_keep_musicbrainzs_pace(tmp_path, monkeypatch):
    sent = []

    def urlopen(request, timeout):
        sent.append(time.monotonic())
        assert request.get_header("User-agent") == musicbrainz.USER_AGENT
        if "release-group?artist" in request.full_url:
            return io.BytesIO(json.dumps({"release-groups": [group(f"g{i}", f"T{i}") for i in range(2)], "release-group-count": 2}).encode())
        return io.BytesIO(json.dumps({"releases": [{"media": [{"tracks": [{"title": "x"}]}]}]}).encode())

    monkeypatch.setattr(musicbrainz.urllib.request, "urlopen", urlopen)
    connect(tmp_path / "oasis.db").execute("insert into lidarr_item(kind, ref, state, mbid, updated) values ('artist', 'ar-b', 'followed', 'mb-beta', 0)")
    acq = Acquire(lambda: connect(tmp_path / "oasis.db"), FakeDownloads())
    read = lambda db: acq.catalogue(db, db.execute("select * from lidarr_item").fetchone())
    reader = threading.Thread(target=lambda: read(connect(tmp_path / "oasis.db")))
    other = threading.Thread(target=lambda: musicbrainz.get("https://musicbrainz.org/ws/2/release/r?fmt=json"))
    reader.start(), other.start()
    reader.join(), other.join()
    gaps = [b - a for a, b in zip(sorted(sent), sorted(sent)[1:])]
    assert len(sent) == 4 and min(gaps) >= 1.0


def test_downloads_failing_make_the_album_due_again(tmp_path):
    db = connect(tmp_path / "oasis.db")
    db.execute("insert into want_album(rg, artist_mbid, title, reason, state, next_search) values ('g', 'a', 'T', 'followed', 'downloading', 9e9)")
    db.execute("insert into download(hash, album_id, album_mbid, title, state, created, updated) values ('h', 0, 'g', 'T', 'added', 0, 0)")

    class TorBox:
        def delete_torrent(self, id):
            pass
    made = Downloads(lambda: connect(tmp_path / "oasis.db"), None, None, str(tmp_path / "dl"))
    made.client = TorBox()
    assert made.groups(db) == {"g"}
    made.fail(db, db.execute("select * from download").fetchone(), "dead torrent")
    assert db.execute("select next_search, error from want_album").fetchone()[:] == (None, "download failed: dead torrent") and made.groups(db) == set()
