import pytest

from conftest import call, make_client, sign_in
from oasis import artists, missing, musicbrainz, tracks
from oasis.recommend import keep_playable
from oasis.youtube import remember_song
from oasis.ytm import song
from test_recommend import FakeLastFm, FakeYtm, item


class LastFmWithGaps(FakeLastFm):
    def similar(self, artist, title, limit):
        return [("Remote Band", "Tune", None), ("Nobody Has", "Lost Song", "rec-lost")]

    def artist_info(self, name):
        return {"similar": {"artist": [{"name": "Beta", "mbid": "mb-beta"}, {"name": "Not In Library", "mbid": "mb-absent"},
                                       {"name": "No Id Either"}]}}


def recording(id="rec-lost", groups=(("rg-live", "Album", ["Live"], "2001"), ("rg-album", "Album", [], "2003"), ("rg-single", "Single", [], "2002"))):
    """A MusicBrainz recording search result, released in groups (id, primary type, secondary types, date)."""
    return {"id": id, "title": "Lost Song", "length": 201000,
            "artist-credit": [{"name": "Nobody Has", "artist": {"id": "mb-nobody", "name": "Nobody Has"}}],
            "releases": [{"status": "Official", "date": date, "title": rg, "release-group": {"id": rg, "title": rg.title(), "primary-type": kind,
                                                                                              "secondary-types": more}}
                         for rg, kind, more, date in groups]}


@pytest.fixture
def client(library, tmp_path, monkeypatch):
    monkeypatch.setattr(musicbrainz, "search", lambda kind, query, db=None: [recording()] if kind == "recording" else [])
    with make_client(library, tmp_path / "oasis.db", FakeYtm()) as c:
        c.app.state.oasis.lastfm = LastFmWithGaps()
        yield c


def seed(client) -> str:
    return client.app.state.oasis.connect().execute("select id from track where title = 'One'").fetchone()[0]


def similar(client, **params):
    return call(client, "getSimilarSongs2", id=seed(client), oasis="1", oasisSource="lastfm", **params)["similarSongs2"]["song"]


def titles(songs) -> list[str]:
    return sorted(s["title"] for s in songs)


def lost_song(client) -> dict:
    return next(s for s in similar(client, unavailableSongs="1") if s["title"] == "Lost Song")


def test_a_song_no_service_has_reaches_only_clients_that_ask_for_unplayable_songs(client):
    extensions = {e["name"] for e in call(client, "getOpenSubsonicExtensions")["openSubsonicExtensions"]}
    assert "unavailableSongs" in extensions
    assert titles(similar(client)) == ["Tune", "Two"]  # Two: Beta, a similar artist, is in the library
    found = similar(client, unavailableSongs="1")
    assert titles(found) == ["Lost Song", "Tune", "Two"]
    lost = next(s for s in found if s["title"] == "Lost Song")
    assert lost["playable"] is False and lost["id"].startswith("ms-") and all("playable" not in s for s in found if s is not lost)
    error = call(client, "stream", id=lost["id"])["error"]
    assert error["code"] == 70 and "Star it to want its album" in error["message"]


def test_the_users_choice_keeps_unplayable_songs_away_even_from_apps_that_ask(client):
    sign_in(client)
    client.post("/account/missing", data={"show": "hide"})
    assert titles(similar(client, unavailableSongs="1")) == ["Tune", "Two"]


def test_starring_an_unplayable_song_wants_its_studio_album_and_unstarring_lets_it_go(client):
    lost = lost_song(client)
    call(client, "star", id=lost["id"])
    db = client.app.state.oasis.connect()
    starred = lambda id: db.execute("select starred from stat where user = 'admin' and id = ?", (id,)).fetchone()
    assert starred(lost["id"])[0] and starred("al-mb-rg-album")[0]
    again = call(client, "getSong", id=lost["id"], unavailableSongs="1")["song"]
    assert again["albumId"] == "al-mb-rg-album" and again["album"] == "Rg-Album" and again["artistId"] == "ar-mb-mb-nobody"
    call(client, "unstar", id=lost["id"])
    assert starred("al-mb-rg-album")[0] is None


def test_the_worker_finds_an_album_later_and_wants_it_for_who_starred(client, monkeypatch):
    lost = lost_song(client)
    monkeypatch.setattr(musicbrainz, "search", lambda *a, **k: (_ for _ in ()).throw(OSError("busy")))
    call(client, "star", id=lost["id"])
    db = client.app.state.oasis.connect()
    assert db.execute("select mb_release_group from track where id = ?", (lost["id"],)).fetchone()[0] is None
    monkeypatch.setattr(musicbrainz, "search", lambda kind, query, db=None: [recording()])
    client.app.state.oasis.fill_missing()
    assert db.execute("select starred from stat where user = 'admin' and id = 'al-mb-rg-album'").fetchone()[0]


def test_a_stream_of_the_song_takes_its_stars_and_playlist_entries(client):
    lost = lost_song(client)
    call(client, "star", id=lost["id"])
    playlist = call(client, "createPlaylist", name="Mine", songId=lost["id"], unavailableSongs="1")["playlist"]
    db = client.app.state.oasis.connect()
    found = remember_song(db, song(item("lost-yt", "Lost Song", artist="Nobody Has", duration=201)))
    assert call(client, "getSong", id=lost["id"])["song"]["id"] == found  # the old id still works
    assert [e["id"] for e in call(client, "getPlaylist", id=playlist["id"])["playlist"]["entry"]] == [found]
    assert db.execute("select starred from stat where id = ?", (found,)).fetchone()[0]


def test_a_local_copy_takes_the_song_by_its_recording(client):
    lost = lost_song(client)
    call(client, "star", id=lost["id"])
    db = client.app.state.oasis.connect()
    local = db.execute("select id from track where title = 'Two'").fetchone()[0]
    db.execute("update track set mb_recording = 'rec-lost' where id = ?", (local,))
    assert tracks.adopt_local_copies(db) == 1
    assert db.execute("select starred from stat where id = ?", (local,)).fetchone()[0]
    assert db.execute("select 1 from track where id = ?", (lost["id"],)).fetchone() is None


def test_playlists_count_unplayable_songs_only_for_apps_that_take_them(client):
    lost = lost_song(client)
    made = call(client, "createPlaylist", name="Mine", songId=[seed(client), lost["id"]], unavailableSongs="1")["playlist"]
    plain = call(client, "getPlaylist", id=made["id"])["playlist"]
    assert plain["songCount"] == 1 and [e["title"] for e in plain["entry"]] == ["One"]
    assert call(client, "getPlaylists")["playlists"]["playlist"][0]["songCount"] == 1
    assert call(client, "getPlaylist", id=made["id"], unavailableSongs="1")["playlist"]["songCount"] == 2


def test_similar_artists_the_library_lacks_come_with_include_not_present(client):
    names = lambda **p: [a["name"] for a in call(client, "getArtistInfo2", id=artist_id(client), **p)["artistInfo2"].get("similarArtist", [])]
    assert names() == ["Beta"]
    found = call(client, "getArtistInfo2", id=artist_id(client), includeNotPresent="true")["artistInfo2"]["similarArtist"]
    assert [(a["name"], a["id"]) for a in found][1:] == [("Not In Library", "ar-mb-mb-absent")]


def artist_id(client):
    return client.app.state.oasis.connect().execute("select artist_id from track where album_artist = 'The Alpha'").fetchone()[0]


def test_an_album_only_musicbrainz_has_lists_its_songs_for_apps_that_take_them(client, monkeypatch):
    edition = {"id": "rel-1", "status": "Official", "date": "2003", "title": "Far", "release-group": {"id": "rg-far", "title": "Far", "primary-type": "Album"},
               "artist-credit": [{"name": "Nobody Has", "artist": {"id": "mb-nobody", "name": "Nobody Has"}}],
               "media": [{"position": 1, "format": "CD", "tracks": [{"position": n, "title": t, "length": 100000, "recording": {"id": f"rec-{t}"}}
                                                                     for n, t in ((1, "First"), (2, "Second"))]}]}
    monkeypatch.setattr(musicbrainz, "editions", lambda rg, db=None: [edition])
    monkeypatch.setattr(musicbrainz, "release_group", lambda rg, db=None: edition["release-group"] | {"artist-credit": edition["artist-credit"]})
    assert call(client, "getAlbum", id="al-mb-rg-far")["album"]["song"] == []
    songs = call(client, "getAlbum", id="al-mb-rg-far", unavailableSongs="1")["album"]["song"]
    assert [(s["title"], s["track"], s["albumId"], s["playable"]) for s in songs] == [("First", 1, "al-mb-rg-far", False), ("Second", 2, "al-mb-rg-far", False)]


def test_the_album_picked_is_the_earliest_official_studio_album_of_the_artists_recording():
    assert missing.pick([recording()], "Nobody Has", "Lost Song", None)[1]["release-group"]["id"] == "rg-album"
    singles = recording(groups=(("rg-live", "Album", ["Live"], "2001"), ("rg-single", "Single", [], "2002")))
    assert missing.pick([singles], "Nobody Has", "lost song", None)[1]["release-group"]["id"] == "rg-single"
    assert missing.pick([recording()], "Nobody Has", "Other Title", None) is None
    assert missing.pick([recording()], "Someone Else", "Lost Song", None) is None  # a cover of the same title
    assert missing.pick([recording()], "Someone Else", "Other Title", "rec-lost") is not None  # a known recording goes by its id


def test_keep_playable_keeps_ids_up_to_the_count_th_playable_one():
    assert keep_playable(["a", "ms-1", "b", "ms-2", "c"], 2) == ["a", "ms-1", "b"]
    assert keep_playable(["ms-1", "ms-2"], 2) == ["ms-1", "ms-2"]


class Acquiring:
    def wake(self):
        pass


def test_the_dashboard_shows_an_unplayable_song_and_stars_it(client, tmp_path):
    lost = lost_song(client)
    sign_in(client)
    oasis = client.app.state.oasis
    oasis.acquire = Acquiring()  # the music pages need acquisition on
    page = client.get(f"/music/library/song/{lost['id']}").text
    assert "Nothing can play it" in page and "Star and want its album" in page
    client.post("/music/song/star", data={"id": lost["id"], "on": "1", "next": f"/music/library/song/{lost['id']}"})
    db = oasis.connect()
    assert db.execute("select starred from stat where user = 'admin' and id = 'al-mb-rg-album'").fetchone()[0]
    similar_page = client.get(f"/music/library/song/{seed(client)}?similar=1").text
    assert "Lost Song" in similar_page and "nothing plays it" in similar_page


def test_the_server_starts_again_with_songs_whose_artist_the_library_lacks(client, library, tmp_path):
    lost = lost_song(client)  # its artist has no id yet: the library lacks them and MusicBrainz wasn't asked
    assert client.app.state.oasis.connect().execute("select artist_id from track where id = ?", (lost["id"],)).fetchone()[0] is None
    with make_client(library, tmp_path / "oasis.db", FakeYtm()) as again:
        assert call(again, "ping")["status"] == "ok"


def test_a_followed_artist_only_musicbrainz_has_stays_theirs(client):
    lost = lost_song(client)
    call(client, "star", id=lost["id"], artistId="ar-mb-mb-nobody")
    db = client.app.state.oasis.connect()
    artists.unify(db)
    assert db.execute("select artist_id from track where id = ?", (lost["id"],)).fetchone()[0] == "ar-mb-mb-nobody"
    assert db.execute("select starred from stat where id = 'ar-mb-mb-nobody'").fetchone()[0]


def test_unstarring_the_song_keeps_an_album_the_user_starred_themselves(client):
    lost = lost_song(client)
    call(client, "star", albumId="al-mb-rg-album")
    call(client, "star", id=lost["id"])
    call(client, "unstar", id=lost["id"])
    db = client.app.state.oasis.connect()
    assert db.execute("select starred from stat where user = 'admin' and id = 'al-mb-rg-album'").fetchone()[0]


def test_another_starred_song_of_the_album_holds_it_till_it_is_unstarred_too(client, monkeypatch):
    first = lost_song(client)
    call(client, "star", id=first["id"])
    db = client.app.state.oasis.connect()
    second = missing.remember(db, "Nobody Has", "Other Song")
    monkeypatch.setattr(musicbrainz, "search", lambda kind, query, db=None: [recording() | {"id": "rec-other", "title": "Other Song"}])
    call(client, "star", id=second)
    album = lambda: db.execute("select starred from stat where user = 'admin' and id = 'al-mb-rg-album'").fetchone()[0]
    call(client, "unstar", id=first["id"])
    assert album()
    call(client, "unstar", id=second)
    assert album() is None


def test_opening_another_album_of_a_starred_song_leaves_it_on_its_own(client, monkeypatch):
    lost = lost_song(client)
    call(client, "star", id=lost["id"])
    single = {"id": "rel-s", "status": "Official", "date": "2002", "title": "Rg-Single", "release-group": {"id": "rg-single", "title": "Rg-Single", "primary-type": "Single"},
              "artist-credit": [{"name": "Nobody Has", "artist": {"id": "mb-nobody", "name": "Nobody Has"}}],
              "media": [{"position": 1, "tracks": [{"position": 1, "title": "Lost Song", "recording": {"id": "rec-lost"}}]}]}
    monkeypatch.setattr(musicbrainz, "editions", lambda rg, db=None: [single])
    monkeypatch.setattr(musicbrainz, "release_group", lambda rg, db=None: single["release-group"] | {"artist-credit": single["artist-credit"]})
    call(client, "getAlbum", id="al-mb-rg-single", unavailableSongs="1")
    db = client.app.state.oasis.connect()
    assert db.execute("select mb_release_group from track where id = ?", (lost["id"],)).fetchone()[0] == "rg-album"


def test_a_song_whose_lookup_breaks_is_set_aside_so_the_others_get_their_albums(client, monkeypatch):
    lost = lost_song(client)
    db = client.app.state.oasis.connect()
    monkeypatch.setattr(missing, "pick", lambda *a: 1 / 0)  # a MusicBrainz answer the code can't read
    assert missing.fill_next(db, lambda *a: None)
    assert db.execute("select looked_up from track where id = ?", (lost["id"],)).fetchone()[0]


def test_get_song_knows_an_unplayable_song_only_for_apps_that_take_them(client):
    lost = lost_song(client)
    assert call(client, "getSong", id=lost["id"])["error"]["code"] == 70
    assert call(client, "getSong", id=lost["id"], unavailableSongs="1")["song"]["playable"] is False


def test_plain_apps_edit_playlists_by_the_songs_they_see_and_keep_the_others(client):
    lost = lost_song(client)
    db = client.app.state.oasis.connect()
    one, two = (db.execute("select id from track where title = ?", (t,)).fetchone()[0] for t in ("One", "Two"))
    made = call(client, "createPlaylist", name="Mine", songId=[lost["id"], one, two], unavailableSongs="1")["playlist"]
    call(client, "updatePlaylist", playlistId=made["id"], songIndexToRemove="0")  # One, the first this app sees
    entries = lambda: [e["id"] for e in call(client, "getPlaylist", id=made["id"], unavailableSongs="1")["playlist"]["entry"]]
    assert entries() == [lost["id"], two]
    call(client, "createPlaylist", playlistId=made["id"], songId=[one, two])  # rewritten whole by an app that doesn't see it
    assert entries() == [lost["id"], one, two]


def test_a_known_stream_of_the_song_takes_its_star_and_joins_the_library(client):
    lost = lost_song(client)
    call(client, "star", id=lost["id"])
    db = client.app.state.oasis.connect()
    stream = remember_song(db, song(item("lost-yt", "Lost Song", artist="Nobody Has", duration=201)))
    assert db.execute("select library from track where id = ?", (stream,)).fetchone()[0] == 1
    again = missing.remember(db, "Nobody Has", "Lost Song")  # recommended again while the stream was known
    call(client, "star", id=again)
    assert remember_song(db, song(item("lost-yt-2", "Lost Song", artist="Nobody Has", duration=201))) == stream  # the same song
    assert db.execute("select 1 from track where id = ?", (again,)).fetchone() is None
