import json
import time

import pytest
from starlette.testclient import TestClient

from conftest import call, make_client, sign_in
from oasis import accounts, config, forward, sources
from oasis.forward import ListenBrainz
from oasis.ytm import song
from test_mixes_lyrics import FakeYtm as MixesYtm, item
from test_sources import FakeService, LastFmWithGaps


class Ytm(MixesYtm):
    """YouTube Music with mixes, one search result, and audio it keeps in its cache once fetched."""

    def search(self, query, limit):
        return [song(item("s1", "Remote Find"))]

    def find(self, artist, title, duration=None):
        return None

    def resolve(self, video_id):
        raise LookupError("no direct audio")

    def cached(self, video_id):
        path = self.cache / f"{video_id}.m4a"
        return path if path.exists() else None

    def fetch(self, video_id):
        path = self.cache / f"{video_id}.m4a"
        path.write_bytes(b"audio:" + video_id.encode())
        return path


class Locker:
    """A service with an account, like Qobuz: it finds one song, under an id from its account, and renews its sign-in."""
    name, title, prefix, suffix, content_type, bitrate = "locker", "Locker", "lk-", "mp3", "audio/mpeg", 128

    def __init__(self, account):
        self.account = account

    def current(self):
        return {**self.account, "renewed": True}

    def find(self, artist, title):
        if (artist, title) == ("Nobody Has", "Lost Song"):
            return {"id": self.account["token"], "key": "1", "title": title, "artist": artist, "album": "Far", "duration": 200, "cover": None}
        return None


@pytest.fixture
def client(library, tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "KINDS", {"locker": Locker, "fake": FakeService, **sources.KINDS})
    monkeypatch.setattr(sources, "ACCOUNTS", (*sources.ACCOUNTS, "locker"))
    monkeypatch.setattr(sources, "CAPABILITIES", {**sources.CAPABILITIES, "locker": ("songs",), "fake": ("songs",)})
    FakeService.asked, FakeService.formats, FakeService.broken = [], ["mp3"], False
    (tmp_path / "ytm").mkdir()
    with make_client(library, tmp_path / "oasis.db", Ytm(tmp_path / "ytm"), turned_on=["ytm", "lastfm"]) as c:
        oasis = c.app.state.oasis
        oasis.lastfm = LastFmWithGaps()
        db = oasis.connect()
        accounts.create(db, "bob", "pw")
        accounts.add_app(db, "bob", "tests", "app")
        oasis.sources.turn_on(db, "bob", "lastfm")
        yield c


BOB = {"u": "bob", "p": "app"}


def similar(client, **params) -> list[str]:
    one = client.app.state.oasis.connect().execute("select id from track where title = 'One'").fetchone()[0]
    return [s["id"] for s in call(client, "getSimilarSongs2", id=one, oasis="1", oasisSource="lastfm", **params)["similarSongs2"]["song"]]


def searched(client, **params) -> list[str]:
    return [s["id"] for s in call(client, "search3", query="remote", **params)["searchResult3"]["song"]]


def stream(client, id, **params):
    return client.get("/rest/stream.view", params={"u": "admin", "p": "secret", "id": id, **params})


def test_each_user_finds_songs_with_their_own_services_and_accounts(client):
    oasis = client.app.state.oasis
    db = oasis.connect()
    oasis.sources.link(db, "admin", "locker", {"token": "a"})
    assert [c.name for c in oasis.sources.active(db, "admin")] == ["locker"] and oasis.sources.active(db, "bob") == []
    assert json.loads(oasis.sources.turned_on(db, "admin")["locker"]) == {"token": "a", "renewed": True}  # a renewed sign-in is kept
    assert "lk-a" in similar(client) and "lk-a" not in similar(client, **BOB)
    oasis.sources.link(db, "bob", "locker", {"token": "b"})
    assert [c.account["token"] for c in oasis.sources.active(db, "bob")] == ["b"]
    assert "lk-a" in similar(client, **BOB)  # the song the service has, which bob's account plays
    assert json.loads(oasis.sources.turned_on(db, "admin")["locker"])["token"] == "a"
    assert "yt-s1" in searched(client) and "yt-s1" not in searched(client, **BOB)  # bob hasn't turned YouTube Music on


def test_a_song_of_a_service_the_user_lacks_plays_only_once_its_audio_is_kept(client):
    oasis = client.app.state.oasis
    db = oasis.connect()
    oasis.sources.turn_on(db, "admin", "fake")
    assert "fk-42" in similar(client)
    refused = call(client, "stream", id="fk-42", **BOB)["error"]
    assert refused["code"] == 70 and "Fake, which you haven't turned on" in refused["message"]
    assert call(client, "getSong", id="fk-42", unavailableSongs="1", **BOB)["song"]["playable"] is False
    assert "playable" not in call(client, "getSong", id="fk-42", **BOB)["song"]  # plain apps see it, and its stream fails
    assert stream(client, "fk-42").content == b"ID3 fake audio 42"  # the admin's play keeps its audio
    assert stream(client, "fk-42", **BOB).content == b"ID3 fake audio 42"
    assert "playable" not in call(client, "getSong", id="fk-42", unavailableSongs="1", **BOB)["song"]

    searched(client)  # the same for YouTube Music's songs
    assert call(client, "stream", id="yt-s1", **BOB)["error"]["code"] == 70
    assert stream(client, "yt-s1").content == b"audio:s1"
    assert stream(client, "yt-s1", **BOB).content == b"audio:s1"


def test_an_admin_offers_each_service_and_each_of_its_capabilities(client):
    oasis = client.app.state.oasis
    db = oasis.connect()
    sign_in(client)
    client.post("/services/bandcamp/on")
    oasis.sources.turn_on(db, "bob", "bandcamp")
    page = client.get("/services").text
    assert 'action="/services/bandcamp/offered/off"' in page and 'action="/services/bandcamp/offered/songs/off"' in page
    client.post("/services/bandcamp/offered/songs/off")
    assert oasis.sources.active(db, "admin") == []
    assert "Full songs aren't offered on this server now" in client.get("/services").text
    client.post("/services/bandcamp/offered/songs/on")
    assert [c.name for c in oasis.sources.active(db, "admin")] == ["bandcamp"]
    page = client.post("/services/bandcamp/offered/off").text
    assert oasis.sources.active(db, "admin") == [] and 'id="service-bandcamp"' not in page  # not listed for users
    assert 'action="/services/bandcamp/offered/on"' in page and "/services/bandcamp/offered/songs" not in page
    assert "bandcamp" in oasis.sources.turned_on(db, "bob")  # bob's choice stays for when it is offered again

    bob = TestClient(client.app)
    sign_in(bob, "bob", "pw")
    page = bob.get("/services").text
    assert "Bandcamp" not in page and "Offered on this server" not in page and "SoundCloud" in page
    assert "isn&#x27;t offered" in bob.post("/services/bandcamp/on").text
    assert "Only admins" in bob.post("/services/bandcamp/offered/on").text


@pytest.mark.parametrize("allowed", [False, True])
def test_youtube_music_starts_as_oasis_toml_says_and_an_admin_switches_it_while_oasis_runs(library, tmp_path, allowed):
    (tmp_path / "ytm").mkdir()
    with make_client(library, tmp_path / "oasis.db", Ytm(tmp_path / "ytm"), ytm_allowed=allowed) as client:
        assert ("yt-s1" in searched(client)) == allowed
        sign_in(client)
        assert ('id="service-ytm"' in client.get("/services").text) == allowed
        client.post(f"/services/ytm/offered/{'off' if allowed else 'on'}")
        assert ("yt-s1" in searched(client)) != allowed


def test_oasis_toml_gives_youtube_music_a_cache_whether_or_not_it_is_offered(tmp_path):
    path = tmp_path / "oasis.toml"
    path.write_text("[ytmusic]\nenabled = false\n")
    loaded = config.load(path)
    assert loaded.ytm_cache == tmp_path / "cache" and loaded.ytm_allowed is False
    path.write_text("[ytmusic]\nenabled = true\n")
    assert config.load(path).ytm_allowed is True


def test_youtube_music_without_full_songs_still_loads_mixes(client):
    oasis = client.app.state.oasis
    db = oasis.connect()
    sign_in(client)
    client.post("/services/ytm/offered/songs/off")
    assert call(client, "oasisLinkYouTubeMusic", cookie="SID=1; __Secure-3PAPISID=abc")["youtubeMusic"] == {"account": "Test Account"}
    oasis.mixes.refresh("admin")
    assert "My Supermix" in {p["name"] for p in call(client, "getPlaylists")["playlists"]["playlist"]}
    assert "yt-s1" not in searched(client) and call(client, "stream", id="yt-m1")["error"]["code"] == 70
    assert "Full songs aren't offered on this server now" in client.get("/services").text
    assert "ytmusic" in forward.user_services(None, oasis.ytm, accounts.get(db, "admin"), oasis.sources.usable(db, "admin"))

    client.post("/services/ytm/offered/songs/on")
    client.post("/services/ytm/offered/account/off")
    assert "yt-s1" in searched(client)
    assert "ytmusic" not in forward.user_services(None, oasis.ytm, accounts.get(db, "admin"), oasis.sources.usable(db, "admin"))
    assert "aren't offered" in call(client, "oasisLinkYouTubeMusic", cookie="SID=1; __Secure-3PAPISID=abc")["error"]["message"]
    assert "Account features aren't offered on this server now" in client.get("/services").text
    assert accounts.get(db, "admin")["ytm_auth"]  # kept for when they are offered again


def test_turning_a_service_on_asks_for_its_sign_in_and_off_forgets_it(client, monkeypatch):
    monkeypatch.setattr(ListenBrainz, "valid", lambda self: self.token == "good")
    oasis = client.app.state.oasis
    db = oasis.connect()
    sign_in(client)
    page = client.get("/services").text
    assert 'action="/services/listenbrainz/on"' in page and "listenbrainz.org/settings" not in page  # folded while off
    page = client.post("/services/listenbrainz/on").text
    assert "listenbrainz.org/settings" in page and "Not used till it is linked" in page
    assert "listenbrainz" not in oasis.sources.usable(db, "admin")
    assert "isn&#x27;t valid" in client.post("/services/listenbrainz/link", data={"token": "bad"}).text
    assert "ListenBrainz linked" in client.post("/services/listenbrainz/link", data={"token": "good"}).text
    assert "listenbrainz" in oasis.sources.usable(db, "admin")
    assert "oasis forgets your sign-in" in client.get("/services").text  # switching off asks first
    client.post("/services/listenbrainz/off")
    assert accounts.get(db, "admin")["listenbrainz_token"] is None and "listenbrainz" not in oasis.sources.turned_on(db, "admin")

    assert "Copy as cURL" in client.get("/services").text  # YouTube Music, on: its account's steps
    client.post("/services/ytm/link", data={"headers": "SID=1; __Secure-3PAPISID=abc"})
    assert "Playlists shown in your apps" in client.get("/services").text
    client.post("/services/ytm/off")
    assert accounts.get(db, "admin")["ytm_auth"] is None and "yt-s1" not in searched(client)


def test_each_user_links_their_own_tidal(client, monkeypatch):
    finish = []

    def link_start(done):
        finish.append(done)
        return {"url": "https://link.tidal.com/ABCDE", "code": f"CODE{len(finish)}", "until": time.time() + 300}
    monkeypatch.setattr(sources.Tidal, "link_start", staticmethod(link_start))
    db = client.app.state.oasis.connect()
    sign_in(client)
    client.post("/services/tidal/on")
    assert "CODE1" in client.post("/services/tidal/link").text
    bob = TestClient(client.app)
    sign_in(bob, "bob", "pw")
    assert "CODE1" not in bob.post("/services/tidal/on").text
    finish[0]({"token_type": "Bearer", "access_token": "x", "name": "Admin's"})
    on = client.app.state.oasis.sources.turned_on
    assert json.loads(on(db, "admin")["tidal"])["name"] == "Admin's" and on(db, "bob")["tidal"] is None
    assert "Linked as <b>Admin&#x27;s</b>" in client.get("/services").text
