import base64
import hashlib
import json

import pytest
import urllib3

from conftest import call, make_client, sign_in
from oasis import sources
from oasis.sources import Bandcamp, Qobuz
from test_missing import Acquiring
from test_recommend import FakeLastFm, FakeYtm


class FakeService:
    """A service that has one song and serves it as a file."""
    name, title, prefix, suffix, content_type, bitrate = "fake", "Fake", "fk-", "mp3", "audio/mpeg", 128
    asked = []

    formats = ["mp3"]  # what each download sends, last first
    broken = False  # the next download stops halfway

    def find(self, artist, title):
        self.asked.append((artist, title))
        if (artist, title) == ("Nobody Has", "Lost Song"):
            return {"id": "42", "key": "42", "title": title, "artist": artist, "album": "Far", "duration": 200, "cover": None}
        return None

    def download(self, key, path):
        target = path.with_name(f"{path.name}.{self.formats[-1]}")
        target.write_bytes(b"ID3 fake audio " + key.encode())
        if FakeService.broken:
            FakeService.broken = False
            path.with_name(path.name + ".m4a.ytdl").write_bytes(b"yt-dlp's notes")
            raise OSError("connection reset")
        return target


class LastFmWithGaps(FakeLastFm):
    def similar(self, artist, title, limit):
        return [("Nobody Has", "Lost Song", None), ("Nobody At All", "Gone", None)]


class FakePreview:
    """A preview service that has a clip of one song."""
    name, title, suffix, content_type = "clips", "Clips", "mp3", "audio/mpeg"

    def find(self, artist, title):
        return "7" if (artist, title) == ("Nobody Has", "Lost Song") else None

    def link(self, key):
        return "https://clips.example/" + key


@pytest.fixture
def client(library, tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "KINDS", {"fake": FakeService, **sources.KINDS})
    monkeypatch.setattr(sources, "PREVIEWS", {"clips": FakePreview})
    monkeypatch.setattr(sources, "save", lambda url, path: path.write_bytes(b"ID3 clip from " + url.encode()))
    FakeService.asked, FakeService.formats, FakeService.broken = [], ["mp3"], False
    with make_client(library, tmp_path / "oasis.db", FakeYtm()) as c:
        c.app.state.oasis.lastfm = LastFmWithGaps()
        yield c


def similar(client, **params):
    one = client.app.state.oasis.connect().execute("select id from track where title = 'One'").fetchone()[0]
    return call(client, "getSimilarSongs2", id=one, oasis="1", oasisSource="lastfm", **params)["similarSongs2"]["song"]


def test_a_song_youtube_music_lacks_plays_from_another_service_once_turned_on(client):
    assert "Lost Song" not in [s["title"] for s in similar(client)]
    db = client.app.state.oasis.connect()
    client.app.state.oasis.sources.set(db, "fake", "1")
    found = next(s for s in similar(client, unavailableSongs="1") if s["title"] == "Lost Song")
    assert found["id"] == "fk-42" and "playable" not in found
    assert db.execute("select source, ref, library from track where id = 'fk-42'").fetchone()[:] == ("fake", "fake:42", 0)
    assert client.get("/rest/stream.view", params={"u": "admin", "p": "secret", "id": "fk-42"}).content == b"ID3 fake audio 42"
    call(client, "star", id="fk-42")
    assert db.execute("select library from track where id = 'fk-42'").fetchone()[0] == 1  # starred: in the library, as streams are


def test_a_service_turned_off_stops_its_streams_and_its_songs_in_recommendations(client):
    db = client.app.state.oasis.connect()
    oasis = client.app.state.oasis
    oasis.sources.set(db, "fake", "1")
    assert "fk-42" in [s["id"] for s in similar(client)]
    oasis.sources.set(db, "fake", None)
    assert call(client, "stream", id="fk-42")["error"]["code"] == 70
    assert "fk-42" not in [s["id"] for s in similar(client)]


def test_the_admin_turns_services_on_and_off_on_the_dashboard(client):
    sign_in(client)
    page = client.get("/admin/services").text
    assert "Bandcamp" in page and "SoundCloud" in page and "Qobuz" in page and "Tidal" in page
    client.post("/admin/services/bandcamp/on")
    db = client.app.state.oasis.connect()
    assert [c.name for c in client.app.state.oasis.sources.active(db)] == ["bandcamp"]
    assert "Turn off" in client.get("/admin/services").text
    client.post("/admin/services/bandcamp/off")
    assert client.app.state.oasis.sources.active(db) == []
    assert "No such service" in client.post("/admin/services/qobuz/on").text  # Qobuz needs its account


class Answer:
    def __init__(self, status, payload):
        self.status, self.payload, self.data = status, payload, json.dumps(payload).encode()

    def json(self):
        return self.payload


def qobuz(method, url, fields=None, **kwargs):
    """Qobuz's API as linking sees it: the account signs in, and only the second of the player's secrets works."""
    if url.endswith("user/login"):
        return Answer(200, {"user_auth_token": "tok", "user": {"display_name": "Listener", "credential": {"parameters": {"lossless": True}}}})
    if url.endswith("track/search"):
        return Answer(200, {"tracks": {"items": [{"id": 5}]}})
    works = fields["request_sig"] == Qobuz.signature("5", Qobuz.FORMAT, fields["request_ts"], "second")
    return Answer(200, {"url": "https://x"}) if works else Answer(400, {"message": "Invalid Request Signature"})


def test_linking_qobuz_keeps_its_sign_in_and_the_secret_that_works_not_the_password(client, monkeypatch):
    monkeypatch.setattr(Qobuz, "fetch_player_keys", classmethod(lambda cls: ("123", ["first", "second"])))
    monkeypatch.setattr(sources, "request", qobuz)
    sign_in(client)
    assert "Qobuz linked as Listener" in client.post("/admin/services/qobuz", data={"email": "a@b", "password": "pw"}).text
    assert "Linked as <b>Listener</b>" in client.get("/admin/services").text
    settings = client.app.state.oasis.sources.settings(client.app.state.oasis.connect())
    assert json.loads(settings["qobuz"]) == {"app_id": "123", "token": "tok", "name": "Listener", "secret": "second"}


def test_qobuz_out_of_reach_says_so_without_the_password(client, monkeypatch):
    def away(method, url, fields=None, **kwargs):
        url += "?" + "&".join(f"{k}={v}" for k, v in (fields or {}).items())
        raise urllib3.exceptions.MaxRetryError(None, url)
    monkeypatch.setattr(Qobuz, "fetch_player_keys", classmethod(lambda cls: ("123", ["s"])))
    monkeypatch.setattr(sources.HTTP, "request", away)
    sign_in(client)
    answer = client.post("/admin/services/qobuz", data={"email": "a@b", "password": "pw"})
    assert answer.status_code == 200 and "couldn&#x27;t be reached" in answer.text
    assert hashlib.md5(b"pw").hexdigest() not in answer.text


def test_a_broken_download_is_never_played_as_the_song_and_the_format_that_came_is_kept(client):
    db = client.app.state.oasis.connect()
    client.app.state.oasis.sources.set(db, "fake", "1")
    similar(client)
    FakeService.broken = True
    assert call(client, "stream", id="fk-42")["error"]["code"] == 70
    FakeService.formats = ["m4a"]  # the service sent another format than it usually does
    assert client.get("/rest/stream.view", params={"u": "admin", "p": "secret", "id": "fk-42"}).content == b"ID3 fake audio 42"
    assert db.execute("select suffix, content_type from track where id = 'fk-42'").fetchone()[:] == ("m4a", "audio/mp4")
    cache = client.app.state.oasis.sources.cache
    assert [f.suffix for f in cache.glob("*.*")] == [".m4a"] and not list((cache / sources.PARTIAL).glob("*"))


def test_cached_audio_no_track_plays_is_deleted(client):
    db = client.app.state.oasis.connect()
    oasis = client.app.state.oasis
    oasis.sources.set(db, "fake", "1")
    similar(client)
    client.get("/rest/stream.view", params={"u": "admin", "p": "secret", "id": "fk-42"})
    orphan = oasis.sources.cache / (sources.stream_stem("fake", "gone") + ".mp3")
    orphan.write_bytes(b"x")
    oasis.prune_streams()
    assert [f.name for f in oasis.sources.cache.glob("*.*")] == [sources.stream_stem("fake", "42") + ".mp3"]


def test_bandcamp_reads_the_track_through_yt_dlp_and_takes_only_the_artists_full_song(monkeypatch):
    info = {"track": "Dancing Creek", "artist": "Hiroshi Yoshimura", "album": "Quiet Forest", "duration": 449.027, "release_year": 1986,
            "track_number": 8, "thumbnail": "https://f4.bcbits.com/img/a7_5.jpg", "formats": [{"format_id": "mp3-128"}]}
    results = [{"type": "t", "id": 1, "name": "Dancing Creek", "band_name": "Hiroshi Yoshimura", "album_name": "Quiet Forest",
                "item_url_path": "https://x.bandcamp.com/track/dancing-creek"}]
    monkeypatch.setattr(Bandcamp, "search", lambda self, q: results)
    monkeypatch.setattr(Bandcamp, "info", lambda self, url: info)
    found = Bandcamp().find("Hiroshi Yoshimura", "Dancing Creek")
    assert (found["album"], found["year"], found["duration"], found["cover"]) == ("Quiet Forest", 1986, 449, "https://f4.bcbits.com/img/a7_5.jpg")
    assert Bandcamp().find("Someone Else", "Dancing Creek") is None
    info["duration"] = 30.0
    assert Bandcamp().find("Hiroshi Yoshimura", "Dancing Creek") is None  # a preview clip
    assert Bandcamp.options()["impersonate"].client == "firefox"  # Bandcamp challenges Chrome's fingerprint


def test_qobuz_takes_its_app_id_and_secrets_from_the_players_script():
    secret = "0123456789abcdef0123456789abcdef"
    joined = base64.standard_b64encode(secret.encode()).decode() + "x" * 44
    seed, info, extras = joined[:10], joined[10:20], joined[20:]
    bundle = (f'production:{{api:{{appId:"123456789",appSecret:"{"a" * 32}"}}}} c.initialSeed("{seed}",window.utimezone.berlin) '
              f'name:"Europe/Berlin",info:"{info}",extras:"{extras}"')
    assert Qobuz.player_keys(bundle) == ("123456789", [secret])
    with pytest.raises(LookupError):
        Qobuz.player_keys("nothing here")
    assert Qobuz.signature("5", 6, 100, "s") == hashlib.md5(b"trackgetFileUrlformat_id6intentstreamtrack_id5100s").hexdigest()


def test_lengths_tell_previews_from_songs():
    assert not sources.full_length(30) and not sources.full_length(None) and sources.full_length(200)


def test_songs_of_other_services_share_their_own_pages():
    assert sources.share_url("bandcamp", "bandcamp:https://x.bandcamp.com/track/y") == "https://x.bandcamp.com/track/y"
    assert sources.share_url("tidal", "tidal:5") == "https://tidal.com/browse/track/5"
    assert sources.share_url("local", "/music/a.flac") is None


def lost(client, **params):
    return next((s for s in similar(client, **params) if s["title"] == "Lost Song"), None)


def test_a_song_nothing_has_plays_a_preview_only_in_apps_that_take_unplayable_songs(client):
    db = client.app.state.oasis.connect()
    assert "preview" not in lost(client, unavailableSongs="1")
    client.app.state.oasis.sources.set(db, "clips", "1")
    song = lost(client, unavailableSongs="1")
    assert song["playable"] is False and song["preview"] == 30
    assert lost(client) is None  # plain Subsonic apps get neither the song nor its clip
    assert client.get("/rest/stream.view", params={"u": "admin", "p": "secret", "id": song["id"]}).content == b"ID3 clip from https://clips.example/7"
    client.app.state.oasis.sources.set(db, "clips", None)
    assert call(client, "stream", id=song["id"])["error"]["code"] == 70


def test_the_dashboard_plays_a_preview_and_turns_preview_services_on(client):
    db = client.app.state.oasis.connect()
    client.app.state.oasis.sources.set(db, "clips", "1")
    song = lost(client, unavailableSongs="1")
    sign_in(client)
    client.app.state.oasis.acquire = Acquiring()
    assert f'src="/music/preview/{song["id"]}"' in client.get(f"/music/library/song/{song['id']}").text
    assert client.get(f"/music/preview/{song['id']}").content.startswith(b"ID3 clip")
    client.post("/admin/services/deezer/on")
    assert client.app.state.oasis.sources.settings(db)["deezer"] == "1"


def test_deezer_and_apple_find_a_clip_of_the_artists_song(monkeypatch):
    answers = {"https://api.deezer.com/search/track": {"data": [
                   {"id": 1, "title": "Dancing Creek", "artist": {"name": "Someone Else"}, "duration": 449, "preview": "x"},
                   {"id": 2, "title": "Dancing Creek", "artist": {"name": "Hiroshi Yoshimura"}, "duration": 449, "preview": "y"}]},
               "https://itunes.apple.com/search": {"results": [
                   {"trackId": 3, "trackName": "Dancing Creek", "artistName": "Hiroshi Yoshimura", "trackTimeMillis": 449027, "previewUrl": "z"}]}}
    monkeypatch.setattr(sources, "get_json", lambda url, **k: answers[url])
    assert sources.DeezerPreview().find("Hiroshi Yoshimura", "dancing creek") == "2"
    assert sources.DeezerPreview().find("Someone Else Again", "Dancing Creek") is None
    assert sources.ApplePreview().find("Hiroshi Yoshimura", "Dancing Creek") == "3"
