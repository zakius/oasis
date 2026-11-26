"""Each user's SoundCloud and Bandcamp accounts, the previews of SoundCloud, Qobuz and Tidal, and other services'
songs played through oasis while their audio is cached. The services' answers are shaped as their APIs answer (read
from the live sites), with made-up names and ids."""
import base64
import json
import time

import pytest

from conftest import call, make_client, sign_in
from oasis import accounts, bandcamp, sources, ytm as ytm_module
from oasis.bandcamp import Bandcamp, Fan
from oasis.sources import Qobuz, QobuzPreview, SoundCloud, SoundCloudPreview, Tidal, TidalPreview
from oasis.ytm import Stream


def encoding(id, preset="aac_160k", protocol="hls", quality="sq", snipped=False, mime='audio/mp4; codecs="mp4a.40.2"'):
    return {"url": f"https://api-v2.soundcloud.com/media/soundcloud:tracks:{id}/{preset}/stream/{protocol}", "preset": preset,
            "duration": 30000 if snipped else 200000, "snipped": snipped, "quality": quality, "is_legacy_transcoding": True,
            "format": {"protocol": protocol, "mime_type": mime}}


def sc_track(id, title, artist="Some Artist", go=False, hq=False, progressive=False):
    """A track as SoundCloud's API answers it: a Go+ track (policy SNIP) has a 30 s duration and only a clip's encodings,
    anonymously."""
    if go:
        encodings = [encoding(id, "mp3_1_0", "hls", snipped=True, mime="audio/mpeg"), encoding(id, "mp3_1_0", "progressive", snipped=True, mime="audio/mpeg")]
    else:
        encodings = [encoding(id), encoding(id, "aac_96k", quality="lq"), encoding(id, "abr_sq")]
        if progressive:
            encodings.append(encoding(id, "mp3_0_1", "progressive", mime="audio/mpeg"))
        if hq:
            encodings.append(encoding(id, "aac_256k", "hls", "hq"))
    return {"id": id, "title": title, "policy": "SNIP" if go else "ALLOW", "duration": 30000 if go else 200000, "full_duration": 200000,
            "permalink_url": f"https://soundcloud.com/some-artist/{id}", "artwork_url": f"https://i1.sndcdn.com/artworks-{id}-large.jpg",
            "created_at": "2024-03-08T12:00:00Z", "track_authorization": "auth", "user": {"username": artist, "permalink": "some-artist"},
            "media": {"transcodings": encodings}}


class SoundCloudApi:
    """api-v2 as SoundCloud answers it, for one signed-in listener (id 77) with likes and two playlists."""

    def __init__(self):
        self.asked = []
        self.tracks = {1: sc_track(1, "Free Song"), 2: sc_track(2, "Go Song", go=True), 3: sc_track(3, "Listed Song"), 4: sc_track(4, "Stub Song")}

    def __call__(self, client, path, **fields):
        self.asked.append((client.token, path, fields))
        if path == "me":
            if client.token != "2-good":
                raise OSError("HTTP 401 from SoundCloud's me")
            return {"id": 77, "username": "Listener", "permalink": "listener"}
        if path == "search/tracks":
            return {"collection": list(self.tracks.values()), "next_href": None}
        if path == "users/77/track_likes":
            return {"collection": [{"created_at": "x", "kind": "like", "track": self.tracks[1]}, {"created_at": "x", "kind": "like", "track": self.tracks[2]}],
                    "next_href": "https://api-v2.soundcloud.com/users/77/track_likes?offset=x&limit=200"}
        if path.startswith("https://api-v2.soundcloud.com/users/77/track_likes?offset"):
            return {"collection": [{"kind": "like", "track": self.tracks[3]}], "next_href": None}
        if path == "users/77/playlists":
            return {"collection": [{"id": 500, "title": "Mine", "artwork_url": None, "kind": "playlist", "tracks": []}]}
        if path == "users/77/playlist_likes":
            return {"collection": [{"created_at": "x", "kind": "like", "playlist": {"id": 501, "title": "Liked List", "kind": "playlist"}}]}
        if path == "playlists/500":
            return {"id": 500, "tracks": [self.tracks[3], {"id": 4, "kind": "track", "monetization_model": "x", "policy": "ALLOW"}]}
        if path == "playlists/501":
            return {"id": 501, "tracks": [self.tracks[1]]}
        if path == "tracks":
            return [self.tracks[int(i)] for i in fields["ids"].split(",")]
        if path == "resolve":
            return next(t for t in self.tracks.values() if t["permalink_url"] == fields["url"])
        if path.startswith("tracks/"):
            return self.tracks[int(path.split("/")[1])]
        if path.startswith("https://api-v2.soundcloud.com/media/"):
            assert fields == {"track_authorization": "auth"}
            return {"url": "https://cf-media.sndcdn.com/" + path.split("/")[-3] + "/" + path.split("/")[-1] + "?Policy=x"}
        raise AssertionError(path)


@pytest.fixture
def soundcloud(monkeypatch):
    api = SoundCloudApi()
    monkeypatch.setattr(SoundCloud, "api", lambda self, path, **fields: api(self, path, **fields))
    return api


@pytest.fixture
def client(library, tmp_path, soundcloud):
    with make_client(library, tmp_path / "oasis.db", turned_on=[]) as c:
        yield c


def test_soundcloud_go_plus_songs_count_only_signed_in_and_then_in_its_hq_bitrate(soundcloud):
    assert SoundCloud().find("Some Artist", "Go Song") is None  # a 30 s clip to anyone else
    found = SoundCloud().find("Some Artist", "Free Song")
    assert (found["duration"], found["bitrate"], found["cover"], found["year"]) == (200, None, "https://i1.sndcdn.com/artworks-1-t500x500.jpg", 2024)
    soundcloud.tracks[2]["media"]["transcodings"].append(encoding(2, "aac_256k", "hls", "hq"))  # as a Go+ sign-in sees it
    found = SoundCloud({"token": "2-good"}).find("Some Artist", "Go Song")
    assert found["duration"] == 200 and found["key"] == "https://soundcloud.com/some-artist/2"
    assert SoundCloud().find("Someone Else", "Free Song") is None  # only the artist's own upload


def test_soundcloud_downloads_its_best_encoding_and_streams_only_a_single_file(soundcloud, monkeypatch, tmp_path):
    saved = []
    monkeypatch.setattr(sources, "save", lambda url, path: saved.append(("file", url, path.name)))
    monkeypatch.setattr(sources, "save_hls", lambda url, path, container: saved.append(("hls", url, path.name, container)))
    monkeypatch.setattr(sources, "measure", lambda url: 1234)
    soundcloud.tracks[1] = sc_track(1, "Free Song", hq=True, progressive=True)
    signed_in = SoundCloud({"token": "2-good"})
    assert signed_in.download("https://soundcloud.com/some-artist/1", tmp_path / "x").name == "x.m4a"
    assert saved[-1] == ("hls", "https://cf-media.sndcdn.com/aac_256k/hls?Policy=x", "x.m4a", "mp4")  # Go+'s 256 kbit/s first
    stream = signed_in.stream("https://soundcloud.com/some-artist/1")
    assert (stream.url, stream.content_type, stream.size) == ("https://cf-media.sndcdn.com/mp3_0_1/progressive?Policy=x", "audio/mpeg", 1234)
    with pytest.raises(LookupError):
        SoundCloud().stream("https://soundcloud.com/some-artist/3")  # HLS only: downloaded, not streamed
    with pytest.raises(LookupError):
        SoundCloud().download("https://soundcloud.com/some-artist/2", tmp_path / "y")  # the clip isn't the song


def test_soundcloud_previews_are_the_public_clips_of_go_plus_songs(soundcloud):
    assert SoundCloudPreview().find("Some Artist", "Free Song") is None
    assert SoundCloudPreview().find("Some Artist", "Go Song") == "2"
    assert SoundCloudPreview().link("2") == "https://cf-media.sndcdn.com/mp3_1_0/progressive?Policy=x"


def test_a_soundcloud_sign_in_is_each_users_and_each_capability_follows_its_switch(client, soundcloud):
    oasis = client.app.state.oasis
    db = oasis.connect()
    sign_in(client)
    page = client.get("/services").text
    assert all(f'action="/services/soundcloud/offered/{c}/off"' in page for c in ("songs", "signed-in", "account", "previews"))
    client.post("/services/soundcloud/on")
    page = client.get("/services").text
    assert "oauth_token" in page and "Your likes and playlists" in page
    assert "didn&#x27;t accept that token" in client.post("/services/soundcloud/link", data={"cookie": "2-bad"}).text
    assert "SoundCloud linked as Listener" in client.post("/services/soundcloud/link", data={"cookie": " OAuth 2-good "}).text
    assert json.loads(oasis.sources.turned_on(db, "admin")["soundcloud"]) == {"token": "2-good", "id": 77, "name": "Listener"}
    assert [c.token for c in oasis.sources.active(db, "admin")] == ["2-good"]
    accounts.create(db, "bob", "pw")
    oasis.sources.turn_on(db, "bob", "soundcloud")
    assert [c.token for c in oasis.sources.active(db, "bob")] == [None]  # bob plays what anyone can

    client.post("/services/soundcloud/offered/signed-in/off")
    assert [c.token for c in oasis.sources.active(db, "admin")] == [None]
    assert "Signed-in songs aren't offered on this server now" in client.get("/services").text
    client.post("/services/soundcloud/offered/account/off")
    assert oasis.sources.account(db, "admin", "soundcloud") is None
    assert "Linked as <b>Listener</b>" in client.get("/services").text  # kept for when it is offered again
    assert "aren&#x27;t offered" in client.post("/services/soundcloud/link", data={"cookie": "2-good"}).text


def test_soundcloud_likes_and_playlists_become_playlists_with_the_songs_the_user_can_play(client, soundcloud):
    oasis = client.app.state.oasis
    db = oasis.connect()
    oasis.sources.link(db, "admin", "soundcloud", {"token": "2-good", "id": 77, "name": "Listener"})
    oasis.soundcloud.refresh("admin")
    lists = {p["name"]: p for p in call(client, "getPlaylists")["playlists"]["playlist"]}
    assert set(lists) == {"SoundCloud likes", "Mine", "Liked List"}
    assert lists["Mine"]["owner"] == "SoundCloud" and lists["Mine"]["readonly"] is True and "youtubeId" not in lists["Mine"]
    titles = lambda name: [s["title"] for s in call(client, "getPlaylist", id=lists[name]["id"])["playlist"]["entry"]]
    assert titles("SoundCloud likes") == ["Free Song", "Go Song", "Listed Song"]  # every page; Go+ songs play signed in
    assert titles("Mine") == ["Listed Song", "Stub Song"]  # the tracks a playlist names by id only are read
    assert call(client, "updatePlaylist", playlistId=lists["Mine"]["id"], name="x")["error"]["message"] == "SoundCloud's playlists are read-only"

    oasis.sources.allow(db, "soundcloud", False, "signed-in")
    oasis.soundcloud.refresh("admin")
    assert titles("SoundCloud likes") == ["Free Song", "Listed Song"]
    oasis.mixes.drop(db, "admin")  # YouTube Music's leave SoundCloud's be
    assert len(call(client, "getPlaylists")["playlists"]["playlist"]) == 3
    sign_in(client)
    client.post("/services/soundcloud/unlink")
    assert call(client, "getPlaylists")["playlists"]["playlist"] == [] and "soundcloud" in oasis.sources.turned_on(db, "admin")


class Answer:
    def __init__(self, status=200, payload=None, data=b"", headers=None):
        self.status, self.payload, self.headers = status, payload, headers or {}
        self.data = json.dumps(payload).encode() if payload is not None else data

    def json(self):
        return self.payload


def test_bandcamp_reads_a_collection_page_by_page_and_readies_a_purchases_flac(monkeypatch):
    asked = []
    blob = {"digital_items": [{"type": "album", "title": "Album", "downloads": {"mp3-320": {"url": "https://popplers5.bandcamp.com/download/album?enc=mp3-320&id=1"},
                                                                               "flac": {"url": "https://popplers5.bandcamp.com/download/album?enc=flac&id=1&ts=1"}}}]}
    page = f'<div id="pagedata" data-blob="{json.dumps(blob).replace(chr(34), "&quot;")}"></div>'

    def answer(method, url, body=None, headers=None, **kwargs):
        asked.append((method, url, json.loads(body) if body else None, headers["Cookie"]))
        if url.endswith("collection_items"):
            older = json.loads(body)["older_than_token"]
            if older.endswith("::a::") and older.count(":") == 4 and not older.startswith("1790"):
                return Answer(payload={"items": [{"sale_item_type": "p", "sale_item_id": 1}], "more_available": True, "last_token": "1790:1:a::",
                                       "redownload_urls": {"p1": "https://bandcamp.com/download?payment_id=1"}})
            return Answer(payload={"items": [{"sale_item_type": "r", "sale_item_id": 2}], "more_available": False, "last_token": "1780:2:a::",
                                   "redownload_urls": {"r2": "https://bandcamp.com/download?payment_id=2"}})
        if url.startswith("https://bandcamp.com/download"):
            return Answer(data=page.encode())
        if "ts=1&" in url:  # the page's link has expired: a fresh one to ask with
            return Answer(data=b'var _statDL_result =  {"result":"err","retry_url":"https://popplers5.bandcamp.com/download/album?enc=flac&id=1&ts=2","errortype":"ExpiredFreeDownloadError"};')
        return Answer(data=b'if ( window.Downloads ) { Downloads.statResult ( {"result":"ok","download_url":"https://p4.bcbits.com/download/album/x/flac/1?token=t"} ) };')
    monkeypatch.setattr(bandcamp, "request", answer)
    fan = Fan("7%09abc")
    items, pages = fan.items(42, "collection")
    assert [i["sale_item_id"] for i in items] == [1, 2] and set(pages) == {"p1", "r2"}
    assert asked[1][2] == {"fan_id": 42, "older_than_token": "1790:1:a::", "count": bandcamp.PAGE} and asked[0][3] == "identity=7%09abc"
    assert fan.flac(pages["p1"]) == "https://p4.bcbits.com/download/album/x/flac/1?token=t"
    assert [a[1] for a in asked[-2:]] == ["https://popplers5.bandcamp.com/statdownload/album?enc=flac&id=1&ts=1&.vrs=1",
                                          "https://popplers5.bandcamp.com/statdownload/album?enc=flac&id=1&ts=2&.vrs=1"]


class FakeFan:
    collection, wishlist = [], []

    def __init__(self, identity):
        assert identity == "id-cookie"

    def items(self, fan_id, kind):
        if kind == "collection":
            return self.collection, {f"{i['sale_item_type']}{i['sale_item_id']}": f"https://bandcamp.com/download?{i['sale_item_id']}" for i in self.collection}
        return self.wishlist, {}

    def flac(self, page):
        return "https://p4.bcbits.com/" + page.rsplit("?", 1)[1]


class FakeImporter:
    def __init__(self):
        self.placed = []

    def place(self, folder, rg):
        self.placed.append((sorted(p.name for p in folder.iterdir()), rg))
        if rg == "rg-unmatched":
            raise LookupError("no edition matches")


def purchase(id, title, band="Some Band", kind="a"):
    return {"sale_item_type": "p", "sale_item_id": id, "item_title": title, "band_name": band, "tralbum_type": kind, "tralbum_id": id,
            "item_type": "album" if kind == "a" else "track", "item_url": f"https://someband.bandcamp.com/album/{id}", "album_title": None}


def test_bandcamp_imports_purchases_and_wants_the_wishlist_for_each_user(client, tmp_path, monkeypatch):
    oasis = client.app.state.oasis
    db = oasis.connect()
    groups = {"First Album": "rg-1", "Unmatched": "rg-unmatched", "Have It": "rg-here", "Wished Album": "rg-wish", "Parent Album": "rg-parent"}
    looked = []
    monkeypatch.setattr(bandcamp, "release_group", lambda db, artist, title, page: looked.append((title, page)) or groups.get(title))
    monkeypatch.setattr(bandcamp, "Fan", FakeFan)
    monkeypatch.setattr(bandcamp, "fetch", lambda url, folder: (folder / "purchase.zip").write_bytes(url.encode()))
    importer, wanted, imported = FakeImporter(), [], []
    worker = Bandcamp(oasis.connect, oasis.sources, importer, tmp_path / "staging", lambda db, user, ids: wanted.append((user, ids)), lambda: imported.append(1))
    db.execute("update track set mb_release_group = 'rg-here', suffix = 'flac' where id = (select id from track where source = 'local' limit 1)")
    FakeFan.collection = [purchase(1, "First Album"), purchase(2, "Nobody Knows"), purchase(3, "Unmatched"), purchase(4, "Have It")]
    FakeFan.wishlist = [{**purchase(5, "Wished Album"), "item_url": "https://someband.bandcamp.com/album/wished"},
                        {**purchase(6, "A Track", kind="t"), "album_title": "Parent Album"}]

    worker.refresh("admin")
    assert importer.placed == []  # Bandcamp isn't linked
    oasis.sources.link(db, "admin", "bandcamp", {"identity": "id-cookie", "fan_id": 42, "name": "Fan"})
    worker.refresh("admin")
    assert importer.placed == [(["purchase.zip"], "rg-1"), (["purchase.zip"], "rg-unmatched")] and imported == [1]
    assert wanted == [("admin", ["al-mb-rg-wish"]), ("admin", ["al-mb-rg-parent"])]
    assert ("Parent Album", None) in looked  # a wished track's album, by title only
    states = dict(db.execute("select id, state from bandcamp_item where user = 'admin'").fetchall())
    assert states == {"p1": "imported", "p2": "unknown", "p3": "failed", "p4": "here", "a5": "wanted", "t6": "wanted"}
    assert not list((tmp_path / "staging").iterdir())

    looked.clear()
    worker.refresh("admin")
    assert looked == [] and len(importer.placed) == 2  # done with, or tried too recently
    db.execute("update bandcamp_item set tried = tried - ?", (bandcamp.RETRY + 1,))
    worker.refresh("admin")
    assert [t for t, _ in looked] == ["Nobody Knows", "Unmatched"]

    oasis.sources.allow(db, "bandcamp", False, "account")
    looked.clear()
    db.execute("delete from bandcamp_item")
    worker.refresh("admin")
    assert looked == []  # account features not offered


def test_bandcamp_finds_the_release_linked_to_its_page_else_the_artists_album_of_that_title(monkeypatch):
    from oasis import musicbrainz
    monkeypatch.setattr(musicbrainz, "releases_at", lambda page, db=None: ["rel-1"] if page == "https://x.bandcamp.com/album/linked" else [])
    monkeypatch.setattr(musicbrainz, "release", lambda mbid, db=None: {"release-group": {"id": "rg-linked"}})
    # MusicBrainz may credit the artist by another name: the artist is found by name or alias first.
    monkeypatch.setattr(musicbrainz, "search_artist", lambda name: ["ar-1"] if name == "Some Band" else [])
    groups = {"ar-1": [{"id": "rg-other", "title": "Album Two"}, {"id": "rg-right", "title": "album"}]}
    monkeypatch.setattr(musicbrainz, "search", lambda kind, query, db=None: groups[query.rsplit("arid:", 1)[1]])
    assert bandcamp.release_group(None, "Some Band", "Album", "https://x.bandcamp.com/album/linked") == "rg-linked"
    assert bandcamp.release_group(None, "Some Band", "Album", "https://x.bandcamp.com/album/unlinked") == "rg-right"
    assert bandcamp.release_group(None, "Some Band", "Album Three", None) is None
    assert bandcamp.release_group(None, "Nobody", "Album", None) is None


def test_linking_bandcamp_checks_the_sign_in(client, monkeypatch):
    def summary(self):
        if self.headers["Cookie"] != "identity=7%09good":
            raise ValueError("Bandcamp didn't accept that sign-in (must be logged in): copy it again.")
        return {"fan_id": 42, "collection_summary": {"fan_id": 42, "username": "somefan"}}
    monkeypatch.setattr(Fan, "summary", summary)
    sign_in(client)
    client.post("/services/bandcamp/on")
    assert "Your purchases come into the library" in client.get("/services").text
    assert "must be logged in" in client.post("/services/bandcamp/link", data={"cookie": "bad"}).text
    assert "Bandcamp linked as somefan" in client.post("/services/bandcamp/link", data={"cookie": "identity=7%09good"}).text
    db = client.app.state.oasis.connect()
    assert json.loads(client.app.state.oasis.sources.turned_on(db, "admin")["bandcamp"]) == {"identity": "7%09good", "fan_id": 42, "name": "somefan"}
    assert [c.name for c in client.app.state.oasis.sources.active(db, "admin")] == ["bandcamp"]


def qobuz(method, url, fields=None, **kwargs):
    """Qobuz's API for an account without a subscription that streams: samples only."""
    if url.endswith("user/login"):
        return Answer(payload={"user_auth_token": "tok", "user": {"display_name": "Sampler", "credential": {"parameters": None}}})
    if url.endswith("track/search"):
        return Answer(payload={"tracks": {"items": [{"id": 5, "title": "Song", "performer": {"name": "Some Artist"}, "streamable": True, "duration": 200}]}})
    assert url.endswith("track/getFileUrl")
    return Answer(payload={"url": f"https://streaming-qobuz-sample/{fields['format_id']}", "sample": True})


def test_a_qobuz_account_without_a_subscription_links_for_samples_only(client, monkeypatch):
    monkeypatch.setattr(Qobuz, "fetch_player_keys", classmethod(lambda cls: ("123", ["s"])))
    monkeypatch.setattr(sources, "request", qobuz)
    oasis = client.app.state.oasis
    db = oasis.connect()
    sign_in(client)
    client.post("/services/qobuz/offered/previews/off")
    assert "no subscription that streams" in client.post("/services/qobuz/link", data={"email": "a@b", "password": "pw"}).text
    client.post("/services/qobuz/offered/previews/on")
    assert "Qobuz linked as Sampler" in client.post("/services/qobuz/link", data={"email": "a@b", "password": "pw"}).text
    assert "gives samples only" in client.get("/services").text
    assert oasis.sources.active(db, "admin") == []  # no full songs from it
    preview = next(p for p in oasis.sources.previews(db, "admin") if p.name == "qobuz")
    assert isinstance(preview, QobuzPreview) and preview.find("Some Artist", "Song") == "5"
    assert preview.link("5") == "https://streaming-qobuz-sample/5"  # MP3 samples


def test_tidal_previews_ask_for_the_clip_with_the_users_account(tmp_path, monkeypatch):
    manifest = base64.b64encode(json.dumps({"mimeType": "audio/mp4", "codecs": "mp4a.40.2", "encryptionType": "NONE",
                                            "urls": ["https://sp-ad-cf.audio.tidal.com/clip.mp4?token=x"]}).encode()).decode()

    class Session:
        class request:
            asked = []

            @classmethod
            def request(cls, method, path, params):
                cls.asked.append((path, params))
                return Answer(payload={"trackId": 9, "audioQuality": "HIGH", "manifestMimeType": "application/vnd.tidal.bts", "manifest": manifest,
                                       "assetPresentation": "PREVIEW"})

    class Client:
        session = Session()
    saved = []
    monkeypatch.setattr(sources, "save", lambda url, path: saved.append((url, path.name)))
    assert TidalPreview(Client()).download("9", tmp_path / "clip").name == "clip.m4a"
    assert Session.request.asked == [("tracks/9/playbackinfopostpaywall", {"playbackmode": "STREAM", "assetpresentation": "PREVIEW", "audioquality": "HIGH"})]
    assert saved == [("https://sp-ad-cf.audio.tidal.com/clip.mp4?token=x", "clip.m4a")]


class Streamer:
    """A service whose audio plays from a URL while it downloads, like Qobuz's."""
    name, title, prefix, suffix, content_type, bitrate = "streamer", "Streamer", "st-", "flac", "audio/flac", 1411
    release = None  # set: downloads wait for it

    def find(self, artist, title):
        return {"id": "8", "key": "8", "title": title, "artist": artist, "album": "Far", "duration": 200} if title == "Lost Song" else None

    def stream(self, key):
        return Stream("https://cdn.example/" + key, "audio/flac", 10, time.time() + 600)

    def download(self, key, path):
        if Streamer.release:
            Streamer.release.wait(5)
        target = path.with_name(path.name + ".flac")
        target.write_bytes(AUDIO)
        return target


AUDIO = b"0123456789"


def test_another_services_song_plays_through_oasis_with_ranges_while_its_audio_is_cached(library, tmp_path, monkeypatch):
    import threading
    from test_sources import LastFmWithGaps, similar
    monkeypatch.setattr(sources, "KINDS", {"streamer": Streamer, **sources.KINDS})
    monkeypatch.setattr(sources, "CAPABILITIES", {**sources.CAPABILITIES, "streamer": ("songs",)})
    read = []
    monkeypatch.setattr(ytm_module, "read_range", lambda stream, start, end: read.append(stream.url) or iter([AUDIO[start:end + 1]]))
    Streamer.release = threading.Event()
    with make_client(library, tmp_path / "oasis.db", turned_on=["lastfm", "streamer"]) as client:
        client.app.state.oasis.lastfm = LastFmWithGaps()
        song = next(s for s in similar(client) if s["title"] == "Lost Song")
        params = {"u": "admin", "p": "secret", "id": song["id"]}
        response = client.get("/rest/stream", params=params, headers={"Range": "bytes=2-5"})
        assert (response.status_code, response.content, response.headers["content-range"]) == (206, b"2345", "bytes 2-5/10")
        assert client.get("/rest/stream", params=params).content == AUDIO and read == ["https://cdn.example/8"] * 2
        Streamer.release.set()
        sources_ = client.app.state.oasis.sources
        for _ in range(50):
            if sources_.cached(sources.stream_stem("streamer", "8")) and "streamer:8" not in sources_.streams:
                break
            time.sleep(0.1)
        response = client.get("/rest/stream", params=params, headers={"Range": "bytes=7-"})
        assert response.content == b"789" and len(read) == 2  # from the cache now
    Streamer.release = None


def test_a_stream_in_parts_reads_each_ranges_share_of_each_part(monkeypatch):
    parts = {"https://seg/0": b"init", "https://seg/1": b"abcdef", "https://seg/2": b"ghij"}
    monkeypatch.setattr(ytm_module, "read_url", lambda url, headers, start, end: iter([parts[url][start:end + 1]]))
    stream = Stream("https://seg/0", "audio/flac", 14, 0, parts=[(url, len(data)) for url, data in parts.items()])
    whole = b"".join(parts.values())
    for start, end in ((0, 13), (2, 5), (4, 9), (9, 13), (10, 10)):
        assert b"".join(ytm_module.read_range(stream, start, end)) == whole[start:end + 1]


def test_measure_reads_a_files_size_from_its_first_byte(monkeypatch):
    monkeypatch.setattr(sources, "request", lambda method, url, headers: Answer(206, headers={"Content-Range": "bytes 0-0/4161200"}))
    assert sources.measure("https://t4.bcbits.com/stream/x") == 4161200
    monkeypatch.setattr(sources, "request", lambda method, url, headers: Answer(200, headers={}))
    with pytest.raises(OSError):
        sources.measure("https://no-ranges.example/x")


def test_previews_of_services_with_accounts_take_the_users_own_client(client, monkeypatch):
    oasis = client.app.state.oasis
    db = oasis.connect()
    monkeypatch.setattr(Tidal, "__init__", lambda self, account: setattr(self, "account", account))
    monkeypatch.setattr(Tidal, "current", lambda self: self.account)
    oasis.sources.turn_on(db, "admin", "tidal")
    assert [p.name for p in oasis.sources.previews(db, "admin")] == []  # not linked
    oasis.sources.link(db, "admin", "tidal", {"token_type": "Bearer", "access_token": "x", "name": "Mine"})
    oasis.sources.turn_on(db, "admin", "soundcloud")
    found = {p.name: p for p in oasis.sources.previews(db, "admin")}
    assert list(found) == ["soundcloud", "tidal"] and found["tidal"].client.account["name"] == "Mine"
    oasis.sources.allow(db, "tidal", False, "previews")
    assert [p.name for p in oasis.sources.previews(db, "admin")] == ["soundcloud"]
