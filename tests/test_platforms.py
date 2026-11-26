"""Libre.fm and the user's own Last.fm-like and ListenBrainz-like servers taking plays, the playlists ListenBrainz makes
for a user and Tidal's mixes, and Deezer, Tidal and SoundCloud as radio sources. The services' answers are shaped as
their APIs answer (ListenBrainz's, Deezer's, SoundCloud's and Libre.fm's read from the live sites; Tidal's as tidalapi
parses them), with made-up names and ids."""
import json
import urllib.error
import urllib.parse

import pytest
from starlette.testclient import TestClient

from conftest import call, make_client, sign_in
from oasis import accounts, forward, lastfm, servers, sources
from oasis.forward import Forwarder
from oasis.lastfm import LIBREFM, LIBREFM_AUTH, OPEN_KEY
from oasis.sources import Deezer, SoundCloud, Tidal
from test_sources import FakeService

SCROBBLED = {"scrobbles": {"@attr": {"accepted": 1, "ignored": 0}}}


@pytest.fixture
def client(library, tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "KINDS", {"fake": FakeService, **sources.KINDS})
    monkeypatch.setattr(sources, "CAPABILITIES", {**sources.CAPABILITIES, "fake": ("songs",)})
    monkeypatch.setattr(Tidal, "__init__", lambda self, account: setattr(self, "account", account))
    monkeypatch.setattr(Tidal, "current", lambda self: self.account)
    FakeService.asked, FakeService.formats, FakeService.broken = [], ["mp3"], False
    with make_client(library, tmp_path / "oasis.db", turned_on=[]) as c:
        db = c.app.state.oasis.connect()
        accounts.create(db, "bob", "pw")
        accounts.add_app(db, "bob", "tests", "app")
        yield c


class Asked(list):
    """Each call's address and parameters, in order; down: the servers that answer 503."""
    down: set


@pytest.fixture
def lastfm_api(monkeypatch):
    """Last.fm's API as Libre.fm answers it."""
    asked, down = Asked(), set()

    def post(url, data, headers):
        params = dict(urllib.parse.parse_qsl(data.decode()))
        asked.append((url, params))
        if url in down:
            return 503, b"Service Unavailable"
        if params["method"] == "auth.getSession":
            if params["token"] != "allowed":
                return 200, json.dumps({"error": 14, "message": "This token has not yet been authorised"}).encode()
            return 200, json.dumps({"session": {"name": "listener", "key": "sk-" + url, "subscriber": 0}}).encode()
        return 200, json.dumps(SCROBBLED).encode()
    monkeypatch.setattr(lastfm, "post", post)
    asked.down = down
    return asked


def song_id(client, title):
    return client.app.state.oasis.connect().execute("select id from track where title = ?", (title,)).fetchone()[0]


def forwarded(client, user="admin") -> Forwarder:
    """A play of "Two" by the user, queued as the API queues it, with the services the user has, and sent."""
    oasis = client.app.state.oasis
    forwarder = Forwarder(oasis.connect, oasis.user_services)
    forwarder.enqueue(oasis.connect(), user, song_id(client, "Two"), 1_700_000_000)
    forwarder.flush()
    return forwarder


def retried(client):
    """Sends the plays waiting to be retried now."""
    oasis = client.app.state.oasis
    oasis.connect().execute("update outbox set next_try = 0")
    Forwarder(oasis.connect, oasis.user_services).flush()


def server_ids(client, protocol, user="admin") -> list[int]:
    return [r["id"] for r in servers.entries(client.app.state.oasis.connect(), user, protocol)]


def sign_in_through(client, data):
    """Starts adding a Last.fm-like server: where oasis sends the browser, and the callback it gives that page."""
    response = client.post("/services/servers/lastfm/add", data=data, follow_redirects=False)
    assert response.status_code == 303
    location = response.headers["location"]
    callback = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(location).query))["cb"]
    return location, callback


def test_each_user_adds_last_fm_compatible_servers_and_plays_reach_each_one_on_its_own(client, lastfm_api):
    oasis = client.app.state.oasis
    db = oasis.connect()
    sign_in(client)
    page = client.get("/services").text
    assert 'id="service-lastfm-servers"' in page and "Sign in with Libre.fm" in page and "from its own host" in page
    assert "No such service" in client.post("/services/lastfm-servers/on").text  # a list, not a service to turn on

    location, callback = sign_in_through(client, {"preset": "librefm"})  # the preset fills in Libre.fm's addresses
    assert location.startswith(f"{LIBREFM_AUTH}?api_key={OPEN_KEY}&cb=")
    assert urllib.parse.urlsplit(callback).path.startswith("/services/servers/signed-in/")
    assert "failed" in client.get(callback + "?token=not-yet").text
    assert "didn&#x27;t start here" in client.get(callback + "?token=allowed").text  # each start signs in once
    _, callback = sign_in_through(client, {"preset": "librefm"})
    assert "Added Libre.fm, signed in as listener" in client.get(callback + "?token=allowed").text
    assert lastfm_api[-1][0] == LIBREFM and lastfm_api[-1][1]["method"] == "auth.getSession"

    for wrong in ("ftp://scrobble.example/2.0/", "scrobble.example/2.0/", "javascript:alert(1)"):
        assert "starting with http:// or https://" in client.post("/services/servers/lastfm/add", data={"api": wrong}).text
    location, _ = sign_in_through(client, {"api": "https://scrobble.example/gnufm/2.0"})
    assert location.startswith(f"https://scrobble.example/gnufm/api/auth/?api_key={OPEN_KEY}&cb=")  # GNU FM's place for it
    location, callback = sign_in_through(client, {"api": "http://10.0.0.5:8080/2.0/", "auth": "http://10.0.0.5:8080/login"})
    assert location.startswith("http://10.0.0.5:8080/login?api_key=")
    client.get(callback + "?token=allowed")
    libre, own = server_ids(client, "lastfm")
    rows = {r["id"]: r for r in servers.entries(db, "admin", "lastfm")}
    assert (rows[libre]["name"], rows[libre]["url"], rows[own]["name"], rows[own]["url"]) == ("Libre.fm", LIBREFM, "10.0.0.5", "http://10.0.0.5:8080/2.0/")
    assert json.loads(rows[own]["account"]) == {"session": "sk-http://10.0.0.5:8080/2.0/", "user": "listener", "auth": "http://10.0.0.5:8080/login"}
    page = client.get("/services").text
    assert "<b>Libre.fm</b> <code>https://libre.fm/2.0/</code>" in page and "<b>10.0.0.5</b>" in page

    def scrobbles():
        return [(url, params["sk"]) for url, params in lastfm_api if params["method"] == "track.scrobble"]
    forwarded(client)
    assert scrobbles() == [(LIBREFM, "sk-" + LIBREFM), ("http://10.0.0.5:8080/2.0/", "sk-http://10.0.0.5:8080/2.0/")]
    lastfm_api.clear()
    lastfm_api.down.add("http://10.0.0.5:8080/2.0/")
    forwarded(client)
    assert [url for url, _ in scrobbles()] == [LIBREFM, "http://10.0.0.5:8080/2.0/"]  # one down holds up no other
    assert db.execute("select service, attempts from outbox").fetchall()[0][:] == (f"server:{own}", 1)
    lastfm_api.down.clear()
    lastfm_api.clear()
    retried(client)
    assert [url for url, _ in scrobbles()] == ["http://10.0.0.5:8080/2.0/"] and db.execute("select count(*) from outbox").fetchone()[0] == 0

    client.post(f"/services/servers/{libre}/off")
    lastfm_api.clear()
    forwarded(client)
    assert [url for url, _ in scrobbles()] == ["http://10.0.0.5:8080/2.0/"]
    client.post(f"/services/servers/{libre}/on")
    client.post(f"/services/servers/{own}/remove")
    assert server_ids(client, "lastfm") == [libre] and list(oasis.user_services(db, "admin")) == [f"server:{libre}"]

    bob = TestClient(client.app)
    sign_in(bob, "bob", "pw")
    assert "No such server" in bob.post(f"/services/servers/{libre}/off").text and "No such server" in bob.post(f"/services/servers/{libre}/remove").text
    assert server_ids(client, "lastfm") == [libre] and oasis.user_services(db, "bob") == {}
    client.post("/services/lastfm-servers/offered/off")
    assert oasis.user_services(db, "admin") == {} and 'id="service-lastfm-servers"' not in bob.get("/services").text
    assert "isn&#x27;t offered" in bob.post("/services/servers/lastfm/add", data={"preset": "librefm"}).text
    assert server_ids(client, "lastfm") == [libre]  # kept for when the list is offered again


class ListenBrainzApi:
    """ListenBrainz's API as servers answer it, by address: one with validate-token, others without it (their answer
    is a 404 page), one down. Submissions to a server in refusing are refused, to one in failing fail for now."""

    def __init__(self):
        self.sent, self.refusing, self.failing = [], set(), set()

    def send(self, request):
        url, token = request.full_url, request.headers.get("Authorization")
        if url.startswith("https://down.example/"):
            raise urllib.error.URLError("connection refused")
        if url == "https://lb.example/apis/listenbrainz/1/validate-token":
            valid = token == "Token good"
            return 200, json.dumps({"code": 200, "message": "Token valid." if valid else "Token invalid.", "valid": valid,
                                    **({"user_name": "someone"} if valid else {})}).encode()
        return 404, b"<html>Not Found</html>"

    def post(self, url, data, headers):
        self.sent.append((url, json.loads(data), headers["Authorization"]))
        if url.split("/")[2] in self.refusing:
            return 401, b'{"code": 401, "error": "Invalid authorization token."}'
        if url.split("/")[2] in self.failing:
            return 502, b"Bad Gateway"
        return 200, b'{"status": "ok"}'


@pytest.fixture
def listenbrainz(monkeypatch):
    api = ListenBrainzApi()
    monkeypatch.setattr(forward, "send", api.send)
    monkeypatch.setattr(forward, "post", api.post)
    return api


def test_each_user_adds_listenbrainz_compatible_servers_checked_when_added_and_each_takes_plays_on_its_own(client, listenbrainz):
    oasis = client.app.state.oasis
    db = oasis.connect()
    sign_in(client)
    page = client.get("/services").text
    assert 'id="service-listenbrainz-servers"' in page and "/apis/listenbrainz/1/" in page

    def add(api, token="good", name=""):
        return client.post("/services/servers/listenbrainz/add", data={"api": api, "token": token, "name": name}).text
    assert "starting with http:// or https://" in add("file:///etc/passwd")
    assert "Paste your token" in add("https://lb.example/apis/listenbrainz/1", "")
    assert "didn&#x27;t answer" in add("https://down.example/1/")
    assert "refused that token" in add("https://lb.example/apis/listenbrainz/1", "bad")
    assert listenbrainz.sent == []  # validate-token said so
    assert "Added https://lb.example/apis/listenbrainz/1/" in add("https://lb.example/apis/listenbrainz/1", name="Mine")

    # A server without validate-token: a song playing now, which counts as no play, tests the token.
    listenbrainz.refusing.add("maloja.example")
    assert "refused that token" in add("https://maloja.example/apis/listenbrainz/1/", "other")
    url, body, token = listenbrainz.sent[-1]
    assert (url, body["listen_type"], token) == ("https://maloja.example/apis/listenbrainz/1/submit-listens", "playing_now", "Token other")
    listenbrainz.refusing.clear()
    assert "Added" in add("https://maloja.example/apis/listenbrainz/1/", "other")
    mine, maloja = server_ids(client, "listenbrainz")
    assert [(r["name"], json.loads(r["account"])) for r in servers.entries(db, "admin", "listenbrainz")] == [
        ("Mine", {"token": "good"}), ("maloja.example", {"token": "other"})]

    listenbrainz.sent.clear()
    listenbrainz.failing.add("lb.example")
    forwarded(client)
    assert [(url.split("/")[2], body["listen_type"], body["payload"][0]["track_metadata"]["track_name"]) for url, body, _ in listenbrainz.sent] == [
        ("lb.example", "single", "Two"), ("maloja.example", "single", "Two")]
    assert db.execute("select service from outbox").fetchall()[0][0] == f"server:{mine}"  # waits for its retry
    listenbrainz.failing.clear()
    listenbrainz.sent.clear()
    retried(client)
    assert [url.split("/")[2] for url, *_ in listenbrainz.sent] == ["lb.example"]

    client.post(f"/services/servers/{maloja}/off")
    assert list(oasis.user_services(db, "admin")) == [f"server:{mine}"]
    client.post(f"/services/servers/{maloja}/on")
    client.post(f"/services/servers/{mine}/remove")
    assert list(oasis.user_services(db, "admin")) == [f"server:{maloja}"]
    oasis.sources.allow(db, "listenbrainz-servers", False)
    assert oasis.user_services(db, "admin") == {}


def jspf(id, title, patch, date, expires=None, tracks=()):
    """A playlist ListenBrainz made for a user, as its API answers it."""
    extra = {"algorithm_metadata": {"source_patch": patch}} | ({"expires_at": expires} if expires else {})
    return {"annotation": "<p>We made this playlist from your recommended tracks.</p>", "creator": "listenbrainz", "date": date,
            "identifier": f"https://listenbrainz.org/playlist/{id}", "title": title,
            "extension": {forward.JSPF_PLAYLIST: {"additional_metadata": extra, "created_for": "someone", "creator": "listenbrainz", "public": True}},
            "track": [{"creator": artist, "title": name, "album": "Some Album", "duration": 200000,
                       "identifier": [f"https://musicbrainz.org/recording/rec-{name.lower().replace(' ', '-')}"]} for artist, name in tracks]}


class MadeForApi(ListenBrainzApi):
    """ListenBrainz's playlists made for the user "someone": a Daily Jams of today and one of before, an expired Weekly
    Jams, and a yearly one that never expires."""

    def __init__(self):
        super().__init__()
        songs = [("Beta", "One"), ("Nobody Has", "Lost Song"), ("Nobody At All", "Gone")]
        self.playlists = {
            "daily-2": jspf("daily-2", "Daily Jams for someone, 2026-11-26 Thu", "daily-jams", "2026-11-25T23:03:08+00:00", "2099-12-09T23:03:02.018205", songs),
            "daily-1": jspf("daily-1", "Daily Jams for someone, 2026-11-25 Wed", "daily-jams", "2026-11-24T23:03:08+00:00", "2099-12-08T23:03:02.018205"),
            "weekly": jspf("weekly", "Weekly Jams for someone, week of 2026-08-24 Mon", "weekly-jams", "2026-08-23T22:07:21+00:00",
                           "2026-09-06T22:07:08.989997+00:00", songs),
            "yearly": jspf("yearly", "Top Discoveries of 2025 for someone", "top-discoveries-of-2025", "2026-02-22T10:42:29+00:00", None, [("Beta", "Two")]),
        }
        self.read = []

    def send(self, request):
        url = request.full_url.removeprefix(forward.LISTENBRAINZ)
        self.read.append(url)
        if url == "validate-token":
            return 200, json.dumps({"code": 200, "message": "Token valid.", "valid": True, "user_name": "someone"}).encode()
        if url == f"user/someone/playlists/createdfor?count={forward.MADE_FOR}":
            listed = [{**p, "track": []} for p in self.playlists.values()]
            return 200, json.dumps({"count": len(listed), "offset": 0, "playlist_count": len(listed),
                                    "playlists": [{"playlist": p} for p in listed]}).encode()
        return 200, json.dumps({"playlist": self.playlists[url.removeprefix("playlist/")]}).encode()


def test_the_playlists_listenbrainz_makes_for_a_user_become_their_read_only_playlists(client, monkeypatch):
    api = MadeForApi()
    monkeypatch.setattr(forward, "send", api.send)
    oasis = client.app.state.oasis
    db = oasis.connect()
    oasis.listenbrainz_playlists.refresh("admin")
    assert api.read == []  # ListenBrainz isn't linked
    accounts.set_field(db, "admin", listenbrainz_token="tok")
    oasis.sources.turn_on(db, "admin", "listenbrainz")
    oasis.sources.turn_on(db, "admin", "fake")
    oasis.listenbrainz_playlists.refresh("admin")
    lists = {p["name"]: p for p in call(client, "getPlaylists", unavailableSongs="1")["playlists"]["playlist"]}
    assert set(lists) == {"Daily Jams for someone, 2026-11-26 Thu", "Top Discoveries of 2025 for someone"}  # the newest of each, unexpired
    daily = lists["Daily Jams for someone, 2026-11-26 Thu"]
    assert daily["owner"] == "ListenBrainz" and daily["readonly"] is True and "youtubeId" not in daily and "oasisMix" not in daily
    assert next(p for p in call(client, "getPlaylists", oasis="1")["playlists"]["playlist"] if p["id"] == daily["id"])["oasisMix"] is True
    assert daily["comment"] == "We made this playlist from your recommended tracks."
    entries = call(client, "getPlaylist", id=daily["id"], unavailableSongs="1")["playlist"]["entry"]
    assert [(s["title"], s["id"][:3], s.get("playable", True)) for s in entries] == [("One", "tr-", True), ("Lost Song", "fk-", True),
                                                                                    ("Gone", "ms-", False)]
    assert [s["title"] for s in call(client, "getPlaylist", id=daily["id"])["playlist"]["entry"]] == ["One", "Lost Song"]
    assert db.execute("select library from track where id = 'fk-42'").fetchone()[0] == 0  # songs made for you aren't yours yet
    assert call(client, "updatePlaylist", playlistId=daily["id"], name="x")["error"]["message"] == "ListenBrainz's playlists are read-only"

    api.playlists["daily-3"] = jspf("daily-3", "Daily Jams for someone, 2026-11-27 Fri", "daily-jams", "2026-11-26T23:03:08+00:00",
                                    "2099-12-10T23:03:02", [("The Alpha", "Song A")])
    accounts.set_field(db, "admin", missing_songs=0)
    oasis.listenbrainz_playlists.refresh("admin")
    renewed = call(client, "getPlaylist", id=daily["id"])["playlist"]  # the same playlist, renamed
    assert renewed["name"] == "Daily Jams for someone, 2026-11-27 Fri" and [s["title"] for s in renewed["entry"]] == ["Song A"]
    oasis.mixes.drop(db, "admin")  # YouTube Music's leave ListenBrainz's be
    assert len(call(client, "getPlaylists")["playlists"]["playlist"]) == 2

    oasis.sources.allow(db, "listenbrainz", False, "account")
    api.read.clear()
    oasis.listenbrainz_playlists.refresh("admin")
    assert api.read == [] and len(call(client, "getPlaylists")["playlists"]["playlist"]) == 2  # kept as they were
    sign_in(client)
    assert "Account features aren't offered on this server now" in client.get("/services").text
    client.post("/services/listenbrainz/off")
    assert call(client, "getPlaylists")["playlists"]["playlist"] == []


def test_tidal_mixes_become_read_only_playlists_while_its_account_features_are_offered(client, monkeypatch):
    mixes = [{"id": "0011aa", "title": "My Mix 1", "comment": "Beta, The Alpha and more", "cover": "https://resources.tidal.com/images/x/640x640.jpg",
              "songs": [("Beta", "Two"), ("Nobody Has", "Lost Song")]}]
    monkeypatch.setattr(Tidal, "mixes", lambda self: mixes)
    oasis = client.app.state.oasis
    db = oasis.connect()
    oasis.sources.turn_on(db, "admin", "fake")
    oasis.sources.turn_on(db, "admin", "tidal")
    oasis.tidal_mixes.refresh("admin")
    assert call(client, "getPlaylists")["playlists"]["playlist"] == []  # not linked
    oasis.sources.link(db, "admin", "tidal", {"token_type": "Bearer", "access_token": "x", "name": "Mine"})
    oasis.tidal_mixes.refresh("admin")
    [mix] = call(client, "getPlaylists")["playlists"]["playlist"]
    assert (mix["name"], mix["owner"], mix["comment"], mix["readonly"]) == ("My Mix 1", "Tidal", "Beta, The Alpha and more", True)
    assert "oasisMix" not in mix and call(client, "getPlaylists", oasis="1")["playlists"]["playlist"][0]["oasisMix"] is True
    assert [s["title"] for s in call(client, "getPlaylist", id=mix["id"])["playlist"]["entry"]] == ["Two", "Lost Song"]
    sign_in(client)
    assert "Your mixes (My Mix" in client.get("/services").text

    mixes.clear()
    oasis.sources.allow(db, "tidal", False, "account")
    oasis.tidal_mixes.refresh("admin")
    assert len(call(client, "getPlaylists")["playlists"]["playlist"]) == 1  # not read while not offered
    oasis.sources.allow(db, "tidal", True, "account")
    oasis.tidal_mixes.refresh("admin")
    assert call(client, "getPlaylists")["playlists"]["playlist"] == []  # gone from Tidal


def test_tidal_reads_the_tracks_of_its_mixes_and_leaves_videos_out(monkeypatch):
    from tidalapi.media import Track, Video
    from tidalapi.mix import Mix

    def made(kind, **fields):
        thing = kind.__new__(kind)
        thing.__dict__.update(fields)
        return thing

    class Artist:
        name = "Beta"
    songs = made(Mix, id="0011aa", title="My Mix 1", sub_title="Beta and more", _retrieved=True,
                 _items=[made(Track, name="Two", artist=Artist()), made(Video, name="A Video", artist=Artist())])
    videos = made(Mix, id="0022bb", title="My Video Mix", sub_title="", _retrieved=True, _items=[made(Video, name="A Video", artist=Artist())])
    monkeypatch.setattr(Mix, "image", lambda self, dimensions: f"https://resources.tidal.com/images/{self.id}/{dimensions}x{dimensions}.jpg")

    class Session:
        def mixes(self):
            return [songs, "a page's other item", videos]
    client = Tidal.__new__(Tidal)
    client.session = Session()
    assert client.mixes() == [{"id": "0011aa", "title": "My Mix 1", "comment": "Beta and more", "cover": "https://resources.tidal.com/images/0011aa/640x640.jpg",
                               "songs": [("Beta", "Two")]}]


def oasis_radio(client, **params) -> list[str]:
    return [s["id"] for s in call(client, "oasisRadio", **params)["oasisRadio"]["source"]]


def test_deezer_tidal_and_soundcloud_are_radio_sources_only_while_the_user_can_use_them(client):
    oasis = client.app.state.oasis
    db = oasis.connect()
    assert oasis_radio(client) == ["all", "library"]
    oasis.sources.turn_on(db, "admin", "deezer")
    oasis.sources.turn_on(db, "admin", "soundcloud")
    oasis.sources.turn_on(db, "admin", "tidal")
    assert oasis_radio(client) == ["all", "library", "deezer", "soundcloud"]  # Tidal's needs the account
    oasis.sources.link(db, "admin", "tidal", {"token_type": "Bearer", "access_token": "x", "name": "Mine"})
    assert oasis_radio(client) == ["all", "library", "deezer", "tidal", "soundcloud"]
    assert oasis_radio(client, u="bob", p="app") == ["all", "library"]
    for name in ("deezer", "tidal", "soundcloud"):
        oasis.sources.allow(db, name, False, "radio")
    assert oasis_radio(client) == ["all", "library"]
    assert call(client, "createPlaylist", oasis="1", name="Radio", oasisSource="deezer")["error"]["message"].startswith("oasisSource deezer isn't available")
    sign_in(client)
    page = client.get("/services").text
    assert all(f'action="/services/{name}/offered/radio/on"' in page for name in ("deezer", "tidal", "soundcloud"))
    assert "Not available: Last.fm, YouTube Music, ListenBrainz, Deezer, Tidal, SoundCloud" in client.get("/").text
    assert "Choose one of the sources" in client.post("/account/radio", data={"source": "tidal"}).text


def test_radio_sources_name_songs_that_play_from_the_library_or_the_users_services(client, monkeypatch):
    monkeypatch.setattr(Deezer, "radio", lambda self, artist: [("The Alpha", "Song A"), ("Nobody Has", "Lost Song")] if artist == "Beta" else [])
    monkeypatch.setattr(Tidal, "track_radio", lambda self, artist, title: [("The Alpha", "Song B")] if (artist, title) == ("Beta", "One") else [])
    monkeypatch.setattr(Tidal, "artist_radio", lambda self, artist: [("Beta", "Two")])
    monkeypatch.setattr(SoundCloud, "related", lambda self, artist, title: [("Nobody At All", "Gone"), ("The Alpha", "Song A")])
    oasis = client.app.state.oasis
    db = oasis.connect()
    for name in ("deezer", "soundcloud", "tidal", "fake"):
        oasis.sources.turn_on(db, "admin", name)
    oasis.sources.link(db, "admin", "tidal", {"token_type": "Bearer", "access_token": "x", "name": "Mine"})
    one = song_id(client, "One")

    def similar(source, **params):
        return [s["title"] for s in call(client, "getSimilarSongs2", id=one, oasis="1", oasisSource=source, **params)["similarSongs2"]["song"]]
    assert similar("deezer") == ["Song A", "Lost Song"]
    assert similar("tidal") == ["Song B", "Two"]  # the song's radio and its artist's, taking turns
    assert similar("soundcloud") == ["Song A"] and similar("soundcloud", unavailableSongs="1") == ["Gone", "Song A"]
    oasis.sources.allow(db, "deezer", False, "radio")
    assert similar("deezer") == []


def test_deezer_reads_an_artists_radio_and_related_artists(monkeypatch):
    asked = []

    def get_json(url, fields=None):
        asked.append((url.removeprefix(Deezer.API), fields))
        if url.endswith("search/artist"):
            return {"data": [{"id": 7, "name": "Some Band", "type": "artist"}, {"id": 8, "name": "Some Band Tribute", "type": "artist"}], "total": 2}
        if url.endswith("artist/7/radio"):
            return {"data": [{"id": 1, "title": "Song (Remastered)", "title_short": "Song", "artist": {"id": 7, "name": "Some Band"}},
                             {"id": 2, "title": "Other", "title_short": "Other", "artist": {"id": 9, "name": "Other Band"}}]}
        if url.endswith("artist/7/related"):
            return {"data": [{"id": 9, "name": "Other Band"}], "total": 1, "next": "https://api.deezer.com/artist/7/related?index=1"}
        return {"error": {"type": "DataException", "message": "no data", "code": 800}}
    monkeypatch.setattr(sources, "get_json", get_json)
    assert Deezer().radio("some band") == [("Some Band", "Song"), ("Other Band", "Other")]
    assert Deezer().related("Some Band") == ["Other Band"]
    assert Deezer().radio("Nobody") == [] and asked[-1] == ("search/artist", {"q": "Nobody", "limit": 5})


def test_soundcloud_reads_a_songs_related_tracks_by_their_artists(monkeypatch):
    def api(self, path, **fields):
        if path == "search/tracks":
            return {"collection": [{"id": 5, "title": "Some Song feat. Guest", "user": {"username": "Some Artist"}, "policy": "ALLOW"}]}
        assert (path, fields) == ("tracks/5/related", {"limit": sources.RELATED})
        return {"collection": [{"id": 6, "title": "Other Song (Remix)", "user": {"username": "A Remixer"}, "publisher_metadata": None},
                               {"id": 7, "title": "Label Act - Signed Song", "user": {"username": "Some Label"},
                                "publisher_metadata": {"id": 7, "urn": "soundcloud:tracks:7", "artist": "Label Act"}}],
                "next_href": None, "variant": "x"}
    monkeypatch.setattr(SoundCloud, "api", api)
    assert SoundCloud().related("Some Artist", "Some Song") == [("A Remixer", "Other Song (Remix)"), ("Label Act", "Signed Song")]


def test_deezers_related_artists_join_similar_artists_for_users_with_its_radio(client, monkeypatch):
    monkeypatch.setattr(Deezer, "related", lambda self, artist: ["The Alpha", "Not Here"] if artist == "Beta" else [])
    oasis = client.app.state.oasis
    db = oasis.connect()
    beta = db.execute("select artist_id from track where artist = 'Beta' limit 1").fetchone()[0]

    def similar(**params):
        return [a["name"] for a in call(client, "getArtistInfo2", id=beta, **params)["artistInfo2"].get("similarArtist", [])]
    assert similar() == []
    oasis.sources.turn_on(db, "admin", "deezer")
    assert similar() == ["The Alpha"]
    oasis.sources.turn_on(db, "bob", "deezer")
    oasis.sources.allow(db, "deezer", False, "radio")
    assert similar() == [] and similar(u="bob", p="app") == []


def test_admins_offer_the_lists_of_servers_and_the_new_capabilities(client):
    sign_in(client)
    page = client.get("/services").text
    for key in ("lastfm-servers", "listenbrainz-servers"):
        assert f'action="/services/{key}/offered/off"' in page and f"/services/{key}/offered/" + "account" not in page
    for key, capability in (("listenbrainz", "account"), ("tidal", "account"), ("tidal", "radio"), ("soundcloud", "radio"), ("deezer", "radio")):
        assert f'action="/services/{key}/offered/{capability}/off"' in page
