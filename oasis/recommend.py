"""Song recommendations from YouTube Music, Last.fm, ListenBrainz and the library itself, and radio playlists that grow
as they are played: from their own songs, from everything played, or as stations from the user's Last.fm."""
import json
import logging
import random
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from itertools import chain, zip_longest

from .artists import linked
from .db import remember

log = logging.getLogger(__name__)

AHEAD = 30      # unplayed tracks a radio keeps queued
KEEP = 300      # older radio entries beyond this are dropped
RECENT = 100    # recently played tracks a radio won't repeat
PER_SEED = 4    # similar songs a station adds after each of the user's Last.fm songs
LB_RADIO = "https://api.listenbrainz.org/1/explore/lb-radio"
SOURCES = {"all": "Everything mixed", "lastfm": "Last.fm", "ytm": "YouTube Music", "library": "Your library", "listenbrainz": "ListenBrainz"}
STATIONS = {"lastfm-top": "Last.fm top tracks", "lastfm-loved": "Last.fm loved tracks"}
PERIODS = ("7day", "1month", "3month", "6month", "12month", "overall")  # Last.fm's periods for top tracks
PERIOD = "3month"


class Recommender:
    def __init__(self, connect, ytm, lastfm):
        self.connect, self.ytm, self.lastfm = connect, ytm, lastfm
        self.wake = threading.Event()
        self.chores = []  # other upkeep done on each pass, given a db
        self.similar_artists: dict[str, list[str]] = {}  # Last.fm's answers by artist, kept while running

    def sources(self, user) -> list[str]:
        """The sources that can give this user anything: Last.fm needs the server's API key, YouTube Music the server's
        YouTube Music, ListenBrainz the user's token."""
        return [s for s, on in (("all", True), ("lastfm", self.lastfm), ("ytm", self.ytm), ("library", True),
                                ("listenbrainz", user["listenbrainz_token"])) if on]

    def stations(self, user) -> list[str]:
        """Stations from the user's Last.fm need the API key and the user's linked Last.fm."""
        return list(STATIONS) if self.lastfm and user["lastfm_session"] and user["lastfm_name"] else []

    def similar(self, db, seed, count: int, deep: bool = False, source: str = "all", token: str | None = None) -> list[str]:
        """Track ids like the seed track from one source, or from every one at hand ("all"), taking turns so local songs
        come throughout. YouTube Music, Last.fm and ListenBrainz (with the user's token) need the network; the library
        methods always give something. A source that gives nothing gives an empty list: no other stands in."""
        methods = {
            "ytm": [("YouTube Music radio", lambda: self.from_youtube(db, seed, count))],
            "lastfm": [("Last.fm similar tracks", lambda: self.from_lastfm(db, seed, min(count, 15 if deep else 10)) if self.lastfm else []),
                       ("Last.fm similar artists", lambda: self.from_similar_artists(db, seed, count) if self.lastfm else [])],
            "listenbrainz": [("ListenBrainz radio", lambda: self.from_listenbrainz(db, seed, count, token))],
            "library": [("linked artists", lambda: self.from_linked(db, seed, count)),
                        ("same artist", lambda: self.from_artist(db, seed, count)),
                        ("played together", lambda: self.from_plays(db, seed, count)),
                        ("same genre", lambda: self.from_tags(db, seed, count))],
        }
        found = []
        for name, source in chain.from_iterable(methods.values()) if source == "all" else methods[source]:
            try:
                found.append(source())
            except Exception as e:
                log.info("%s failed: %s", name, e)
        picks = [id for id in chain.from_iterable(zip_longest(*found)) if id and id != seed["id"]]
        return list(dict.fromkeys(picks))[:count]

    def from_youtube(self, db, seed, count: int) -> list[str]:
        if not self.ytm:
            return []
        video_id = seed["ytm_id"] if seed["ytm_id"] is not None else self.ytm.match(seed)
        if seed["ytm_id"] is None:
            db.execute("update track set ytm_id = ? where id = ?", (video_id or "", seed["id"]))
        return [remember(db, row) for row in self.ytm.radio(video_id, count)] if video_id else []

    def from_similar_artists(self, db, seed, count: int) -> list[str]:
        """Library songs by the artists Last.fm calls similar, a few from each in turn."""
        artist = seed["album_artist"] if seed["source"] == "ytm" else seed["artist"]
        if artist not in self.similar_artists:
            info = self.lastfm.artist_info(artist)
            self.similar_artists[artist] = [a["name"] for a in info.get("similar", {}).get("artist", [])]
        per_artist = [[r[0] for r in db.execute(
            "select id from track where library = 1 and (album_artist = ? collate nocase or artist = ? collate nocase) "
            "order by random() limit 3", (name, name))] for name in self.similar_artists[artist]]
        return [id for id in chain.from_iterable(zip_longest(*per_artist)) if id][:count]

    def from_linked(self, db, seed, count: int) -> list[str]:
        """Library songs by the seed artist's personas, related artists and band members, closest first, a few each in turn."""
        per_artist = [[r[0] for r in db.execute("select id from track where library = 1 and artist_id = ? order by random() limit 3", (other,))]
                      for other, _ in linked(db, seed["artist_id"])]
        return [id for id in chain.from_iterable(zip_longest(*per_artist)) if id][:count]

    def from_artist(self, db, seed, count: int) -> list[str]:
        """The artist's other library songs, most played first."""
        return [r[0] for r in db.execute(
            "select t.id from track t where t.library = 1 and t.id != ? and (t.artist_id = ? or t.artist = ? collate nocase) "
            "order by (select coalesce(sum(play_count), 0) from stat s where s.id = t.id) desc, random() limit ?",
            (seed["id"], seed["artist_id"], seed["artist"], count))]

    def from_plays(self, db, seed, count: int) -> list[str]:
        """Songs played within an hour of the seed, by the same listener, most often first."""
        return [r[0] for r in db.execute(
            "select other.track_id, count(*) n from play mine join play other on other.user = mine.user "
            "and other.track_id != mine.track_id and abs(other.played - mine.played) <= 3600 "
            "join track t on t.id = other.track_id where mine.track_id = ? group by other.track_id order by n desc limit ?",
            (seed["id"], count))]

    def from_tags(self, db, seed, count: int) -> list[str]:
        """Library songs of the same genre by other artists, closest in year first. Era alone says too little."""
        if not seed["genre"]:
            return []
        return [r[0] for r in db.execute(
            "select id from track where library = 1 and genre = ? collate nocase and artist_id != ? "
            "order by abs(coalesce(year, 0) - ?), random() limit ?", (seed["genre"], seed["artist_id"], seed["year"] or 0, count))]

    def from_lastfm(self, db, seed, count: int) -> list[str]:
        artist = seed["album_artist"] if seed["source"] == "ytm" else seed["artist"]
        try:
            pairs = self.lastfm.similar(artist, seed["title"], count)
        except Exception as e:
            log.info("Last.fm similar failed: %s", e)
            return []
        return self.resolve(db, pairs)

    def from_listenbrainz(self, db, seed, count: int, token: str | None) -> list[str]:
        """LB Radio's songs for the seed's artist and those ListenBrainz finds similar. Its API is experimental and needs
        a user token: any failure is logged and gives nothing."""
        if not token:
            return []
        artist = seed["album_artist"] if seed["source"] == "ytm" else seed["artist"]
        try:
            pairs = lb_radio(token, artist)
        except Exception as e:
            log.info("ListenBrainz radio failed: %s", e)
            return []
        return self.resolve(db, pairs[:count])

    def resolve(self, db, pairs: list[tuple[str, str]]) -> list[str]:
        """(artist, title) pairs as track ids, in their order: a library song, else the song on YouTube Music."""
        ids, missing = {}, []
        for i, (a, t) in enumerate(pairs):
            local = db.execute("select id from track where library = 1 and (artist = ? or album_artist = ?) collate nocase "
                               "and title = ? collate nocase", (a, a, t)).fetchone()
            if local:
                ids[i] = local["id"]
            elif self.ytm:
                missing.append(i)
        if missing:
            with ThreadPoolExecutor(5) as pool:
                found = pool.map(lambda i: self.ytm.find(*pairs[i]), missing)
            ids |= {i: remember(db, row) for i, row in zip(missing, found) if row}
        return [ids[i] for i in sorted(ids)]

    # Radio playlists

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        while True:
            self.wake.wait(600)
            self.wake.clear()
            db = self.connect()
            try:
                for job in [self.top_up_all, *self.chores]:  # each on its own: one failing leaves the others be
                    try:
                        job(db)
                    except Exception:
                        log.exception("%s failed", getattr(job, "__qualname__", job))
            finally:
                db.close()

    def top_up_all(self, db):
        for playlist in db.execute("select * from playlist where radio > 0").fetchall():
            try:  # one failing leaves the others be
                self.top_up(db, playlist)
            except Exception:
                log.exception("radio %s failed", playlist["name"])

    def played(self, db, owner: str, track_ids: list[str]):
        """Moves radios past the played tracks and makes sure the owner has a radio that follows all listening."""
        if not db.execute("select 1 from playlist where owner = ? and radio = 2", (owner,)).fetchone():
            now = time.time()  # or ignore: two plays at once both find none, and the index one_radio keeps one
            db.execute("insert or ignore into playlist(name, owner, comment, created, changed, radio) values (?, ?, ?, ?, ?, 2)",
                       ("Radio", owner, "Grows from what you play", now, now))
        for id in track_ids:
            db.execute("update playlist set played_pos = max(played_pos, (select max(pos) from playlist_entry e "
                       "where e.playlist_id = playlist.id and e.track_id = ?)) where radio > 0 and owner = ? and exists "
                       "(select 1 from playlist_entry e where e.playlist_id = playlist.id and e.track_id = ?)", (id, owner, id))
        self.wake.set()

    def top_up(self, db, playlist):
        entries = [r[0] for r in db.execute("select track_id from playlist_entry where playlist_id = ? order by pos", (playlist["id"],))]
        played_pos = min(playlist["played_pos"], len(entries) - 1)
        missing = AHEAD - (len(entries) - 1 - played_pos)
        if missing <= 0:
            return
        owner = db.execute("select * from user where name = ?", (playlist["owner"],)).fetchone()
        if not owner:
            return
        source = playlist["radio_source"] or owner["radio_source"]  # the automatic radio follows the user's default
        station = playlist["station"]
        if station:
            seeds = self.station_seeds(db, owner, station, playlist["period"] or PERIOD)
        else:
            if playlist["radio"] == 2:
                seed_ids = [r[0] for r in db.execute("select id from stat where user = ? and played is not null order by played desc limit 3", (owner["name"],))]
            else:
                seed_ids = list(reversed(entries[max(0, played_pos - 2):played_pos + 1])) or entries[:3]
            seeds = [r for id in seed_ids if (r := db.execute("select * from track where id = ?", (id,)).fetchone())]
        skip = set(entries) | {r[0] for r in db.execute("select id from stat where user = ? and played is not null order by played desc limit ?", (owner["name"], RECENT))}
        added = []

        def add(id):
            if id not in skip:
                skip.add(id)
                added.append(id)

        for seed in seeds:
            if station:  # the user's own song, then a few like it
                add(seed["id"])
                for id in [i for i in self.similar(db, seed, PER_SEED * 3, source=source, token=owner["listenbrainz_token"]) if i not in skip][:PER_SEED]:
                    add(id)
            else:
                for id in self.similar(db, seed, AHEAD + 20, deep=True, source=source, token=owner["listenbrainz_token"]):
                    add(id)
            if len(added) >= missing:
                break
        if not added:
            return
        added = added[:max(missing, 10)]
        entries += added
        dropped = max(0, len(entries) - KEEP)
        set_entries(db, playlist["id"], entries[dropped:])
        db.execute("update playlist set played_pos = ? where id = ?", (played_pos - dropped, playlist["id"]))
        log.info("radio %s: added %d tracks", playlist["name"], len(added))

    def station_seeds(self, db, user, station: str, period: str):
        """The user's Last.fm top or loved tracks in random order, as tracks of the library or YouTube Music, read one
        at a time as the station needs them."""
        if station not in self.stations(user):
            return
        pairs = self.lastfm.user_tracks(user["lastfm_name"], station == "lastfm-loved", period)
        random.shuffle(pairs)
        for pair in pairs:
            try:
                ids = self.resolve(db, [pair])
            except Exception as e:
                log.info("station song %s failed: %s", pair, e)
                continue
            for id in ids:
                if row := db.execute("select * from track where id = ?", (id,)).fetchone():
                    yield row


def lb_radio(token: str, artist: str) -> list[tuple[str, str]]:
    """ListenBrainz's LB Radio for an artist and the artists it finds similar, popular songs first ("easy")."""
    query = urllib.parse.urlencode({"prompt": f"artist:({artist.replace('(', ' ').replace(')', ' ').strip()})", "mode": "easy"})
    request = urllib.request.Request(f"{LB_RADIO}?{query}", headers={"Authorization": "Token " + token, "User-Agent": "oasis/0.1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        playlist = json.load(response)["payload"]["jspf"].get("playlist") or {}
    return [(t["creator"], t["title"]) for t in playlist.get("track") or [] if t.get("creator") and t.get("title")]


def set_entries(db, playlist_id, track_ids):
    db.execute("delete from playlist_entry where playlist_id = ?", (playlist_id,))
    db.executemany("insert into playlist_entry values (?, ?, ?)", [(playlist_id, i, t) for i, t in enumerate(track_ids)])
    db.execute("update playlist set changed = ? where id = ?", (time.time(), playlist_id))
