import json
import logging
import re
import threading
import unicodedata
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


def fold(text: str) -> str:
    """Case, accents, spaces and punctuation aside: 'Suite‐Pee' (Unicode hyphen) and 'Suite-Pee' are the same title."""
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).casefold()
    return re.sub(r"[\W_]+", "", text)


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
            if fold(r["title"]) != fold(title):
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

    def playlists(self, client) -> list[dict]:
        """The account's personal mixes (My Supermix, Discover, Replay, the themed My Mixes...) from its home feed, then
        its library playlists: Liked Music and the playlists it made or saved."""
        with self.api_lock:
            home = client.get_home(limit=50)
            library = client.get_library_playlists(limit=None)
        # ytmusicapi gives None for home items it can't parse (new kinds YouTube adds): those are skipped.
        mixes = [c for section in home if section for c in section.get("contents") or [] if c and (c.get("playlistId") or "").startswith("RDTMAK")]
        saved = [p for p in library if p and p.get("playlistId") and p["playlistId"] != "SE"]  # SE: podcast episodes
        return list({p["playlistId"]: {"id": p["playlistId"], "title": p.get("title") or p["playlistId"],
                                       "description": p.get("description"), "cover": big_cover(p.get("thumbnails"))}
                     for p in mixes + saved}.values())

    def playlist_tracks(self, client, playlist_id: str) -> list[dict]:
        with self.api_lock:
            try:
                tracks = client.get_playlist(playlist_id, limit=100 if playlist_id.startswith("RDTMAK") else None)["tracks"]
            except Exception:
                if not playlist_id.startswith("RDTMAK"):
                    raise
                # A mix's watch playlist is the fallback only: for the themed My Mixes it holds another mix's songs.
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

    def artist(self, name: str) -> dict | None:
        """The YouTube Music artist of that name: description, photo, every album and single (with paged lists
        followed), and where its top songs are. None when YouTube Music has no artist by that name."""
        with self.api_lock:
            found = next((a for a in self.api.search(name, filter="artists", limit=5) if fold(a.get("artist") or "") == fold(name)), None)
            if not found:
                return None
            artist = self.api.get_artist(found["browseId"])
            releases = []
            for kind in ("albums", "singles"):
                shelf = artist.get(kind) or {}
                items = self.api.get_artist_albums(shelf["browseId"], shelf.get("params"), limit=None) \
                    if shelf.get("browseId") and shelf.get("params") else shelf.get("results", [])
                releases += [{"browse_id": r["browseId"], "title": r["title"], "year": int(r["year"]) if str(r.get("year") or "").isdigit() else None,
                              "type": "album" if kind == "albums" else (r.get("type") or "single").lower(), "cover": big_cover(r.get("thumbnails"))}
                             for r in items if r.get("browseId")]
        return {"name": artist.get("name") or name, "description": artist.get("description"), "image": big_cover(artist.get("thumbnails")),
                "releases": list({r["browse_id"]: r for r in releases}.values()), "songs": (artist.get("songs") or {}).get("browseId")}

    def playlist_rows(self, playlist_id: str, limit: int) -> list[dict]:
        with self.api_lock:
            tracks = self.api.get_playlist(playlist_id, limit=limit)["tracks"]
        return [row for t in tracks if (row := track_row(t))]

    def album(self, browse_id: str) -> list[dict]:
        """The album page's songs, each with its year, number and the album's own artist (album_by: 'Various Artists'
        for a soundtrack, where each song names its singers)."""
        with self.api_lock:
            album = self.api.get_album(browse_id)
        cover, by = album.get("thumbnails") or [], ", ".join(a["name"] for a in album.get("artists") or [] if a.get("name")) or None
        rows = []
        for t in album.get("tracks", []):
            row = track_row({**t, "album": {"name": album["title"], "id": browse_id}, "thumbnails": t.get("thumbnails") or cover,
                             "artists": t.get("artists") or album.get("artists"), "year": album.get("year")})
            if row:
                rows.append(row | {"track_no": t.get("trackNumber"), "album_by": by or row["album_artist"]})
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
