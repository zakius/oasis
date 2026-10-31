import json
import time

import pytest

from conftest import call, make_client
from oasis import lidarr as lidarr_module
from oasis.lidarr import Library, Lidarr, adopt, adoptable, artist_names


class FakeLidarr(Lidarr):
    def __init__(self):
        self.albums, self.by_mbid, self.queue_albums, self.calls, self.artist_albums, self.album_tracks = {}, {}, set(), [], {}, {}

    def find_album(self, artist, title):
        self.calls.append(("find_album", artist, title))
        return {"foreignAlbumId": f"mb-{title}", "title": title, "artist": {"artistName": artist, "foreignArtistId": f"mb-{artist}"}}

    def find_artist(self, name):
        return {"foreignArtistId": f"mb-{name}", "artistName": name}

    def album(self, mbid):
        return self.albums.get(mbid)

    def artist(self, mbid):
        return self.by_mbid.get(mbid)

    def add_album(self, found):
        self.calls.append(("add_album", found["foreignAlbumId"]))
        album = {"id": len(self.albums) + 1, "monitored": True, "foreignAlbumId": found["foreignAlbumId"]}
        self.albums[found["foreignAlbumId"]] = album
        return album

    def add_artist(self, found):
        self.calls.append(("add_artist", found["foreignArtistId"]))
        artist = {"id": 100 + len(self.by_mbid), "monitored": True, "monitorNewItems": "all", "artistName": found["artistName"]}
        self.by_mbid[found["foreignArtistId"]] = artist
        return artist

    def artists(self):
        return [{**a, "foreignArtistId": mbid} for mbid, a in self.by_mbid.items()]

    def set_albums(self, album_ids, monitored):
        self.calls.append(("set_albums", tuple(album_ids), monitored))
        for album in [*self.albums.values(), *(a for albums in self.artist_albums.values() for a in albums)]:
            if album["id"] in album_ids:
                album["monitored"] = monitored

    def search_album(self, album_id):
        self.calls.append(("search", album_id))

    def albums_of(self, artist_id):
        return self.artist_albums.get(artist_id, [])

    def tracks(self, album):
        return [{"title": t} for t in self.album_tracks.get(album["id"], [])]

    def follow_artist(self, artist, follow):
        self.calls.append(("follow", artist["id"], follow))
        artist["monitored"] = follow

    def missing(self):
        return [{**a, "artistId": artist_id} for artist_id, albums in self.artist_albums.items() for a in albums
                if a["monitored"] and not a.get("have")]

    def cutoff_unmet(self):
        return []

    def queued(self):
        return self.queue_albums, set(), len(self.queue_albums)

    def why_nothing(self, album_id):
        return "searched, nothing grabbed yet"


@pytest.fixture
def lidarr():
    return FakeLidarr()


@pytest.fixture
def client(library, tmp_path, lidarr):
    with make_client(library, tmp_path / "oasis.db") as c:
        c.app.state.api.library = Library(c.app.state.api._db, lidarr)
        yield c


def album_of(client, title):
    return next(a for a in call(client, "getAlbumList2", type="alphabeticalByName", size=50)["albumList2"]["album"] if a["name"] == title)


def sync(client):
    client.app.state.api.library.reconcile()


def test_starred_album_is_added_then_searched_paced_and_tracked_honestly(client, lidarr, monkeypatch):
    first, second = album_of(client, "First"), album_of(client, "Second")
    call(client, "star", albumId=first["id"])
    call(client, "star", albumId=second["id"])
    call(client, "star", albumId=first["id"])  # starring twice changes nothing
    sync(client)
    assert sorted(c for c in lidarr.calls if c[0] == "add_album") == [("add_album", "mb-First"), ("add_album", "mb-Second")]
    sync(client)
    assert [c for c in lidarr.calls if c[0] == "search"] == [("search", 1)]  # one search per gap
    assert album_of(client, "First")["oasisWanted"] == "wanted"
    library = client.app.state.api.library
    library.last_search -= lidarr_module.SEARCH_GAP
    sync(client)
    assert [c for c in lidarr.calls if c[0] == "search"] == [("search", 1), ("search", 2)]
    lidarr.queue_albums.add(1)
    sync(client)
    assert album_of(client, "First")["oasisWanted"] == "downloading"
    lidarr.queue_albums.clear()
    monkeypatch.setattr(time, "time", lambda real=time.time: real() + lidarr_module.GRAB_WAIT + 1)
    sync(client)
    first_now = album_of(client, "First")
    assert first_now["oasisWanted"] == "wanted" and first_now["oasisWantedError"] == "searched, nothing grabbed yet"
    lidarr.albums["mb-First"]["statistics"] = {"trackCount": 2, "trackFileCount": 2}
    sync(client)
    assert "oasisWanted" not in album_of(client, "First") and "oasisWantedError" not in album_of(client, "First")


def test_unstarred_album_is_only_unmonitored_once_nobody_wants_it(client, lidarr):
    first = album_of(client, "First")
    call(client, "createUser", username="bob", password="pw")
    call(client, "star", albumId=first["id"])
    call(client, "star", albumId=first["id"], u="bob", p="pw")
    sync(client)
    call(client, "unstar", albumId=first["id"])
    sync(client)
    assert ("set_albums", (1,), False) not in lidarr.calls  # bob still wants it
    call(client, "unstar", albumId=first["id"], u="bob", p="pw")
    sync(client)
    assert ("set_albums", (1,), False) in lidarr.calls
    assert "oasisWanted" not in album_of(client, "First")


def test_starred_artist_gets_its_catalogue_searched_paced(client, lidarr):
    artist = next(a for i in call(client, "getArtists")["artists"]["index"] for a in i["artist"] if a["name"] == "The Alpha")
    call(client, "star", artistId=artist["id"])
    sync(client)
    assert ("add_artist", "mb-The Alpha") in lidarr.calls and not any(c[0] == "search" for c in lidarr.calls)
    shown = next(a for i in call(client, "getArtists")["artists"]["index"] for a in i["artist"] if a["name"] == "The Alpha")
    assert "oasisWanted" not in shown  # followed: nothing to show
    # After Lidarr's first refresh, albums and EPs get monitored once; singles don't.
    lidarr.artist_albums[100] = [{"id": 50, "monitored": False, "albumType": "Album", "foreignAlbumId": "mb-50", "title": "Herzeleid", "releaseDate": "2001"},
                                 {"id": 51, "monitored": False, "albumType": "EP", "foreignAlbumId": "mb-51", "title": "Neu EP", "releaseDate": "2002"},
                                 {"id": 52, "monitored": False, "albumType": "Single", "foreignAlbumId": "mb-52", "title": "Engel", "releaseDate": "2003"},
                                 {"id": 53, "monitored": False, "albumType": "Album", "foreignAlbumId": "mb-53", "title": "Live aus Berlin", "releaseDate": "2010"},
                                 {"id": 54, "monitored": False, "albumType": "Album", "foreignAlbumId": "mb-54", "releaseDate": "2011",
                                  "title": "Sehnsucht (Anniversary Edition)"}]
    lidarr.album_tracks = {50: ["Engel", "Du hast", "Mein Herz"], 51: ["Neu"], 53: ["Engel (Remastered)", "Du Hast - Live", "Neu"]}
    library = client.app.state.api.library
    sync(client)
    sync(client)
    # 53 mostly repeats 50's songs, 54 is an anniversary edition: neither is monitored.
    assert [c for c in lidarr.calls if c[0] == "set_albums"] == [("set_albums", (50, 51), True), ("set_albums", (), False)]
    assert [c for c in lidarr.calls if c[0] == "search"] == [("search", 50)]
    library.last_search -= lidarr_module.SEARCH_GAP
    lidarr.queue_albums.update({*range(1, lidarr_module.MAX_ACTIVE), 50})  # busy: no new search
    sync(client)
    assert [c for c in lidarr.calls if c[0] == "search"] == [("search", 50)]
    lidarr.queue_albums.clear()
    sync(client)
    assert [c for c in lidarr.calls if c[0] == "search"] == [("search", 50), ("search", 51)]
    call(client, "unstar", artistId=artist["id"])
    sync(client)
    assert ("follow", 100, False) in lidarr.calls and ("set_albums", (50, 51), False) in lidarr.calls


def test_repackages_lidarr_monitors_later_are_unmonitored_unless_starred_or_downloaded(client, lidarr, monkeypatch):
    artist = next(a for i in call(client, "getArtists")["artists"]["index"] for a in i["artist"] if a["name"] == "The Alpha")
    call(client, "star", artistId=artist["id"])
    lidarr.artist_albums[100] = [{"id": 60, "monitored": False, "albumType": "Album", "foreignAlbumId": "mb-60", "title": "First", "releaseDate": "2001"}]
    lidarr.album_tracks = {60: ["One", "Two"], 61: ["One", "Two", "Three"], 62: ["One", "Two"]}
    sync(client)
    sync(client)
    lidarr.artist_albums[100] += [
        {"id": 61, "monitored": True, "albumType": "Album", "foreignAlbumId": "mb-61", "title": "Greatest Hits", "releaseDate": "2020"},
        {"id": 62, "monitored": True, "albumType": "Album", "foreignAlbumId": "mb-62", "title": "Hits", "releaseDate": "2021",
         "statistics": {"trackFileCount": 2}}]
    monkeypatch.setattr(time, "time", lambda real=time.time: real() + lidarr_module.RECHECK_CATALOGUE + 1)
    sync(client)
    assert ("set_albums", (61,), False) in lidarr.calls and not any(c[0] == "set_albums" and 62 in c[1] for c in lidarr.calls)


def test_no_match_fails_with_reason_and_waits_while_errors_retry_soon(client, lidarr, monkeypatch):
    first = album_of(client, "First")
    monkeypatch.setattr(lidarr, "find_album", lambda artist, title: None)
    call(client, "star", albumId=first["id"])
    sync(client)
    shown = album_of(client, "First")
    assert shown["oasisWanted"] == "failed" and "no MusicBrainz album" in shown["oasisWantedError"]
    asked = []
    monkeypatch.setattr(lidarr, "find_album", lambda artist, title: asked.append(title))
    sync(client)
    assert asked == []
    db = client.app.state.api._db()
    retry = db.execute("select retry_at - updated from lidarr_item").fetchone()[0]
    assert abs(retry - lidarr_module.RETRY_NO_MATCH) < 5

    def down(artist, title):
        raise OSError("Lidarr GET album/lookup: 503")
    monkeypatch.setattr(lidarr, "find_album", down)
    db.execute("update lidarr_item set retry_at = null")
    sync(client)
    shown = album_of(client, "First")
    assert shown["oasisWanted"] == "wanted" and "503" in shown["oasisWantedError"]
    assert abs(db.execute("select retry_at - updated from lidarr_item").fetchone()[0] - lidarr_module.RETRY_ERROR) < 5


def test_artists_followed_in_lidarr_itself_are_adopted_once_by_mbid_and_searched(client, lidarr, tmp_path):
    starred = next(a for i in call(client, "getArtists")["artists"]["index"] for a in i["artist"] if a["name"] == "Beta")
    call(client, "star", artistId=starred["id"])
    sync(client)  # Beta: followed through oasis, Lidarr 100
    lidarr.by_mbid.update({"mb-hunter-de": {"id": 7, "artistName": "Hunter", "monitored": True, "monitorNewItems": "all"},
                           "mb-hunter-pl": {"id": 8, "artistName": "Hunter", "monitored": True, "monitorNewItems": "all"},
                           "mb-alpha": {"id": 9, "artistName": "The Alpha", "monitored": True, "monitorNewItems": "all"},
                           "mb-ost": {"id": 10, "artistName": "Composer", "monitored": False, "monitorNewItems": "none"}})  # a starred album's
    lidarr.artist_albums[7] = [{"id": 80, "monitored": False, "albumType": "Album", "foreignAlbumId": "mb-80", "title": "Bez", "releaseDate": "2005"},
                               {"id": 81, "monitored": True, "albumType": "Single", "foreignAlbumId": "mb-81", "title": "Kiedy", "releaseDate": "2006"}]
    db = client.app.state.api._db()
    found = adoptable(db, lidarr)
    assert sorted(a["id"] for a in found) == [7, 8, 9]
    adopt(db, lidarr, "zakius", found, tmp_path / "snapshot.json")
    assert {a["id"]: a["monitoredAlbumIds"] for a in json.loads((tmp_path / "snapshot.json").read_text())} == {7: [81], 8: [], 9: []}
    rows = {r["lidarr_id"]: r for r in db.execute("select * from lidarr_item where kind = 'artist'")}
    assert rows[7]["ref"] != rows[8]["ref"] and rows[7]["state"] == rows[8]["state"] == "followed"  # one name, two artists
    assert all(db.execute("select 1 from wanted where kind = 'artist' and ref = ?", (rows[i]["ref"],)).fetchone() for i in (7, 8, 9))
    shown = call(client, "getStarred2")["starred2"]["artist"]
    assert {a["name"] for a in shown} == {"Beta", "The Alpha"} and rows[9]["ref"] == next(a["id"] for a in shown if a["name"] == "The Alpha")
    assert adoptable(db, lidarr) == []  # a second run changes nothing
    sync(client)  # the Hunters have no tracks in oasis: still followed, catalogue monitored, searched
    assert all(r["state"] == "followed" and r["error"] is None for r in db.execute("select * from lidarr_item where kind = 'artist'"))
    assert ("set_albums", (80,), True) in lidarr.calls and [c for c in lidarr.calls if c[0] == "search"] == [("search", 80)]


def test_collaboration_credits_split_into_artist_names():
    assert artist_names("SawanoHiroyuki[nZk]:mizuki") == ["SawanoHiroyuki[nZk]:mizuki", "SawanoHiroyuki[nZk]", "SawanoHiroyuki", "mizuki"]
    assert artist_names("Falling In Reverse, Corey Taylor, Serj Tankian")[:2] == ["Falling In Reverse, Corey Taylor, Serj Tankian", "Falling In Reverse"]
    assert artist_names("A feat. B")[1:] == ["A", "B"] and artist_names("X × Y")[1:] == ["X", "Y"]


def test_a_followed_artists_singles_wait_behind_its_albums(client, lidarr):
    artist = next(a for i in call(client, "getArtists")["artists"]["index"] for a in i["artist"] if a["name"] == "The Alpha")
    call(client, "star", artistId=artist["id"])
    sync(client)
    lidarr.artist_albums[100] = [{"id": 70, "monitored": True, "albumType": "Single", "foreignAlbumId": "mb-70", "title": "New Single", "releaseDate": "2024"},
                                 {"id": 71, "monitored": False, "albumType": "Album", "foreignAlbumId": "mb-71", "title": "Old Album", "releaseDate": "2001"}]
    library = client.app.state.api.library
    sync(client)
    for _ in range(2):
        library.last_search -= lidarr_module.SEARCH_GAP
        sync(client)
    assert [c for c in lidarr.calls if c[0] == "search"] == [("search", 71), ("search", 70)]


def release(title, rejections=("Unable to parse release",), size=400e6, seeders=5, quality="Unknown", **extra):
    return {"title": title, "rejections": list(rejections), "size": size, "seeders": seeders, "guid": title, "indexerId": 1,
            "infoHash": f"HASH-{len(title)}", "quality": {"quality": {"name": quality}}, **extra}


def test_releases_lidarr_cant_read_are_matched_by_artist_and_album_title():
    flac = release("[마신] [2022.09.07] Creepy Nuts 3rdアルバム「アンサンブル・プレイ」[FLAC 48kHz/24bit]", seeders=2)
    mp3 = release("[마신] [2022.09.07] Creepy Nuts 3rdアルバム「アンサンブル・プレイ」[MP3 320K]", seeders=40)
    single = release("[2025 07 05] TVアニメ「よふかしのうた Season 2」EDテーマ「眠れ」／Creepy Nuts [FLAC]")
    rejected = release("Creepy Nuts - アンサンブル・プレイ [FLAC]", rejections=["Not enough seeders: 0"])
    bundle = release("Creepy Nuts Discography アンサンブル・プレイ クリープ・ショー [FLAC]")
    assert lidarr_module.unparsed_match([mp3, single, rejected, bundle, flac], "Creepy Nuts", "アンサンブル・プレイ") is flac
    assert lidarr_module.unparsed_match([single], "Creepy Nuts", "アンサンブル・プレイ") is None
    assert lidarr_module.unparsed_match([release("SawanoHiroyuki[nZk] V [FLAC]")], "SawanoHiroyuki[nZk]", "V") is None  # too short to tell


def test_search_grabs_lidarrs_pick_or_an_unparsed_match_and_remembers_the_latter(client):
    calls = []
    lidarr = Lidarr("http://lidarr", "key")
    results = {"release?albumId=5": [release("Creepy Nuts 3rdアルバム「アンサンブル・プレイ」[FLAC]")],
               "album/5": {"title": "アンサンブル・プレイ", "artistId": 9, "artist": {"artistName": "Creepy Nuts"}},
               "release?albumId=6": [release("Rammstein - Mutter (2001) [FLAC]", rejections=[]), release("Rammstein - Mutter [MP3]", rejections=[])]}
    lidarr.call = lambda method, path, body=None: calls.append((method, path, body)) or results.get(path)
    assert lidarr.search_album(6) is None and calls[-1][2] == {"guid": "Rammstein - Mutter (2001) [FLAC]", "indexerId": 1}
    library = client.app.state.api.library or lidarr_module.Library(client.app.state.api._db, lidarr)
    library.lidarr = lidarr
    db = client.app.state.api._db()
    library.search(db, 5)
    assert calls[-1][2] == {"guid": "Creepy Nuts 3rdアルバム「アンサンブル・プレイ」[FLAC]", "indexerId": 1, "albumId": 5, "artistId": 9}
    grabbed = results["release?albumId=5"][0]["infoHash"].lower()
    assert [tuple(r) for r in db.execute("select hash, album_id from lidarr_rescue")] == [(grabbed, 5)]


def test_a_release_that_fails_to_grab_gives_way_to_the_next_and_a_failed_search_still_counts(client):
    lidarr = Lidarr("http://lidarr", "key")
    grabs = []

    def call(method, path, body=None):
        if method == "POST":
            grabs.append(body["guid"])
            if body["guid"] == "dead":
                raise OSError("Lidarr POST release: 500 Download client failed to add torrent by url")
            return None
        if path == "release?albumId=6":
            return [release("dead", rejections=[]), release("alive", rejections=[])]
        raise OSError("Lidarr GET release: 500")
    lidarr.call = call
    assert lidarr.search_album(6) is None and grabs == ["dead", "alive"]
    library = lidarr_module.Library(client.app.state.api._db, lidarr)
    library.search(client.app.state.api._db(), 7)  # the search itself fails: logged, not raised
