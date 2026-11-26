"""Forwards each user's plays and likes (Subsonic stars) to the services they use, through the outbox, retrying until
each service takes them, each on its own. Plays go to Last.fm, ListenBrainz, YouTube Music's history, SoundCloud's
listening history and the other servers each user added (servers.py). A starred song is loved on Last.fm and the
servers that speak its API, loved on ListenBrainz and the servers like it when MusicBrainz knows the recording, liked on
YouTube Music and SoundCloud, and made a favourite on Tidal and Qobuz; a starred album is saved on YouTube Music, Tidal
and Qobuz, and a starred artist on Tidal and Qobuz. A service that doesn't have it gets nothing. An unstar takes back
what the star did, on the same service's song, album or artist (the liked table)."""
import json
import logging
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

from . import musicbrainz, tasks
from .lastfm import LastFm, elsewhere, signs_in
from .remote import Rejected, check, post, send
from .sources import KINDS
from .tracks import IDS, album_name_of, artist_name_of, mbid_of, outside_names
from .youtube import video_of
from .ytm import WATCH

log = logging.getLogger(__name__)

LISTENBRAINZ = "https://api.listenbrainz.org/1/"
JSPF_PLAYLIST = "https://musicbrainz.org/doc/jspf#playlist"  # the key of ListenBrainz's own fields in a JSPF playlist
MADE_FOR = 100  # playlists made for a user read at most: about two weeks of daily ones, and the weekly and yearly ones
MAX_ATTEMPTS = 20
JOB = "forwarding"


@dataclass
class Listen:
    track_id: str
    artist: str
    title: str
    album: str | None
    duration: int | None
    played: float
    ytm_id: str | None


@dataclass
class Item:
    """A song, album or artist a user starred, as services find it."""
    kind: str             # song, album or artist
    id: str               # oasis's
    artist: str
    title: str | None     # the song's or album's
    mbid: str | None      # a song's MusicBrainz recording
    track: object = None  # a song's track row


def kind_of(id: str) -> str:
    """What a starred id is: an album, an artist, else a song."""
    if id.startswith(IDS["album"]):
        return "album"
    return "artist" if id.startswith(IDS["artist"]) else "song"


def item_of(db, id: str) -> Item | None:
    """The starred song, album or artist, by the names the library or MusicBrainz gives it; None when neither knows it."""
    kind, mbid = kind_of(id), mbid_of(id)
    if kind == "song":
        r = db.execute("select * from track where id = ?", (id,)).fetchone()
        if r is None:
            return None
        return Item("song", id, outside_names(r)[0], r["title"], r["mb_recording"], r)
    if kind == "album":
        if found := album_name_of(db, id):
            return Item("album", id, found["name"], found["title"], None)
        if mbid:
            group = musicbrainz.release_group(mbid, db)
            credit = group.get("artist-credit") or []
            return Item("album", id, credit[0]["name"] if credit else "", group["title"], None)
        return None
    name = artist_name_of(db, id) or (musicbrainz.artist(mbid, db)["name"] if mbid else None)
    return Item("artist", id, name, None, None) if name else None


class ListenBrainz:
    """ListenBrainz's API, or the same API of a server of the user's own (Maloja, Koito...) at api, its root."""

    def __init__(self, token: str, api: str = LISTENBRAINZ):
        self.token, self.api = token, api

    def headers(self) -> dict:
        return {"Authorization": "Token " + self.token, "User-Agent": "oasis/0.1"}

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
        check(*post(self.api + "submit-listens", body, self.headers() | {"Content-Type": "application/json"}), "ListenBrainz")

    likes = ("song",)

    def scrobble(self, listen: Listen):
        self.submit("single", listen)

    def find_liked(self, db, item: Item) -> str | None:
        """Feedback is given to a MusicBrainz recording."""
        return item.mbid

    def like(self, kind: str, recording: str, on: bool):
        """Loves the recording, or takes the love back (score 0)."""
        body = json.dumps({"recording_mbid": recording, "score": 1 if on else 0}).encode()
        check(*post(self.api + "feedback/recording-feedback", body, self.headers() | {"Content-Type": "application/json"}), "ListenBrainz")

    def read(self, path: str, **query) -> dict:
        url = self.api + path + ("?" + urllib.parse.urlencode(query) if query else "")
        status, body = send(urllib.request.Request(url, headers=self.headers()))
        check(status, body, "ListenBrainz")
        return json.loads(body)

    def checked(self) -> dict | None:
        """The server's answer to validate-token: valid, and user_name when it is; None when the server has no such
        call. An exception when it doesn't answer."""
        status, body = send(urllib.request.Request(self.api + "validate-token", headers=self.headers()))
        if status == 429 or status >= 500:
            check(status, body, "ListenBrainz")
        try:
            found = json.loads(body)
        except ValueError:
            return None
        return found if isinstance(found, dict) and "valid" in found else None

    def valid(self) -> bool:
        """Whether the server knows the token: by validate-token, else by a test of what oasis sends (a song playing
        now, which no server counts as a play). An exception when it doesn't answer."""
        found = self.checked()
        if found is not None:
            return bool(found["valid"])
        try:
            self.now_playing(Listen("", "oasis", "Linking oasis", None, None, time.time(), None))
        except Rejected:
            return False
        return True

    def now_playing(self, listen: Listen):
        self.submit("playing_now", listen)

    def made_for(self) -> list[dict]:
        """The newest of each kind of playlist ListenBrainz makes for the user (Weekly Jams, Weekly Exploration, Daily
        Jams...) that hasn't expired, with their tracks, as JSPF playlists."""
        name = (self.checked() or {}).get("user_name")
        if not name:
            raise Rejected("ListenBrainz doesn't know this token")
        newest = {}
        for entry in self.read(f"user/{urllib.parse.quote(name, safe='')}/playlists/createdfor", count=MADE_FOR)["playlists"]:
            playlist = entry["playlist"]
            more = (playlist.get("extension") or {}).get(JSPF_PLAYLIST) or {}
            extra = more.get("additional_metadata") or {}
            kind = (extra.get("algorithm_metadata") or {}).get("source_patch") or playlist["identifier"]
            if extra.get("expires_at") and expired(extra["expires_at"]):
                continue
            if kind not in newest or playlist.get("date", "") > newest[kind].get("date", ""):
                newest[kind] = playlist
        return [self.read("playlist/" + p["identifier"].rstrip("/").rsplit("/", 1)[-1])["playlist"] | {"kind": kind}
                for kind, p in newest.items()]


def expired(when: str) -> bool:
    """Whether an ISO time ListenBrainz gives has passed; one without a zone is UTC."""
    moment = datetime.fromisoformat(when)
    return (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)) < datetime.now(timezone.utc)


class YouTubeMusicAccount:
    """The user's YouTube Music account: plays go to its history, which feeds its recommendations, also for plays of
    local copies; a starred song gets its like, and a starred album joins its library."""
    likes = ("song", "album")

    def __init__(self, ytm, client):
        self.ytm, self.client = ytm, client

    def scrobble(self, listen: Listen):
        if not listen.ytm_id:
            raise Rejected(f"no YouTube Music match for {listen.artist} - {listen.title}")
        self.ytm.add_history(self.client, listen.ytm_id)

    def now_playing(self, listen: Listen):
        pass

    def find_liked(self, db, item: Item) -> str | None:
        """A song's video, or the playlist of an album's songs, which is what YouTube Music saves to the library."""
        if item.kind == "song":
            return video_of(db, item.track, self.ytm)
        page = db.execute("select ytm_album from track where album_id = ? and ytm_album is not null union all "
                          "select browse_id from ytm_release where album_id = ? limit 1", (item.id, item.id)).fetchone()
        return self.ytm.album_playlist(item.artist, item.title, page and page[0])

    def like(self, kind: str, target: str, on: bool):
        self.ytm.rate(self.client, kind, target, on)


class Favourites:
    """The user's favourites on one of their streaming services (sources.Sources.liking): Tidal's and Qobuz's songs,
    albums and artists, SoundCloud's liked tracks. A song is found by its id there when it is one of that service's
    tracks or the service's answer to a search is kept (kept: recommend.Recommender.kept), else by a search."""

    def __init__(self, kind, start, kept=lambda question: None):
        """kind: the service's client class (sources.KINDS); start() gives the user's client of it, None when it fails."""
        self.likes, self.start, self.kept = kind.likes, start, kept

    def client(self):
        client = self.start()
        if client is None:
            raise OSError("the service didn't start")
        return client

    def find_liked(self, db, item: Item) -> str | None:
        client = self.client()
        if item.kind == "album":
            return client.find_album(item.artist, item.title)
        if item.kind == "artist":
            return client.find_artist(item.artist)
        if item.id.startswith(client.prefix):  # one of this service's songs
            return item.id.removeprefix(client.prefix)
        if kept := self.kept(("find", client.name, getattr(client, "variant", ""), item.artist, item.title)):
            answer, _ = kept[0]  # (client, song), or None when it hasn't the song
            return answer[1]["id"] if answer else None
        found = client.find(item.artist, item.title)
        return found["id"] if found else None

    def like(self, kind: str, id: str, on: bool):
        self.client().favourite(kind, id, on)


class SoundCloudAccount(Favourites):
    """The user's SoundCloud account: also its listening history, for each play of a song SoundCloud has."""

    def scrobble(self, listen: Listen):
        found = self.find_liked(None, Item("song", listen.track_id, listen.artist, listen.title, None))
        if found is None:
            raise Rejected(f"SoundCloud has no {listen.artist} - {listen.title}")
        self.client().add_history(found)

    def now_playing(self, listen: Listen):
        pass


class Forwarder:
    """Keeps plays and likes in the database outbox until every service the user linked has taken them."""

    def __init__(self, connect, services_for, ytm=None):
        """services_for(db, user) returns the user's services by name. ytm finds the YouTube Music video of a play
        for YouTube Music's history."""
        self.connect, self.services_for, self.ytm = connect, services_for, ytm

    def enqueue(self, db, user: str, track_id: str, played: float):
        db.executemany("insert into outbox(user, service, track_id, played, next_try) values (?, ?, ?, ?, 0)",
                       [(user, name, track_id, played) for name, service in self.services_for(db, user).items()
                        if hasattr(service, "scrobble")])
        tasks.wake(db, JOB)

    def like(self, db, user: str, ids: list[str], on: bool):
        """Sends the user's star (on) or unstar of songs, albums and artists to each of their services that keeps such
        likes. A like or unlike still waiting is replaced: only the newest says what the user wants."""
        services, now = self.services_for(db, user), time.time()
        for id in ids:
            db.execute("delete from outbox where user = ? and track_id = ? and action != 'play'", (user, id))
            db.executemany("insert into outbox(user, service, track_id, action, played, next_try) values (?, ?, ?, ?, ?, 0)",
                           [(user, name, id, "like" if on else "unlike", now) for name, service in services.items()
                            if kind_of(id) in getattr(service, "likes", ())])
        tasks.wake(db, JOB)

    def now_playing(self, db, user: str, listen: Listen):
        services = {name: s for name, s in self.services_for(db, user).items() if hasattr(s, "now_playing")}

        def send():
            for name, service in services.items():
                try:
                    service.now_playing(listen)
                except Exception as e:
                    log.info("%s now playing failed: %s", name, e)
        threading.Thread(target=send, daemon=True).start()

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
        try:
            if service is None:
                raise Rejected(f"{entry['user']} unlinked {entry['service']}")
            if entry["action"] == "play":
                listen = self.listen(db, entry)
                if listen is None:
                    raise Rejected(f"track {entry['track_id']} is gone")
                service.scrobble(listen)
            else:
                self.send_like(db, entry, service)
        except Rejected as e:
            log.warning("dropping %s: %s", entry["action"], e)
        except Exception as e:
            if entry["attempts"] + 1 < MAX_ATTEMPTS:
                delay = min(3600, 30 * 2 ** entry["attempts"])
                db.execute("update outbox set attempts = attempts + 1, next_try = ? where id = ?", (time.time() + delay, entry["id"]))
                log.info("%s will retry in %d s: %s", entry["service"], delay, e)
                return
            log.warning("giving up on %s after %d attempts: %s", entry["action"], MAX_ATTEMPTS, e)
        db.execute("delete from outbox where id = ?", (entry["id"],))

    def send_like(self, db, entry, service):
        """Likes what the user starred on the service, remembering the service's own id of it; an unlike takes back
        that like. A service that doesn't have it gets nothing, and neither does an unlike of what oasis never liked."""
        key = (entry["user"], entry["service"], entry["track_id"])
        kind = kind_of(entry["track_id"])
        if entry["action"] == "unlike":
            row = db.execute("select key from liked where user = ? and service = ? and id = ?", key).fetchone()
            if row:
                try:
                    service.like(kind, row["key"], False)
                except Rejected as e:  # taken back on the service already: nothing left to undo
                    log.info("%s unlike of %s needs nothing: %s", entry["service"], entry["track_id"], e)
                db.execute("delete from liked where user = ? and service = ? and id = ?", key)
            return
        item = item_of(db, entry["track_id"])
        if item is None:
            raise Rejected(f"{entry['track_id']} is gone")
        found = service.find_liked(db, item)
        if found is None:
            log.info("%s has no %s %s - %s", entry["service"], kind, item.artist, item.title)
            return
        service.like(kind, found, True)
        db.execute("insert or replace into liked values (?, ?, ?, ?)", (*key, found))

    def listen(self, db, entry) -> Listen | None:
        r = db.execute("select * from track where id = ?", (entry["track_id"],)).fetchone()
        if r is None:
            return None
        ytm_id = video_of(db, r, self.ytm) if entry["service"] == "ytmusic" and self.ytm else None
        return listen_for(r, entry["played"], ytm_id)


def listen_for(r, played: float, ytm_id: str | None = None) -> Listen:
    artist, album = outside_names(r)
    return Listen(r["id"], artist, r["title"], album, r["duration"], played, ytm_id or r["ytm_id"])


def user_services(lastfm: LastFm | None, ytm, user, usable, servers: list, liking: dict, kept) -> dict:
    """The services a user linked and can use (usable: sources.Sources.usable), by name, the other servers they send
    plays to (servers.sending), each by 'server:<its id>', and the streaming services their likes go to (liking:
    sources.Sources.liking; kept: what Favourites takes). lastfm: oasis's Last.fm app, which a user's session needs."""
    found = {}
    if user is None:
        return found
    if signs_in(lastfm) and user["lastfm_session"] and "lastfm" in usable:
        found["lastfm"] = LastFm(lastfm.api_key, lastfm.secret, user["lastfm_session"])
    if user["listenbrainz_token"] and "listenbrainz" in usable:
        found["listenbrainz"] = ListenBrainz(user["listenbrainz_token"])
    if ytm and user["ytm_auth"] and "account" in usable.get("ytm", ()):
        found["ytmusic"] = YouTubeMusicAccount(ytm, ytm.client(user["ytm_auth"]))
    for r in servers:
        account = json.loads(r["account"])
        if r["protocol"] == "lastfm":
            found[f"server:{r['id']}"] = elsewhere(r["url"], account["auth"], account["session"])
        else:
            found[f"server:{r['id']}"] = ListenBrainz(account["token"], r["url"])
    for name, start in liking.items():
        found[name] = (SoundCloudAccount if name == "soundcloud" else Favourites)(KINDS[name], start, kept)
    return found
