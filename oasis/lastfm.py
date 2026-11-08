"""Last.fm's API: signed calls with oasis's API key and secret (an app registered by the admin), signing a user in to
get their session key, the read-only methods recommendations and artist info use, and scrobbles for forward.py. oasis's own app is
oasis.toml's, else the one an admin connected on the dashboard, kept in the setting table."""
import hashlib
import json
import urllib.parse

from .remote import Rejected, check, get, post

LASTFM = "https://ws.audioscrobbler.com/2.0/"


def answer(status: int, body: bytes) -> dict:
    result = json.loads(body or b"{}")
    if "error" in result:
        # 11 and 16 are temporary outages, 29 is the rate limit; everything else is a real refusal.
        if result["error"] in (11, 16, 29):
            raise OSError(f"Last.fm: {result.get('message')}")
        raise Rejected(f"Last.fm: {result.get('message')}")
    check(status, body, "Last.fm")
    return result


class LastFm:
    def __init__(self, api_key: str, secret: str, session: str | None):
        self.api_key, self.secret, self.session = api_key, secret, session

    def call(self, params: dict) -> dict:
        params = {**params, "api_key": self.api_key}
        signature = "".join(k + str(params[k]) for k in sorted(params)) + self.secret
        params |= {"api_sig": hashlib.md5(signature.encode()).hexdigest(), "format": "json"}
        return answer(*post(LASTFM, urllib.parse.urlencode(params).encode(), {"Content-Type": "application/x-www-form-urlencoded"}))

    def auth_url(self, callback: str) -> str:
        """Last.fm's page where the user allows oasis; it comes back to callback with a token."""
        return f"https://www.last.fm/api/auth/?api_key={self.api_key}&cb={urllib.parse.quote(callback, safe='')}"

    def sign_in(self, token: str) -> tuple[str, str]:
        """Trades the token from the callback for a session key, which never expires, and the user name."""
        session = self.call({"method": "auth.getSession", "token": token})["session"]
        return session["key"], session["name"]

    def read(self, params: dict) -> dict:
        """Read-only methods need only the API key."""
        query = urllib.parse.urlencode({**params, "api_key": self.api_key, "format": "json"})
        return answer(*get(f"{LASTFM}?{query}"))

    def similar(self, artist: str, title: str, limit: int) -> list[tuple[str, str]]:
        tracks = self.read({"method": "track.getSimilar", "artist": artist, "track": title, "limit": limit,
                            "autocorrect": 1})["similartracks"]["track"]
        return [(t["artist"]["name"], t["name"]) for t in tracks]

    def user_tracks(self, user: str, loved: bool, period: str) -> list[tuple[str, str]]:
        """A Last.fm user's loved tracks, or top tracks over period, as (artist, title)."""
        if loved:
            tracks = self.read({"method": "user.getLovedTracks", "user": user, "limit": 100})["lovedtracks"]["track"]
        else:
            tracks = self.read({"method": "user.getTopTracks", "user": user, "period": period, "limit": 100})["toptracks"]["track"]
        return [(t["artist"]["name"], t["name"]) for t in tracks]

    def artist_info(self, name: str) -> dict:
        return self.read({"method": "artist.getInfo", "artist": name, "autocorrect": 1})["artist"]

    def track_params(self, listen) -> dict:
        params = {"artist": listen.artist, "track": listen.title}
        if listen.album:
            params["album"] = listen.album
        if listen.duration:
            params["duration"] = listen.duration
        return params

    def scrobble(self, listen):
        self.call({"method": "track.scrobble", "sk": self.session, "timestamp": int(listen.played), **self.track_params(listen)})

    def now_playing(self, listen):
        self.call({"method": "track.updateNowPlaying", "sk": self.session, **self.track_params(listen)})


def similar_artists(info: dict) -> list[str]:
    """The names of the artists Last.fm calls similar, from artist_info, most similar first."""
    return [a["name"] for a in info.get("similar", {}).get("artist", [])]


def bio(info: dict) -> str | None:
    """The artist's summary from artist_info, without the "Read more on Last.fm" link Last.fm ends it with."""
    return info.get("bio", {}).get("summary", "").split("<a href")[0].strip() or None


def app(db, key: str | None, secret: str | None) -> LastFm | None:
    """oasis's own Last.fm client, without a user's session: key and secret (oasis.toml's), else the ones an admin
    connected on the dashboard, else none."""
    if not key:
        settings = dict(db.execute("select key, value from setting where key in ('lastfm_key', 'lastfm_secret')").fetchall())
        key, secret = settings.get("lastfm_key"), settings.get("lastfm_secret")
    return LastFm(key, secret or "", None) if key else None


def connect_app(db, key: str, secret: str) -> LastFm:
    """Checks a Last.fm API key and secret with a signed call, then keeps them for app."""
    client = LastFm(key, secret, None)
    client.call({"method": "auth.getToken"})
    db.executemany("insert or replace into setting values (?, ?)", [("lastfm_key", key), ("lastfm_secret", secret)])
    return client


def signs_in(app: LastFm | None) -> bool:
    """Whether oasis's app can sign users in, which takes its secret: oasis.toml may give only a key, enough to read."""
    return bool(app and app.secret)
