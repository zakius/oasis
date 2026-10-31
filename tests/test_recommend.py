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
            assert call(client, "getSong", id=stream)["error"]["code"] == 70
            entry = call(client, "getPlaylist", id=playlist["id"])["playlist"]["entry"][0]
            assert entry["id"] == local["id"] and entry["starred"] and entry["playCount"] == 1 and entry["youtubeId"] == "vidNew"
        finally:
            extra.unlink()
            scan(db, [library])
