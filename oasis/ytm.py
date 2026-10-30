import re
import threading
import time
from collections import defaultdict
from pathlib import Path

from .db import make_id

WATCH = "https://music.youtube.com/watch?v="


def track_row(item: dict) -> dict | None:
    """Turns a ytmusicapi song (search result or watch-playlist track) into a track row."""
    video_id = item.get("videoId")
    if not video_id:
        return None
    artists = [a["name"] for a in item.get("artists") or [] if a.get("name")] or ["Unknown Artist"]
    title = item.get("title") or video_id
    album = (item.get("album") or {}).get("name") or title
    thumbnails = item.get("thumbnails") or item.get("thumbnail") or []
    duration = item.get("duration_seconds")
    if duration is None and (length := item.get("length") or item.get("duration")):
        duration = sum(int(part) * 60 ** i for i, part in enumerate(reversed(length.split(":"))))
    year = item.get("year")
    return {
        "id": "yt-" + video_id, "source": "ytm", "ref": video_id, "ytm_id": video_id, "library": 0,
        "title": title, "artist": ", ".join(artists), "album_artist": artists[0], "album": album,
        "artist_id": make_id("ar-", artists[0]), "album_id": make_id("al-", artists[0], album),
        "year": int(year) if str(year or "").isdigit() else None, "duration": duration,
        "bitrate": 128, "suffix": "m4a", "content_type": "audio/mp4",
        "cover": re.sub(r"=w\d+-h\d+", "=w544-h544", thumbnails[-1]["url"]) if thumbnails else None,
        "created": time.time(),
    }


class YouTubeMusic:
    def __init__(self, cache: Path):
        from ytmusicapi import YTMusic
        self.cache = cache
        self.cache.mkdir(parents=True, exist_ok=True)
        self.api = YTMusic()
        self.api_lock = threading.Lock()
        self.locks = defaultdict(threading.Lock)

    def search(self, query: str, limit: int) -> list[dict]:
        with self.api_lock:
            results = self.api.search(query, filter="songs", limit=limit)
        return [row for item in results[:limit] if (row := track_row(item))]

    def cached(self, video_id: str) -> Path | None:
        return next((f for f in self.cache.glob(video_id + ".*") if f.suffix != ".part"), None)

    def fetch(self, video_id: str) -> Path:
        """Downloads the audio once; later calls return the cached file."""
        import yt_dlp
        with self.locks[video_id]:
            if path := self.cached(video_id):
                return path
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
