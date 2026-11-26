"""Streaming services besides YouTube Music, asked for songs YouTube Music doesn't have: Qobuz and Tidal (lossless, with
the user's own account, linked on the dashboard), Bandcamp and SoundCloud (without an account; SoundCloud's Go+ tracks
with the user's sign-in). Each user turns on the ones they use, among those an admin allows (Sources); the same goes for
YouTube Music, Last.fm and ListenBrainz. Each finds one song by artist and title, and downloads its audio, which oasis
keeps in a cache and plays from there; those that can (stream) play through oasis while it downloads. Their songs are
streaming tracks like YouTube Music's: source is the service's name, ref '<service>:<its id>'. Deezer, Apple Music,
SoundCloud, Qobuz and Tidal give 30 s previews of songs none has (PREVIEWS), which stay songs nothing can play
(missing.py). Deezer, Tidal and SoundCloud also name songs for radios (recommend.py), and Tidal has each user's mixes
(mixes.py)."""
import base64
import hashlib
import json
import logging
import re
import subprocess
import threading
import time
import urllib.parse
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import urllib3

from .text import fold
from .tracks import album_id, make_id, name_id, remember
from .ytm import Stream

log = logging.getLogger(__name__)

BROWSER = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"
HTTP = urllib3.PoolManager(maxsize=4, retries=urllib3.Retry(connect=1, read=1, status=0, other=0, redirect=5),
                           timeout=urllib3.Timeout(connect=10, read=30), headers={"User-Agent": BROWSER})
PREVIEW = 31  # seconds: a "song" this short is a service's preview clip, not the song
ALLOW = "allow."  # the setting table's keys for what an admin offers users: allow.<service> and allow.<service>.<capability>, "1" or "0"
YDL = {"quiet": True, "no_warnings": True, "noprogress": True}  # yt-dlp's options for every service it reads
STREAM_KEPT = 600  # seconds a service's audio URL is played from before it is asked for again: they expire


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


def save_parts(urls: list[str], path: Path):
    """Downloads segments that make one file in their order (a DASH manifest's) into path."""
    part = path.with_name(path.name + ".part")
    with part.open("wb") as out:
        for url in urls:
            response = request("GET", url)
            if response.status != 200:
                raise OSError(f"HTTP {response.status} from a segment of {path.name}")
            out.write(response.data)
    part.rename(path)


def save_hls(url: str, path: Path, container: str):
    """Downloads an HLS playlist's audio into path, as it is (ffmpeg's copy into container)."""
    part = path.with_name(path.name + ".part")
    try:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", url, "-map", "0:a:0", "-c", "copy", "-f", container, str(part)],
                       check=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=900)
    except subprocess.CalledProcessError as e:
        raise OSError(f"ffmpeg couldn't download {path.name}: {e.stderr.decode(errors='replace')[-300:]}") from None
    part.rename(path)


def measure(url: str) -> int:
    """The size of the file at url, from the answer to a request for its first byte: the server must take ranges."""
    response = request("GET", url, headers={"Range": "bytes=0-0"})
    total = (response.headers.get("Content-Range") or "").rpartition("/")[2]
    if response.status != 206 or not total.isdigit():
        raise OSError(f"HTTP {response.status} from {url.split('?')[0]} without its size")
    return int(total)


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
        import yt_dlp
        for r in [r for r in self.search(f"{artist} {title}") if fold(r.get("name") or "") == fold(title)][:self.PAGES]:
            try:
                info = self.info(r["item_url_path"])
            except yt_dlp.utils.DownloadError as e:
                if "No video formats found" in str(e):  # a track its artist doesn't let stream: not this service's song
                    continue
                raise
            if fold(artist) not in (fold(r.get("band_name") or ""), fold(info.get("artist") or "")):
                continue
            if not any(f.get("format_id") == self.FORMAT for f in info.get("formats") or []) or not full_length(info.get("duration")):
                continue
            return {"id": str(r["id"]), "key": r["item_url_path"], "title": info.get("track") or r["name"],
                    "artist": info.get("artist") or r.get("band_name") or artist, "album": info.get("album") or r.get("album_name") or r["name"],
                    "year": info.get("release_year"), "duration": round(info["duration"]), "track_no": info.get("track_number"),
                    "cover": info.get("thumbnail") or r.get("img")}
        return None

    def stream(self, key: str) -> Stream:
        found = next((f for f in self.info(key).get("formats") or [] if f.get("format_id") == self.FORMAT), None)
        if not found:
            raise LookupError("Bandcamp has no MP3 stream of this track")
        return Stream(found["url"], self.content_type, measure(found["url"]), time.time() + STREAM_KEPT)

    def download(self, key: str, path: Path) -> Path:
        import yt_dlp
        with yt_dlp.YoutubeDL(self.options() | {"format": self.FORMAT, "outtmpl": str(path) + ".%(ext)s"}) as ydl:
            return Path(ydl.prepare_filename(ydl.extract_info(key, download=True)))


class SoundCloud:
    """SoundCloud through its web player's API (api-v2): full-length AAC of public uploads. Tracks only SoundCloud Go+
    plays in full give everyone else a 30 s clip: they count only with a user's Go+ sign-in (account), which also
    gets Go+'s 256 kbit/s AAC, and their clips come from SoundCloudPreview. Only the artist's own uploads count. Its
    audio is a single file (progressive) or HLS; only the single file plays through oasis while it downloads.
    yt-dlp isn't used: it keeps a sign-in for every later download in the process, whoever's it is."""
    name, title, prefix, suffix, content_type, bitrate = "soundcloud", "SoundCloud", "sc-", "m4a", "audio/mp4", 160
    API = "https://api-v2.soundcloud.com/"
    HQ = 256  # kbit/s of Go+'s AAC
    PAGES = 50  # pages of 200 read of a user's likes or playlists at most
    CONTAINERS = {"audio/mp4": ("m4a", "mp4"), "audio/mpeg": ("mp3", "mp3"), "audio/ogg": ("opus", "ogg")}  # suffix, ffmpeg's format
    client_id = None  # the web player's, read from its scripts (read_client_id) and shared by every client

    def __init__(self, account: dict | None = None):
        """account: the user's sign-in (token, id and name, as link gives it); None for what SoundCloud plays anyone."""
        self.account = account
        self.token = (account or {}).get("token")
        self.variant = "signed-in" if self.token else ""  # what SoundCloud finds depends on it (recommend.py's kept answers)

    @staticmethod
    def read_client_id() -> str:
        page = request("GET", "https://soundcloud.com/").data.decode(errors="replace")
        for src in reversed(re.findall(r'<script[^>]+src="([^"]+)"', page)):
            if found := re.search(r'client_id\s*:\s*"(\w{32})"', request("GET", src).data.decode(errors="replace")):
                return found[1]
        raise LookupError("SoundCloud's player has no client id where it used to")

    def api(self, path: str, **fields) -> dict | list:
        """The answer at path (or a whole URL, as next_href gives one). The client id is read again once when refused:
        SoundCloud changes it now and then."""
        url = path if path.startswith("http") else self.API + path
        headers = {"Authorization": "OAuth " + self.token} if self.token else {}
        for again in (False, True):
            if again or not SoundCloud.client_id:
                SoundCloud.client_id = self.read_client_id()
            query = urllib.parse.urlencode({**{k: v for k, v in fields.items() if v is not None}, "client_id": SoundCloud.client_id})
            response = request("GET", url + ("&" if "?" in url else "?") + query, headers=headers)
            if response.status not in (401, 403):
                break
        if response.status != 200:
            raise OSError(f"HTTP {response.status} from SoundCloud's {url.split('?')[0].removeprefix(self.API)}")
        return response.json()

    def pages(self, path: str) -> list[dict]:
        found, url = [], path
        for _ in range(self.PAGES):
            page = self.api(url, limit=200, linked_partitioning=1) if url == path else self.api(url)
            found += page.get("collection") or []
            if not (url := page.get("next_href")):
                break
        return found

    @staticmethod
    def link(token: str) -> dict:
        """Checks the sign-in by reading the account it belongs to: the account to keep. ValueError when refused."""
        try:
            me = SoundCloud({"token": token}).api("me")
        except OSError as e:
            raise ValueError("SoundCloud didn't accept that token: copy it again." if "HTTP 401" in str(e) else f"SoundCloud didn't answer: {e}")
        return {"token": token, "id": me["id"], "name": me.get("username") or me.get("permalink") or "SoundCloud"}

    def full(self, track: dict) -> bool:
        """Whether SoundCloud plays this client the whole track: a Go+ track (policy SNIP) only with a sign-in."""
        length = track.get("full_duration") if self.token else track.get("duration")
        return track.get("policy") != "BLOCK" and full_length((length or 0) / 1000) and bool(track.get("permalink_url"))

    def song(self, track: dict) -> dict:
        art = track.get("artwork_url") or (track.get("user") or {}).get("avatar_url")
        hq = self.token and any(t.get("quality") == "hq" for t in self.encodings(track))
        length = (track.get("full_duration") if self.token else None) or track.get("duration") or 0
        return {"id": str(track["id"]), "key": track["permalink_url"], "title": track["title"], "artist": track["user"]["username"],
                "album": track["title"], "year": int(track["created_at"][:4]) if track.get("created_at") else None,
                "duration": round(length / 1000), "cover": art and art.replace("-large.", "-t500x500."), "bitrate": self.HQ if hq else None}

    def search(self, artist: str, title: str) -> list[dict]:
        """The artist's own uploads of that title ('Title' or 'Artist - Title')."""
        titles = {fold(title), fold(f"{artist} - {title}")}
        return [t for t in self.api("search/tracks", q=f"{artist} {title}", limit=10).get("collection") or []
                if fold((t.get("user") or {}).get("username") or "") == fold(artist) and fold(t.get("title") or "") in titles]

    def find(self, artist: str, title: str) -> dict | None:
        found = next((t for t in self.search(artist, title) if self.full(t)), None)
        return self.song(found) | {"title": title, "album": title} if found else None

    def track(self, key: str) -> dict:
        return self.api("resolve", url=key)

    @staticmethod
    def encodings(track: dict, clip: bool = False) -> list[dict]:
        """The track's encodings of the whole song (or of its clip), best first: Go+'s, then standard, then low quality,
        AAC before MP3. DRM's are left out."""
        found = [t for t in (track.get("media") or {}).get("transcodings") or [] if t.get("url") and bool(t.get("snipped")) == clip
                 and (t.get("format") or {}).get("protocol") in ("progressive", "hls") and not t.get("preset", "").startswith("abr")]
        return sorted(found, key=lambda t: ({"hq": 0, "sq": 1}.get(t.get("quality"), 2), "aac" not in t.get("preset", "")))

    def media_url(self, track: dict, encoding: dict) -> str:
        return self.api(encoding["url"], track_authorization=track.get("track_authorization"))["url"]

    def stream(self, key: str) -> Stream:
        track = self.track(key)
        encoding = next((e for e in self.encodings(track) if e["format"]["protocol"] == "progressive"), None)
        if not encoding:
            raise LookupError("SoundCloud has this track as HLS only")
        url = self.media_url(track, encoding)
        return Stream(url, encoding["format"]["mime_type"].split(";")[0], measure(url), time.time() + STREAM_KEPT)

    def download(self, key: str, path: Path) -> Path:
        track = self.track(key)
        encodings = self.encodings(track)
        if not encodings:
            raise LookupError("SoundCloud only gives a preview of this track" + ("" if self.token else " without a Go+ sign-in"))
        best = encodings[0]
        suffix, container = self.CONTAINERS.get(best["format"]["mime_type"].split(";")[0], ("m4a", "mp4"))
        target = path.with_name(f"{path.name}.{suffix}")
        url = self.media_url(track, best)
        if best["format"]["protocol"] == "progressive":
            save(url, target)
        else:
            save_hls(url, target, container)
        return target

    def related(self, artist: str, title: str) -> list[tuple[str, str]]:
        """The tracks SoundCloud plays after the song (its related tracks), as (artist, title): the artist its label
        names, else the uploader, and the title without the artist's name before it. The song is the artist's own
        upload, else the first one found whose title holds the song's."""
        found = self.search(artist, title)
        if not found:
            results = self.api("search/tracks", q=f"{artist} {title}", limit=10).get("collection") or []
            found = [t for t in results if fold(title) in fold(t.get("title") or "")]
        if not found:
            return []
        pairs = []
        for t in self.api(f"tracks/{found[0]['id']}/related", limit=RELATED).get("collection") or []:
            by = (t.get("publisher_metadata") or {}).get("artist") or (t.get("user") or {}).get("username")
            if by and t.get("title"):
                pairs.append((by, t["title"].removeprefix(by + " - ")))
        return pairs

    def likes(self) -> list[dict]:
        """The account's liked tracks, newest first."""
        return [e["track"] for e in self.pages(f"users/{self.account['id']}/track_likes") if e.get("track")]

    def playlists(self) -> list[dict]:
        """The account's own playlists and albums, then those it liked."""
        own = self.pages(f"users/{self.account['id']}/playlists")
        return own + [e["playlist"] for e in self.pages(f"users/{self.account['id']}/playlist_likes") if e.get("playlist")]

    def playlist_tracks(self, playlist_id: str) -> list[dict]:
        """A playlist's tracks in order. Its answer has the first few whole and only the ids of the rest, which are read
        50 at a time."""
        tracks = self.api(f"playlists/{playlist_id}").get("tracks") or []
        stubs = [str(t["id"]) for t in tracks if "title" not in t]
        whole = {}
        for i in range(0, len(stubs), 50):
            whole |= {t["id"]: t for t in self.api("tracks", ids=",".join(stubs[i:i + 50]))}
        return [t if "title" in t else whole[t["id"]] for t in tracks if "title" in t or t["id"] in whole]


class Qobuz:
    """Qobuz with a subscriber's account: FLAC at CD quality. Its web player's app id and secrets are read from the
    player's own script when the account is linked, as no published app has them. Qobuz answers nothing, not even a
    search, without a signed-in account; one without a subscription that streams gets samples only (previews_only)."""
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

    def file_url(self, track_id: str, secret: str | None = None, format_id: int = FORMAT) -> dict:
        """The track's audio URL; sample true when the account gets only its 30 s sample."""
        at = int(time.time())
        return self.call("track/getFileUrl", track_id=track_id, format_id=format_id, intent="stream", request_ts=at,
                         request_sig=self.signature(track_id, format_id, at, secret or self.account["secret"]))

    @classmethod
    def link(cls, email: str, password: str, samples: bool = False) -> dict:
        """Signs in and finds the secret that works: the account to keep. ValueError saying why when it can't. samples:
        an account without a subscription that streams is kept too, for previews only."""
        app_id, secrets = cls.fetch_player_keys()
        response = request("GET", cls.API + "user/login", fields={"email": email, "password": hashlib.md5(password.encode()).hexdigest(),
                                                                         "app_id": app_id})
        if response.status != 200:
            raise ValueError("Qobuz didn't accept that email and password." if response.status in (400, 401) else f"Qobuz answered HTTP {response.status}.")
        found = response.json()
        user = found.get("user") or {}
        account = {"app_id": app_id, "token": found["user_auth_token"], "name": user.get("display_name") or user.get("login") or email}
        if not (user.get("credential") or {}).get("parameters"):
            if not samples:
                raise ValueError("That Qobuz account has no subscription that streams.")
            account["previews_only"] = True
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

    def search(self, artist: str, title: str) -> list[dict]:
        """The artist's tracks of that title."""
        found = []
        for t in self.call("track/search", query=f"{artist} {title}", limit=10)["tracks"]["items"]:
            names = {fold((t.get("performer") or {}).get("name") or ""), fold(((t.get("album") or {}).get("artist") or {}).get("name") or "")}
            if fold(t.get("title") or "") == fold(title) and fold(artist) in names:
                found.append(t)
        return found

    def find(self, artist: str, title: str) -> dict | None:
        for t in self.search(artist, title):
            album = t.get("album") or {}
            if t.get("streamable") and full_length(t.get("duration")):
                year = (album.get("release_date_original") or "")[:4]
                return {"id": str(t["id"]), "key": str(t["id"]), "title": t["title"], "artist": (t.get("performer") or {}).get("name") or artist,
                        "album": album.get("title") or t["title"], "year": int(year) if year.isdigit() else None, "duration": t["duration"],
                        "track_no": t.get("track_number"), "disc_no": t.get("media_number"), "cover": (album.get("image") or {}).get("large")}
        return None

    def full_url(self, key: str) -> str:
        found = self.file_url(key)
        if found.get("sample") or not found.get("url"):
            raise LookupError("Qobuz only gives a preview of this track to this account")
        return found["url"]

    def stream(self, key: str) -> Stream:
        url = self.full_url(key)
        return Stream(url, self.content_type, measure(url), time.time() + STREAM_KEPT)

    def download(self, key: str, path: Path) -> Path:
        target = path.with_suffix(".flac")
        save(self.full_url(key), target)
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

    def search(self, artist: str, title: str) -> list:
        """The artist's tracks of that title."""
        import tidalapi
        return [t for t in self.session.search(f"{artist} {title}", models=[tidalapi.Track], limit=10).get("tracks") or []
                if fold(t.name or "") == fold(title) and fold(artist) in {fold(a.name or "") for a in t.artists or []}]

    def find(self, artist: str, title: str) -> dict | None:
        for t in self.search(artist, title):
            if full_length(t.duration) and t.allow_streaming:
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

    def track_radio(self, artist: str, title: str) -> list[tuple[str, str]]:
        """Tidal's radio of the song, as (artist, title)."""
        found = self.search(artist, title)
        return [pair(t) for t in found[0].get_track_radio(RELATED)] if found else []

    def artist_radio(self, artist: str) -> list[tuple[str, str]]:
        """Tidal's radio of the artist, as (artist, title)."""
        import tidalapi
        found = [a for a in self.session.search(artist, models=[tidalapi.Artist], limit=5).get("artists") or [] if fold(a.name or "") == fold(artist)]
        return [pair(t) for t in found[0].get_radio(RELATED)] if found else []

    def mixes(self) -> list[dict]:
        """The account's mixes (My Mix 1..., My Daily Discovery, My New Arrivals) with their songs as (artist, title):
        id, title, comment, cover, songs. Video mixes are left out."""
        from tidalapi.media import Track
        from tidalapi.mix import Mix
        found = []
        for mix in [m for m in self.session.mixes() if isinstance(m, Mix)]:
            songs = [pair(t) for t in mix.items() if isinstance(t, Track)]
            if songs:
                try:
                    cover = mix.image(640)
                except Exception:
                    cover = None
                found.append({"id": mix.id, "title": mix.title, "comment": mix.sub_title or None, "cover": cover, "songs": songs})
        return found

    def stream(self, key: str) -> Stream:
        """One URL, or a DASH manifest's segments sent one after another, each measured first."""
        manifest = unencrypted(self.session.track(int(key)).get_stream())
        urls = manifest.get_urls()
        with ThreadPoolExecutor(8) as pool:
            sizes = list(pool.map(measure, urls))
        kind = CONTENT_TYPES.get(manifest.file_extension.lstrip("."), self.content_type)
        return Stream(urls[0], kind, sum(sizes), time.time() + STREAM_KEPT, parts=list(zip(urls, sizes)) if len(urls) > 1 else None)

    def download(self, key: str, path: Path) -> Path:
        return save_manifest(unencrypted(self.session.track(int(key)).get_stream()), path)


def pair(track) -> tuple[str, str]:
    """A tidalapi track as (artist, title)."""
    return (track.artist.name if track.artist else ""), track.name


def unencrypted(stream):
    """A tidalapi stream's manifest, which oasis can play only unencrypted."""
    manifest = stream.get_stream_manifest()
    if manifest.is_encrypted:
        raise LookupError("Tidal sent this track encrypted")
    return manifest


def save_manifest(manifest, path: Path) -> Path:
    """Downloads a Tidal manifest's audio: one URL, or a DASH manifest's segments, which make the file in their order."""
    target = path.with_name(path.name + ("" if manifest.file_extension.startswith(".") else ".") + manifest.file_extension)
    urls = manifest.get_urls()
    if len(urls) == 1:
        save(urls[0], target)
    else:
        save_parts(urls, target)
    return target


class Preview:
    """A service's 30 s clips: find gives the id of the artist's song's clip, link the URL it downloads from (or
    download fetches it itself)."""

    def download(self, key: str, path: Path) -> Path:
        link = self.link(key)
        if not link:
            raise LookupError(f"{self.title} no longer has this preview")
        target = path.with_name(f"{path.name}.{self.suffix}")
        save(link, target)
        return target


class Deezer:
    """Deezer's public API, without an account: an artist's radio and related artists."""
    API = "https://api.deezer.com/"

    def answer(self, path: str, **fields) -> dict:
        found = get_json(self.API + path, fields=fields)
        if "error" in found:
            raise OSError(f"Deezer: {found['error']}")
        return found

    def artist(self, name: str) -> int | None:
        """The id of the artist of that name."""
        return next((a["id"] for a in self.answer("search/artist", q=name, limit=5).get("data") or [] if fold(a.get("name") or "") == fold(name)), None)

    def radio(self, artist: str) -> list[tuple[str, str]]:
        """The artist's radio: songs by the artist and those like them, as (artist, title)."""
        id = self.artist(artist)
        if not id:
            return []
        tracks = self.answer(f"artist/{id}/radio", limit=RELATED).get("data") or []
        return [(t["artist"]["name"], t.get("title_short") or t["title"]) for t in tracks if (t.get("artist") or {}).get("name")]

    def related(self, artist: str) -> list[str]:
        """The names of the artists Deezer calls related, closest first."""
        id = self.artist(artist)
        if not id:
            return []
        return [a["name"] for a in self.answer(f"artist/{id}/related", limit=20).get("data") or [] if a.get("name")]


class DeezerPreview(Preview):
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


class ApplePreview(Preview):
    """Apple Music's 30 s AAC previews, from the iTunes Search API. No account. The API takes about 20 searches a
    minute from one address and refuses more (429) for a while, so oasis keeps under that itself: a search past it is
    skipped at once, as an error, which isn't kept as a miss."""
    name, title, suffix, content_type = "apple", "Apple Music", "m4a", "audio/mp4"
    API = "https://itunes.apple.com/"
    LIMIT = 20  # searches a minute
    asked: deque = deque()  # when each search of the last minute was made, from every user's lookups
    lock = threading.Lock()

    @classmethod
    def take_turn(cls):
        with cls.lock:
            now = time.time()
            while cls.asked and cls.asked[0] < now - 60:
                cls.asked.popleft()
            if len(cls.asked) >= cls.LIMIT:
                raise OSError(f"skipped: Apple Music's search takes {cls.LIMIT} searches a minute")
            cls.asked.append(now)

    @classmethod
    def refused(cls):
        """Apple refused anyway (429): no search for a minute."""
        with cls.lock:
            cls.asked.extend([time.time()] * cls.LIMIT)

    def find(self, artist: str, title: str) -> str | None:
        self.take_turn()
        response = request("GET", self.API + "search", fields={"term": f"{artist} {title}", "entity": "song", "limit": 10}, retries=False)
        if response.status == 429:
            self.refused()
            raise OSError("Apple Music's search refused (429): paused for a minute")
        if response.status != 200:
            raise OSError(f"HTTP {response.status} from Apple Music's search")
        found = response.json()
        for t in found.get("results") or []:
            if fold(t.get("trackName") or "") == fold(title) and fold(t.get("artistName") or "") == fold(artist) and t.get("previewUrl"):
                return str(t["trackId"])
        return None

    def link(self, key: str) -> str | None:
        found = get_json(self.API + "lookup", fields={"id": key}).get("results") or []
        return found[0].get("previewUrl") if found else None


class SoundCloudPreview(Preview):
    """SoundCloud's public 30 s MP3 clips of the tracks only SoundCloud Go+ plays in full. No account."""
    name, title, suffix, content_type = "soundcloud", "SoundCloud", "mp3", "audio/mpeg"

    def find(self, artist: str, title: str) -> str | None:
        return next((str(t["id"]) for t in SoundCloud().search(artist, title) if t.get("policy") == "SNIP"), None)

    def link(self, key: str) -> str | None:
        client = SoundCloud()
        track = client.api(f"tracks/{key}")
        clip = next((e for e in client.encodings(track, clip=True) if e["format"]["protocol"] == "progressive"), None)
        return client.media_url(track, clip) if clip else None


class QobuzPreview(Preview):
    """Qobuz's 30 s samples of the tracks an account can't stream in full (every track, without a subscription), with
    the user's Qobuz account: Qobuz answers nothing without one."""
    name, title, suffix, content_type = "qobuz", "Qobuz", "mp3", "audio/mpeg"
    FORMAT = 5  # MP3 at 320 kbit/s

    def __init__(self, client: Qobuz):
        self.client = client

    def find(self, artist: str, title: str) -> str | None:
        return next((str(t["id"]) for t in self.client.search(artist, title)), None)

    def link(self, key: str) -> str | None:
        return self.client.file_url(key, format_id=self.FORMAT).get("url")


class TidalPreview(Preview):
    """Tidal's 30 s clips, with the user's Tidal account."""
    name, title, suffix, content_type = "tidal", "Tidal", "m4a", "audio/mp4"

    def __init__(self, client: Tidal):
        self.client = client

    def find(self, artist: str, title: str) -> str | None:
        return next((str(t.id) for t in self.client.search(artist, title)), None)

    def download(self, key: str, path: Path) -> Path:
        """The clip as Tidal's players ask for one (asset presentation PREVIEW), parsed like a full song's answer."""
        from tidalapi.media import Stream as TidalStream
        answer = self.client.session.request.request("GET", f"tracks/{key}/playbackinfopostpaywall",
                                                     {"playbackmode": "STREAM", "assetpresentation": "PREVIEW", "audioquality": "HIGH"})
        return save_manifest(unencrypted(TidalStream().parse(answer.json())), path)


KINDS = {kind.name: kind for kind in (Qobuz, Tidal, Bandcamp, SoundCloud)}  # also the order they are asked in
ACCOUNTS = ("qobuz", "tidal")  # the services that need a linked account; the others are only turned on
# Where songs nothing can play get a clip from, in the order they are asked; each user turns each on. Clips reach only
# apps that take songs nothing can play (extension unavailableSongs), so plain Subsonic apps never play one. Those of
# ACCOUNTS take the user's client of that service.
PREVIEWS = {kind.name: kind for kind in (DeezerPreview, ApplePreview, SoundCloudPreview, QobuzPreview, TidalPreview)}
# Every service a user can turn on, with the capabilities of it an admin can allow or not: songs, full songs to find,
# search and stream; signed-in, SoundCloud's Go+ songs with the user's sign-in; previews, clips of songs nothing can
# play; radio, the service as a source of radios and similar songs (recommend.py), and Deezer's related artists;
# account, what a linked account adds: YouTube Music's mixes and history, SoundCloud's likes and playlists, Bandcamp's
# purchases and wishlist (bandcamp.py), the playlists ListenBrainz makes for the user and Tidal's mixes (mixes.py).
# The lists of other servers that take plays (servers.py) are offered or not as a whole, and turned on by no one.
CAPABILITIES = {"ytm": ("songs", "account"), "qobuz": ("songs", "previews"), "tidal": ("songs", "previews", "radio", "account"),
                "bandcamp": ("songs", "account"), "soundcloud": ("songs", "signed-in", "account", "previews", "radio"), "lastfm": (),
                "listenbrainz": ("account",), "lastfm-servers": (), "listenbrainz-servers": (), "deezer": ("previews", "radio"),
                "apple": ("previews",)}
RELATED = 50  # songs asked of a service's radio or related tracks
PREVIEW_SECONDS = 30


class Off(LookupError):
    """A song's service is one the user can't use now: not turned on, not linked, or not allowed on this server."""


def off(title: str) -> Off:
    return Off(f"This song plays from {title}, which you haven't turned on: see Services on the oasis dashboard.")


class Sources:
    """Each user's services (user_service) as an admin allows them (the setting table), the songs their streaming
    services find as tracks, and those songs' audio, downloaded into cache when first played. Cached audio plays for
    everyone; downloading it takes the requesting user's own service."""

    def __init__(self, cache: Path, ytm_allowed: bool = False, connect=None):
        """ytm_allowed: whether YouTube Music is allowed till an admin decides (oasis.toml); the others are. connect:
        the database, for downloads that finish in the background."""
        self.cache, self.ytm_allowed, self.connect = cache, ytm_allowed, connect
        self.clients: dict[tuple[str, str], tuple[str, object]] = {}  # by (user, service): (its account, its client)
        self.failed: dict[tuple[str, str], tuple[str, float]] = {}  # by (user, service): (the account it failed to start with, when)
        self.locks = defaultdict(threading.Lock)
        self.streams: dict[str, Stream] = {}  # by ref: the audio URL a track plays from while its download runs

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

    def account(self, db, user: str, name: str) -> dict | None:
        """The user's sign-in to the service, while they can use its account features."""
        value = self.turned_on(db, user).get(name)
        return json.loads(value) if value and self.uses(db, user, name, "account") else None

    def unlink(self, db, user: str, name: str):
        """Forgets the user's sign-in to the service, which stays on."""
        db.execute("update user_service set account = null where user = ? and service = ?", (user, name))
        self.clients.pop((user, name), None)
        self.failed.pop((user, name), None)

    def link(self, db, user: str, name: str, account: dict):
        """Keeps the user's sign-in to the service, turning it on."""
        db.execute("insert into user_service(user, service, account) values (?, ?, ?) on conflict(user, service) do update "
                   "set account = excluded.account", (user, name, json.dumps(account)))
        self.clients.pop((user, name), None)
        self.failed.pop((user, name), None)

    def client(self, db, user: str, name: str):
        """The user's client of one of their streaming services (KINDS) they can use, whatever for; None when it fails to
        start."""
        usable = self.usable(db, user)
        return self.started(db, user, name, self.turned_on(db, user).get(name), usable[name]) if name in usable else None

    def active(self, db, user: str) -> list:
        """The clients of the user's streaming services whose full songs the user can use, in the order they are asked;
        one that fails to start is left out, and an account that gets samples only."""
        usable, accounts = self.usable(db, user), self.turned_on(db, user)
        found = [self.started(db, user, name, accounts.get(name), usable[name]) for name in KINDS if "songs" in usable.get(name, ())]
        return [c for c in found if c and not (getattr(c, "account", None) or {}).get("previews_only")]

    def started(self, db, user: str, name: str, value: str | None, capabilities: set):
        """The user's client of the service, made once for its account (JSON) and capabilities; None when it fails to
        start, and for RETRY_START after. SoundCloud signs in only for its signed-in songs."""
        kind, key = KINDS[name], (user, name)
        if name not in ACCOUNTS and not (name == "soundcloud" and "signed-in" in capabilities):
            value = None
        value = value or ""
        kept = self.clients.get(key)
        if not kept or kept[0] != value:
            failed = self.failed.get(key)
            if failed and failed[0] == value and failed[1] > time.time() - RETRY_START:
                return None
            try:
                kept = (value, kind(json.loads(value)) if value else kind())
            except Exception as e:
                log.warning("%s is unavailable for %s: %s", kind.title, user, e)
                self.failed[key] = (value, time.time())
                return None
            self.clients[key] = kept
        if hasattr(kept[1], "current") and (now := json.dumps(kept[1].current())) != kept[0]:
            # a renewed sign-in, kept for the next start
            db.execute("update user_service set account = ? where user = ? and service = ?", (now, user, name))
            self.clients[key] = kept = (now, kept[1])
        return kept[1]

    @staticmethod
    def track_row(client, song: dict) -> dict:
        """song: a service's find; its bitrate, where it gives one, is that song's (SoundCloud's Go+ songs)."""
        artist = song["artist"]
        return {"id": client.prefix + song["id"], "source": client.name, "ref": f"{client.name}:{song['key']}", "library": 0,
                "title": song["title"], "artist": artist, "album_artist": artist, "album": song["album"],
                "artist_id": name_id(artist), "album_id": album_id(artist, song["album"]), "year": song.get("year"),
                "duration": song["duration"], "track_no": song.get("track_no"), "disc_no": song.get("disc_no"),
                "bitrate": song.get("bitrate") or client.bitrate, "suffix": client.suffix, "content_type": client.content_type, "cover": song.get("cover"),
                "created": time.time()}

    def remember(self, db, client, song: dict) -> str:
        """Keeps a service's song as a streaming track and returns its id (see tracks.remember)."""
        return remember(db, self.track_row(client, song))

    def plays(self, usable: dict[str, set[str]], ytm, source: str, ref: str) -> bool:
        """Whether a streaming track plays for a user with these usable services: its service's full songs are on for
        them, or its audio is cached. ytm: YouTube Music's client, None when oasis has none."""
        if source == "ytm":
            return bool(ytm) and ("songs" in usable.get("ytm", ()) or bool(ytm.cached(ref)))
        if source in KINDS:
            return "songs" in usable.get(source, ()) or bool(self.cached(stream_stem(source, ref.removeprefix(source + ":"))))
        return False

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

    def audio(self, db, user: str, source: str, ref: str, one_url: bool = False) -> Path | Stream:
        """What the track plays from: the cached file; else the service's audio URL, played through oasis while the
        download fills the cache in the background, for the services that give one (stream); else the download, waited
        for. Downloads take the user's own service. Off when the user can't use it, LookupError when it no longer has the
        song. one_url: a stream that comes in parts (Tidal's DASH segments) won't do."""
        stem = stream_stem(source, ref.removeprefix(source + ":"))
        if path := self.cached(stem):
            return path
        client = next((c for c in self.active(db, user) if c.name == source), None)
        if client is None:
            raise off(KINDS[source].title if source in KINDS else source)
        if (stream := self.streams.get(ref)) and stream.expires > time.time() and not (one_url and stream.parts):
            return stream
        if hasattr(client, "stream"):
            try:
                stream = client.stream(ref.removeprefix(source + ":"))
            except Exception as e:
                log.info("no stream of %s (%s), downloading", ref, e)
            else:
                self.streams[ref] = stream
                threading.Thread(target=self.fetch_quietly, args=(client, ref), daemon=True).start()
                if not (one_url and stream.parts):
                    return stream
        return self.fetch(client, ref, db)

    def fetch(self, client, ref: str, db=None) -> Path:
        """The track's audio downloaded into the cache, once. A service may send another format than the one it usually
        does, so the track takes the file's."""
        key = ref.removeprefix(client.name + ":")
        with self.locks[ref]:
            if path := self.cached(stream_stem(client.name, key)):
                return path
            path = self.kept(stream_stem(client.name, key), lambda target: client.download(key, target))
        self.streams.pop(ref, None)
        own = db or self.connect()
        try:
            own.execute("update track set suffix = ?, content_type = ? where ref = ?", (path.suffix[1:], content_type(path), ref))
        finally:
            if db is None:
                own.close()
        return path

    def fetch_quietly(self, client, ref: str):
        try:
            self.fetch(client, ref)
        except Exception as e:
            log.warning("download of %s failed: %s", ref, e)

    def forget(self, ref: str):
        """Drops the track's audio URL: it broke while playing."""
        self.streams.pop(ref, None)

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
        """The user's usable preview services, in the order they are asked; those of ACCOUNTS with the user's client of
        the service, which must start."""
        usable, accounts, found = self.usable(db, user), self.turned_on(db, user), []
        for name, kind in PREVIEWS.items():
            if "previews" not in usable.get(name, ()):
                continue
            if name in ACCOUNTS:
                if client := self.started(db, user, name, accounts.get(name), usable[name]):
                    found.append(kind(client))
            else:
                found.append(kind())
        return found

    def clip(self, db, user: str, preview: str) -> tuple[Path, str]:
        """A song's preview ('<service>:<its id>') as its file and content type: the cached one, else downloaded now
        with the user's preview service. Off when the user can't use it, LookupError when it no longer has the clip."""
        name, key = preview.split(":", 1)
        with self.locks[preview]:
            if not (path := self.cached(preview_stem(preview))):
                client = next((c for c in self.previews(db, user) if c.name == name), None)
                if client is None:
                    raise off(f"{PREVIEWS[name].title if name in PREVIEWS else name} previews")
                path = self.kept(preview_stem(preview), lambda target: client.download(key, target))
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
