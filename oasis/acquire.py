"""oasis's own catalogue: what users want, from MusicBrainz. A starred album resolves to its release group; a followed
artist's albums, EPs and singles are read daily, and guest appearances, repackages and singles whose songs are all on its
albums left out.
An album is here when the library holds as many of its tracks as its smallest edition has. Missing albums are searched
(Prowlarr, then TorBox: download.py) one a minute, or 15 s after a search that found nothing, again and again, ever
further apart; an artist with gaps gets a weekly search for discographies, singles collections and bundles, which can
fill many at once: one a pass while they hold albums the others don't, before its albums' own searches. Its EPs,
singles, live, demo and compilation releases, likelier in bundles than on their own, wait for that search. Anything
MusicBrainz has can be wanted, under ids of its own until it arrives (see adopt). want_album holds the albums;
want_item the stars and follows."""
import itertools
import json
import logging
import re
import threading
import time
from collections import Counter

from . import musicbrainz, releases, worker
from .wants import Outcome
from .formats import LOSSLESS
from .musicbrainz import lucene, search_artist
from .text import fold, song_key
from .tracks import MB_IDS, album_name_of, artist_name_of, keep_old_id, library_id, mb_only, mbid_of, move
from .wants import DUE, SEARCH_FAILED, STILL_WANTED

log = logging.getLogger(__name__)

RECATALOGUE = 86400                  # seconds between reads of a followed artist's catalogue: new releases show within a day
KEEP_EDITIONS = 30 * 86400           # seconds a release group's edition (mb_group) stays
RESEARCH, RESEARCH_MOST = 6 * 3600, 7 * 86400       # a missing album is searched again after 6 h, then twice as long each time
SINGLE_RESEARCH, SINGLE_MOST = 7 * 86400, 28 * 86400
BUNDLE_AGAIN = 7 * 86400             # seconds between an artist's bundle searches
# Releases far likelier found in an artist's bundles than on their own (fan-club and live EPs, singles, demos, B-sides
# and rarities): searched on their own only after their artist's bundle search.
BUNDLE_FIRST = "(primary_type in ('EP', 'Single') or secondary_types like '%Live%' or secondary_types like '%Demo%' or secondary_types like '%Compilation%')"
# SQL: a BUNDLE_FIRST album still waiting for its artist's bundle search, given the time of the last one that counts.
DEFERRED = f"({BUNDLE_FIRST} and artist is not null and artist_mbid not in (select artist_mbid from bundle_search where searched > ?))"
# Nothing due and TorBox has room: a search ahead of schedule every EARLY_GAP, leaving backoffs as they are. An artist's
# bundles at most daily, an album (or a lossless copy) every RESEARCH, a single daily.
EARLY_GAP, BUNDLE_EARLY, SINGLE_EARLY = 5 * 60, 86400, 86400
MAP_RELEASES = 20                    # files tagged with a release but no release group, looked up per pass
# want_album's states: missing, downloading, have; what a catalogue leaves out, or a user gave up, has no row. want_item's, which
# clients see: an album's, by its want_album row's; an artist's, followed or downloading; failed when MusicBrainz can't
# tell which it is.
CLIENT_STATES = {"missing": "wanted", "downloading": "downloading", "have": "have"}
BADGES = ("wanted", "downloading", "failed")  # want_item's states shown on clients' albums and artists; arrived and followed need none
GIVEN_UP = "given up"  # why a catalogue leaves out an album a user gave up on
RETRY_NO_MATCH = 1800     # seconds before looking an unmatched name up again
RETRY_ERROR = 300         # seconds before retrying after MusicBrainz or Prowlarr errors
SEARCH_GAP = 60           # seconds between searches, so a burst of stars doesn't trip TorBox's rate limit
MISS_GAP = 15             # seconds after a search that asked TorBox to add nothing: only Prowlarr had work
RETRY_FULL = 600          # seconds before an album TorBox had no free slot for is searched again
UPGRADE_AGAIN = 7 * 86400  # seconds between searches for a lossless copy of an album here lossy
# A lossless track by its file: ALAC shares .m4a with AAC, which stops at 512 kbps, and ALAC runs well above it.
LOSSLESS_TRACK = "(t.suffix in (" + ", ".join(f"'{s}'" for s in LOSSLESS) + ") or t.suffix = 'm4a' and t.bitrate > 512)"
REPEATS = 0.6             # share of an album's songs already on the artist's earlier releases that makes it a repackage
GUEST = re.compile(r"feat|ft\.|\bwith\b", re.I)  # joinphrases after which artists are guests: 'Beyoncé feat. Jay-Z'
REPACKAGE = re.compile(rf"\b({releases.REISSUES}|collection|remaster(ed)?|bonus ?tracks?|re-?recorded|reimagined|remixes)\b", re.I)
# A single that is another version of its song, said in a bracket or after a dash: 'Song (Acoustic)', 'Song - Live',
# not 'Live and Let Die'.
VERSION = re.compile(r"(\([^)]*|\[[^\]]*|\s[-‐–—]\s.*)\b(re-?recorded|reimagined|acoustic|remix(es)?|live|instrumentals?)\b", re.I)


def other_titles(db, mbid: str) -> dict[str, list[str]]:
    """Each of the artist's release groups' other whole titles in MusicBrainz, as releases.other_titles keeps them:
    its aliases ('To Us Former Prodigies') and its pseudo-releases' titles ('Ensemble Play' for アンサンブル・プレイ).
    Search hints aren't titles."""
    groups = musicbrainz.release_groups(mbid, db)
    found = {g["id"]: musicbrainz.aliases(g) for g in groups}
    for r in musicbrainz.pseudo_releases(mbid, db):
        found.get((r.get("release-group") or {}).get("id"), []).append(r["title"])
    titles = {g["id"]: g["title"] for g in groups}
    return {rg: kept for rg, names in found.items() if (kept := releases.other_titles(titles[rg], names, [t for o, t in titles.items() if o != rg]))}


def refs(db, kind: str, mbid: str) -> set[str]:
    """Every id the artist or release group may be wanted under."""
    return {MB_IDS[kind] + mbid, *(r[0] for r in db.execute("select ref from want_item where kind = ? and mbid = ?", (kind, mbid))),
            *filter(None, [library_id(db, MB_IDS[kind] + mbid)])}


def adopt(db):
    """A MusicBrainz-only artist or album that arrived in the library takes its library id: stars, wants and follows
    move there, and the old id keeps working through moved, as clients keep ids."""
    for (old,) in db.execute(f"select ref from wanted where {mb_only('ref')} union select ref from want_item where {mb_only('ref')} "
                             f"union select id from stat where {mb_only('id')}").fetchall():
        if new := library_id(db, old):
            move(db, "artist" if old.startswith(MB_IDS["artist"]) else "album", old, new)
            keep_old_id(db, old, new)


def want(db, user: str, kind: str, ids: list[str], on: bool):
    """The user wants the albums or follows the artists (kind) under these ids, or no longer (on false). The next pass
    (Acquire.wake) acts on it."""
    for id in ids:
        if on:
            db.execute("insert or ignore into wanted values (?, ?, ?)", (user, kind, id))
        else:
            db.execute("delete from wanted where user = ? and kind = ? and ref = ?", (user, kind, id))


def want_again(db, rg: str):
    """The album searched for soon: its want_album row if the catalogue or a star has one, else wanted for the first
    admin, under its MusicBrainz id. A given-up album is wanted again. No star: the user didn't ask for it."""
    if db.execute("select 1 from want_album where rg = ?", (rg,)).fetchone():
        db.execute("update want_album set state = 'missing', next_search = null where rg = ?", (rg,))
        return
    user = db.execute("select name from user order by admin desc, created limit 1").fetchone()
    if user is None:
        log.warning("nobody to want %s for", rg)
        return
    ref = MB_IDS["album"] + rg
    want(db, user[0], "album", [ref], True)
    db.execute("insert or ignore into want_item(kind, ref, state, mbid, updated) values ('album', ?, 'wanted', ?, ?)", (ref, rg, time.time()))


def give_up(db, rg: str):
    """No more searches for the album: its followed artist's catalogue leaves it out from now on. Stars of it stay:
    Oasis.give_up takes back the user's too."""
    db.execute("insert or replace into given_up values (?, ?)", (rg, time.time()))
    db.execute("delete from want_album where rg = ? and reason = 'followed'", (rg,))


def wanted_by(db, kind: str, mbid: str, user: str | None = None) -> list[str]:
    """The ids the user (or anyone, for None) wants the album or follows the artist (kind) under."""
    ids = refs(db, kind, mbid)
    sql = f"select distinct ref from wanted where kind = ? and ref in ({','.join('?' * len(ids))})"
    if user is None:
        return [r[0] for r in db.execute(sql, (kind, *ids))]
    return [r[0] for r in db.execute(sql + " and user = ?", (kind, *ids, user))]


def followed_refs(db) -> set[str]:
    """Every id an artist is followed under."""
    return {r[0] for r in db.execute("select ref from wanted where kind = 'artist'")}


def album(db, rg: str):
    """The album's want_album row, None when oasis doesn't look for it."""
    return db.execute("select * from want_album where rg = ?", (rg,)).fetchone()


def wanted_albums(db, artist: str | None = None, kind: str | None = None, text: str | None = None) -> list:
    """The albums still wanted (still_wanted), of the artist (MusicBrainz id), of the primary type (kind), with text in
    their title or artist: starred first, then by artist, albums, EPs, the rest, oldest first."""
    where, params = [STILL_WANTED], []
    if artist:
        where.append("artist_mbid = ?"), params.append(artist)
    if kind:
        where.append("primary_type = ?"), params.append(kind)
    if text:
        where.append("(title like ? or artist like ?)"), params.extend([f"%{text}%"] * 2)
    return db.execute(f"select * from want_album where {' and '.join(where)} order by reason != 'starred', artist collate nocase, "
                      "primary_type != 'Album', primary_type != 'EP', first_date", params).fetchall()


def wanted_artists(db) -> list:
    """The artists with albums still wanted: artist_mbid, artist (a name), n (how many) and rg (one of them), by name."""
    return db.execute(f"select artist_mbid, min(artist) artist, count(*) n, min(rg) rg from want_album where {STILL_WANTED} "
                      "group by artist_mbid order by artist collate nocase").fetchall()


def counts(db) -> dict[str, int]:
    """How many albums are in each state."""
    return dict(db.execute("select state, count(*) from want_album group by state").fetchall())


def untried(db) -> int:
    """How many missing albums were never searched."""
    return db.execute("select count(*) from want_album where state = 'missing' and searched is null").fetchone()[0]


def unresolved(db, user: str) -> list:
    """Stars and follows still wanted that MusicBrainz can't tell which it is (want_item rows), each with mine:
    whether the user's own."""
    return db.execute("select wi.*, exists (select 1 from wanted w where w.kind = wi.kind and w.ref = wi.ref and w.user = ?) mine "
                      "from want_item wi where wi.state = 'failed' and exists (select 1 from wanted w where w.kind = wi.kind "
                      "and w.ref = wi.ref)", (user,)).fetchall()


class Acquire:
    def __init__(self, connect, downloads, settings):
        """downloads: download.py's. settings: config.Acquisition."""
        self.connect, self.downloads, self.allowed, self.bundles = connect, downloads, set(settings.secondary_types), settings.bundles
        self.woken = threading.Event()
        self.last_search, self.gap = 0.0, SEARCH_GAP
        self.soon: set[str] = set()  # release groups to search before any other (the dashboard's "search now")
        self.idle = False  # nothing was due at the last pass: searching early

    def start(self):
        worker.start("acquire pass", self.step, 60, self.woken)

    def step(self) -> float:
        """A pass, then a wait until the next search is due, at most a minute."""
        self.reconcile()
        return self.gap if time.time() - self.last_search < self.gap else 60

    def wake(self):
        """A pass now: what users want changed."""
        self.woken.set()

    def search_soon(self, rg: str):
        """The album is searched before any other, at a pass now."""
        self.soon.add(rg)
        self.woken.set()

    def searching_soon(self, rg: str) -> bool:
        return rg in self.soon

    def to_find(self, db) -> list[dict]:
        """The albums still to find, albums and EPs, then singles: of each (single), how many (count), how many were
        searched and wait for their next try (waiting), the soonest of those (next), and how many wait for their
        artist's bundle search (deferred); the rest are due."""
        now, found = time.time(), []
        for single in (False, True):
            n, waiting, first, held = db.execute(
                "select count(*), coalesce(sum(next_search > ?), 0), min(case when next_search > ? then next_search end), "
                f"coalesce(sum(coalesce(next_search, 0) <= ? and {DEFERRED}), 0) from want_album where state = 'missing' and (primary_type is 'Single') = ?",
                (now, now, now, now - BUNDLE_AGAIN, single)).fetchone()
            found.append({"single": single, "count": n, "waiting": waiting, "next": first, "deferred": held if self.bundles else 0})
        return found

    def reconcile(self):
        """One pass: stars and follows resolved or let go, one artist's catalogue read, states updated, one search."""
        db = self.connect()
        try:
            adopt(db)
            for kind, ref in db.execute("select distinct kind, ref from wanted").fetchall():
                db.execute("insert or ignore into want_item(kind, ref, state, updated) values (?, ?, 'wanted', ?)", (kind, ref, time.time()))
            for item in db.execute("select * from want_item").fetchall():
                wanted = db.execute("select 1 from wanted where kind = ? and ref = ?", (item["kind"], item["ref"])).fetchone()
                if wanted and (item["retry_at"] or 0) > time.time():
                    continue
                try:
                    if not wanted:
                        self.release(db, item)
                        db.execute("delete from want_item where kind = ? and ref = ?", (item["kind"], item["ref"]))
                    else:
                        set_state(db, item, *(self.want(db, item) if item["kind"] == "album" else self.follow(db, item)))
                except LookupError as e:
                    set_state(db, item, "failed", str(e), retry=RETRY_NO_MATCH)
                except Exception as e:  # MusicBrainz down, timeouts, an answer of an unexpected shape: soon again
                    if isinstance(e, OSError):
                        log.warning("%s %s: %s", item["kind"], item["ref"], e)
                    else:
                        log.exception("%s %s", item["kind"], item["ref"])
                    set_state(db, item, item["state"] if item["mbid"] else "wanted", str(e)[:300], retry=RETRY_ERROR)
            # One catalogue a pass: a first read of many artists takes MusicBrainz minutes, and searches go on meanwhile.
            due = db.execute("select * from want_item where kind = 'artist' and mbid is not null and coalesce(catalogued, 0) < ? "
                             "and coalesce(retry_at, 0) <= ? and exists (select 1 from wanted w where w.kind = 'artist' and w.ref = want_item.ref) "
                             "order by catalogued is not null, catalogued limit 1", (time.time() - RECATALOGUE, time.time())).fetchone()
            if due:
                try:
                    self.catalogue(db, due)
                except (OSError, LookupError) as e:
                    log.warning("catalogue of %s: %s", due["mbid"], e)
                    set_state(db, due, due["state"], str(e)[:300], retry=RETRY_ERROR)
            self.map_releases(db)
            self.update_states(db)
            if self.room():
                self.search_next(db)
        finally:
            db.close()

    def want(self, db, item) -> tuple[str, str | None]:
        rg = item["mbid"]
        if not rg:
            rg = self.find_album(db, item)
            db.execute("update want_item set mbid = ? where kind = ? and ref = ?", (rg, item["kind"], item["ref"]))
        row = db.execute("select * from want_album where rg = ?", (rg,)).fetchone()
        if row is None or row["reason"] != "starred":
            group = musicbrainz.release_group(rg, db)
            credit = group["artist-credit"][0]
            self.put(db, group, credit["artist"]["id"], credit["name"], "starred")
            db.execute("delete from given_up where rg = ?", (rg,))
            self.update_states(db)
            row = db.execute("select * from want_album where rg = ?", (rg,)).fetchone()
        return CLIENT_STATES[row["state"]], row["error"] or (None if row["searched"] or row["state"] != "missing" else "waiting for its search")

    def follow(self, db, item) -> tuple[str, str | None]:
        mbid = item["mbid"]
        if not mbid:
            mbid = self.find_artist(db, item)
            db.execute("update want_item set mbid = ? where kind = ? and ref = ?", (mbid, item["kind"], item["ref"]))
        busy = db.execute("select 1 from want_album where artist_mbid = ? and state = 'downloading'", (mbid,)).fetchone()
        return ("downloading" if busy else "followed"), None

    def find_artist(self, db, item) -> str:
        """The MusicBrainz id: its own, known from the library's tags, else by name."""
        if item["ref"].startswith(MB_IDS["artist"]):
            return mbid_of(item["ref"])
        if known := db.execute("select mbid from mb_artist where artist_id = ? limit 1", (item["ref"],)).fetchone():
            return known[0]
        name = artist_name_of(db, item["ref"])
        if name is None:
            raise LookupError("not in oasis any more")
        if found := next((m for c in artist_names(name) for m in search_artist(c)), None):
            return found
        raise LookupError(f"no MusicBrainz artist named {name}")

    def find_album(self, db, item) -> str:
        """The release group: its own, from the library's tags, else by title among the artist's (known from tags, else by name)."""
        if item["ref"].startswith(MB_IDS["album"]):
            return mbid_of(item["ref"])
        if known := db.execute("select mb_release_group from track where album_id = ? and coalesce(mb_release_group, '') != '' limit 1",
                               (item["ref"],)).fetchone():
            return known[0]
        row = album_name_of(db, item["ref"])
        if row is None:
            raise LookupError("not in oasis any more")
        known = [r[0] for r in db.execute("select mbid from mb_artist where artist_id = ?", (row["artist_id"],))]
        for mbid in itertools.chain(known, (m for c in artist_names(row["name"]) for m in search_artist(c))):
            groups = musicbrainz.search("release-group", f"releasegroup:{lucene(row['title'])} AND arid:{mbid}")
            if fold(row["title"]) and (match := next((g for g in groups if fold(g["title"]) == fold(row["title"])), None)):
                return match["id"]
        raise LookupError(f"no MusicBrainz album {row['title']!r} by {row['name']}")

    def release(self, db, item):
        """Nobody wants it any more: oasis stops looking for it. Files stay."""
        if not item["mbid"]:
            return
        if item["kind"] == "artist":
            db.execute("delete from want_album where artist_mbid = ? and reason = 'followed'", (item["mbid"],))
        elif row := db.execute("select artist_mbid from want_album where rg = ? and reason = 'starred'", (item["mbid"],)).fetchone():
            db.execute("delete from want_album where rg = ?", (item["mbid"],))
            db.execute("update want_item set catalogued = null where kind = 'artist' and mbid = ?", (row[0],))  # back, if followed

    def browse(self, db, mbid: str) -> list[dict]:
        """The artist's albums, EPs and singles with an allowed secondary type (or none), oldest first."""
        return [g for g in musicbrainz.by_date(musicbrainz.release_groups(mbid, db)) if set(g.get("secondary-types") or []) <= self.allowed]

    def edition(self, db, rg: str) -> dict:
        """The smallest official edition's track count and song keys, and the most discs of any; tracks 0 without one."""
        if row := db.execute("select * from mb_group where rg = ? and fetched > ?", (rg, time.time() - KEEP_EDITIONS)).fetchone():
            return {"tracks": row["tracks"], "discs": row["discs"], "songs": set(json.loads(row["songs"]))}
        # Video discs aren't the album: a DVD-only live edition would be its smallest.
        found = [media for r in musicbrainz.official_editions(rg)
                 if (media := [m for m in r.get("media") or [] if not musicbrainz.VIDEO_MEDIA.search(m.get("format") or "")])]
        count = lambda media: sum(len(m.get("tracks") or []) for m in media)
        smallest = min((r for r in found if count(r)), key=count, default=None)
        songs = {song_key(t["title"]) for m in smallest for t in m.get("tracks") or []} if smallest else set()
        edition = {"tracks": count(smallest) if smallest else 0, "discs": max(map(len, found), default=1), "songs": songs}
        db.execute("insert or replace into mb_group values (?, ?, ?, ?, ?)", (rg, edition["tracks"], edition["discs"], json.dumps(sorted(songs)), time.time()))
        return edition

    def catalogue(self, db, item):
        """Reads the followed artist's catalogue into want_album. Left out: guest appearances (see guest), repackages
        (best-ofs, deluxe and anniversary editions, re-recordings, albums most of whose songs are on its earlier ones),
        singles whose song is on its albums or EPs (B-sides and versions don't make one wanted), singles that are
        another version or a re-release, what has no official release and what a user gave up on.
        Starred albums and albums with files never are, given up or not, and nor are albums and EPs Lidarr looked for
        unless the artist is only a guest on them."""
        groups = self.browse(db, item["mbid"])
        editions = {g["id"]: self.edition(db, g["id"]) for g in groups}
        starred = {r[0] for r in db.execute("select mbid from want_item where kind = 'album' and mbid is not null")}
        looked_for = {r[0] for r in db.execute("select mbid from catalogue_search where mbid is not null")}  # Lidarr's, carried over once
        why = {g["id"]: "a guest appearance" for g in groups if guest(g, item["mbid"])}
        guests, seen = set(why), set()
        kept = lambda g: g["id"] not in why or g["id"] in starred or self.have_files(db, g["id"]) or \
            g["primary-type"] != "Single" and g["id"] in looked_for and g["id"] not in guests
        for g in (g for g in groups if g["primary-type"] != "Single" and g["id"] not in guests):
            songs = editions[g["id"]]["songs"]
            if not editions[g["id"]]["tracks"]:
                why[g["id"]] = "no official release"
            elif REPACKAGE.search(g["title"]):
                why[g["id"]] = "a repackage"
            elif songs and len(songs & seen) >= REPEATS * len(songs):
                why[g["id"]] = "most of its songs are on earlier releases"
            seen |= songs
        on_albums = set().union(*(editions[g["id"]]["songs"] for g in groups if g["primary-type"] != "Single" and kept(g)))
        for g in (g for g in groups if g["primary-type"] == "Single" and g["id"] not in guests):
            if not editions[g["id"]]["tracks"]:
                why[g["id"]] = "no official release"
            elif editions[g["id"]]["songs"] <= on_albums:
                why[g["id"]] = "its songs are on the artist's albums"
            elif all(song_key(part) in on_albums for part in re.split(r"\s+/\s+", g["title"])):  # 'A / B': double A-sides
                why[g["id"]] = "its song is on the artist's albums"
            elif REPACKAGE.search(g["title"]) or VERSION.search(g["title"]):
                why[g["id"]] = "another version or a re-release"
        name = (db.execute("select name from mb_artist where mbid = ?", (item["mbid"],)).fetchone() or [None])[0] or \
            next((c["artist"]["name"] for g in groups for c in g.get("artist-credit") or [] if c["artist"]["id"] == item["mbid"]), None)
        given_up = {r[0] for r in db.execute("select rg from given_up")} - starred
        why.update({rg: GIVEN_UP for rg in given_up})
        skipped = {g["id"] for g in groups if not kept(g) or g["id"] in given_up}
        for g in (g for g in groups if g["id"] not in skipped):
            self.put(db, g, item["mbid"], name, "starred" if g["id"] in starred else "followed")
        listed = {g["id"] for g in groups} - skipped  # what's left out isn't wanted: no row
        db.executemany("delete from want_album where rg = ?", [(rg,) for (rg,) in db.execute(
            "select rg from want_album where artist_mbid = ? and reason = 'followed'", (item["mbid"],)).fetchall() if rg not in listed])
        db.execute("update want_item set catalogued = ?, error = null, retry_at = null where kind = 'artist' and ref = ?", (time.time(), item["ref"]))
        log.info("catalogue of %s: %d release groups, %d left out (%s)", name or item["mbid"], len(groups), len(skipped),
                 ", ".join(f"{n} {r}" for r, n in sorted(Counter(why[rg] for rg in skipped).items(), key=lambda x: (-x[1], x[0]))) or "none")

    def have_files(self, db, rg: str) -> bool:
        return bool(db.execute("select 1 from track where mb_release_group = ? and source = 'local'", (rg,)).fetchone())

    def put(self, db, group: dict, artist_mbid: str, artist: str | None, reason: str):
        """Adds or updates a release group's row. A new one carries over Lidarr's last search of it."""
        self.edition(db, group["id"])
        searched = db.execute("select max(searched) from (select searched from catalogue_search where mbid = ? union all "
                              "select searched from want_item where kind = 'album' and mbid = ?)", (group["id"], group["id"])).fetchone()[0]
        db.execute("insert into want_album(rg, artist_mbid, artist, title, primary_type, secondary_types, first_date, reason, state, error, "
                   "searched, next_search, tries) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) on conflict(rg) do update set "
                   "artist_mbid = excluded.artist_mbid, artist = coalesce(excluded.artist, want_album.artist), title = excluded.title, "
                   "primary_type = excluded.primary_type, secondary_types = excluded.secondary_types, first_date = excluded.first_date, "
                   "reason = excluded.reason",
                   (group["id"], artist_mbid, artist, group["title"], group.get("primary-type"), "; ".join(group.get("secondary-types") or []),
                    group.get("first-release-date"), reason, "missing", None, searched,
                    searched + RESEARCH if searched else None, int(bool(searched))))

    def map_releases(self, db):
        """Files tagged with a release but not its group get the group, from MusicBrainz, once."""
        for (release,) in db.execute("select distinct mb_release from track where source = 'local' and coalesce(mb_release, '') != '' "
                                     "and mb_release_group is null limit ?", (MAP_RELEASES,)).fetchall():
            try:
                group = musicbrainz.release(release)["release-group"]["id"]
            except LookupError:
                group = ""  # MusicBrainz doesn't have it: not asked again
            except OSError as e:
                log.info("release group of %s: %s", release, e)
                return
            db.execute("update track set mb_release_group = ? where mb_release = ? and mb_release_group is null", (group, release))

    def update_states(self, db):
        """have: the library holds as many tracks of the release group as its smallest edition has; downloading: oasis is."""
        active = self.downloads.groups(db)
        for row in db.execute("select w.rg, w.state, coalesce(g.tracks, 0) tracks, (select count(distinct coalesce(t.mb_track, t.id)) from track t "
                              "where t.mb_release_group = w.rg and t.source = 'local') n from want_album w left join mb_group g on g.rg = w.rg").fetchall():
            state = "downloading" if row["rg"] in active else "have" if row["n"] and row["n"] >= row["tracks"] else "missing"
            if state != row["state"]:
                db.execute("update want_album set state = ? where rg = ?", (state, row["rg"]))

    def room(self) -> bool:
        try:
            return self.downloads.room()
        except OSError as e:
            log.warning("TorBox: %s", e)
            return False

    def search_next(self, db):
        """One search, a gap apart (see searched): albums asked for on the dashboard first; then an artist's bundles
        (bundle_next); then albums never searched, then those searched before (a dead or slow release's album waits
        behind every untried one), each by starred albums, followed artists' albums and EPs, their singles, newest
        first; BUNDLE_FIRST ones once their artist's bundles are. Then albums here only lossy, for a lossless copy weekly."""
        if time.time() - self.last_search < self.gap:
            return
        if self.soon and (row := db.execute("select * from want_album where rg = ? and state = 'missing'", (self.soon.pop(),)).fetchone()):
            return self.search(db, row)
        if self.bundles and self.bundle_next(db):
            return
        now, queued = time.time(), self.downloads.groups(db)
        bundled = f"and not {DEFERRED}"
        rows = db.execute(f"select * from want_album w where ({DUE} {bundled if self.bundles else ''}) or (state = 'have' and "
                          "coalesce(searched, 0) < ? and not exists (select 1 from track t where t.mb_release_group = w.rg and t.source = 'local' "
                          f"and {LOSSLESS_TRACK})) order by state = 'have', searched is not null, reason != 'starred', primary_type = 'Single', "
                          "first_date desc limit 50", (now, *[now - BUNDLE_AGAIN] * self.bundles, now - UPGRADE_AGAIN)).fetchall()
        if row := next((r for r in rows if r["rg"] not in queued), None):
            if self.idle:
                log.info("searches due again")
            self.idle = False
            return self.search(db, row)
        self.early(db, queued)

    def early(self, db, queued):
        """Nothing due while TorBox has room: one search ahead of schedule every EARLY_GAP, bundles first, then lossless
        copies, then the albums whose next try is soonest. Their backoff stays: only searched moves."""
        now = time.time()
        if not self.idle:
            first = db.execute("select min(next_search) from want_album where state = 'missing'").fetchone()[0]
            log.info("nothing due; next search at %s; searching early every %d min meanwhile",
                     time.strftime("%H:%M", time.localtime(first)) if first else "none", EARLY_GAP // 60)
            self.idle = True
        if now - self.last_search < EARLY_GAP or self.bundles and self.bundle_next(db, BUNDLE_EARLY):
            return
        rows = db.execute("select * from want_album w where (state = 'have' and coalesce(searched, 0) < ? and not exists (select 1 from track t "
                          f"where t.mb_release_group = w.rg and t.source = 'local' and {LOSSLESS_TRACK})) or (state = 'missing' and "
                          "coalesce(searched, 0) < case when primary_type = 'Single' then ? else ? end) "
                          "order by state = 'missing', next_search limit 50", (now - RESEARCH, now - SINGLE_EARLY, now - RESEARCH)).fetchall()
        if row := next((r for r in rows if r["rg"] not in queued), None):
            self.search(db, row, early=True)

    def bundle_next(self, db, again: float = BUNDLE_AGAIN) -> bool:
        """A bundle search instead of an album search, for an artist with two albums, EPs or singles missing, one a
        search found nothing for or one BUNDLE_FIRST, weekly at most; but again at the next pass after one that sent a
        bundle, for one of the rest (see Downloads.bundle), till none holds any. True when one ran."""
        row = db.execute("select artist_mbid, artist from want_album where state = 'missing' and artist is not null "
                         "and artist_mbid not in (select artist_mbid from bundle_search where searched > ?) group by artist_mbid "
                         f"having count(*) >= 2 or max(searched) is not null or max({BUNDLE_FIRST}) order by min(reason != 'starred') limit 1",
                         (time.time() - again,)).fetchone()
        if row is None:
            return False
        mbid, sent, failed = row["artist_mbid"], Outcome.NOTHING_SENT, None
        try:
            alternatives, queued = other_titles(db, mbid), self.downloads.groups(db)
            wanted = {rg for (rg,) in db.execute("select rg from want_album where artist_mbid = ? and state = 'missing'", (mbid,)) if rg not in queued}
            kept = {g["id"] for g in self.browse(db, mbid)} | wanted  # starred ones of other types too
            groups = [{**g, "other_titles": alternatives.get(g["id"], [])} for g in musicbrainz.release_groups(mbid, db) if g["id"] in kept]
            titles = [g["title"] for g in groups if g["primary-type"] != "Single"]
            names = releases.aliases(db, row["artist"], mbid, titles)
            sent = self.downloads.bundle(db, {"artist": row["artist"], "names": names, "native": releases.native(names), "albums": titles},
                                         groups, wanted)
        except (OSError, LookupError) as e:
            log.warning("bundle search for %s failed: %s", row["artist"], e)
            failed = e
        now = self.searched(sent, failed)
        # One sent: the next pass, for the rest. TorBox had no free slot, or Prowlarr or MusicBrainz failed: soon again.
        if sent is Outcome.SENT:
            searched = now - BUNDLE_AGAIN
        elif sent is Outcome.NO_SLOT:
            searched = now - BUNDLE_AGAIN + RETRY_FULL
        elif isinstance(failed, OSError):
            searched = now - BUNDLE_AGAIN + RETRY_ERROR
        else:
            searched = now
        db.execute("insert or replace into bundle_search values (?, ?)", (mbid, searched))
        return True

    def search(self, db, row, early=False):
        """Never raises: a search Prowlarr or MusicBrainz couldn't answer (OSError) is due again after RETRY_ERROR, not
        counted as a try; the next one moves on to another album meanwhile. An early one leaves the album's backoff
        (tries, next_search) as it was."""
        error, failed = "searched, no release found yet", None
        try:
            sent = self.downloads.find(db, self.target(db, row), row["rg"])
        except (OSError, LookupError) as e:
            log.warning("search for %s - %s failed: %s", row["artist"], row["title"], e)
            sent, error, failed = Outcome.NOTHING_SENT, f"{SEARCH_FAILED}{e}"[:300], e
        now = self.searched(sent, failed)
        if sent is Outcome.SENT:
            error = None
        if early:
            db.execute("update want_album set searched = ?, error = ? where rg = ?", (now, error, row["rg"]))
            return
        if isinstance(failed, OSError):  # unreachable, or no indexer to ask: not the album's fault
            db.execute("update want_album set next_search = ?, error = ? where rg = ?", (now + RETRY_ERROR, error, row["rg"]))
            return
        if sent is Outcome.NO_SLOT:
            db.execute("update want_album set next_search = ? where rg = ?", (now + RETRY_FULL, row["rg"]))
            return
        first, most = (SINGLE_RESEARCH, SINGLE_MOST) if row["primary_type"] == "Single" else (RESEARCH, RESEARCH_MOST)
        db.execute("update want_album set searched = ?, tries = tries + 1, next_search = ?, error = ? where rg = ?",
                   (now, now + min(first * 2 ** row["tries"], most), error, row["rg"]))

    def searched(self, sent, failed: Exception | None) -> float:
        """Sets the gap to the next search: SEARCH_GAP after one that asked TorBox to add a torrent (or failed), for its
        rate limit; MISS_GAP after one that found nothing to add. Returns the time."""
        self.last_search = now = time.time()
        self.gap = MISS_GAP if sent is Outcome.NOTHING_SENT and not failed else SEARCH_GAP
        return now

    def target(self, db, row) -> dict:
        """What releases.find looks for: the artist under all its names, the title and its other titles, year, discs, the
        artist's other albums and EPs (a name holding one is a bundle), and whether the album is here lossy already
        (then only lossless will do)."""
        try:
            groups = [(g["title"], g.get("primary-type"), g.get("first-release-date")) for g in self.browse(db, row["artist_mbid"]) if g["id"] != row["rg"]]
        except (OSError, LookupError):
            groups = db.execute("select title, primary_type, first_date from want_album where artist_mbid = ? and rg != ?", (row["artist_mbid"], row["rg"])).fetchall()
        others = [t for t, kind, _ in groups if kind != "Single"]
        same = releases.squashed(row["title"])
        namesakes = [{"type": kind, "year": (date or "")[:4]} for t, kind, date in groups if releases.squashed(t) == same]
        names = releases.aliases(db, row["artist"], row["artist_mbid"], [row["title"], *others])
        discs = (db.execute("select discs from mb_group where rg = ?", (row["rg"],)).fetchone() or [1])[0]
        try:
            alternatives = other_titles(db, row["artist_mbid"]).get(row["rg"], [])
        except (OSError, LookupError) as e:
            log.info("other titles of %s: %s", row["title"], e)
            alternatives = []
        return {"artist": row["artist"], "names": names, "title": row["title"], "other_titles": alternatives, "year": (row["first_date"] or "")[:4],
                "native": releases.native(names), "discs": discs, "others": others, "upgrade": row["state"] == "have",
                "type": row["primary_type"], "namesakes": namesakes}


def artist_names(credit: str) -> list[str]:
    """Names to look a credit up by: the credit itself, then each artist of a collaboration, with and without
    a bracketed suffix: 'SawanoHiroyuki[nZk]:mizuki' -> SawanoHiroyuki[nZk]:mizuki, SawanoHiroyuki[nZk], SawanoHiroyuki, mizuki."""
    names = [credit]
    for part in re.split(r"\s*(?:,|&|:|×|/|\bfeat\.?|\bft\.?|\bwith\b)\s*", credit, flags=re.I):
        names += [part, re.sub(r"\s*[\[(][^\])]*[\])]\s*$", "", part)]
    return [n for n in dict.fromkeys(n.strip() for n in names) if n]


def guest(group: dict, mbid: str) -> bool:
    """Whether the artist is only a guest on the release group: credited after a 'feat.', 'ft.' or 'with'
    ('Beyoncé feat. Jay-Z'), not as a main or joint artist ('Jay-Z & Kanye West', 'Jay-Z featuring Gwen Dickey')."""
    for credit in group.get("artist-credit") or []:
        if credit["artist"]["id"] == mbid:
            return False
        if GUEST.search(credit.get("joinphrase") or ""):
            return True
    return False


def set_state(db, item, state: str, error: str | None, retry: float | None = None):
    db.execute("update want_item set state = ?, error = ?, retry_at = ?, updated = ? where kind = ? and ref = ?",
               (state, error, time.time() + retry if retry else None, time.time(), item["kind"], item["ref"]))
