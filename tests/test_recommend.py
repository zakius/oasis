import pytest

from conftest import call, make_client
from oasis.db import connect
from oasis.ytm import track_row


def item(video_id, title, artist="Gamma", album="Far Away", album_id="MPREb_far", duration=200):
    return {"videoId": video_id, "title": title, "artists": [{"name": artist}], "album": {"name": album, "id": album_id},
            "duration_seconds": duration, "thumbnails": [{"url": "https://img/x=w60-h60"}]}


class FakeYtm:
    account = None

    def __init__(self):
        self.radios = []

    def search(self, query, limit):
        rows = [track_row(item("s1", "Gamma Hit")), track_row(item("s2", "Other", artist="Someone"))]
        return [r for r in rows if r]

    def match(self, track):
        return {"One": "one-yt"}.get(track["title"])

    def find(self, artist, title, duration=None):
        return track_row(item("lf-" + title, title, artist=artist)) if artist == "Remote Band" else None

    def radio(self, video_id, limit):
        self.radios.append(video_id)
        # Every radio starts with a song the library has, then new ones unique to its seed.
        local_copy = track_row(item("two-yt", "Two", artist="Beta", album="First", duration=2))
        return [local_copy] + [track_row(item(f"{video_id}-{i}", f"{video_id} song {i}")) for i in range(limit - 1)]

    def cached(self, video_id):
        return None

    def fetch(self, video_id):
        pass

    def album(self, browse_id):
        return [track_row(item(f"alb{i}", f"Track {i}")) | {"track_no": i} for i in (1, 2, 3)]


class FakeLastFm:
    def similar(self, artist, title, limit):
        return [("The Alpha", "Song B"), ("Remote Band", "Tune")]

    def artist_info(self, name):
        return {"url": "https://last.fm/a", "bio": {"summary": "Bio text <a href=\"x\">more</a>"},
                "similar": {"artist": [{"name": "Beta"}, {"name": "Not In Library"}]}}


@pytest.fixture
def ytm():
    return FakeYtm()


@pytest.fixture
def client(library, tmp_path, ytm):
    with make_client(library, tmp_path / "oasis.db", ytm) as c:
        c.app.state.api.recommender.lastfm = FakeLastFm()
        yield c


@pytest.fixture
def api(client):
    return client.app.state.api


def song_id(client, title):
    return next(s["id"] for s in call(client, "search3", query="")["searchResult3"]["song"] if s["title"] == title)


def titles(songs):
    return [s["title"] for s in songs]


def test_similar_songs_mix_radio_with_library_methods(client, ytm):
    songs = call(client, "getSimilarSongs2", id=song_id(client, "One"), count=12)["similarSongs2"]["song"]
    assert ytm.radios == ["one-yt"]
    assert songs[0]["title"] == "Two" and songs[0]["id"] == song_id(client, "Two")  # radio's first pick, as the local copy
    titles_ = titles(songs)
    assert "one-yt song 0" in titles_ and "Song B" in titles_  # YouTube radio and Last.fm's library pick both come
    assert len(songs) == len(set(s["id"] for s in songs)) and song_id(client, "One") not in [s["id"] for s in songs]


def test_song_missing_on_youtube_still_gets_library_songs(client, ytm):
    songs = titles(call(client, "getSimilarSongs2", id=song_id(client, "Song A"))["similarSongs2"]["song"])
    assert ytm.radios == []
    assert "Song B" in songs                     # same artist
    assert {"One", "Two"} & set(songs)           # Last.fm's similar artist Beta


def test_played_together_comes_from_history(api, client):
    a, b, c = song_id(client, "Song A"), song_id(client, "One"), song_id(client, "untagged")
    for id, t in ((a, 1000), (c, 1100), (a, 90000), (c, 90100), (b, 200000)):
        call(client, "scrobble", id=id, time=t * 1000)
    db = api._db()
    seed = db.execute("select * from track where id = ?", (a,)).fetchone()
    assert api.recommender.from_plays(db, seed, 10) == [c]


def test_lastfm_picks_map_to_library_or_youtube(api, client):
    db = api._db()
    seed = db.execute("select * from track where title = 'One'").fetchone()
    picks = api.recommender.from_lastfm(db, seed, 5)
    assert picks == [song_id(client, "Song B"), "yt-lf-Tune"]


def test_top_songs_put_library_first(client):
    songs = call(client, "getTopSongs", artist="Gamma")["topSongs"]["song"]
    assert titles(songs) == ["Gamma Hit"]


def test_artist_info_from_lastfm_lists_library_artists_only(client):
    alpha = next(a for i in call(client, "getArtists")["artists"]["index"] for a in i["artist"] if a["name"] == "The Alpha")
    info = call(client, "getArtistInfo2", id=alpha["id"])["artistInfo2"]
    assert info["biography"] == "Bio text" and info["lastFmUrl"] == "https://last.fm/a"
    assert [a["name"] for a in info["similarArtist"]] == ["Beta"]


def test_streaming_album_is_completed_on_open(client):
    remote = call(client, "search3", query="gamma")["searchResult3"]["song"][0]
    album = call(client, "getAlbum", id=remote["albumId"])["album"]
    assert titles(album["song"]) == ["Track 1", "Track 2", "Track 3", "Gamma Hit"]


def entries(client, playlist_id):
    return titles(call(client, "getPlaylist", id=playlist_id)["playlist"].get("entry", []))


def test_radio_playlist_grows_as_it_is_played(api, client):
    radio = call(client, "createPlaylist", name="Radio from One", songId=song_id(client, "One"))["playlist"]
    db = api._db()
    api.recommender.top_up(db, db.execute("select * from playlist where id = ?", (radio["id"],)).fetchone())
    first = entries(client, radio["id"])
    # YouTube Music radio, Last.fm and the library's own methods take turns.
    assert first[0] == "One" and len(first) == 30 and len(set(first)) == 30
    assert {"Two", "one-yt song 0", "Song B", "Tune"} <= set(first)
    assert "one-yt song 1" not in {s["title"] for s in call(client, "search3", query="")["searchResult3"]["song"]}

    played = call(client, "getPlaylist", id=radio["id"])["playlist"]["entry"][20]
    call(client, "scrobble", id=played["id"])
    api.recommender.top_up(db, db.execute("select * from playlist where id = ?", (radio["id"],)).fetchone())
    grown = entries(client, radio["id"])
    assert grown[:30] == first and len(grown) == 51


def test_first_play_creates_a_radio_that_follows_listening(api, client):
    call(client, "scrobble", id=song_id(client, "One"))
    radio = next(p for p in call(client, "getPlaylists")["playlists"]["playlist"] if p["name"] == "Radio")
    db = api._db()
    api.recommender.top_up(db, db.execute("select * from playlist where id = ?", (radio["id"],)).fetchone())
    queued = entries(client, radio["id"])
    assert queued[0] == "Two" and {"Song B", "one-yt song 0"} <= set(queued) and "One" not in queued


def test_other_uploads_of_a_song_merge_and_albums_place_them(api, client, ytm):
    db = api._db()
    from oasis.db import remember
    single = track_row(item("upload-a", "Track 2", album="Track 2", album_id=None))
    first = remember(db, single)
    assert remember(db, track_row(item("upload-b", "Track 2", album="Some Live Set"))) == first
    remote = call(client, "search3", query="gamma")["searchResult3"]["song"][0]
    album = call(client, "getAlbum", id=remote["albumId"])["album"]
    assert titles(album["song"]) == ["Track 1", "Track 2", "Track 3", "Gamma Hit"]
    assert next(s for s in album["song"] if s["title"] == "Track 2")["id"] == first


def test_stream_moves_to_its_new_local_copy(library, tmp_path, ytm):
    from conftest import make_track
    from oasis.db import connect, remember
    from oasis.scan import scan
    with make_client(library, tmp_path / "oasis.db", ytm) as client:
        db = connect(tmp_path / "oasis.db")
        stream = remember(db, track_row(item("vidNew", "Brand New", artist="Beta", album="First", duration=2)))
        call(client, "star", id=stream)
        playlist = call(client, "createPlaylist", name="Mine", songId=stream)["playlist"]
        call(client, "scrobble", id=stream, time=5_000_000)
        extra = library / "Beta/First/09.flac"
        make_track(extra, title="Brand New", artist="Beta", album="First", tracknumber="9")
        try:
            scan(db, [library])
            local = next(s for s in call(client, "search3", query="Brand New")["searchResult3"]["song"] if s.get("path"))
            entry = call(client, "getPlaylist", id=playlist["id"], oasis="1")["playlist"]["entry"][0]
            assert entry["id"] == local["id"] and entry["starred"] and entry["playCount"] == 1 and entry["youtubeId"] == "vidNew"
            # The old stream id keeps working for clients that still hold it.
            assert call(client, "getSong", id=stream)["song"]["id"] == local["id"]
            call(client, "scrobble", id=stream)
            call(client, "unstar", id=stream)
            assert call(client, "getSong", id=local["id"])["song"]["playCount"] == 2
            assert "starred" not in call(client, "getSong", id=local["id"])["song"]
            response = client.get("/rest/stream", params={"u": "admin", "p": "secret", "id": stream})
            assert response.status_code == 200 and response.content == extra.read_bytes()
        finally:
            extra.unlink()
            scan(db, [library])


def test_artist_page_lists_the_whole_youtube_discography(client, ytm, monkeypatch):
    beta = next(a for i in call(client, "getArtists")["artists"]["index"] for a in i["artist"] if a["name"] == "Beta")
    pages = []
    def artist(name):
        pages.append(name)
        return {"name": "Beta", "description": "A band.", "image": "https://img/beta", "songs": "PLtop", "releases": [
            {"browse_id": "MPREb_first", "title": "First", "year": 2001, "type": "album", "cover": "https://img/first"},
            {"browse_id": "MPREb_new", "title": "Newer Album", "year": 2020, "type": "album", "cover": "https://img/new"},
            {"browse_id": "MPREb_single", "title": "A Single", "year": 2021, "type": "single", "cover": None}]}
    monkeypatch.setattr(ytm, "artist", artist, raising=False)
    monkeypatch.setattr(ytm, "playlist_rows", lambda playlist_id, limit: [track_row(item("top1", "Hit", artist="Beta"))], raising=False)
    albums = call(client, "getArtist", id=beta["id"])["artist"]["album"]
    assert [a["name"] for a in albums] == ["First", "A Single", "Newer Album"]  # the local album once, then newest first
    assert albums[1]["releaseTypes"] == ["Single"] and albums[2]["year"] == 2020
    call(client, "getArtist", id=beta["id"])
    assert pages == ["Beta"]  # fetched once a day
    newer = albums[2]
    album = call(client, "getAlbum", id=newer["id"])["album"]
    assert titles(album["song"]) == ["Track 1", "Track 2", "Track 3"] and album["artistId"] == beta["id"]
    assert [s["title"] for s in call(client, "getTopSongs", artist="Beta")["topSongs"]["song"]][-1] == "Hit"
    info = call(client, "getArtistInfo2", id=beta["id"])["artistInfo2"]
    assert info["largeImageUrl"] == "https://img/beta"


def test_a_song_on_an_album_and_its_single_stays_on_both(client, ytm, monkeypatch):
    beta = next(a for i in call(client, "getArtists")["artists"]["index"] for a in i["artist"] if a["name"] == "Beta")
    monkeypatch.setattr(ytm, "artist", lambda name: {"name": "Beta", "description": None, "image": None, "songs": None, "releases": [
        {"browse_id": "MPREb_new", "title": "Newer Album", "year": 2020, "type": "album", "cover": None},
        {"browse_id": "MPREb_single", "title": "Track 2", "year": 2019, "type": "single", "cover": None}]}, raising=False)
    listings = {"MPREb_new": [(f"alb{i}", f"Track {i}", i) for i in (1, 2, 3)], "MPREb_single": [("alb2", "Track 2", 1)]}
    monkeypatch.setattr(ytm, "album", lambda browse_id: [
        track_row(item(v, t, artist="Beta", album="Newer Album" if browse_id == "MPREb_new" else "Track 2", album_id=browse_id)) | {"track_no": n}
        for v, t, n in listings[browse_id]])
    albums = {a["name"]: a for a in call(client, "getArtist", id=beta["id"])["artist"]["album"]}
    first = call(client, "getAlbum", id=albums["Newer Album"]["id"])["album"]
    single = call(client, "getAlbum", id=albums["Track 2"]["id"])["album"]
    again = call(client, "getAlbum", id=albums["Newer Album"]["id"])["album"]
    assert titles(first["song"]) == titles(again["song"]) == ["Track 1", "Track 2", "Track 3"] and again["songCount"] == 3
    assert titles(single["song"]) == ["Track 2"] and single["songCount"] == 1 and single["song"][0]["id"] == first["song"][1]["id"]
    assert [x["track"] for x in again["song"]] == [1, 2, 3] and single["song"][0]["track"] == 1  # each album's own numbering
    counts = {a["name"]: a["songCount"] for a in call(client, "getArtist", id=beta["id"])["artist"]["album"]}
    assert counts["Track 2"] == 1


def test_search_lists_youtube_artists_and_albums_after_the_librarys(client):
    found = call(client, "search3", query="gamma")["searchResult3"]
    assert [a["name"] for a in found["artist"]] == ["Gamma"] and [a["name"] for a in found["album"]] == ["Far Away"]
    album = call(client, "getAlbum", id=found["album"][0]["id"])["album"]
    assert titles(album["song"]) == ["Track 1", "Track 2", "Track 3", "Gamma Hit"]  # opening it fetches the rest
    assert call(client, "getArtist", id=found["artist"][0]["id"])["artist"]["album"][0]["name"] == "Far Away"


def test_search_for_artists_and_albums_alone_still_asks_youtube(client):
    found = call(client, "search3", query="gamma", songCount=0)["searchResult3"]
    assert [a["name"] for a in found["artist"]] == ["Gamma"] and [a["name"] for a in found["album"]] == ["Far Away"]
    assert not found.get("song")


def test_one_automatic_radio_per_user_even_when_two_plays_race(tmp_path):
    from oasis import db as oasis_db
    path = tmp_path / "race.db"
    db = oasis_db.connect(path)
    db.execute("drop index one_radio")  # a database from before the index, where a race made two
    for _ in range(2):
        db.execute("insert into playlist(name, owner, created, changed, radio) values ('Radio', 'admin', 0, 0, 2)")
    oasis_db.migrated.discard(path)
    db = oasis_db.connect(path)
    assert [tuple(r) for r in db.execute("select id, radio from playlist order by id")] == [(1, 2), (2, 1)]  # the extra stays, as a plain radio
    # Both racing plays found no radio: the second insert is ignored.
    db.execute("insert or ignore into playlist(name, owner, comment, created, changed, radio) values ('Radio', 'admin', '', 0, 0, 2)")
    assert db.execute("select count(*) from playlist where radio = 2").fetchone()[0] == 1


def test_a_failing_chore_leaves_the_radios_and_the_other_chores_be(api):
    import threading
    from oasis.recommend import Recommender
    done, recommender = threading.Event(), Recommender(api.recommender.connect, None, None)
    recommender.top_up_all = lambda db: 1 / 0
    recommender.chores = [lambda db: 1 / 0, lambda db: done.set()]
    recommender.start()
    recommender.wake.set()
    assert done.wait(10)
