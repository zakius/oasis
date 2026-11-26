"""Streaming services besides YouTube Music, asked for songs YouTube Music doesn't have: Qobuz and Tidal (lossless, with
the user's own account, linked on the dashboard), Bandcamp and SoundCloud (no account). Each user turns on the ones they
use, among those an admin allows (Sources); the same goes for YouTube Music, Last.fm and ListenBrainz. Each finds
one song by artist and title, and downloads its audio, which oasis keeps in a cache and plays from there. Their songs
are streaming tracks like YouTube Music's: source is the service's name, ref '<service>:<its id>'. Deezer and Apple
Music give 30 s previews of songs none has (PREVIEWS), which stay songs nothing can play (missing.py)."""
import base64
import hashlib
import json
import logging
import re
import threading
import time
from collections import defaultdict
from pathlib import Path

import urllib3

from .text import fold
from .tracks import album_id, make_id, name_id, remember

log = logging.getLogger(__name__)

BROWSER = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"
HTTP = urllib3.PoolManager(maxsize=4, retries=urllib3.Retry(connect=1, read=1, status=0, other=0, redirect=5),
                           timeout=urllib3.Timeout(connect=10, read=30), headers={"User-Agent": BROWSER})
PREVIEW = 31  # seconds: a "song" this short is a service's preview clip, not the song
ALLOW = "allow."  # the setting table's keys for what an admin offers users: allow.<service> and allow.<service>.<capability>, "1" or "0"
YDL = {"quiet": True, "no_warnings": True, "noprogress": True}  # yt-dlp's options for every service it reads


def full_length(seconds: float | None) -> bool:
    """Whether a service's song is the whole song, not a preview clip."""
    return bool(seconds) and seconds > PREVIEW


def request(method: str, url: str, **kwargs) -> urllib3.BaseHTTPResponse:
    """HTTP's request, failing with OSError like other network errors. Its message leaves out the query, which holds a
    password's hash when Qobuz signs in."""
    try:
        return HTTP.request(method, url, **kwargs)
    except urllib3.exceptions.HTTPError as e:
        raise OSError(f"{url.split('?')[0]} couldn't be reached ({type(e).__name__})") from None


def get_json(url: str, **kwargs) -> dict:
    response = request("GET", url, **kwargs)
    if response.status != 200:
        raise OSError(f"HTTP {response.status} from {url.split('?')[0]}: {response.data[:200]!r}")
    return response.json()


def save(url: str, path: Path, headers: dict | None = None):
    """Downloads url into path, through a .part file so a broken download never looks complete."""
    part = path.with_name(path.name + ".part")
    response = request("GET", url, headers=headers, preload_content=False, timeout=urllib3.Timeout(connect=10, read=60))
    try:
        if response.status != 200:
            raise OSError(f"HTTP {response.status} downloading {path.name}")
        with part.open("wb") as out:
            for data in response.stream(1 << 16):
                out.write(data)
    finally:
        response.release_conn()
    part.rename(path)


class Bandcamp:
    """Bandcamp's full-length 128 kbit/s MP3 streams, which most of its tracks have, through yt-dlp. No account.
    Bandcamp answers Chrome's connection with a bot check but lets Firefox's through, so yt-dlp copies Firefox's
    (impersonation, which needs curl_cffi)."""
    name, title, prefix, suffix, content_type, bitrate = "bandcamp", "Bandcamp", "bc-", "mp3", "audio/mpeg", 128
    SEARCH = "https://bandcamp.com/api/bcsearch_public_api/1/autocomplete_elastic"
    PAGES = 3
    FORMAT = "mp3-128"

    def search(self, query: str) -> list[dict]:
        """yt-dlp can't search Bandcamp, so this is the search box's own."""
        body = json.dumps({"search_text": query, "search_filter": "t", "full_page": False, "fan_id": None}).encode()
        response = request("POST", self.SEARCH, body=body, headers={"Content-Type": "application/json"})
        if response.status != 200:
            raise OSError(f"HTTP {response.status} from Bandcamp's search")
        return [r for r in response.json().get("auto", {}).get("results", []) if r.get("type") == "t" and r.get("item_url_path")]

    @staticmethod
    def options() -> dict:
        from yt_dlp.networking.impersonate import ImpersonateTarget
        return YDL | {"impersonate": ImpersonateTarget.from_str("firefox")}

    def info(self, url: str) -> dict:
        import yt_dlp
        with yt_dlp.YoutubeDL(self.options()) as ydl:
            return ydl.extract_info(url, download=False)

    def find(self, artist: str, title: str) -> dict | None:
        """The artist's own full-length track of that title; at most PAGES track pages are read."""
        for r in [r for r in self.search(f"{artist} {title}") if fold(r.get("name") or "") == fold(title)][:self.PAGES]:
            info = self.info(r["item_url_path"])
            if fold(artist) not in (fold(r.get("band_name") or ""), fold(info.get("artist") or "")):
                continue
            if not any(f.get("format_id") == self.FORMAT for f in info.get("formats") or []) or not full_length(info.get("duration")):
                continue
            return {"id": str(r["id"]), "key": r["item_url_path"], "title": info.get("track") or r["name"],
                    "artist": info.get("artist") or r.get("band_name") or artist, "album": info.get("album") or r.get("album_name") or r["name"],
                    "year": info.get("release_year"), "duration": round(info["duration"]), "track_no": info.get("track_number"),
                    "cover": info.get("thumbnail") or r.get("img")}
        return None

    def download(self, key: str, path: Path) -> Path:
        import yt_dlp
        with yt_dlp.YoutubeDL(self.options() | {"format": self.FORMAT, "outtmpl": str(path) + ".%(ext)s"}) as ydl:
            return Path(ydl.prepare_filename(ydl.extract_info(key, download=True)))


class SoundCloud:
    """SoundCloud through yt-dlp: full-length AAC streams of public uploads; tracks only SoundCloud Go+ plays in full
    give 30 s previews, which the length check leaves out. Only the artist's own uploads count. No account."""
    name, title, prefix, suffix, content_type, bitrate = "soundcloud", "SoundCloud", "sc-", "m4a", "audio/mp4", 160
    def find(self, artist: str, title: str) -> dict | None:
        """The search's flat listing has all that is needed, in about a second; reading each track would take four."""
        import yt_dlp
        with yt_dlp.YoutubeDL(YDL | {"extract_flat": "in_playlist"}) as ydl:
            entries = ydl.extract_info(f"scsearch5:{artist} {title}", download=False).get("entries") or []
        titles = {fold(title), fold(f"{artist} - {title}")}
        for e in entries:
            if e and fold(e.get("uploader") or "") == fold(artist) and fold(e.get("title") or "") in titles \
                    and full_length(e.get("duration")) and e.get("webpage_url"):
                art = (e.get("thumbnails") or [{}])[-1].get("url")
                return {"id": str(e["id"]), "key": e["webpage_url"], "title": title, "artist": e["uploader"], "album": title,
                        "year": time.gmtime(e["timestamp"]).tm_year if e.get("timestamp") else None,
                        "duration": round(e["duration"]), "cover": art and art.replace("-original.", "-t500x500.")}
        return None

    def download(self, key: str, path: Path) -> Path:
        import yt_dlp
        options = {**YDL, "format": "bestaudio", "outtmpl": str(path) + ".%(ext)s"}
        with yt_dlp.YoutubeDL(options) as ydl:
            return Path(ydl.prepare_filename(ydl.extract_info(key, download=True)))


class Qobuz:
    """Qobuz with a subscriber's account: FLAC at CD quality. Its web player's app id and secrets are read from the
    player's own script when the account is linked, as no published app has them."""
    name, title, prefix, suffix, content_type, bitrate = "qobuz", "Qobuz", "qb-", "flac", "audio/flac", 1411
    API = "https://www.qobuz.com/api.json/0.2/"
    PLAYER = "https://play.qobuz.com"
    FORMAT = 6  # FLAC 16-bit 44.1 kHz; 27 would be the highest resolution there is

    def __init__(self, account: dict):
        """account: app_id, secret and token (the user's), as link() made them."""
        self.account = account

    @classmethod
    def player_keys(cls, bundle: str) -> tuple[str, list[str]]:
        """The web player's app id and its candidate secrets, from its bundle.js: each secret is a seed joined with its
        timezone's info and extras, base64, less the last 44 characters."""
        app = re.search(r'production:\{api:\{appId:"(\d{9})",appSecret:"\w{32}"', bundle)
        if not app:
            raise LookupError("Qobuz's player has no app id where it used to")
        secrets = []
        for seed, zone in re.findall(r'[a-z]\.initialSeed\("([\w=]+)",window\.utimezone\.([a-z]+)\)', bundle):
            parts = re.search(r'name:"\w+/' + zone.capitalize() + r'",info:"([\w=]+)",extras:"([\w=]+)"', bundle)
            if parts:
                secrets.append(base64.standard_b64decode((seed + parts[1] + parts[2])[:-44]).decode())
        if not secrets:
            raise LookupError("Qobuz's player has no secrets where it used to")
        return app[1], secrets

    @classmethod
    def fetch_player_keys(cls) -> tuple[str, list[str]]:
        page = request("GET", cls.PLAYER + "/login").data.decode()
        bundle = re.search(r'<script src="(/resources/[\w.-]+/bundle\.js)"', page)
        if not bundle:
            raise LookupError("Qobuz's player page has no bundle.js")
        return cls.player_keys(request("GET", cls.PLAYER + bundle[1]).data.decode())

    @staticmethod
    def signature(track_id: str, format_id: int, at: int, secret: str) -> str:
        return hashlib.md5(f"trackgetFileUrlformat_id{format_id}intentstreamtrack_id{track_id}{at}{secret}".encode()).hexdigest()

    def call(self, method: str, **params) -> dict:
        headers = {"X-App-Id": self.account["app_id"], "X-User-Auth-Token": self.account.get("token", "")}
        return get_json(self.API + method, fields={**params, "app_id": self.account["app_id"]}, headers=headers)

    def file_url(self, track_id: str, secret: str | None = None) -> dict:
        at = int(time.time())
        return self.call("track/getFileUrl", track_id=track_id, format_id=self.FORMAT, intent="stream", request_ts=at,
                         request_sig=self.signature(track_id, self.FORMAT, at, secret or self.account["secret"]))

    @classmethod
    def link(cls, email: str, password: str) -> dict:
        """Signs in and finds the secret that works: the account to keep. ValueError saying why when it can't."""
        app_id, secrets = cls.fetch_player_keys()
        response = request("GET", cls.API + "user/login", fields={"email": email, "password": hashlib.md5(password.encode()).hexdigest(),
                                                                         "app_id": app_id})
        if response.status != 200:
            raise ValueError("Qobuz didn't accept that email and password." if response.status in (400, 401) else f"Qobuz answered HTTP {response.status}.")
        found = response.json()
        user = found.get("user") or {}
        account = {"app_id": app_id, "token": found["user_auth_token"], "name": user.get("display_name") or user.get("login") or email}
        if not (user.get("credential") or {}).get("parameters"):
            raise ValueError("That Qobuz account has no subscription that streams.")
        client = cls(account)
        probe = client.call("track/search", query="a", limit=1)["tracks"]["items"]
        if not probe:
            raise ValueError("Qobuz's search found nothing to try its player secrets on; try again later.")
        for secret in secrets:
            try:
                client.file_url(str(probe[0]["id"]), secret)
            except OSError:
                continue
            return account | {"secret": secret}
        raise ValueError("None of Qobuz's player secrets work any more; oasis needs an update.")

    def find(self, artist: str, title: str) -> dict | None:
        for t in self.call("track/search", query=f"{artist} {title}", limit=10)["tracks"]["items"]:
            album = t.get("album") or {}
            names = {fold((t.get("performer") or {}).get("name") or ""), fold((album.get("artist") or {}).get("name") or "")}
            if t.get("streamable") and fold(t.get("title") or "") == fold(title) and fold(artist) in names and full_length(t.get("duration")):
                year = (album.get("release_date_original") or "")[:4]
                return {"id": str(t["id"]), "key": str(t["id"]), "title": t["title"], "artist": (t.get("performer") or {}).get("name") or artist,
                        "album": album.get("title") or t["title"], "year": int(year) if year.isdigit() else None, "duration": t["duration"],
                        "track_no": t.get("track_number"), "disc_no": t.get("media_number"), "cover": (album.get("image") or {}).get("large")}
        return None

    def download(self, key: str, path: Path) -> Path:
        found = self.file_url(key)
        if found.get("sample") or not found.get("url"):
            raise LookupError("Qobuz only gives a preview of this track to this account")
        target = path.with_suffix(".flac")
        save(found["url"], target)
        return target


class Tidal:
    """Tidal with a subscriber's account, through tidalapi: FLAC at CD quality. The account is linked like a TV's, by
    a code entered on Tidal's site (link_start)."""
    name, title, prefix, suffix, content_type, bitrate = "tidal", "Tidal", "td-", "flac", "audio/flac", 1411

    def __init__(self, account: dict):
        """account: the OAuth tokens saved() gives."""
        import tidalapi
        self.account = account
        self.session = tidalapi.Session()
        self.session.audio_quality = tidalapi.Quality.high_lossless
        expiry = account.get("expiry")
        from datetime import datetime
        if not self.session.load_oauth_session(account["token_type"], account["access_token"], account.get("refresh_token"),
                                               datetime.fromisoformat(expiry) if expiry else None):
            raise ValueError("Tidal no longer accepts this sign-in: link it again")

    def current(self) -> dict:
        """The account with the tokens the session has now: tidalapi gets a new access token when one expires."""
        return {**self.account, **{k: v for k, v in Tidal.saved(self.session).items() if k != "name"}}

    @staticmethod
    def saved(session) -> dict:
        user = getattr(session, "user", None)
        return {"token_type": session.token_type, "access_token": session.access_token, "refresh_token": session.refresh_token,
                "expiry": session.expiry_time.isoformat() if session.expiry_time else None,
                "name": getattr(user, "username", None) or getattr(user, "email", None) or "Tidal"}

    @staticmethod
    def link_start(done):
        """Starts a device sign-in: the page to open and the code to enter there, and when it is due. done(account) is
        called from a thread once the user has allowed it, done(None) when it expired."""
        import tidalapi
        session = tidalapi.Session()
        login, future = session.login_oauth()

        def finished(f):
            try:
                f.result()
                done(Tidal.saved(session))
            except Exception as e:
                log.info("Tidal sign-in didn't finish: %s", e)
                done(None)
        future.add_done_callback(finished)
        uri = login.verification_uri_complete
        return {"url": uri if uri.startswith("http") else "https://" + uri, "code": login.user_code, "until": time.time() + login.expires_in}

    def find(self, artist: str, title: str) -> dict | None:
        import tidalapi
        for t in self.session.search(f"{artist} {title}", models=[tidalapi.Track], limit=10).get("tracks") or []:
            names = {fold(a.name or "") for a in t.artists or []}
            if fold(t.name or "") == fold(title) and fold(artist) in names and full_length(t.duration) and t.allow_streaming:
                album = t.album
                try:
                    cover = album.image(640) if album else None
                except Exception:
                    cover = None
                date = album and (album.release_date or getattr(album, "tidal_release_date", None))
                return {"id": str(t.id), "key": str(t.id), "title": t.name, "artist": t.artist.name if t.artist else artist,
                        "album": album.name if album else t.name, "year": date.year if date else None, "duration": t.duration,
                        "track_no": t.track_num, "disc_no": t.volume_num, "cover": cover}
        return None

    def download(self, key: str, path: Path) -> Path:
        manifest = self.session.track(int(key)).get_stream().get_stream_manifest()
        if manifest.is_encrypted:
            raise LookupError("Tidal sent this track encrypted")
        urls = manifest.get_urls()
        target = path.with_suffix(manifest.file_extension if manifest.file_extension.startswith(".") else "." + manifest.file_extension)
        if len(urls) == 1:
            save(urls[0], target)
            return target
        part = target.with_name(target.name + ".part")
        with part.open("wb") as out:  # a DASH manifest: its segments in order make the file
            for url in urls:
                response = request("GET", url)
                if response.status != 200:
                    raise OSError(f"HTTP {response.status} from a Tidal segment")
                out.write(response.data)
        part.rename(target)
        return target


class DeezerPreview:
    """Deezer's 30 s MP3 previews, from its public API. No account. Its links expire, so the clip's link is asked for
    again when it is first played."""
    name, title, suffix, content_type = "deezer", "Deezer", "mp3", "audio/mpeg"
    API = "https://api.deezer.com/"

    def find(self, artist: str, title: str) -> str | None:
        """The id of the artist's track of that title that has a preview."""
        found = get_json(self.API + "search/track", fields={"q": f"{artist} {title}", "limit": 10})
        if "error" in found:
            raise OSError(f"Deezer: {found['error']}")
        for t in found.get("data") or []:
            if fold(t.get("title") or "") == fold(title) and fold((t.get("artist") or {}).get("name") or "") == fold(artist) and t.get("preview"):
                return str(t["id"])
        return None

    def link(self, key: str) -> str | None:
        return get_json(self.API + "track/" + key).get("preview") or None


class ApplePreview:
    """Apple Music's 30 s AAC previews, from the iTunes Search API. No account."""
    name, title, suffix, content_type = "apple", "Apple Music", "m4a", "audio/mp4"
    API = "https://itunes.apple.com/"

    def find(self, artist: str, title: str) -> str | None:
        found = get_json(self.API + "search", fields={"term": f"{artist} {title}", "entity": "song", "limit": 10})
        for t in found.get("results") or []:
            if fold(t.get("trackName") or "") == fold(title) and fold(t.get("artistName") or "") == fold(artist) and t.get("previewUrl"):
                return str(t["trackId"])
        return None

    def link(self, key: str) -> str | None:
        found = get_json(self.API + "lookup", fields={"id": key}).get("results") or []
        return found[0].get("previewUrl") if found else None


KINDS = {kind.name: kind for kind in (Qobuz, Tidal, Bandcamp, SoundCloud)}  # also the order they are asked in
ACCOUNTS = ("qobuz", "tidal")  # the services that need a linked account; the others are only turned on
# Where songs nothing can play get a clip from, in the order they are asked; each user turns each on. Clips reach only
# apps that take songs nothing can play (extension unavailableSongs), so plain Subsonic apps never play one.
PREVIEWS = {kind.name: kind for kind in (DeezerPreview, ApplePreview)}
# Every service a user can turn on, with the capabilities of it an admin can allow or not: songs, full songs to find,
# search and stream; previews, clips of songs nothing can play; account, what a linked account adds (YouTube Music's
# mixes and history).
CAPABILITIES = {"ytm": ("songs", "account"), "qobuz": ("songs",), "tidal": ("songs",), "bandcamp": ("songs",), "soundcloud": ("songs",),
                "lastfm": (), "listenbrainz": (), "deezer": ("previews",), "apple": ("previews",)}
PREVIEW_SECONDS = 30


class Off(LookupError):
    """A song's service is one the user can't use now: not turned on, not linked, or not allowed on this server."""


def off(title: str) -> Off:
    return Off(f"This song plays from {title}, which you haven't turned on: see Services on the oasis dashboard.")


class Sources:
    """Each user's services (user_service) as an admin allows them (the setting table), the songs their streaming
    services find as tracks, and those songs' audio, downloaded into cache when first played. Cached audio plays for
    everyone; downloading it takes the requesting user's own service."""

    def __init__(self, cache: Path, ytm_allowed: bool = False):
        """ytm_allowed: whether YouTube Music is allowed till an admin decides (oasis.toml); the others are."""
        self.cache, self.ytm_allowed = cache, ytm_allowed
        self.clients: dict[tuple[str, str], tuple[str, object]] = {}  # by (user, service): (its account, its client)
        self.failed: dict[tuple[str, str], tuple[str, float]] = {}  # by (user, service): (the account it failed to start with, when)
        self.locks = defaultdict(threading.Lock)

    def offered(self, db) -> dict[str, set[str]]:
        """The services an admin offers users, each with the capabilities of it allowed (CAPABILITIES). Until an admin
        decides, all are, but YouTube Music as oasis.toml says."""
        settings = dict(db.execute("select key, value from setting where key like ?", (ALLOW + "%",)).fetchall())

        def on(key: str, default: bool = True) -> bool:
            value = settings.get(ALLOW + key)
            return value == "1" if value is not None else default
        return {name: {c for c in capabilities if on(f"{name}.{c}")} for name, capabilities in CAPABILITIES.items()
                if on(name, self.ytm_allowed if name == "ytm" else True)}

    def allowed(self, db, name: str, capability: str | None = None) -> bool:
        """Whether users may use the service, and that capability of it."""
        offered = self.offered(db)
        return name in offered and (capability is None or capability in offered[name])

    def allow(self, db, name: str, on: bool, capability: str | None = None):
        """Offers the service, or a capability of it, to users or takes it back. Their accounts stay."""
        key = ALLOW + name + (f".{capability}" if capability else "")
        db.execute("insert or replace into setting values (?, ?)", (key, "1" if on else "0"))

    def turned_on(self, db, user: str) -> dict[str, str | None]:
        """The services the user turned on, offered or not, each with its account (JSON) where it keeps one here."""
        return dict(db.execute("select service, account from user_service where user = ?", (user,)).fetchall())

    def usable(self, db, user: str) -> dict[str, set[str]]:
        """The services the user turned on that the server offers, each with the capabilities allowed. Those that need
        an account (ACCOUNTS, and ListenBrainz's token) count once it is linked."""
        offered, found = self.offered(db), {}
        for name, account, token in db.execute("select s.service, s.account, u.listenbrainz_token from user_service s "
                                               "join user u on u.name = s.user where s.user = ?", (user,)):
            if name in ACCOUNTS and not account or name == "listenbrainz" and not token:
                continue
            if name in offered:
                found[name] = offered[name]
        return found

    def uses(self, db, user: str, name: str, capability: str | None = None) -> bool:
        """Whether the user can use the service now, and that capability of it."""
        usable = self.usable(db, user)
        return name in usable and (capability is None or capability in usable[name])

    def turn_on(self, db, user: str, name: str):
        db.execute("insert or ignore into user_service(user, service) values (?, ?)", (user, name))

    def turn_off(self, db, user: str, name: str):
        """Turns the service off for the user and forgets its account."""
        db.execute("delete from user_service where user = ? and service = ?", (user, name))
        self.clients.pop((user, name), None)
        self.failed.pop((user, name), None)

    def link(self, db, user: str, name: str, account: dict):
        """Keeps the account of a service that needs one (ACCOUNTS), turning it on."""
        db.execute("insert into user_service(user, service, account) values (?, ?, ?) on conflict(user, service) do update "
                   "set account = excluded.account", (user, name, json.dumps(account)))
        self.clients.pop((user, name), None)
        self.failed.pop((user, name), None)

    def active(self, db, user: str) -> list:
        """The clients of the user's streaming services whose full songs the user can use, in the order they are asked;
        one that fails to start is left out."""
        usable, accounts, found = self.usable(db, user), self.turned_on(db, user), []
        for name, kind in KINDS.items():
            if "songs" not in usable.get(name, ()):
                continue
            value, key = accounts.get(name) or "", (user, name)
            kept = self.clients.get(key)
            if not kept or kept[0] != value:
                failed = self.failed.get(key)
                if failed and failed[0] == value and failed[1] > time.time() - RETRY_START:
                    continue
                try:
                    kept = (value, kind(json.loads(value)) if name in ACCOUNTS else kind())
                except Exception as e:
                    log.warning("%s is unavailable for %s: %s", kind.title, user, e)
                    self.failed[key] = (value, time.time())
                    continue
                self.clients[key] = kept
            if hasattr(kept[1], "current") and (now := json.dumps(kept[1].current())) != kept[0]:
                # a renewed sign-in, kept for the next start
                db.execute("update user_service set account = ? where user = ? and service = ?", (now, user, name))
                self.clients[key] = kept = (now, kept[1])
            found.append(kept[1])
        return found

    @staticmethod
    def track_row(client, song: dict) -> dict:
        artist = song["artist"]
        return {"id": client.prefix + song["id"], "source": client.name, "ref": f"{client.name}:{song['key']}", "library": 0,
                "title": song["title"], "artist": artist, "album_artist": artist, "album": song["album"],
                "artist_id": name_id(artist), "album_id": album_id(artist, song["album"]), "year": song.get("year"),
                "duration": song["duration"], "track_no": song.get("track_no"), "disc_no": song.get("disc_no"),
                "bitrate": client.bitrate, "suffix": client.suffix, "content_type": client.content_type, "cover": song.get("cover"),
                "created": time.time()}

    def remember(self, db, client, song: dict) -> str:
        """Keeps a service's song as a streaming track and returns its id (see tracks.remember)."""
        return remember(db, self.track_row(client, song))

    def cached(self, stem: str) -> Path | None:
        return next((f for f in self.cache.glob(stem + ".*") if f.is_file()), None)

    def kept(self, stem: str, download) -> Path:
        """The cached file named stem, else download(path to write, without its suffix), which returns the file it
        wrote. Downloads go to PARTIAL and move into the cache once complete, so a broken one never looks cached."""
        if path := self.cached(stem):
            return path
        partial = self.cache / PARTIAL
        partial.mkdir(parents=True, exist_ok=True)
        for left in partial.glob(stem + ".*"):  # what a broken download left, yt-dlp's .ytdl and .part-Frag files too
            left.unlink(missing_ok=True)
        done = download(partial / stem)
        return done.rename(self.cache / done.name)

    def audio(self, db, user: str, source: str, ref: str) -> Path:
        """The track's audio file: the cached one, else downloaded now with the user's own service. Off when the user
        can't use the service, LookupError when it no longer has the song. A service may send another format than the
        one it usually does, so the track takes the file's."""
        key = ref.removeprefix(source + ":")
        with self.locks[ref]:
            if path := self.cached(stream_stem(source, key)):
                return path
            client = next((c for c in self.active(db, user) if c.name == source), None)
            if client is None:
                raise off(KINDS[source].title if source in KINDS else source)
            path = self.kept(stream_stem(source, key), lambda target: client.download(key, target))
            db.execute("update track set suffix = ?, content_type = ? where ref = ?", (path.suffix[1:], content_type(path), ref))
            return path

    def prune(self, db):
        """Deletes cached audio that no track plays any more (a stream that moved to a local copy, or its service's
        song went), and what broken downloads left in PARTIAL."""
        keep = {stream_stem(source, ref.removeprefix(source + ":"))
                for source, ref in db.execute(f"select source, ref from track where source in ({','.join('?' * len(KINDS))})", list(KINDS))}
        keep |= {preview_stem(preview) for (preview,) in db.execute("select preview from track where source = 'missing' and preview is not null")}
        for f in self.cache.glob("*.*"):
            if f.is_file() and f.name.split(".")[0] not in keep:
                f.unlink(missing_ok=True)
        for f in (self.cache / PARTIAL).glob("*"):
            if f.stat().st_mtime < time.time() - 86400:
                f.unlink(missing_ok=True)

    def previews(self, db, user: str) -> list:
        """The user's usable preview services, in the order they are asked."""
        usable = self.usable(db, user)
        return [kind() for name, kind in PREVIEWS.items() if "previews" in usable.get(name, ())]

    def clip(self, db, user: str, preview: str) -> tuple[Path, str]:
        """A song's preview ('<service>:<its id>') as its file and content type: the cached one, else downloaded now
        with the user's preview service. Off when the user can't use it, LookupError when it no longer has the clip."""
        name, key = preview.split(":", 1)
        kind = PREVIEWS.get(name)

        def download(path: Path) -> Path:
            link = kind().link(key)
            if not link:
                raise LookupError(f"{kind.title} no longer has this preview")
            target = path.with_name(f"{path.name}.{kind.suffix}")
            save(link, target)
            return target
        with self.locks[preview]:
            if not self.cached(preview_stem(preview)) and not (kind and self.uses(db, user, name, "previews")):
                raise off(f"{kind.title if kind else name} previews")
            path = self.kept(preview_stem(preview), download)
            return path, content_type(path)


PARTIAL = "partial"  # the cache's folder for downloads till they are complete
RETRY_START = 600  # seconds before a service that failed to start (a sign-in it refused) is tried again
CONTENT_TYPES = {"mp3": "audio/mpeg", "m4a": "audio/mp4", "mp4": "audio/mp4", "flac": "audio/flac", "opus": "audio/ogg",
                 "ogg": "audio/ogg", "webm": "audio/webm", "wav": "audio/wav"}


def content_type(path: Path) -> str:
    return CONTENT_TYPES.get(path.suffix[1:].lower(), "application/octet-stream")


def stream_stem(source: str, key: str) -> str:
    """The cache's name for a service's track, without its suffix."""
    return make_id(source + "-", key)


def preview_stem(preview: str) -> str:
    name, key = preview.split(":", 1)
    return make_id("preview-" + name + "-", key)


SHARE = {"bandcamp": "{}", "soundcloud": "{}", "qobuz": "https://open.qobuz.com/track/{}", "tidal": "https://tidal.com/browse/track/{}"}


def share_url(source: str, ref: str) -> str | None:
    """The service's own page of one of its songs, to share."""
    return SHARE[source].format(ref.removeprefix(source + ":")) if source in SHARE else None
