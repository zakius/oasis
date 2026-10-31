import time

import pytest

from conftest import call, make_client
from oasis import lidarr as lidarr_module
from oasis.lidarr import Library, Lidarr, artist_names


class FakeLidarr(Lidarr):
    def __init__(self):
        self.albums, self.artists, self.queue_albums, self.calls, self.artist_albums, self.album_tracks = {}, {}, set(), [], {}, {}

    def find_album(self, artist, title):
        self.calls.append(("find_album", artist, title))
        return {"foreignAlbumId": f"mb-{title}", "title": title, "artist": {"artistName": artist, "foreignArtistId": f"mb-{artist}"}}

    def find_artist(self, name):
        return {"foreignArtistId": f"mb-{name}", "artistName": name}

    def album(self, mbid):
        return self.albums.get(mbid)

    def artist(self, mbid):
        return self.artists.get(mbid)

    def add_album(self, found):
        self.calls.append(("add_album", found["foreignAlbumId"]))
        album = {"id": len(self.albums) + 1, "monitored": True, "foreignAlbumId": found["foreignAlbumId"]}
        self.albums[found["foreignAlbumId"]] = album
        return album

    def add_artist(self, found):
        self.calls.append(("add_artist", found["foreignArtistId"]))
        artist = {"id": 100 + len(self.artists), "monitored": True, "monitorNewItems": "all"}
        self.artists[found["foreignArtistId"]] = artist
        return artist

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
    lidarr.queue_albums.update({1, 2, 50})  # busy: no new search
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


def test_collaboration_credits_split_into_artist_names():
    assert artist_names("SawanoHiroyuki[nZk]:mizuki") == ["SawanoHiroyuki[nZk]:mizuki", "SawanoHiroyuki[nZk]", "SawanoHiroyuki", "mizuki"]
    assert artist_names("Falling In Reverse, Corey Taylor, Serj Tankian")[:2] == ["Falling In Reverse, Corey Taylor, Serj Tankian", "Falling In Reverse"]
    assert artist_names("A feat. B")[1:] == ["A", "B"] and artist_names("X × Y")[1:] == ["X", "Y"]
