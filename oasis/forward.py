"""Forwards plays to Last.fm, ListenBrainz and YouTube Music history, retrying until each service takes them."""
import hashlib
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

log = logging.getLogger(__name__)

LASTFM = "https://ws.audioscrobbler.com/2.0/"
LISTENBRAINZ = "https://api.listenbrainz.org/1/submit-listens"
MAX_ATTEMPTS = 20


class Rejected(Exception):
    """The service refused this play for good; retrying won't help."""


@dataclass
class Listen:
    track_id: str
    artist: str
    title: str
    album: str | None
    duration: int | None
    played: float
    ytm_id: str | None


def post(url: str, data: bytes, headers: dict) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data, headers), timeout=20) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def check(status: int, body: bytes, service: str):
    if status == 429 or status >= 500:
        raise OSError(f"{service}: HTTP {status}")
    if status >= 400:
        raise Rejected(f"{service}: HTTP {status} {body[:200]!r}")


class LastFm:
    def __init__(self, api_key: str, secret: str, session: str | None):
        self.api_key, self.secret, self.session = api_key, secret, session

    def call(self, params: dict) -> dict:
        params = {**params, "api_key": self.api_key}
        signature = "".join(k + str(params[k]) for k in sorted(params)) + self.secret
        params |= {"api_sig": hashlib.md5(signature.encode()).hexdigest(), "format": "json"}
        status, body = post(LASTFM, urllib.parse.urlencode(params).encode(), {"Content-Type": "application/x-www-form-urlencoded"})
        result = json.loads(body or b"{}")
        if "error" in result:
            # 11 and 16 are temporary outages, 29 is the rate limit; everything else is a real refusal.
            if result["error"] in (11, 16, 29):
                raise OSError(f"Last.fm: {result.get('message')}")
            raise Rejected(f"Last.fm: {result.get('message')}")
        check(status, body, "Last.fm")
        return result

    def read(self, params: dict) -> dict:
        """Read-only methods need only the API key."""
        query = urllib.parse.urlencode({**params, "api_key": self.api_key, "format": "json"})
        with urllib.request.urlopen(f"{LASTFM}?{query}", timeout=15) as response:
            result = json.load(response)
        if "error" in result:
            raise LookupError(f"Last.fm: {result.get('message')}")
        return result

    def similar(self, artist: str, title: str, limit: int) -> list[tuple[str, str]]:
        tracks = self.read({"method": "track.getSimilar", "artist": artist, "track": title, "limit": limit,
                            "autocorrect": 1})["similartracks"]["track"]
        return [(t["artist"]["name"], t["name"]) for t in tracks]

    def artist_info(self, name: str) -> dict:
        return self.read({"method": "artist.getInfo", "artist": name, "autocorrect": 1})["artist"]

    def track_params(self, listen: Listen) -> dict:
        params = {"artist": listen.artist, "track": listen.title}
        if listen.album:
            params["album"] = listen.album
        if listen.duration:
            params["duration"] = listen.duration
        return params

    def scrobble(self, listen: Listen):
        self.call({"method": "track.scrobble", "sk": self.session, "timestamp": int(listen.played), **self.track_params(listen)})

    def now_playing(self, listen: Listen):
        self.call({"method": "track.updateNowPlaying", "sk": self.session, **self.track_params(listen)})


class ListenBrainz:
    def __init__(self, token: str):
        self.token = token

    def submit(self, kind: str, listen: Listen):
        metadata = {"artist_name": listen.artist, "track_name": listen.title,
                    "additional_info": {"media_player": "oasis", "submission_client": "oasis"}}
        if listen.album:
            metadata["release_name"] = listen.album
        if listen.duration:
            metadata["additional_info"]["duration_ms"] = listen.duration * 1000
        if listen.ytm_id:
            metadata["additional_info"]["origin_url"] = "https://music.youtube.com/watch?v=" + listen.ytm_id
        entry = {"track_metadata": metadata} | ({"listened_at": int(listen.played)} if kind == "single" else {})
        body = json.dumps({"listen_type": kind, "payload": [entry]}).encode()
        check(*post(LISTENBRAINZ, body, {"Authorization": "Token " + self.token, "Content-Type": "application/json"}), "ListenBrainz")

    def scrobble(self, listen: Listen):
        self.submit("single", listen)

    def now_playing(self, listen: Listen):
        self.submit("playing_now", listen)


class YouTubeMusicHistory:
    """Adds plays to YouTube Music's history, which feeds its recommendations, also for plays of local copies."""

    def __init__(self, ytm):
        self.ytm = ytm

    def scrobble(self, listen: Listen):
        if not listen.ytm_id:
            raise Rejected(f"no YouTube Music match for {listen.artist} - {listen.title}")
        self.ytm.add_history(listen.ytm_id)

    def now_playing(self, listen: Listen):
        pass


class Forwarder:
    """Keeps plays in the database outbox until every configured service has taken them."""

    def __init__(self, connect, services: dict, match=None):
        self.connect, self.services, self.match = connect, services, match
        self.wake = threading.Event()

    def enqueue(self, db, track_id: str, played: float):
        db.executemany("insert into outbox(service, track_id, played, next_try) values (?, ?, ?, 0)",
                       [(name, track_id, played) for name in self.services])
        self.wake.set()

    def now_playing(self, listen: Listen):
        def send():
            for name, service in self.services.items():
                try:
                    service.now_playing(listen)
                except Exception as e:
                    log.info("%s now playing failed: %s", name, e)
        threading.Thread(target=send, daemon=True).start()

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        while True:
            try:
                self.flush()
            except Exception:
                log.exception("forwarding failed")
            self.wake.wait(60)
            self.wake.clear()

    def flush(self):
        db = self.connect()
        try:
            due = db.execute("select * from outbox where next_try <= ? order by played", (time.time(),)).fetchall()
            for entry in due:
                self.send(db, entry)
        finally:
            db.close()

    def send(self, db, entry):
        listen = self.listen(db, entry)
        try:
            if listen is None:
                raise Rejected(f"track {entry['track_id']} is gone")
            self.services[entry["service"]].scrobble(listen)
        except Rejected as e:
            log.warning("dropping play: %s", e)
        except Exception as e:
            if entry["attempts"] + 1 < MAX_ATTEMPTS:
                delay = min(3600, 30 * 2 ** entry["attempts"])
                db.execute("update outbox set attempts = attempts + 1, next_try = ? where id = ?", (time.time() + delay, entry["id"]))
                log.info("%s will retry in %d s: %s", entry["service"], delay, e)
                return
            log.warning("giving up on play after %d attempts: %s", MAX_ATTEMPTS, e)
        db.execute("delete from outbox where id = ?", (entry["id"],))

    def listen(self, db, entry) -> Listen | None:
        r = db.execute("select * from track where id = ?", (entry["track_id"],)).fetchone()
        if r is None:
            return None
        ytm_id = r["ytm_id"]
        if entry["service"] == "ytmusic" and not ytm_id and self.match:
            if ytm_id := self.match(r):
                db.execute("update track set ytm_id = ? where id = ?", (ytm_id, r["id"]))
        return listen_for(r, entry["played"], ytm_id)


def listen_for(r, played: float, ytm_id: str | None = None) -> Listen:
    # YouTube Music joins artists into one string; services match better on the main one.
    artist = r["album_artist"] if r["source"] == "ytm" else r["artist"]
    album = r["album"] if r["album"] != r["title"] else None
    return Listen(r["id"], artist, r["title"], album, r["duration"], played, ytm_id or r["ytm_id"])


def services(config, db, ytm) -> dict:
    found = {}
    session = db.execute("select value from setting where key = 'lastfm_session'").fetchone()
    if config.lastfm_key and config.lastfm_secret and session:
        found["lastfm"] = LastFm(config.lastfm_key, config.lastfm_secret, session[0])
    if config.listenbrainz_token:
        found["listenbrainz"] = ListenBrainz(config.listenbrainz_token)
    if ytm and ytm.account:
        found["ytmusic"] = YouTubeMusicHistory(ytm)
    return found


def lastfm_login(config, db):
    """Signs oasis in to Last.fm once; the session key never expires."""
    if not (config.lastfm_key and config.lastfm_secret):
        raise SystemExit("Set [lastfm] api_key and secret first: https://www.last.fm/api/account/create")
    lastfm = LastFm(config.lastfm_key, config.lastfm_secret, None)
    token = lastfm.call({"method": "auth.getToken"})["token"]
    input(f"Allow oasis at https://www.last.fm/api/auth/?api_key={config.lastfm_key}&token={token}\nthen press Enter: ")
    session = lastfm.call({"method": "auth.getSession", "token": token})["session"]
    db.execute("insert or replace into setting values ('lastfm_session', ?)", (session["key"],))
    print(f"Signed in to Last.fm as {session['name']}.")
