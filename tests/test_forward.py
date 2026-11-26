import json
import threading
import time
import urllib.parse

import pytest

from conftest import call, make_client
from oasis import forward, lastfm, servers, sources
from oasis.db import connect
from oasis.forward import Listen, ListenBrainz
from oasis.lastfm import LastFm
from oasis.remote import Rejected
from oasis.sources import Qobuz, SoundCloud


class Service:
    def __init__(self, fail=None):
        self.scrobbled, self.playing, self.fail = [], [], fail
        self.played = threading.Event()

    def scrobble(self, listen):
        if self.fail:
            raise self.fail
        self.scrobbled.append(listen)

    def now_playing(self, listen):
        self.playing.append(listen)
        self.played.set()


@pytest.fixture
def setup(library, tmp_path):
    services = {"ok": Service(), "down": Service(OSError("timeout")), "refuses": Service(Rejected("bad"))}
    with make_client(library, tmp_path / "oasis.db", services=services) as client:
        yield client, services, connect(tmp_path / "oasis.db")


def song_id(client, title):
    return next(s["id"] for s in call(client, "search3", query="")["searchResult3"]["song"] if s["title"] == title)


def test_plays_reach_services_and_retry_until_taken(setup):
    client, services, db = setup
    id = song_id(client, "Two")
    call(client, "scrobble", id=id, time=1_700_000_000_000)
    api_forwarder = client.app.state.oasis.forwarder
    api_forwarder.flush()
    listen = services["ok"].scrobbled[0]
    assert (listen.artist, listen.title, listen.album, listen.played) == ("Beta", "Two", "First", 1_700_000_000)
    left = db.execute("select service, attempts, next_try from outbox").fetchall()
    assert [(r["service"], r["attempts"]) for r in left] == [("down", 1)] and left[0]["next_try"] > time.time()
    services["down"].fail = None
    db.execute("update outbox set next_try = 0")
    api_forwarder.flush()
    assert len(services["down"].scrobbled) == 1 and db.execute("select count(*) from outbox").fetchone()[0] == 0


def db_path(db):
    return db.execute("pragma database_list").fetchone()["file"]


def test_now_playing_is_sent_but_not_queued(setup):
    client, services, db = setup
    call(client, "scrobble", id=song_id(client, "One"), submission="false")
    assert services["ok"].played.wait(10) and services["ok"].playing[0].title == "One"
    assert db.execute("select count(*) from outbox").fetchone()[0] == 0


def test_unknown_track_scrobble_fails(setup):
    client, _, _ = setup
    assert call(client, "scrobble", id="tr-missing")["error"]["code"] == 70


def test_scrobble_batch_skips_unknown_ids_without_half_commits(setup):
    client, services, db = setup
    one, two = song_id(client, "One"), song_id(client, "Two")
    assert call(client, "scrobble", id=[one, "tr-missing", two], time=[1000, 2000, 3000])["status"] == "ok"
    counts = dict(db.execute("select id, play_count from stat where id in (?, ?)", (one, two)).fetchall())
    assert counts == {one: 1, two: 1}
    assert db.execute("select played from play where track_id = ?", (two,)).fetchone()[0] == 3


def test_youtube_history_matches_local_tracks_once(setup):
    client, _, db = setup
    id = song_id(client, "One")
    db.execute("insert into outbox(user, service, track_id, played, next_try) values ('admin', 'ytmusic', ?, 1, 0)", (id,))
    history, matched = [], []

    class Ytm:
        def add_history(self, client, video_id):
            history.append(video_id)

        def match(self, track):
            matched.append(track["title"])
            return "vidOne"

    services = {"ytmusic": forward.YouTubeMusicAccount(Ytm(), "client")}
    forward.Forwarder(lambda: connect(db_path(db)), lambda db, user: services, Ytm()).flush()
    assert history == ["vidOne"] and matched == ["One"]
    assert db.execute("select ytm_id from track where id = ?", (id,)).fetchone()[0] == "vidOne"


LISTEN = Listen("tr-1", "Artist", "Song", "Album", 200, 1_700_000_000, "vid")


def test_lastfm_scrobble_is_signed(monkeypatch):
    sent = []

    def post(url, data, headers):
        sent.append(dict(urllib.parse.parse_qsl(data.decode())))
        return 200, b'{"scrobbles": {}}'
    monkeypatch.setattr(lastfm, "post", post)
    LastFm("key", "secret", "session").scrobble(LISTEN)
    params = sent[0]
    assert params["method"] == "track.scrobble" and params["timestamp"] == "1700000000" and params["album"] == "Album"
    # md5 of "albumAlbumapi_keykeyartistArtistduration200methodtrack.scrobblesksessiontimestamp1700000000trackSongsecret":
    # each parameter but format, sorted by name, then the secret
    assert params["api_sig"] == "419325f62b4b2d177c21c8ea241a3db0"


@pytest.mark.parametrize("error, kind", [(16, OSError), (29, OSError), (9, Rejected)])
def test_lastfm_errors(monkeypatch, error, kind):
    monkeypatch.setattr(lastfm, "post", lambda *a: (200, json.dumps({"error": error, "message": "x"}).encode()))
    with pytest.raises(kind):
        LastFm("key", "secret", "session").scrobble(LISTEN)


@pytest.mark.parametrize("status, kind", [(200, None), (503, OSError), (429, OSError), (400, Rejected)])
def test_listenbrainz(monkeypatch, status, kind):
    sent = []
    monkeypatch.setattr(forward, "post", lambda url, data, headers: (sent.append((json.loads(data), headers)), (status, b""))[1])
    if kind:
        with pytest.raises(kind):
            ListenBrainz("tok").scrobble(LISTEN)
        return
    ListenBrainz("tok").scrobble(LISTEN)
    body, headers = sent[0]
    entry = body["payload"][0]
    assert body["listen_type"] == "single" and entry["listened_at"] == 1_700_000_000
    assert entry["track_metadata"]["additional_info"]["origin_url"].endswith("vid") and headers["Authorization"] == "Token tok"


class Liker:
    """A service that keeps likes: of the kinds in likes, of what it has (catalogue: title or artist -> its own id)."""

    def __init__(self, likes=("song",), catalogue=None, fail=None):
        self.likes, self.fail, self.liked = likes, fail, []
        self.catalogue = catalogue if catalogue is not None else {"One": "s1", "Two": "s2", "First": "a1", "Beta": "b1"}

    def find_liked(self, db, item):
        return self.catalogue.get(item.title or item.artist)

    def like(self, kind, key, on):
        if self.fail:
            raise self.fail
        self.liked.append((kind, key, on))


def ids_of(db, title):
    return db.execute("select id, album_id, artist_id from track where title = ?", (title,)).fetchone()


def queued(db):
    return sorted((r["service"], r["action"]) for r in db.execute("select service, action from outbox"))


@pytest.fixture
def likers(library, tmp_path):
    services = {"songs": Liker(), "all": Liker(("song", "album", "artist")), "plays": Service(),
                "empty": Liker(("song", "album"), catalogue={})}
    with make_client(library, tmp_path / "oasis.db", services=services) as client:
        yield client, services, connect(tmp_path / "oasis.db")


def test_a_star_is_liked_on_each_service_that_has_the_song_and_unstar_takes_it_back(likers):
    client, services, db = likers
    song = ids_of(db, "One")["id"]
    call(client, "star", id=song)
    assert queued(db) == [("all", "like"), ("empty", "like"), ("songs", "like")]  # the service of plays only gets none
    client.app.state.oasis.forwarder.flush()
    assert services["songs"].liked == [("song", "s1", True)] and services["all"].liked == [("song", "s1", True)]
    assert services["empty"].liked == [] and queued(db) == []  # a service without the song is done
    assert {r[0] for r in db.execute("select service from liked")} == {"songs", "all"}

    services["songs"].catalogue["One"] = "s1-elsewhere"  # what its search finds now doesn't matter
    call(client, "unstar", id=song)
    client.app.state.oasis.forwarder.flush()
    assert services["songs"].liked[-1] == ("song", "s1", False) and services["all"].liked[-1] == ("song", "s1", False)
    assert services["empty"].liked == [] and db.execute("select count(*) from liked").fetchone()[0] == 0


def test_album_and_artist_stars_go_only_where_the_service_keeps_them(likers):
    client, services, db = likers
    ids = ids_of(db, "One")
    call(client, "star", albumId=ids["album_id"], artistId=ids["artist_id"])
    assert sorted((r["service"], r["track_id"]) for r in db.execute("select service, track_id from outbox")) == \
        [("all", ids["album_id"]), ("all", ids["artist_id"]), ("empty", ids["album_id"])]
    client.app.state.oasis.forwarder.flush()
    assert sorted(services["all"].liked) == [("album", "a1", True), ("artist", "b1", True)]


def test_a_failing_service_retries_without_holding_up_the_others(likers):
    client, services, db = likers
    services["songs"].fail = OSError("timeout")
    call(client, "star", id=ids_of(db, "Two")["id"])
    forwarder = client.app.state.oasis.forwarder
    forwarder.flush()
    assert services["all"].liked == [("song", "s2", True)]
    left = db.execute("select service, action, attempts, next_try from outbox").fetchall()
    assert [(r["service"], r["action"], r["attempts"]) for r in left] == [("songs", "like", 1)] and left[0]["next_try"] > time.time()
    services["songs"].fail = None
    db.execute("update outbox set next_try = 0")
    forwarder.flush()
    assert services["songs"].liked == [("song", "s2", True)] and queued(db) == []


def test_an_unstar_replaces_a_like_still_waiting(likers):
    client, services, db = likers
    services["songs"].fail = OSError("timeout")
    song = ids_of(db, "Two")["id"]
    call(client, "star", id=song)
    client.app.state.oasis.forwarder.flush()
    call(client, "unstar", id=song)
    assert queued(db) == [("all", "unlike"), ("empty", "unlike"), ("songs", "unlike")]
    services["songs"].fail = None
    client.app.state.oasis.forwarder.flush()
    assert services["songs"].liked == []  # it never liked the song, so there is nothing to take back
    assert services["all"].liked == [("song", "s2", True), ("song", "s2", False)]


def test_a_star_goes_to_the_services_the_user_can_use(client):
    oasis = client.app.state.oasis
    db = oasis.connect()
    oasis.lastfm = LastFm("key", "secret", None)
    db.execute("update user set lastfm_session = 'sk', listenbrainz_token = 'tok' where name = 'admin'")
    for name in ("lastfm", "listenbrainz", "tidal", "soundcloud"):  # Tidal not linked, SoundCloud not signed in
        oasis.sources.turn_on(db, "admin", name)
    libre = servers.add(db, "admin", "lastfm", "Libre", "https://libre.example/2.0/", {"auth": "https://libre.example/api/auth/", "session": "s"})
    ids = ids_of(db, "One")
    forwarder = forward.Forwarder(oasis.connect, oasis.user_services)  # what users linked, not the test's services
    forwarder.like(db, "admin", [ids["id"]], True)
    assert queued(db) == [("lastfm", "like"), ("listenbrainz", "like"), (f"server:{libre}", "like")]

    oasis.sources.link(db, "admin", "qobuz", {"app_id": "1", "token": "t", "secret": "s"})  # started only when sent
    forwarder.like(db, "admin", [ids["album_id"]], True)
    assert [r[0] for r in db.execute("select service from outbox where track_id = ?", (ids["album_id"],))] == ["qobuz"]


def test_lastfm_loves_and_a_server_without_love_counts_as_done(monkeypatch, tmp_path):
    sent = []

    def post(url, data, headers):
        sent.append(dict(urllib.parse.parse_qsl(data.decode())))
        return 200, b"{}"
    monkeypatch.setattr(lastfm, "post", post)
    service = LastFm("key", "secret", "session")
    service.like("song", service.find_liked(None, forward.Item("song", "tr-1", "Artist", "Song", None)), True)
    assert {k: sent[0][k] for k in ("method", "artist", "track", "sk")} == {"method": "track.love", "artist": "Artist", "track": "Song", "sk": "session"}

    monkeypatch.setattr(lastfm, "post", lambda *a: (200, b'{"error": 3, "message": "Invalid Method"}'))
    with pytest.raises(Rejected):
        service.like("song", '["Artist", "Song"]', True)
    monkeypatch.setattr(lastfm, "post", lambda *a: (404, b"<html>Not found</html>"))
    with pytest.raises(Rejected):
        service.like("song", '["Artist", "Song"]', False)


def test_listenbrainz_loves_recordings_only(monkeypatch):
    sent = []
    monkeypatch.setattr(forward, "post", lambda url, data, headers: (sent.append((url, json.loads(data))), (200, b""))[1])
    service = ListenBrainz("tok", "https://maloja.example/apis/listenbrainz/1/")
    assert service.find_liked(None, forward.Item("song", "tr-1", "Artist", "Song", None)) is None
    service.like("song", "rec-1", False)
    assert sent == [("https://maloja.example/apis/listenbrainz/1/feedback/recording-feedback", {"recording_mbid": "rec-1", "score": 0})]


class Streaming:
    """A streaming service's client as sources.KINDS has them."""
    name, prefix, variant, likes = "tidal", "td-", "", ("song", "album", "artist")

    def __init__(self):
        self.favourites, self.searched = [], []

    def find(self, artist, title):
        self.searched.append(title)
        return {"id": "found"} if title == "Searched" else None

    def find_album(self, artist, title):
        return "album-9"

    def favourite(self, kind, id, on):
        self.favourites.append((kind, id, on))


def test_favourites_find_the_service_s_own_song_then_a_kept_answer_then_search():
    client = Streaming()
    kept = {("find", "tidal", "", "Artist", "Kept"): ((client, {"id": "kept-7"}), 0.5), ("find", "tidal", "", "Artist", "Missed"): (None, 0.2)}
    service = forward.Favourites(Streaming, lambda: client, lambda question: (kept[question],) if question in kept else None)

    def song(title, id="tr-1"):
        return forward.Item("song", id, "Artist", title, None)
    assert service.find_liked(None, song("Own", "td-123")) == "123"
    assert service.find_liked(None, song("Kept")) == "kept-7" and service.find_liked(None, song("Missed")) is None
    assert service.find_liked(None, song("Searched")) == "found" and client.searched == ["Searched"]
    assert service.find_liked(None, forward.Item("album", "al-1", "Artist", "Album", None)) == "album-9"
    service.like("album", "album-9", False)
    assert client.favourites == [("album", "album-9", False)]
    with pytest.raises(OSError):
        forward.Favourites(Streaming, lambda: None).like("song", "1", True)  # a service that doesn't start is retried


def test_streaming_favourites_call_each_service_s_own_api(monkeypatch):
    asked = []

    class Response:
        status, data = 200, b"{}"

        def json(self):
            return {}

    def request(method, url, **kwargs):
        asked.append((method, url.split("?")[0], kwargs.get("fields") or kwargs.get("body")))
        return Response()
    monkeypatch.setattr(sources, "request", request)
    monkeypatch.setattr(SoundCloud, "client_id", "cid")
    SoundCloud({"token": "t", "id": 77}).favourite("song", "123", True)
    SoundCloud({"token": "t", "id": 77}).favourite("song", "123", False)
    SoundCloud({"token": "t", "id": 77}).add_history("123")
    Qobuz({"app_id": "1", "token": "t"}).favourite("album", "0075679933652", True)
    assert asked == [("PUT", "https://api-v2.soundcloud.com/users/77/track_likes/123", None),
                     ("DELETE", "https://api-v2.soundcloud.com/users/77/track_likes/123", None),
                     ("POST", "https://api-v2.soundcloud.com/me/play-history", '{"track_urn": "soundcloud:tracks:123"}'),
                     ("POST", "https://www.qobuz.com/api.json/0.2/favorite/create", {"album_ids": "0075679933652", "app_id": "1"})]


def test_youtube_music_saves_an_album_by_its_known_page(setup):
    _, _, db = setup
    album = ids_of(db, "One")["album_id"]
    db.execute("insert into ytm_release values (?, 'ar-x', 'MPREb_1', 'First', 2001, 'album', null)", (album,))
    asked = []

    class Ytm:
        def album_playlist(self, artist, title, browse_id):
            asked.append((artist, title, browse_id))
            return "OLAK5uy_1"

        def rate(self, client, kind, target, on):
            asked.append((kind, target, on))

    service = forward.YouTubeMusicAccount(Ytm(), "client")
    found = service.find_liked(db, forward.item_of(db, album))
    service.like("album", found, True)
    assert asked == [("Beta", "First", "MPREb_1"), ("album", "OLAK5uy_1", True)]


def test_soundcloud_history_takes_plays_of_songs_soundcloud_has(setup):
    _, _, db = setup
    client = Streaming()
    client.name, client.prefix, client.history = "soundcloud", "sc-", []
    client.add_history = client.history.append
    db.execute("insert into outbox(user, service, track_id, played, next_try) values ('admin', 'soundcloud', ?, 1, 0), "
               "('admin', 'soundcloud', ?, 2, 0)", (ids_of(db, "One")["id"], ids_of(db, "Two")["id"]))
    services = {"soundcloud": forward.SoundCloudAccount(Streaming, lambda: client)}
    forward.Forwarder(lambda: connect(db_path(db)), lambda db, user: services).flush()
    assert client.searched == ["One", "Two"] and client.history == []  # neither is on SoundCloud: dropped
    assert queued(db) == []
    client.find = lambda artist, title: {"id": "555"}
    db.execute("insert into outbox(user, service, track_id, played, next_try) values ('admin', 'soundcloud', 'sc-321', 3, 0), "
               "('admin', 'soundcloud', ?, 4, 0)", (ids_of(db, "One")["id"],))
    db.execute("insert into track(id, source, ref, title, artist) values ('sc-321', 'soundcloud', 'soundcloud:https://x', 'Own', 'Artist')")
    forward.Forwarder(lambda: connect(db_path(db)), lambda db, user: services).flush()
    assert client.history == ["321", "555"]
