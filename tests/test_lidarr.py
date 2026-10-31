import pytest

from conftest import call, make_client
from oasis.lidarr import Library, Lidarr


class FakeLidarr(Lidarr):
    def __init__(self):
        self.albums, self.artists, self.queue_albums, self.calls = {}, {}, set(), []

    def find_album(self, artist, title):
        self.calls.append(("find_album", artist, title))
        if title == "Unknown":
            return None
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

    def add_artist(self, found, monitor_all):
        self.calls.append(("add_artist", found["foreignArtistId"], monitor_all))
        artist = {"id": 100 + len(self.artists), "monitored": True, "monitorNewItems": "all"}
        self.artists[found["foreignArtistId"]] = artist
        return artist

    def set_album(self, album_id, monitored, search=False):
        self.calls.append(("set_album", album_id, monitored, search))
        for album in self.albums.values():
            if album["id"] == album_id:
                album["monitored"] = monitored

    def set_artist(self, artist, monitored):
        self.calls.append(("set_artist", artist["id"], monitored))
        artist["monitored"] = monitored

    def queued(self):
        return self.queue_albums, set()


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


def test_starred_album_goes_to_lidarr_and_tracks_progress(client, lidarr):
    first = album_of(client, "First")
    call(client, "star", albumId=first["id"])
    call(client, "star", albumId=first["id"])  # starring twice changes nothing
    sync(client)
    assert lidarr.calls.count(("add_album", "mb-First")) == 1
    assert album_of(client, "First")["oasisWanted"] == "wanted"
    lidarr.queue_albums.add(1)
    sync(client)
    assert album_of(client, "First")["oasisWanted"] == "downloading"
    lidarr.queue_albums.clear()
    lidarr.albums["mb-First"]["statistics"] = {"trackCount": 2, "trackFileCount": 2}
    sync(client)
    assert "oasisWanted" not in album_of(client, "First")
    assert ("find_album", "Beta", "First") in lidarr.calls and lidarr.calls.count(("find_album", "Beta", "First")) == 1  # mbid kept


def test_unstarred_album_is_only_unmonitored_once_nobody_wants_it(client, lidarr):
    first = album_of(client, "First")
    call(client, "createUser", username="bob", password="pw")
    call(client, "star", albumId=first["id"])
    call(client, "star", albumId=first["id"], u="bob", p="pw")
    sync(client)
    call(client, "unstar", albumId=first["id"])
    sync(client)
    assert not any(c[0] == "set_album" and c[2] is False for c in lidarr.calls)  # bob still wants it
    call(client, "unstar", albumId=first["id"], u="bob", p="pw")
    sync(client)
    assert ("set_album", 1, False, False) in lidarr.calls
    assert "oasisWanted" not in album_of(client, "First")


def test_unmonitored_album_is_monitored_and_searched_again(client, lidarr):
    first = album_of(client, "First")
    lidarr.albums["mb-First"] = {"id": 7, "monitored": False, "foreignAlbumId": "mb-First"}
    call(client, "star", albumId=first["id"])
    sync(client)
    assert ("set_album", 7, True, True) in lidarr.calls and not any(c[0] == "add_album" for c in lidarr.calls)


def test_starred_artist_is_added_with_everything_monitored(client, lidarr):
    artist = next(a for i in call(client, "getArtists")["artists"]["index"] for a in i["artist"] if a["name"] == "The Alpha")
    call(client, "star", artistId=artist["id"])
    sync(client)
    assert ("add_artist", "mb-The Alpha", True) in lidarr.calls
    assert next(a for i in call(client, "getArtists")["artists"]["index"] for a in i["artist"] if a["name"] == "The Alpha")["oasisWanted"] == "wanted"


def test_album_without_musicbrainz_match_fails_and_waits_before_retrying(client, lidarr, monkeypatch):
    first = album_of(client, "First")
    monkeypatch.setattr(lidarr, "find_album", lambda artist, title: None)
    call(client, "star", albumId=first["id"])
    sync(client)
    assert album_of(client, "First")["oasisWanted"] == "failed"
    asked = []
    monkeypatch.setattr(lidarr, "find_album", lambda artist, title: asked.append(title))
    sync(client)
    assert asked == []  # retried only after a while
