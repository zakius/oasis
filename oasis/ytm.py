import json
import logging
import re
import threading
import time
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .db import make_id

log = logging.getLogger(__name__)

WATCH = "https://music.youtube.com/watch?v="
PLAYER = "https://music.youtube.com/youtubei/v1/player?prettyPrint=false"
# The visionOS client's audio URLs need no signature or n-parameter work and play at once; yt-dlp's default
# clients make new URLs wait about 5 s. Unreleased client, so it may stop working: fetch() falls back to yt-dlp.
VISIONOS_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 15_7_3) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Safari/605.1.15"
VISIONOS = {"clientName": "VISIONOS", "clientVersion": "1.02", "osName": "visionOS", "osVersion": "26.5.23O471",
            "deviceMake": "Apple", "deviceModel": "RealityDevice17,1", "hl": "en", "gl": "US"}
CHUNK = 8 << 20  # googlevideo throttles long single requests


@dataclass
class Stream:
    url: str
    content_type: str
    size: int
    expires: float


def big_cover(thumbnails) -> str | None:
    return re.sub(r"=w\d+-h\d+", "=w544-h544", thumbnails[-1]["url"]) if thumbnails else None


def track_row(item: dict) -> dict | None:
    """Turns a ytmusicapi song (search result or watch-playlist track) into a track row."""
    video_id = item.get("videoId")
    if not video_id:
        return None
    artists = [a["name"] for a in item.get("artists") or [] if a.get("name")] or ["Unknown Artist"]
    title = item.get("title") or video_id
    album_ref = item.get("album") if isinstance(item.get("album"), dict) else {"name": item.get("album")}
    album = album_ref.get("name") or title
    thumbnails = item.get("thumbnails") or item.get("thumbnail") or []
    duration = item.get("duration_seconds")
    if duration is None and (length := item.get("length") or item.get("duration")):
        duration = sum(int(part) * 60 ** i for i, part in enumerate(reversed(length.split(":"))))
    year = item.get("year")
    return {
        "id": "yt-" + video_id, "source": "ytm", "ref": video_id, "ytm_id": video_id, "ytm_album": album_ref.get("id"), "library": 0,
        "title": title, "artist": ", ".join(artists), "album_artist": artists[0], "album": album,
        "artist_id": make_id("ar-", artists[0]), "album_id": make_id("al-", artists[0], album),
        "year": int(year) if str(year or "").isdigit() else None, "duration": duration,
        "bitrate": 128, "suffix": "m4a", "content_type": "audio/mp4",
        "cover": big_cover(thumbnails),
        "created": time.time(),
    }


def read_range(url: str, start: int, end: int):
    """Yields the bytes start..end (inclusive) in chunks."""
    for pos in range(start, end + 1, CHUNK):
        request = urllib.request.Request(url, headers={"User-Agent": VISIONOS_UA, "Range": f"bytes={pos}-{min(end, pos + CHUNK - 1)}"})
        with urllib.request.urlopen(request, timeout=30) as response:
            while data := response.read(1 << 16):
                yield data


class YouTubeMusic:
    def __init__(self, cache: Path):
        from ytmusicapi import YTMusic
        self.cache = cache
        self.cache.mkdir(parents=True, exist_ok=True)
        self.api = YTMusic()
        self.clients: dict[str, YTMusic] = {}
        self.visitor = self.api.headers.get("X-Goog-Visitor-Id")
        self.api_lock = threading.Lock()
        self.locks = defaultdict(threading.Lock)
        self.streams: dict[str, Stream] = {}

    def search(self, query: str, limit: int) -> list[dict]:
        with self.api_lock:
            results = self.api.search(query, filter="songs", limit=limit)
        return [row for item in results[:limit] if (row := track_row(item))]

    def find(self, artist: str, title: str, duration: int | None = None) -> dict | None:
        """The same song on YouTube Music: same title, and length within 5 s if known, else the same artist."""
        def same(r):
            if r["title"].casefold() != title.casefold():
                return False
            if duration:
                return abs((r["duration"] or 0) - duration) <= 5
            return artist.casefold() in (r["album_artist"].casefold(), r["artist"].casefold())
        found = [r for r in self.search(f"{artist} {title}", 10) if same(r)]
        found.sort(key=lambda r: r["album_artist"].casefold() != artist.casefold())
        return found[0] if found else None

    def match(self, track) -> str | None:
        found = self.find(track["album_artist"], track["title"], track["duration"])
        return found["ref"] if found else None

    def radio(self, video_id: str, limit: int) -> list[dict]:
        """YouTube Music's radio for a song, without the song itself."""
        with self.api_lock:
            tracks = self.api.get_watch_playlist(videoId=video_id, radio=True, limit=limit + 1)["tracks"]
        return [row for t in tracks if t.get("videoId") != video_id and (row := track_row(t))][:limit]

    def client(self, auth: str):
        """A signed-in ytmusicapi client for a user's browser headers (JSON), kept for reuse."""
        from ytmusicapi import YTMusic
        if auth not in self.clients:
            self.clients[auth] = YTMusic(auth)
        return self.clients[auth]

    def account_name(self, client) -> str:
        with self.api_lock:
            return client.get_account_info()["accountName"]

    def mixes(self, client) -> list[dict]:
        """The account's personal mixes (My Supermix, My Mix 1-7, Discover, Replay, New Release...) from its home feed."""
        with self.api_lock:
            home = client.get_home(limit=12)
        cards = [c for section in home for c in section.get("contents", []) if (c.get("playlistId") or "").startswith("RDTMAK")]
        return list({c["playlistId"]: {"id": c["playlistId"], "title": c.get("title") or c["playlistId"],
                                        "description": c.get("description"), "cover": big_cover(c.get("thumbnails"))}
                     for c in cards}.values())

    def mix_tracks(self, client, playlist_id: str) -> list[dict]:
        with self.api_lock:
            tracks = client.get_watch_playlist(playlistId=playlist_id, limit=100)["tracks"]
        return [row for t in tracks if (row := track_row(t))]

    def lyrics(self, video_id: str):
        """YouTube Music's own lyrics, line-timed when it has timing; None when it has none."""
        from .lyrics import Lyrics
        with self.api_lock:
            browse_id = self.api.get_watch_playlist(videoId=video_id, limit=1).get("lyrics")
            found = self.api.get_lyrics(browse_id, timestamps=True) if browse_id else None
        if not found or not found.get("lyrics"):
            return None
        source = "YouTube Music" + (f" · {found['source'].removeprefix('Source: ')}" if found.get("source") else "")
        if found.get("hasTimestamps"):
            return Lyrics(source, [(line.start_time, line.text) for line in found["lyrics"]], True)
        return Lyrics(source, [(None, line) for line in found["lyrics"].splitlines()], False)

    def album(self, browse_id: str) -> list[dict]:
        with self.api_lock:
            album = self.api.get_album(browse_id)
        cover = album.get("thumbnails") or []
        rows = []
        for t in album.get("tracks", []):
            row = track_row({**t, "album": {"name": album["title"], "id": browse_id}, "thumbnails": t.get("thumbnails") or cover,
                             "artists": t.get("artists") or album.get("artists"), "year": album.get("year")})
            if row:
                rows.append(row | {"track_no": t.get("trackNumber")})
        return rows

    def add_history(self, client, video_id: str):
        with self.api_lock:
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
        request = urllib.request.Request(PLAYER, json.dumps(body).encode(), headers)
        with urllib.request.urlopen(request, timeout=15) as response:
            player = json.load(response)
        status = player.get("playabilityStatus", {})
        if status.get("status") != "OK":
            raise LookupError(status.get("reason") or status.get("status"))
        formats = [f for f in player.get("streamingData", {}).get("adaptiveFormats", [])
                   if f.get("mimeType", "").startswith("audio/mp4") and f.get("url") and f.get("contentLength")]
        if not formats:
            raise LookupError("no direct m4a audio")
        best = max(formats, key=lambda f: f.get("bitrate", 0))
        expires = int(parse_qs(urlparse(best["url"]).query).get("expire", [time.time() + 3600])[0])
        stream = Stream(best["url"], "audio/mp4", int(best["contentLength"]), expires - 300)
        self.streams[video_id] = stream
        return stream

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
            for data in read_range(stream.url, 0, stream.size - 1):
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
