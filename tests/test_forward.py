import hashlib
import json
import time
import urllib.parse

import pytest

from conftest import call, make_client
from oasis import forward
from oasis.db import connect
from oasis.forward import LastFm, Listen, ListenBrainz, Rejected


class Service:
    def __init__(self, fail=None):
        self.scrobbled, self.playing, self.fail = [], [], fail

    def scrobble(self, listen):
        if self.fail:
            raise self.fail
        self.scrobbled.append(listen)

    def now_playing(self, listen):
        self.playing.append(listen)


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
    api_forwarder = forward.Forwarder(lambda: connect(db_path(db)), services)
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
    for _ in range(50):
        if services["ok"].playing:
            break
        time.sleep(0.01)
    assert services["ok"].playing[0].title == "One"
    assert db.execute("select count(*) from outbox").fetchone()[0] == 0


def test_unknown_track_scrobble_fails(setup):
    client, _, _ = setup
    assert call(client, "scrobble", id="tr-missing")["error"]["code"] == 70


def test_youtube_history_matches_local_tracks_once(setup):
    client, _, db = setup
    id = song_id(client, "One")
    db.execute("insert into outbox(service, track_id, played, next_try) values ('ytmusic', ?, 1, 0)", (id,))
    history, matched = [], []

    class Ytm:
        def add_history(self, video_id):
            history.append(video_id)

    def match(track):
        matched.append(track["title"])
        return "vidOne"

    forward.Forwarder(lambda: connect(db_path(db)), {"ytmusic": forward.YouTubeMusicHistory(Ytm())}, match).flush()
    assert history == ["vidOne"] and matched == ["One"]
    assert db.execute("select ytm_id from track where id = ?", (id,)).fetchone()[0] == "vidOne"


LISTEN = Listen("tr-1", "Artist", "Song", "Album", 200, 1_700_000_000, "vid")


def test_lastfm_scrobble_is_signed(monkeypatch):
    sent = []

    def post(url, data, headers):
        sent.append(dict(urllib.parse.parse_qsl(data.decode())))
        return 200, b'{"scrobbles": {}}'
    monkeypatch.setattr(forward, "post", post)
    LastFm("key", "secret", "session").scrobble(LISTEN)
    params = sent[0]
    assert params["method"] == "track.scrobble" and params["timestamp"] == "1700000000" and params["album"] == "Album"
    unsigned = {k: v for k, v in params.items() if k not in ("api_sig", "format")}
    base = "".join(k + unsigned[k] for k in sorted(unsigned)) + "secret"
    assert params["api_sig"] == hashlib.md5(base.encode()).hexdigest()


@pytest.mark.parametrize("error, kind", [(16, OSError), (29, OSError), (9, Rejected)])
def test_lastfm_errors(monkeypatch, error, kind):
    monkeypatch.setattr(forward, "post", lambda *a: (200, json.dumps({"error": error, "message": "x"}).encode()))
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
