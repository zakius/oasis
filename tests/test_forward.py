import json
import threading
import time
import urllib.parse

import pytest

from conftest import call, make_client
from oasis import forward, lastfm
from oasis.db import connect
from oasis.forward import Listen, ListenBrainz
from oasis.lastfm import LastFm
from oasis.remote import Rejected


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

    services = {"ytmusic": forward.YouTubeMusicHistory(Ytm(), "client")}
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
