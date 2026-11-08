"""Forwards plays to Last.fm, ListenBrainz and YouTube Music history, retrying until each service takes them."""
import json
import logging
import threading
import time
import urllib.request
from dataclasses import dataclass

from . import worker
from .lastfm import LastFm, signs_in
from .remote import Rejected, check, post
from .tracks import outside_names
from .youtube import video_of
from .ytm import WATCH

log = logging.getLogger(__name__)

LISTENBRAINZ = "https://api.listenbrainz.org/1/"
MAX_ATTEMPTS = 20


@dataclass
class Listen:
    track_id: str
    artist: str
    title: str
    album: str | None
    duration: int | None
    played: float
    ytm_id: str | None


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
            metadata["additional_info"]["origin_url"] = WATCH + listen.ytm_id
        entry = {"track_metadata": metadata} | ({"listened_at": int(listen.played)} if kind == "single" else {})
        body = json.dumps({"listen_type": kind, "payload": [entry]}).encode()
        check(*post(LISTENBRAINZ + "submit-listens", body, {"Authorization": "Token " + self.token, "Content-Type": "application/json"}), "ListenBrainz")

    def scrobble(self, listen: Listen):
        self.submit("single", listen)

    def valid(self) -> bool:
        """Whether ListenBrainz knows the token; an exception when it doesn't answer."""
        request = urllib.request.Request(LISTENBRAINZ + "validate-token", headers={"Authorization": "Token " + self.token})
        with urllib.request.urlopen(request, timeout=15) as response:
            return bool(json.load(response).get("valid"))

    def now_playing(self, listen: Listen):
        self.submit("playing_now", listen)


class YouTubeMusicHistory:
    """Adds plays to YouTube Music's history, which feeds its recommendations, also for plays of local copies."""

    def __init__(self, ytm, client):
        self.ytm, self.client = ytm, client

    def scrobble(self, listen: Listen):
        if not listen.ytm_id:
            raise Rejected(f"no YouTube Music match for {listen.artist} - {listen.title}")
        self.ytm.add_history(self.client, listen.ytm_id)

    def now_playing(self, listen: Listen):
        pass


class Forwarder:
    """Keeps plays in the database outbox until every service the user linked has taken them."""

    def __init__(self, connect, services_for, ytm=None):
        """services_for(db, user) returns the user's services by name. ytm finds the YouTube Music video of a play
        for YouTube Music's history."""
        self.connect, self.services_for, self.ytm = connect, services_for, ytm
        self.wake = threading.Event()

    def enqueue(self, db, user: str, track_id: str, played: float):
        db.executemany("insert into outbox(user, service, track_id, played, next_try) values (?, ?, ?, ?, 0)",
                       [(user, name, track_id, played) for name in self.services_for(db, user)])
        self.wake.set()

    def now_playing(self, db, user: str, listen: Listen):
        services = self.services_for(db, user)

        def send():
            for name, service in services.items():
                try:
                    service.now_playing(listen)
                except Exception as e:
                    log.info("%s now playing failed: %s", name, e)
        threading.Thread(target=send, daemon=True).start()

    def start(self):
        worker.start("forwarding", self.flush, 60, self.wake)

    def flush(self):
        db = self.connect()
        try:
            due = db.execute("select * from outbox where next_try <= ? order by played", (time.time(),)).fetchall()
            for entry in due:
                self.send(db, entry)
        finally:
            db.close()

    def send(self, db, entry):
        service = self.services_for(db, entry["user"]).get(entry["service"])
        listen = self.listen(db, entry)
        try:
            if service is None:
                raise Rejected(f"{entry['user']} unlinked {entry['service']}")
            if listen is None:
                raise Rejected(f"track {entry['track_id']} is gone")
            service.scrobble(listen)
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
        ytm_id = video_of(db, r, self.ytm) if entry["service"] == "ytmusic" and self.ytm else None
        return listen_for(r, entry["played"], ytm_id)


def listen_for(r, played: float, ytm_id: str | None = None) -> Listen:
    artist, album = outside_names(r)
    return Listen(r["id"], artist, r["title"], album, r["duration"], played, ytm_id or r["ytm_id"])


def user_services(lastfm: LastFm | None, ytm, user) -> dict:
    """The services a user linked, by name. lastfm: oasis's Last.fm app, which a user's session needs."""
    found = {}
    if user is None:
        return found
    if signs_in(lastfm) and user["lastfm_session"]:
        found["lastfm"] = LastFm(lastfm.api_key, lastfm.secret, user["lastfm_session"])
    if user["listenbrainz_token"]:
        found["listenbrainz"] = ListenBrainz(user["listenbrainz_token"])
    if ytm and user["ytm_auth"]:
        found["ytmusic"] = YouTubeMusicHistory(ytm, ytm.client(user["ytm_auth"]))
    return found
