"""The YouTube Music client: ytmusicapi for searches, artist and album pages, playlists and signed-in accounts, the
visionOS player for audio URLs, and the cache of downloaded audio. What oasis keeps of YouTube Music in its database is
youtube.py's."""
import json
import logging
import re
import shlex
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import urllib3

from .text import fold
from .tracks import SAME_SONG_SECONDS

log = logging.getLogger(__name__)

WATCH = "https://music.youtube.com/watch?v="
PLAYER = "https://music.youtube.com/youtubei/v1/player?prettyPrint=false"
# The visionOS client's audio URLs need no signature or n-parameter work and play at once; yt-dlp's default
# clients make new URLs wait about 5 s. Unreleased client, so it may stop working: fetch() falls back to yt-dlp.
VISIONOS_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 15_7_3) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Safari/605.1.15"
VISIONOS = {"clientName": "VISIONOS", "clientVersion": "1.02", "osName": "visionOS", "osVersion": "26.5.23O471",
            "deviceMake": "Apple", "deviceModel": "RealityDevice17,1", "hl": "en", "gl": "US"}
CHUNK = 8 << 20  # googlevideo throttles long single requests
# Connections to YouTube kept open between requests: a new one costs a TLS handshake, about 0.1 s. googlevideo
# redirects some requests to another server. A request that got no answer is sent once more (a GET also when the kept
# connection turned out closed); one that got an error status isn't.
HTTP = urllib3.PoolManager(maxsize=8, retries=urllib3.Retry(connect=1, read=1, status=0, other=0, redirect=5),
                           timeout=urllib3.Timeout(connect=10, read=30))
ANONYMOUS = 6  # anonymous YouTube Music calls at once: more looks like an abuser to YouTube
MIX_PREFIX = "RDTMAK"  # the ids of an account's personal mixes
LIKED = "LM"  # Liked Music: its songs stay in the library even with the playlist hidden


@dataclass
class Stream:
    url: str
    content_type: str
    size: int
    expires: float
    headers: dict = field(default_factory=dict)  # what a request for url must send: the URL only works with them


def big_cover(thumbnails) -> str | None:
    return re.sub(r"=w\d+-h\d+", "=w544-h544", thumbnails[-1]["url"]) if thumbnails else None


def song(item: dict) -> dict | None:
    """A ytmusicapi song (search result or watch-playlist track) as oasis reads it: video_id, title, artist (all of
    its artists), album_artist (the first), album, album_browse_id, year, duration and cover."""
    video_id = item.get("videoId")
    if not video_id:
        return None
    artists = [a["name"] for a in item.get("artists") or [] if a.get("name")] or ["Unknown Artist"]
    title = item.get("title") or video_id
    album_ref = item.get("album") if isinstance(item.get("album"), dict) else {"name": item.get("album")}
    thumbnails = item.get("thumbnails") or item.get("thumbnail") or []
    duration = item.get("duration_seconds")
    if duration is None and (length := item.get("length") or item.get("duration")):
        duration = sum(int(part) * 60 ** i for i, part in enumerate(reversed(length.split(":"))))
    year = item.get("year")
    return {
        "video_id": video_id, "title": title, "artist": ", ".join(artists), "album_artist": artists[0],
        "album": album_ref.get("name") or title, "album_browse_id": album_ref.get("id"),
        "year": int(year) if str(year or "").isdigit() else None, "duration": duration, "cover": big_cover(thumbnails),
    }


def is_mix(playlist_id: str | None) -> bool:
    """Whether a YouTube Music playlist id is one of an account's personal mixes, not a library playlist."""
    return (playlist_id or "").startswith(MIX_PREFIX)


def read_range(stream: Stream, start: int, end: int):
    """Yields the stream's bytes start..end (inclusive) in chunks."""
    for pos in range(start, end + 1, CHUNK):
        response = HTTP.request("GET", stream.url, headers={**stream.headers, "Range": f"bytes={pos}-{min(end, pos + CHUNK - 1)}"},
                                preload_content=False)
        try:
            if response.status not in (200, 206):
                raise OSError(f"HTTP {response.status} from googlevideo")
            yield from response.stream(1 << 16)
        except BaseException:  # also the client leaving mid-song: a connection with unread bytes can't be reused
            response.close()
            raise
        response.release_conn()


def youtube_headers(pasted: str) -> dict:
    """Browser headers for ytmusicapi from what the user copied out of a signed-in music.youtube.com tab: the request
    headers, a "Copy as cURL" command, or only the Cookie value."""
    text = pasted.strip()
    headers = {}
    if text.startswith("curl "):
        words = shlex.split(text.replace("\\\n", " "))
        for flag, value in zip(words, words[1:]):
            if flag in ("-H", "--header") and ":" in value:
                key, _, val = value.partition(":")
                headers[key.strip().lower()] = val.strip()
            elif flag in ("-b", "--cookie"):
                headers["cookie"] = value
    elif re.search(r"^\s*cookie\s*:", text, re.I | re.M):
        for line in text.splitlines():
            key, sep, val = line.partition(":")
            if sep and key.strip() and not key.startswith(":"):
                headers[key.strip().lower()] = val.strip()
    else:
        headers["cookie"] = text.removeprefix("Cookie:").strip()
    cookie = headers.get("cookie", "")
    if not (sapisid := re.search(r"(?:^|;\s*)__Secure-3PAPISID=([^;\s]+)", cookie)):
        raise ValueError("That has no YouTube sign-in cookie (__Secure-3PAPISID). Copy it from music.youtube.com while signed in.")
    from ytmusicapi.helpers import get_authorization
    origin = "https://music.youtube.com"
    # ytmusicapi takes headers with a SAPISIDHASH authorization for a browser sign-in and renews it per request.
    return {"cookie": cookie, "x-goog-authuser": headers.get("x-goog-authuser", "0"), "origin": origin,
            "authorization": get_authorization(sapisid[1] + " " + origin),
            "user-agent": headers.get("user-agent", "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0")}


class YouTubeMusic:
    def __init__(self, cache: Path):
        from ytmusicapi import YTMusic
        self.cache = cache
        self.cache.mkdir(parents=True, exist_ok=True)
        self.idle = [YTMusic()]  # anonymous clients not in use (see anonymous)
        self.calls = threading.BoundedSemaphore(ANONYMOUS)
        self.clients: dict[str, YTMusic] = {}
        self.visitor = self.idle[0].headers.get("X-Goog-Visitor-Id")
        self.account_lock = threading.Lock()  # the signed-in clients', one call at a time
        self.locks = defaultdict(threading.Lock)
        self.streams: dict[str, Stream] = {}

    @contextmanager
    def anonymous(self):
        """An anonymous ytmusicapi client for one call: calls run at once, each on a client of its own, because a client
        isn't thread-safe (get_lyrics switches its context). A new one is made when all are busy, up to ANONYMOUS."""
        from ytmusicapi import YTMusic
        with self.calls:
            try:
                api = self.idle.pop()
            except IndexError:
                api = YTMusic()
            try:
                yield api
            finally:
                self.idle.append(api)

    def search(self, query: str, limit: int) -> list[dict]:
        with self.anonymous() as api:
            results = api.search(query, filter="songs", limit=limit)
        return [found for item in results[:limit] if (found := song(item))]

    def find(self, artist: str, title: str, duration: int | None = None) -> dict | None:
        """The same song on YouTube Music: same title, and length within SAME_SONG_SECONDS if known, else the same artist."""
        def same(r):
            if fold(r["title"]) != fold(title):
                return False
            if duration:
                return abs((r["duration"] or 0) - duration) <= SAME_SONG_SECONDS
            return artist.casefold() in (r["album_artist"].casefold(), r["artist"].casefold())
        found = [r for r in self.search(f"{artist} {title}", 10) if same(r)]
        found.sort(key=lambda r: r["album_artist"].casefold() != artist.casefold())
        return found[0] if found else None

    def match(self, track) -> str | None:
        """The video id of a track's song on YouTube Music, or None."""
        found = self.find(track["album_artist"], track["title"], track["duration"])
        return found["video_id"] if found else None

    def radio(self, video_id: str, limit: int) -> list[dict]:
        """YouTube Music's radio for a song, without the song itself."""
        with self.anonymous() as api:
            tracks = api.get_watch_playlist(videoId=video_id, radio=True, limit=limit + 1)["tracks"]
        return [found for t in tracks if t.get("videoId") != video_id and (found := song(t))][:limit]

    def client(self, auth: str):
        """A signed-in ytmusicapi client for a user's browser headers (JSON), kept for reuse."""
        from ytmusicapi import YTMusic
        if auth not in self.clients:
            self.clients[auth] = YTMusic(auth)
        return self.clients[auth]

    def forget_client(self, auth: str):
        self.clients.pop(auth, None)

    def account_name(self, client) -> str:
        with self.account_lock:
            return client.get_account_info()["accountName"]

    def playlists(self, client) -> list[dict]:
        """The account's personal mixes (My Supermix, Discover, Replay, the themed My Mixes...) from its home feed, then
        its library playlists: Liked Music and the playlists it made or saved."""
        with self.account_lock:
            home = client.get_home(limit=50)
            library = client.get_library_playlists(limit=None)
        # ytmusicapi gives None for home items it can't parse (new kinds YouTube adds): those are skipped.
        mixes = [c for section in home if section for c in section.get("contents") or [] if c and is_mix(c.get("playlistId"))]
        saved = [p for p in library if p and p.get("playlistId") and p["playlistId"] != "SE"]  # SE: podcast episodes
        return list({p["playlistId"]: {"id": p["playlistId"], "title": p.get("title") or p["playlistId"],
                                       "description": p.get("description"), "cover": big_cover(p.get("thumbnails"))}
                     for p in mixes + saved}.values())

    def playlist_tracks(self, client, playlist_id: str) -> list[dict]:
        with self.account_lock:
            try:
                tracks = client.get_playlist(playlist_id, limit=100 if is_mix(playlist_id) else None)["tracks"]
            except Exception:
                if not is_mix(playlist_id):
                    raise
                # A mix's watch playlist is the fallback only: for the themed My Mixes it holds another mix's songs.
                tracks = client.get_watch_playlist(playlistId=playlist_id, limit=100)["tracks"]
        return [found for t in tracks if (found := song(t))]

    def lyrics(self, video_id: str) -> dict | None:
        """YouTube Music's own lyrics: source, lines as (start ms or None, text), and whether they are synced (line-timed,
        when it has timing). None when it has none."""
        with self.anonymous() as api:
            browse_id = api.get_watch_playlist(videoId=video_id, limit=1).get("lyrics")
            found = api.get_lyrics(browse_id, timestamps=True) if browse_id else None
        if not found or not found.get("lyrics"):
            return None
        source = "YouTube Music" + (f" · {found['source'].removeprefix('Source: ')}" if found.get("source") else "")
        if found.get("hasTimestamps"):
            return {"source": source, "lines": [(line.start_time, line.text) for line in found["lyrics"]], "synced": True}
        return {"source": source, "lines": [(None, line) for line in found["lyrics"].splitlines()], "synced": False}

    def artist(self, name: str) -> dict | None:
        """The YouTube Music artist of that name: description, photo, every album and single (with paged lists
        followed), and where its top songs are. None when YouTube Music has no artist by that name."""
        with self.anonymous() as api:
            found = next((a for a in api.search(name, filter="artists", limit=5) if fold(a.get("artist") or "") == fold(name)), None)
            if not found:
                return None
            artist = api.get_artist(found["browseId"])
            releases = []
            for kind in ("albums", "singles"):
                shelf = artist.get(kind) or {}
                items = api.get_artist_albums(shelf["browseId"], shelf.get("params"), limit=None) \
                    if shelf.get("browseId") and shelf.get("params") else shelf.get("results", [])
                releases += [{"browse_id": r["browseId"], "title": r["title"], "year": int(r["year"]) if str(r.get("year") or "").isdigit() else None,
                              "type": "album" if kind == "albums" else (r.get("type") or "single").lower(), "cover": big_cover(r.get("thumbnails"))}
                             for r in items if r.get("browseId")]
        return {"name": artist.get("name") or name, "description": artist.get("description"), "image": big_cover(artist.get("thumbnails")),
                "releases": list({r["browse_id"]: r for r in releases}.values()), "songs": (artist.get("songs") or {}).get("browseId")}

    def playlist_rows(self, playlist_id: str, limit: int) -> list[dict]:
        with self.anonymous() as api:
            tracks = api.get_playlist(playlist_id, limit=limit)["tracks"]
        return [found for t in tracks if (found := song(t))]

    def album(self, browse_id: str) -> list[dict]:
        """The album page's songs, each with its year, number and the album's own artist (album_by: 'Various Artists'
        for a soundtrack, where each song names its singers)."""
        with self.anonymous() as api:
            album = api.get_album(browse_id)
        cover, by = album.get("thumbnails") or [], ", ".join(a["name"] for a in album.get("artists") or [] if a.get("name")) or None
        songs = []
        for t in album.get("tracks", []):
            found = song({**t, "album": {"name": album["title"], "id": browse_id}, "thumbnails": t.get("thumbnails") or cover,
                          "artists": t.get("artists") or album.get("artists"), "year": album.get("year")})
            if found:
                songs.append(found | {"track_no": t.get("trackNumber"), "album_by": by or found["album_artist"]})
        return songs

    def add_history(self, client, video_id: str):
        with self.account_lock:
            client.add_history_item(client.get_song(video_id))

    def resolve(self, video_id: str) -> Stream:
        """A playable audio URL from one visionOS player request (about 0.3 s), reused until it expires."""
        if (stream := self.streams.get(video_id)) and stream.expires > time.time():
            return stream
        body = {"context": {"client": {**VISIONOS, "visitorData": self.visitor}}, "videoId": video_id,
                "contentCheckOk": True, "racyCheckOk": True}
        headers = {"Content-Type": "application/json", "User-Agent": VISIONOS_UA, "X-YouTube-Client-Name": "101",
                   "X-YouTube-Client-Version": VISIONOS["clientVersion"], "Origin": "https://music.youtube.com",
                   "Referer": "https://music.youtube.com/", "X-Goog-Visitor-Id": self.visitor or ""}
        response = HTTP.request("POST", PLAYER, body=json.dumps(body).encode(), headers=headers, timeout=15)
        if response.status != 200:
            raise OSError(f"HTTP {response.status} from the player")
        player = response.json()
        status = player.get("playabilityStatus", {})
        if status.get("status") != "OK":
            raise LookupError(status.get("reason") or status.get("status"))
        formats = [f for f in player.get("streamingData", {}).get("adaptiveFormats", [])
                   if f.get("mimeType", "").startswith("audio/mp4") and f.get("url") and f.get("contentLength")]
        if not formats:
            raise LookupError("no direct m4a audio")
        best = max(formats, key=lambda f: f.get("bitrate", 0))
        expires = int(parse_qs(urlparse(best["url"]).query).get("expire", [time.time() + 3600])[0])
        stream = Stream(best["url"], "audio/mp4", int(best["contentLength"]), expires - 300, {"User-Agent": VISIONOS_UA})
        self.streams[video_id] = stream
        return stream

    def warm(self, video_id: str):
        """Gets a song ready to stream: its URL resolved and a connection to the server holding it open."""
        if (stream := self.streams.get(video_id)) and stream.expires > time.time():
            return
        stream = self.resolve(video_id)
        HTTP.request("GET", stream.url, headers={**stream.headers, "Range": "bytes=0-0"})

    def forget(self, video_id: str):
        self.streams.pop(video_id, None)

    def cached(self, video_id: str) -> Path | None:
        return next((f for f in self.cache.glob(video_id + ".*") if f.suffix != ".part"), None)

    def fetch(self, video_id: str) -> Path:
        """Downloads the audio once; later calls return the cached file."""
        with self.locks[video_id]:
            if path := self.cached(video_id):
                return path
            try:
                return self._fetch_direct(video_id)
            except Exception as e:
                log.info("direct download of %s failed (%s), trying yt-dlp", video_id, e)
                self.forget(video_id)
                return self._fetch_ytdlp(video_id)

    def _fetch_direct(self, video_id: str) -> Path:
        stream = self.resolve(video_id)
        part = self.cache / f"{video_id}.m4a.part"
        with part.open("wb") as out:
            for data in read_range(stream, 0, stream.size - 1):
                out.write(data)
        if part.stat().st_size != stream.size:
            raise OSError(f"got {part.stat().st_size} of {stream.size} bytes")
        return part.rename(self.cache / f"{video_id}.m4a")

    def _fetch_ytdlp(self, video_id: str) -> Path:
        import yt_dlp
        options = {
            "quiet": True, "no_warnings": True, "noprogress": True,
            "format": "140/bestaudio[ext=m4a]/bestaudio",
            "fixup": "never",  # YouTube's DASH m4a plays and seeks fine as is, so no ffmpeg is needed
            "js_runtimes": {"node": {}, "bun": {}},
            "outtmpl": str(self.cache / "%(id)s.%(ext)s"),
        }
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(WATCH + video_id, download=True)
            return Path(ydl.prepare_filename(info))
