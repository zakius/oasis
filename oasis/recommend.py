"""Song recommendations from YouTube Music, Last.fm, ListenBrainz, Deezer, Tidal, SoundCloud and the library itself, and
radio playlists that grow as they are played: from their own songs, from everything played, or as stations from the
user's Last.fm. A song Last.fm, ListenBrainz or another service's radio suggests is played from the library, else from
the best of what the user's streaming services answer, all asked at once (YouTube Music and the others of sources.py);
when none has it, it can be kept as a song nothing plays (missing.py), with a preview where a preview service has one,
for the users who want to see those."""
import json
import logging
import queue
import random
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait as wait_for
from itertools import chain, zip_longest

from . import accounts, lastfm, missing, playlists, tasks
from .forward import LISTENBRAINZ
from .artists import linked
from .formats import LOSSLESS
from .playlists import LISTENING_RADIO, PLAIN, set_entries
from .sources import Deezer
from .tracks import BY_ARTIST_NAME, IDS, outside_names
from .youtube import BITRATE, remember_song, video_of


log = logging.getLogger(__name__)

AHEAD = 30      # unplayed tracks a radio keeps queued
KEEP = 300      # older radio entries beyond this are dropped
RECENT = 100    # recently played tracks a radio won't repeat
PER_SEED = 4    # similar songs a station adds after each of the user's Last.fm songs
SOURCES = {"all": "Everything mixed", "lastfm": "Last.fm", "ytm": "YouTube Music", "library": "Your library", "listenbrainz": "ListenBrainz",
           "deezer": "Deezer", "tidal": "Tidal", "soundcloud": "SoundCloud"}
STATIONS = {"lastfm-top": "Last.fm top tracks", "lastfm-loved": "Last.fm loved tracks"}
PERIODS = ("7day", "1month", "3month", "6month", "12month", "overall")  # Last.fm's periods for top tracks
PERIOD = "3month"
FINDS = 6       # songs resolve looks for at once (ytm.ANONYMOUS)
ASKS = 64       # services asked at once, across every song looked for
KEPT = 3600     # seconds a source's answer is kept (Recommender.answer)
# After the best answer so far came at t seconds, a better one is waited for min(GRACE·t, GRACE_MOST) seconds more.
GRACE, GRACE_MOST = 0.3, 3.0
LASTFM_SIMILAR = 15  # similar tracks asked of Last.fm, the most a method uses
RADIOS = ("deezer", "tidal", "soundcloud")  # the services a user's radio capability (sources.CAPABILITIES) makes sources
JOB = "radio"
EVERY = 600  # seconds between top-ups of every radio when nothing woke them


class Recommender:
    def __init__(self, connect, ytm, lastfm, services):
        """lastfm(): oasis's Last.fm app as it is now, or None. services: each user's services and the other streaming
        services (sources.Sources): a user gets songs only from the services they have on."""
        self.connect, self.ytm, self.lastfm, self.services = connect, ytm, lastfm, services
        self.answers: dict[tuple, tuple[float, object]] = {}  # (expiry, answer) by question (answer, find)
        self.answers_lock = threading.Lock()
        self.asking = ThreadPoolExecutor(ASKS)

    def sources(self, db, user) -> list[str]:
        """The sources that can give this user anything: YouTube Music, Last.fm and ListenBrainz when the user has them
        on (sources.Sources.usable), Deezer, Tidal and SoundCloud when their radio capability is usable too; Last.fm
        needs the server's API key too."""
        usable = self.services.usable(db, user["name"])
        return [s for s, on in (("all", True), ("lastfm", self.lastfm() and "lastfm" in usable), ("ytm", self.ytm and "songs" in usable.get("ytm", ())),
                                ("library", True), ("listenbrainz", "listenbrainz" in usable),
                                *((name, "radio" in usable.get(name, ())) for name in RADIOS)) if on]

    def stations(self, db, user) -> list[str]:
        """Stations from the user's Last.fm need the API key and the user's Last.fm, on and linked."""
        linked = user["lastfm_session"] and user["lastfm_name"] and self.services.uses(db, user["name"], "lastfm")
        return list(STATIONS) if self.lastfm() and linked else []

    def similar(self, db, seed, count: int, user, deep: bool = False, source: str = "all", wait: float | None = None,
                unplayable: bool = False) -> list[str]:
        """Track ids like the seed track for the user (their row) from one source, or from every one at hand ("all"),
        taking turns so local songs come throughout. The services need the network and the user to have them on; the
        library methods always give something. A source that gives nothing gives an empty list: no other stands in.
        The methods run at once, each on its own connection (sqlite connections stay in their thread), so the answer
        takes as long as the slowest. With wait, a method that hasn't answered within that many seconds is left out; those
        that look songs up on YouTube Music one by one give what they found by then. unplayable: songs no service has come
        too, as missing songs (missing.py)."""
        deadline = time.monotonic() + wait - 0.5 if wait else None  # half a second to keep what they found
        usable = self.services.usable(db, user["name"])
        ytm = self.ytm if "songs" in usable.get("ytm", ()) else None
        lastfm = self.lastfm() if "lastfm" in usable else None
        token = user["listenbrainz_token"] if "listenbrainz" in usable else None
        name = user["name"]
        radios = [s for s in RADIOS if "radio" in usable.get(s, ())]
        tidal = self.services.client(db, name, "tidal") if "tidal" in radios else None
        soundcloud = self.services.client(db, name, "soundcloud") if "soundcloud" in radios else None
        artist, _ = outside_names(seed)

        def asked(question: tuple, ask):
            """The songs a radio of another service names, found for the user (resolve)."""
            return lambda db: self.resolve(db, self.answer(question, ask)[:count], name, deadline, unplayable)
        methods = {
            "ytm": [("YouTube Music radio", lambda db: self.from_youtube(db, seed, count, ytm))],
            "lastfm": [("Last.fm similar tracks", lambda db: self.from_lastfm(db, seed, min(count, 15 if deep else 10), name, deadline, unplayable) if lastfm else []),
                       ("Last.fm similar artists", lambda db: self.from_similar_artists(db, seed, count) if lastfm else [])],
            "listenbrainz": [("ListenBrainz radio", lambda db: self.from_listenbrainz(db, seed, count, token, name, deadline, unplayable))],
            "deezer": [("Deezer artist radio", asked(("deezer", artist), lambda: Deezer().radio(artist)))] if "deezer" in radios else [],
            "tidal": [("Tidal track radio", asked(("tidal track", artist, seed["title"]), lambda: tidal.track_radio(artist, seed["title"]))),
                      ("Tidal artist radio", asked(("tidal artist", artist), lambda: tidal.artist_radio(artist)))] if tidal else [],
            "soundcloud": [("SoundCloud related tracks", asked(("soundcloud", soundcloud.variant, artist, seed["title"]),
                                                              lambda: soundcloud.related(artist, seed["title"])))] if soundcloud else [],
            "library": [("linked artists", lambda db: self.from_linked(db, seed, count)),
                        ("same artist", lambda db: self.from_artist(db, seed, count)),
                        ("played together", lambda db: self.from_plays(db, seed, count)),
                        ("same genre", lambda db: self.from_tags(db, seed, count))],
        }
        chosen = list(chain.from_iterable(methods.values())) if source == "all" else methods[source]
        if not chosen:
            return []

        def run(method):
            own = self.connect()
            try:
                return method(own)
            finally:
                own.close()

        pool = ThreadPoolExecutor(len(chosen))
        futures = [(name, pool.submit(run, method)) for name, method in chosen]
        pool.shutdown(wait=False)  # a method past its wait finishes in the background; its answer is dropped
        done, _ = wait_for([f for _, f in futures], timeout=wait)
        found = []
        for name, future in futures:
            if future not in done:
                log.info("%s took over %ss, left out", name, wait)
            elif future.exception():
                log.info("%s failed: %s", name, future.exception())
            else:
                found.append(future.result())
        picks = [id for id in chain.from_iterable(zip_longest(*found)) if id and id != seed["id"]]
        return list(dict.fromkeys(picks))[:count]

    def answer(self, question: tuple, ask):
        """ask(), the answer of a source on the network to question, kept for KEPT seconds. The radio that follows
        listening asks about each song played (top_up), so an instant mix of a song just played finds the answers here."""
        if kept := self.kept(question):
            return kept[0]
        found = ask()
        self.keep(question, found)
        return found

    def kept(self, question: tuple) -> tuple | None:
        """The answer kept for question, in a tuple; None when there is none."""
        kept = self.answers.get(question)
        return (kept[1],) if kept and kept[0] > time.monotonic() else None

    def keep(self, question: tuple, answer):
        now = time.monotonic()
        with self.answers_lock:
            self.answers = {q: a for q, a in self.answers.items() if a[0] > now}
            self.answers[question] = (now + KEPT, answer)

    def from_youtube(self, db, seed, count: int, ytm) -> list[str]:
        """ytm: YouTube Music's client, None when the user doesn't have it on."""
        if not ytm:
            return []
        video_id = video_of(db, seed, ytm)
        if not video_id:
            return []
        return [remember_song(db, song) for song in self.answer(("ytm", video_id, count), lambda: ytm.radio(video_id, count))]

    def from_similar_artists(self, db, seed, count: int) -> list[str]:
        """Library songs by the artists Last.fm calls similar, a few from each in turn."""
        artist, _ = outside_names(seed)
        found = self.answer(("lastfm artists", artist), lambda: lastfm.similar_artists(self.lastfm().artist_info(artist)))
        names = [name for name, _ in found]
        per_artist = [[r[0] for r in db.execute(
            f"select id from track where library = 1 and {BY_ARTIST_NAME} order by random() limit 3", (name, name))] for name in names]
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

    def from_lastfm(self, db, seed, count: int, user: str, deadline: float | None = None, unplayable: bool = False) -> list[str]:
        artist, _ = outside_names(seed)
        try:
            pairs = self.answer(("lastfm", artist, seed["title"]), lambda: self.lastfm().similar(artist, seed["title"], LASTFM_SIMILAR))[:count]
        except Exception as e:
            log.info("Last.fm similar failed: %s", e)
            return []
        return self.resolve(db, pairs, user, deadline, unplayable)

    def from_listenbrainz(self, db, seed, count: int, token: str | None, user: str, deadline: float | None = None,
                          unplayable: bool = False) -> list[str]:
        """LB Radio's songs for the seed's artist and those ListenBrainz finds similar. Its API is experimental and needs
        a user token: any failure is logged and gives nothing."""
        if not token:
            return []
        artist = seed["album_artist"] if seed["source"] == "ytm" else seed["artist"]
        try:
            pairs = self.answer(("listenbrainz", token, artist), lambda: lb_radio(token, artist))
        except Exception as e:
            log.info("ListenBrainz radio failed: %s", e)
            return []
        return self.resolve(db, pairs[:count], user, deadline, unplayable)

    def resolve(self, db, pairs: list[tuple], user: str, deadline: float | None = None, unplayable: bool = False) -> list[str]:
        """(artist, title) pairs, or (artist, title, MusicBrainz recording id), as track ids in their order: a library
        song, else a known streaming one of a service the user has on, else the song searched for on YouTube Music, then
        the user's other streaming services in turn, whose answer is kept too. Searches not done by deadline
        (time.monotonic) are left out. unplayable: a song every service was asked for and none has comes as a missing
        song (missing.py); otherwise it is left out."""
        ids, unknown = {}, []
        clients = self.services.active(db, user)
        ytm = self.ytm if self.services.uses(db, user, "ytm", "songs") else None
        playing = {"local", *(c.name for c in clients), *(["ytm"] if ytm else [])}  # the sources that play for this user
        for i, pair in enumerate(pairs):
            a, t = pair[0], pair[1]
            rows = db.execute(f"select id, source from track where title = ? collate nocase and {BY_ARTIST_NAME} "
                              "and (library = 1 or source not in ('local', 'missing')) order by library desc", (t, a, a)).fetchall()
            known = next((r for r in rows if r["source"] in playing), None)
            if known:
                ids[i] = known["id"]
            else:
                unknown.append(i)
        previews = self.services.previews(db, user) if unplayable else []
        searched = {}  # index: what the search found, None for nothing
        if unknown and (ytm or clients or previews):
            pool = ThreadPoolExecutor(FINDS)
            searches = {i: pool.submit(self.find, ytm, clients, *pairs[i][:2], previews, deadline) for i in unknown}
            done, _ = wait_for(searches.values(), timeout=deadline - time.monotonic() if deadline else None)
            pool.shutdown(wait=False, cancel_futures=True)
            for i, search in searches.items():
                if search in done and search.exception():
                    log.info("search for %s failed: %s", pairs[i][:2], search.exception())
                elif search in done:
                    searched[i] = search.result()
        elif unknown:
            searched = dict.fromkeys(unknown)
        for i, found in searched.items():
            pair = pairs[i]
            if found and found[0] == "ytm":
                ids[i] = remember_song(db, found[1])
            elif found and found[0] == "preview":
                ids[i] = missing.remember(db, pair[0], pair[1], pair[2] if len(pair) > 2 else None, preview=found[1])
            elif found:
                ids[i] = self.services.remember(db, *found)
            elif unplayable:
                ids[i] = missing.remember(db, pair[0], pair[1], pair[2] if len(pair) > 2 else None)
        return [ids[i] for i in sorted(ids)]

    def find(self, ytm, clients: list, artist: str, title: str, previews: list = (), deadline: float | None = None):
        """The best answer (rank) of the services asked, all at once: ("ytm", song) from YouTube Music (ytm, None to
        skip it), (client, song) from another streaming service, ("preview", "<service>:<its id>") from a preview
        service; None when none has the song. A full lossless song ends the wait. Otherwise, after the best answer so
        far came at t seconds, a better one is waited for till t + min(GRACE·t, GRACE_MOST), and never past deadline
        (time.monotonic). Each service's answer, a miss too, is kept with the time it took, and counts as coming then.
        OSError when every service asked for the whole song failed; TimeoutError when the deadline came first."""
        start, answers = time.monotonic(), queue.Queue()
        asks = [("YouTube Music", ("ytm", ""), True, lambda: ("ytm", found) if (found := ytm.find(artist, title)) else None)] if ytm else []
        asks += [(c.title, (c.name, getattr(c, "variant", "")), True, lambda c=c: (c, found) if (found := c.find(artist, title)) else None)
                 for c in clients]
        asks += [(f"{s.title} previews", ("preview", s.name), False,
                  lambda s=s: ("preview", f"{s.name}:{found}") if (found := s.find(artist, title)) else None) for s in previews]

        def answered(future, label: str, question: tuple, full: bool):
            took, error = time.monotonic() - start, future.exception()
            if error is None:
                self.keep(question, (future.result(), took))
            log.info("%s - %s: %s answered %s in %.2fs", artist, title, label, f"with an error ({error})" if error else said(future.result()), took)
            answers.put((full, None if error else future.result(), error, took))

        for label, service, full, ask in asks:
            question = ("find", *service, artist, title)
            if kept := self.kept(question):
                found, took = kept[0]
                log.info("%s - %s: %s answered %s in %.2fs before", artist, title, label, said(found), took)
                answers.put((full, found, None, took))
            else:
                self.asking.submit(ask).add_done_callback(lambda f, l=label, q=question, full=full: answered(f, l, q, full))
        best, pending, failed = None, len(asks), 0
        while pending:
            waits = [deadline - time.monotonic()] if deadline else []
            if best:
                waits.append(start + best[1] + min(GRACE * best[1], GRACE_MOST) - time.monotonic())
            try:
                full, found, error, took = answers.get(timeout=max(0.0, min(waits)) if waits else None)
            except queue.Empty:
                break
            pending -= 1
            failed += bool(error) and full
            if found and (best is None or rank(found) > rank(best[0])):
                best = (found, took)
                if rank(found)[0] == LOSSLESS_RANK:
                    break
        if failed and failed == sum(full for _, _, full, _ in asks):
            raise OSError("every service failed")
        if best:
            return best[0]
        if pending:
            raise TimeoutError("no service answered by the deadline")
        return None

    @staticmethod
    def wake(db):
        """Tops radios up now: a playlist was made or changed."""
        tasks.wake(db, JOB)

    def top_up_pass(self):
        db = self.connect()
        try:
            self.top_up_all(db)
        finally:
            db.close()

    def top_up_all(self, db):
        for playlist in db.execute("select * from playlist where radio != ?", (PLAIN,)).fetchall():
            try:  # one failing leaves the others be
                self.top_up(db, playlist)
            except Exception:
                log.exception("radio %s failed", playlist["name"])

    def played(self, db, owner: str, track_ids: list[str]):
        """Moves radios past the played tracks and makes sure the owner has a radio that follows all listening."""
        if not db.execute("select 1 from playlist where owner = ? and radio = ?", (owner, LISTENING_RADIO)).fetchone():
            playlists.create(db, owner, "Radio", LISTENING_RADIO, comment="Grows from what you play")
        for id in track_ids:
            db.execute("update playlist set played_pos = max(played_pos, (select max(pos) from playlist_entry e "
                       "where e.playlist_id = playlist.id and e.track_id = ?)) where radio != ? and owner = ? and exists "
                       "(select 1 from playlist_entry e where e.playlist_id = playlist.id and e.track_id = ?)", (id, PLAIN, owner, id))
        tasks.wake(db, JOB)

    def top_up(self, db, playlist):
        """Adds songs to a radio until it has AHEAD playable ones after the last played. Songs nothing plays come too
        when its owner sees those (user.missing_songs), but don't count."""
        rows = db.execute("select e.track_id, t.source from playlist_entry e left join track t on t.id = e.track_id "
                          "where e.playlist_id = ? order by e.pos", (playlist["id"],)).fetchall()
        entries = [r[0] for r in rows]
        played_pos = min(playlist["played_pos"], len(entries) - 1)
        playable = sum(1 for r in rows[played_pos + 1:] if r["source"] != "missing")
        needed = AHEAD - playable
        if needed <= 0:
            return
        owner = accounts.get(db, playlist["owner"])
        if not owner:
            return
        unplayable = bool(owner["missing_songs"])
        source = playlist["radio_source"] or owner["radio_source"]  # the automatic radio follows the user's default
        station = playlist["station"]
        if station:
            seeds = self.station_seeds(db, owner, station, playlist["period"] or PERIOD)
        else:
            if playlist["radio"] == LISTENING_RADIO:
                seed_ids = [r[0] for r in db.execute("select id from stat where user = ? and played is not null order by played desc limit 3", (owner["name"],))]
            else:
                heard = [e for e in entries if not e.startswith(IDS["missing"])]  # a song nothing can play was never heard
                seed_ids = [e for e in reversed(entries[:played_pos + 1]) if e in heard][:3] or heard[:3]
            seeds = [r for id in seed_ids if (r := db.execute("select * from track where id = ?", (id,)).fetchone())]
        skip = set(entries) | {r[0] for r in db.execute("select id from stat where user = ? and played is not null order by played desc limit ?", (owner["name"], RECENT))}
        added = []

        def add(id):
            if id not in skip:
                skip.add(id)
                added.append(id)

        def playable_added() -> int:
            return sum(1 for id in added if not id.startswith(IDS["missing"]))

        for seed in seeds:
            if station:  # the user's own song, then a few like it
                add(seed["id"])
                for id in [i for i in self.similar(db, seed, PER_SEED * 3, owner, source=source, unplayable=unplayable)
                           if i not in skip][:PER_SEED]:
                    add(id)
            else:
                for id in self.similar(db, seed, AHEAD + 20, owner, deep=True, source=source, unplayable=unplayable):
                    add(id)
            if playable_added() >= needed:
                break
        if not added:
            return
        added = keep_playable(added, max(needed, 10))
        entries += added
        dropped = max(0, len(entries) - KEEP)
        set_entries(db, playlist["id"], entries[dropped:])
        db.execute("update playlist set played_pos = ? where id = ?", (played_pos - dropped, playlist["id"]))
        log.info("radio %s: added %d tracks", playlist["name"], len(added))

    def station_seeds(self, db, user, station: str, period: str):
        """The user's Last.fm top or loved tracks in random order, as tracks of the library or YouTube Music, read one
        at a time as the station needs them."""
        if station not in self.stations(db, user):
            return
        pairs = self.lastfm().user_tracks(user["lastfm_name"], station == "lastfm-loved", period)
        random.shuffle(pairs)
        for pair in pairs:
            try:
                ids = self.resolve(db, [pair], user["name"])
            except Exception as e:
                log.info("station song %s failed: %s", pair, e)
                continue
            for id in ids:
                if row := db.execute("select * from track where id = ?", (id,)).fetchone():
                    yield row


LOSSLESS_RANK = 2


def rank(found: tuple) -> tuple:
    """How good a service's answer (Recommender.find) is, best highest: a full lossless song, then a full lossy song by
    bitrate (YouTube Music's winning a tie), then a preview."""
    kind, song = found
    if kind == "preview":
        return 0, 0, 0
    if kind == "ytm":
        return 1, BITRATE, 1
    return LOSSLESS_RANK if kind.suffix in LOSSLESS else 1, song.get("bitrate") or kind.bitrate, 0


def said(found: tuple | None) -> str:
    """A service's answer as a log line says it."""
    if found is None:
        return "nothing"
    if found[0] == "preview":
        return "a preview"
    return "the song" if found[0] == "ytm" else f"the song ({found[1].get('bitrate') or found[0].bitrate} kbit/s {found[0].suffix})"


def keep_playable(ids: list[str], count: int) -> list[str]:
    """ids up to the count-th playable one, with the missing songs among them."""
    kept, playable = [], 0
    for id in ids:
        if playable == count:
            break
        kept.append(id)
        playable += not id.startswith(IDS["missing"])
    return kept


def lb_radio(token: str, artist: str) -> list[tuple[str, str, str | None]]:
    """ListenBrainz's LB Radio for an artist and the artists it finds similar, popular songs first ("easy"), as (artist,
    title, MusicBrainz recording id)."""
    query = urllib.parse.urlencode({"prompt": f"artist:({artist.replace('(', ' ').replace(')', ' ').strip()})", "mode": "easy"})
    request = urllib.request.Request(f"{LISTENBRAINZ}explore/lb-radio?{query}", headers={"Authorization": "Token " + token, "User-Agent": "oasis/0.1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        playlist = json.load(response)["payload"]["jspf"].get("playlist") or {}
    return [(t["creator"], t["title"], recording_of(t)) for t in playlist.get("track") or [] if t.get("creator") and t.get("title")]


def recording_of(track: dict) -> str | None:
    """The MusicBrainz recording id of a JSPF track, from its identifier link."""
    identifiers = track.get("identifier") or []
    for link in [identifiers] if isinstance(identifiers, str) else identifiers:
        if "/recording/" in link:
            return link.rstrip("/").rsplit("/", 1)[-1]
    return None
