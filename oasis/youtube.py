"""YouTube Music's catalogue as oasis keeps it: its songs as streaming tracks, the artist pages and their albums and
singles (ytm_artist, ytm_release), albums filled in from YouTube Music's listing, each track's video there
(track.ytm_id), and when a streaming track's audio is played straight from YouTube or downloaded into the cache.
ytm.py is the client that asks YouTube Music; this decides what of its answers the database keeps and when to ask again."""
import json
import logging
import threading
import time
from pathlib import Path

from .tracks import IDS, album_id, name_id, remember
from .ytm import Stream

log = logging.getLogger(__name__)

ARTIST_PAGE_KEPT = 86400  # seconds an artist's YouTube Music page is used before it is fetched again
WARMED = 3600             # seconds before a video is readied (warm) again
ALBUM_GAP = 5             # seconds between the album pages complete_next_album reads
ALBUM_IDLE = 600          # seconds before looking again when every album page was read


def track_row(song: dict) -> dict:
    """The track row of a song YouTube Music lists (ytm.song)."""
    video_id = song["video_id"]
    row = {
        "id": IDS["stream"] + video_id, "source": "ytm", "ref": video_id, "ytm_id": video_id, "ytm_album": song["album_browse_id"], "library": 0,
        "title": song["title"], "artist": song["artist"], "album_artist": song["album_artist"], "album": song["album"],
        "artist_id": name_id(song["album_artist"]), "album_id": album_id(song["album_artist"], song["album"]),
        "year": song["year"], "duration": song["duration"],
        "bitrate": 128, "suffix": "m4a", "content_type": "audio/mp4",  # the m4a audio resolve() picks and the cache keeps
        "cover": song["cover"],
        "created": time.time(),
    }
    for key in ("track_no", "album_by"):  # album pages give these (ytm.album)
        if key in song:
            row[key] = song[key]
    return row


def remember_song(db, song: dict) -> str:
    """Keeps a song YouTube Music lists as a streaming track and returns its id (see tracks.remember)."""
    return remember(db, track_row(song))


def video_of(db, r, ytm) -> str | None:
    """The track's video on YouTube Music, searched for once: a miss is kept as '' and not searched for again."""
    if r["ytm_id"] is not None:
        return r["ytm_id"] or None
    video_id = ytm.match(r)
    db.execute("update track set ytm_id = ? where id = ?", (video_id or "", r["id"]))
    return video_id


def fetch_soon(ytm, video_id: str):
    """Downloads a streaming track in the background unless it is cached."""
    if not ytm.cached(video_id):
        threading.Thread(target=prefetch, args=(ytm, video_id), daemon=True).start()


def prefetch(ytm, video_id: str):
    try:
        ytm.fetch(video_id)
    except Exception as e:
        log.warning("download of %s failed: %s", video_id, e)


def warm(ytm, video_id: str):
    try:
        ytm.warm(video_id)
    except Exception as e:
        log.info("warming %s failed: %s", video_id, e)


def audio(ytm, video_id: str) -> Path | Stream:
    """What to play a streaming track from: the cached file; else YouTube's stream while the cache fills in the
    background, so playback starts in well under a second; else the download, waited for."""
    if path := ytm.cached(video_id):
        return path
    try:
        stream = ytm.resolve(video_id)
    except Exception as e:
        log.info("no direct stream for %s (%s), downloading", video_id, e)
        return ytm.fetch(video_id)
    fetch_soon(ytm, video_id)
    return stream


class YouTubeCatalogue:
    """The catalogue's pages and albums as the front ends ask for them, for one YouTube Music client (None when it is
    off: then nothing is fetched)."""

    def __init__(self, ytm):
        self.ytm = ytm
        self.completed: set[str] = set()  # albums complete_album fetched in full
        self.album_tried: set[str] = set()  # albums complete_next_album asked YouTube Music for, read or not
        self.warmed: dict[str, float] = {}  # when each video was last readied (warm), worked or not

    def fetch_soon(self, video_id: str):
        if self.ytm:
            fetch_soon(self.ytm, video_id)

    def warm(self, db, track_ids: list[str]):
        """Readies the stream of the first of these songs that plays from YouTube, in the background: a client is about to
        play it. One only, as YouTube refuses players that ask too often; a client fetches the next song during this one."""
        if not self.ytm or not track_ids:
            return
        refs = dict(db.execute(f"select id, ref from track where source = 'ytm' and id in ({','.join('?' * len(track_ids))})", track_ids).fetchall())
        video_id = next((refs[id] for id in track_ids if id in refs), None)
        if not video_id or self.warmed.get(video_id, 0) > time.time() - WARMED or self.ytm.cached(video_id):
            return  # a client asking for a radio again and again asks YouTube once an hour, even for a song it refuses
        self.warmed = {v: at for v, at in self.warmed.items() if at > time.time() - WARMED} | {video_id: time.time()}
        threading.Thread(target=warm, args=(self.ytm, video_id), daemon=True).start()

    def search(self, db, query: str, limit: int) -> list[str]:
        """The ids of YouTube Music's songs for a query, kept as streaming tracks; none when it fails."""
        if not self.ytm:
            return []
        try:
            return [remember_song(db, song) for song in self.ytm.search(query, limit)]
        except Exception as e:
            log.warning("YouTube Music search failed: %s", e)
            return []

    def artist_page(self, db, artist_id: str, name: str):
        """Fetches the artist's YouTube Music page into ytm_artist and ytm_release, at most once per ARTIST_PAGE_KEPT."""
        page = db.execute("select fetched from ytm_artist where artist_id = ?", (artist_id,)).fetchone()
        if not self.ytm or (page and page["fetched"] > time.time() - ARTIST_PAGE_KEPT):
            return
        try:
            found = self.ytm.artist(name)
        except Exception as e:
            log.info("YouTube Music artist %s unavailable: %s", name, e)
            return
        db.execute("insert or replace into ytm_artist values (?, ?, ?, ?, ?, ?)", (artist_id, *(
            (found["name"], found["description"], found["image"], found["songs"]) if found else (name, None, None, None)), time.time()))
        for x in (found or {}).get("releases", []):
            # Ids follow the oasis artist, so these albums sit with the local ones and merge with an album of the same title.
            db.execute("insert or replace into ytm_release values (?, ?, ?, ?, ?, ?, ?)",
                       (album_id(name, x["title"]), artist_id, x["browse_id"], x["title"], x["year"], x["type"], x["cover"]))

    def top_songs(self, db, name: str, count: int) -> list[str]:
        """The ids of the artist's top songs on YouTube Music, kept as streaming tracks: its page's list, or a search
        when the page has none. None of them when it fails."""
        if not self.ytm:
            return []
        try:
            owner = db.execute("select artist_id from track where album_artist = ? collate nocase limit 1", (name,)).fetchone()
            if owner:
                self.artist_page(db, owner[0], name)
            page = owner and db.execute("select songs from ytm_artist where artist_id = ?", (owner[0],)).fetchone()
            songs = self.ytm.playlist_rows(page["songs"], count) if page and page["songs"] else \
                [s for s in self.ytm.search(name, count) if name.casefold() in (s["album_artist"].casefold(), s["artist"].casefold())]
            return [remember_song(db, song) for song in songs]
        except Exception as e:
            log.warning("YouTube Music top songs failed: %s", e)
            return []

    def complete_album(self, db, album_id: str):
        """Fetches the rest of an album that is only known from streaming tracks seen in search or radio, or only
        from an artist's YouTube Music page."""
        if not self.ytm or album_id in self.completed:
            return
        sources = db.execute("select source, ytm_album from track where album_id = ?", (album_id,)).fetchall()
        release = db.execute("select * from ytm_release where album_id = ?", (album_id,)).fetchone()
        browse_id = next((r["ytm_album"] for r in sources if r["ytm_album"]), None) or (release and release["browse_id"])
        if not browse_id or any(r["source"] == "local" for r in sources):
            return
        try:
            songs = self.ytm.album(browse_id)
        except Exception as e:
            log.info("album %s unavailable: %s", browse_id, e)
            return
        ids = [remember_song(db, song) for song in songs]
        db.executemany("insert or replace into album_track select ?, id, ?, ? from track where id = ? and source = 'ytm'",
                       [(album_id, song.get("disc_no"), song.get("track_no"), id) for id, song in zip(ids, songs)])
        if release:  # file this album's own songs under the artist page's album, whatever spelling YouTube credits them with
            db.executemany("update track set album_id = ?, artist_id = ? where id = ? and source = 'ytm' and ytm_album = ?",
                           [(album_id, release["artist_id"], id, browse_id) for id in ids])
        self.completed.add(album_id)

    def complete_next_album(self, db) -> bool:
        """Reads the YouTube Music album page of one library streaming song's album not read yet, for its album
        artist, year and numbers (complete_album); False when there is none."""
        row = db.execute("select album_id from track where source = 'ytm' and library = 1 and ytm_album is not null "
                         "and id not in (select track_id from album_track) and album_id not in (select value from json_each(?)) limit 1",
                         (json.dumps([*self.completed, *self.album_tried]),)).fetchone()
        if not row:
            return False
        self.album_tried.add(row[0])
        self.complete_album(db, row[0])
        return True
