"""oasis's own catalogue: what users want, from MusicBrainz. A starred album resolves to its release group; a followed
artist's albums, EPs and singles are read daily, and repackages and singles whose songs are all on its albums left out.
An album is here when the library holds as many of its tracks as its smallest edition has. Missing albums are searched
(Prowlarr, then TorBox: download.py) one a minute, again and again, ever further apart; an artist with gaps gets a
weekly search for cached discographies and bundles, which can fill many at once. Anything MusicBrainz has can be
wanted, under ids of its own until it arrives (see adopt). want_album holds the albums; lidarr_item, named from when
Lidarr kept them, the stars and follows, under that name still so the code before can come back."""
import itertools
import json
import logging
import re
import threading
import time
import urllib.parse

from . import musicbrainz, releases
from .db import move_stat, resolve
from .importer import LOSSLESS
from .ytm import fold

log = logging.getLogger(__name__)

MB = "https://musicbrainz.org/ws/2/"
CATALOGUE = MB + "release-group?artist={}&type=album|ep|single&inc=artist-credits&limit=100&offset={}&fmt=json"
EDITIONS = MB + "release?release-group={}&status=official&inc=recordings&limit=100&fmt=json"
GROUP = MB + "release-group/{}?inc=artist-credits&fmt=json"
RELEASE = MB + "release/{}?inc=release-groups&fmt=json"
SEARCH = MB + "{}?query={}&limit=25&fmt=json"
KEEP_FIND = 7 * 86400                # the dashboard's and clients' searches
MB_IDS = {"artist": "ar-mb-", "album": "al-mb-"}  # ids of artists and release groups only MusicBrainz has (see adopt)
TYPES = ("Album", "EP", "Single")
VIDEO = re.compile(r"video|^dvd$|blu-ray|hd-dvd|vhs|^s?vcd$|laserdisc|umd", re.I)  # MusicBrainz's medium formats; DVD-Audio is audio
SECONDARY = ("Soundtrack",)          # secondary types wanted besides none, as Lidarr's metadata profile had them
RECATALOGUE = 86400                  # seconds between reads of a followed artist's catalogue: new releases show within a day
KEEP_CATALOGUE = RECATALOGUE - 3600  # so the daily read is fresh, and searches in between ask nothing
KEEP_EDITIONS = 30 * 86400
KEEP_GROUP = 30 * 86400
RESEARCH, RESEARCH_MOST = 6 * 3600, 7 * 86400       # a missing album is searched again after 6 h, then twice as long each time
SINGLE_RESEARCH, SINGLE_MOST = 7 * 86400, 28 * 86400
BUNDLE_AGAIN = 7 * 86400             # seconds between an artist's bundle searches
MAP_RELEASES = 20                    # files tagged with a release but no release group, looked up per pass
CLIENT_STATES = {"missing": "wanted", "downloading": "downloading", "have": "have"}  # lidarr_item's, which clients see
RETRY_NO_MATCH = 1800     # seconds before looking an unmatched name up again
RETRY_ERROR = 300         # seconds before retrying after MusicBrainz errors
SEARCH_GAP = 60           # seconds between searches, so a burst of stars doesn't trip TorBox's rate limit
RETRY_FULL = 600          # seconds before an album TorBox had no free slot for is searched again
UPGRADE_AGAIN = 7 * 86400  # seconds between searches for a lossless copy of an album here lossy
REPEATS = 0.6             # share of an album's songs already on the artist's earlier releases that makes it a repackage
REPACKAGE = re.compile(r"\b(best of|greatest hits|very best|the essential|anthology|collection|singles|remaster(ed)?|anniversary|"
                       r"deluxe|bonus ?tracks?|re-?recorded|reissue|remixes|instrumentals?|karaoke)\b", re.I)


def lucene(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def find(db, kind: str, text: str) -> list[dict]:
    """MusicBrainz artists (by any name or alias, in any script) or release groups named like text, best first."""
    words = re.sub(r'[-+&|!(){}\[\]^"~*?:\\/]', " ", text).casefold().split()
    if not words:
        return []
    path = "artist" if kind == "artist" else "release-group"
    return musicbrainz.get(SEARCH.format(path, urllib.parse.quote(" ".join(words))), db, KEEP_FIND).get(path + "s") or []


def release_groups(db, mbid: str) -> list[dict]:
    """All the artist's release groups."""
    found = []
    while True:
        page = musicbrainz.get(CATALOGUE.format(mbid, len(found)), db, KEEP_CATALOGUE)
        found += page.get("release-groups") or []
        if not page.get("release-groups") or len(found) >= page.get("release-group-count", 0):
            return found


def mbid_of(id: str) -> str | None:
    return next((id.removeprefix(p) for p in MB_IDS.values() if id.startswith(p)), None)


def library_id(db, id: str) -> str | None:
    """The library's id of a MusicBrainz-only artist or album that has arrived."""
    if id.startswith(MB_IDS["album"]):
        row = db.execute("select album_id from track where mb_release_group = ? and source = 'local' limit 1", (mbid_of(id),)).fetchone()
        return row and row[0]
    if id.startswith(MB_IDS["artist"]) and (row := db.execute("select artist_id from mb_artist where mbid = ?", (mbid_of(id),)).fetchone()):
        own = resolve(db, row[0])
        return own if db.execute("select 1 from track where artist_id = ? and source = 'local'", (own,)).fetchone() else None
    return None


def refs(db, kind: str, mbid: str) -> set[str]:
    """Every id the artist or release group may be wanted under."""
    return {MB_IDS[kind] + mbid, *(r[0] for r in db.execute("select ref from lidarr_item where kind = ? and mbid = ?", (kind, mbid))),
            *filter(None, [library_id(db, MB_IDS[kind] + mbid)])}


def adopt(db):
    """A MusicBrainz-only artist or album that arrived in the library takes its library id: stars, wants and follows
    move there, and the old id keeps working through moved, as clients keep ids."""
    for (old,) in db.execute("select ref from wanted where ref like 'a_-mb-%' union select ref from lidarr_item where ref like 'a_-mb-%' "
                             "union select id from stat where id like 'a_-mb-%'").fetchall():
        if new := library_id(db, old):
            for table in ("wanted", "lidarr_item"):
                db.execute(f"update or ignore {table} set ref = ? where ref = ?", (new, old))
                db.execute(f"delete from {table} where ref = ?", (old,))
            move_stat(db, old, new)
            db.execute("insert or replace into moved values (?, ?)", (old, new))


class Acquire:
    def __init__(self, connect, downloads, secondary_types=SECONDARY, bundles=False):
        """downloads: download.py's. bundles: also search for cached discographies and bundles."""
        self.connect, self.downloads, self.allowed, self.bundles = connect, downloads, set(secondary_types), bundles
        self.wake = threading.Event()
        self.last_search = 0.0
        self.soon: set[str] = set()  # release groups to search before any other (the dashboard's "search now")

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        while True:
            try:
                self.reconcile()
            except Exception:
                log.exception("acquire pass failed")
            self.wake.wait(60)
            self.wake.clear()

    def reconcile(self):
        """One pass: stars and follows resolved or let go, one artist's catalogue read, states updated, one search."""
        db = self.connect()
        try:
            adopt(db)
            for kind, ref in db.execute("select distinct kind, ref from wanted").fetchall():
                db.execute("insert or ignore into lidarr_item(kind, ref, state, updated) values (?, ?, 'wanted', ?)", (kind, ref, time.time()))
            for item in db.execute("select * from lidarr_item").fetchall():
                wanted = db.execute("select 1 from wanted where kind = ? and ref = ?", (item["kind"], item["ref"])).fetchone()
                if wanted and (item["retry_at"] or 0) > time.time():
                    continue
                try:
                    if not wanted:
                        self.release(db, item)
                        db.execute("delete from lidarr_item where kind = ? and ref = ?", (item["kind"], item["ref"]))
                    else:
                        set_state(db, item, *(self.want(db, item) if item["kind"] == "album" else self.follow(db, item)))
                except LookupError as e:
                    set_state(db, item, "failed", str(e), retry=RETRY_NO_MATCH)
                except Exception as e:  # MusicBrainz down, timeouts: soon again
                    log.warning("%s %s: %s", item["kind"], item["ref"], e)
                    set_state(db, item, item["state"] if item["mbid"] else "wanted", str(e)[:300], retry=RETRY_ERROR)
            # One catalogue a pass: a first read of many artists takes MusicBrainz minutes, and searches go on meanwhile.
            due = db.execute("select * from lidarr_item where kind = 'artist' and mbid is not null and coalesce(catalogued, 0) < ? "
                             "and coalesce(retry_at, 0) <= ? and exists (select 1 from wanted w where w.kind = 'artist' and w.ref = lidarr_item.ref) "
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
            db.execute("update lidarr_item set mbid = ? where kind = ? and ref = ?", (rg, item["kind"], item["ref"]))
        row = db.execute("select * from want_album where rg = ?", (rg,)).fetchone()
        if row is None or row["reason"] != "starred":
            group = musicbrainz.get(GROUP.format(rg), db, KEEP_GROUP)
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
            db.execute("update lidarr_item set mbid = ? where kind = ? and ref = ?", (mbid, item["kind"], item["ref"]))
        busy = db.execute("select 1 from want_album where artist_mbid = ? and state = 'downloading'", (mbid,)).fetchone()
        return ("downloading" if busy else "followed"), None

    def find_artist(self, db, item) -> str:
        """The MusicBrainz id: its own, known from the library's tags, else by name."""
        if item["ref"].startswith(MB_IDS["artist"]):
            return mbid_of(item["ref"])
        if known := db.execute("select mbid from mb_artist where artist_id = ? limit 1", (item["ref"],)).fetchone():
            return known[0]
        row = db.execute("select album_artist from track where artist_id = ? limit 1", (item["ref"],)).fetchone() or \
            db.execute("select name from ytm_artist where artist_id = ?", (item["ref"],)).fetchone()
        if row is None:
            raise LookupError("not in oasis any more")
        if found := next((m for c in artist_names(row[0]) for m in self.search_artist(db, c)), None):
            return found
        raise LookupError(f"no MusicBrainz artist named {row[0]}")

    def find_album(self, db, item) -> str:
        """The release group: its own, from the library's tags, else by title among the artist's (known from tags, else by name)."""
        if item["ref"].startswith(MB_IDS["album"]):
            return mbid_of(item["ref"])
        if known := db.execute("select mb_release_group from track where album_id = ? and coalesce(mb_release_group, '') != '' limit 1",
                               (item["ref"],)).fetchone():
            return known[0]
        row = db.execute("select album_artist name, album title, artist_id from track where album_id = ? limit 1", (item["ref"],)).fetchone() or \
            db.execute("select a.name, r.title, r.artist_id from ytm_release r join ytm_artist a on a.artist_id = r.artist_id "
                       "where r.album_id = ?", (item["ref"],)).fetchone()
        if row is None:
            raise LookupError("not in oasis any more")
        known = [r[0] for r in db.execute("select mbid from mb_artist where artist_id = ?", (row["artist_id"],))]
        for mbid in itertools.chain(known, (m for c in artist_names(row["name"]) for m in self.search_artist(db, c))):
            groups = musicbrainz.get(SEARCH.format("release-group", urllib.parse.quote(f"releasegroup:{lucene(row['title'])} AND arid:{mbid}")))
            if fold(row["title"]) and (match := next((g for g in groups.get("release-groups") or [] if fold(g["title"]) == fold(row["title"])), None)):
                return match["id"]
        raise LookupError(f"no MusicBrainz album {row['title']!r} by {row['name']}")

    def search_artist(self, db, name: str) -> list[str]:
        """Artists named so, by name or alias: exact matches only, case, accents and punctuation aside."""
        if not fold(name):
            return []
        found = musicbrainz.get(SEARCH.format("artist", urllib.parse.quote(lucene(name)))).get("artists") or []
        return [a["id"] for a in found if fold(name) in {fold(a["name"]), *(fold(x["name"]) for x in a.get("aliases") or [])}]

    def release(self, db, item):
        """Nobody wants it any more: oasis stops looking for it. Files stay."""
        if not item["mbid"]:
            return
        if item["kind"] == "artist":
            db.execute("delete from want_album where artist_mbid = ? and reason = 'followed'", (item["mbid"],))
        elif row := db.execute("select artist_mbid from want_album where rg = ? and reason = 'starred'", (item["mbid"],)).fetchone():
            db.execute("delete from want_album where rg = ?", (item["mbid"],))
            db.execute("update lidarr_item set catalogued = null where kind = 'artist' and mbid = ?", (row[0],))  # back, if followed

    def browse(self, db, mbid: str) -> list[dict]:
        """The artist's albums, EPs and singles with an allowed secondary type (or none)."""
        return [g for g in release_groups(db, mbid) if g.get("primary-type") in TYPES and set(g.get("secondary-types") or []) <= self.allowed]

    def edition(self, db, rg: str) -> dict:
        """The smallest official edition's track count and song keys, and the most discs of any; tracks 0 without one."""
        if row := db.execute("select * from mb_group where rg = ? and fetched > ?", (rg, time.time() - KEEP_EDITIONS)).fetchone():
            return {"tracks": row["tracks"], "discs": row["discs"], "songs": set(json.loads(row["songs"]))}
        # Video discs aren't the album: a DVD-only live edition would be its smallest.
        found = [media for r in musicbrainz.get(EDITIONS.format(rg)).get("releases") or []
                 if (media := [m for m in r.get("media") or [] if not VIDEO.search(m.get("format") or "")])]
        count = lambda media: sum(len(m.get("tracks") or []) for m in media)
        smallest = min((r for r in found if count(r)), key=count, default=None)
        songs = {song_key(t["title"]) for m in smallest for t in m.get("tracks") or []} if smallest else set()
        edition = {"tracks": count(smallest) if smallest else 0, "discs": max(map(len, found), default=1), "songs": songs}
        db.execute("insert or replace into mb_group values (?, ?, ?, ?, ?)", (rg, edition["tracks"], edition["discs"], json.dumps(sorted(songs)), time.time()))
        return edition

    def catalogue(self, db, item):
        """Reads the followed artist's catalogue into want_album. Left out: repackages (best-ofs, deluxe and anniversary
        editions, re-recordings, albums most of whose songs are on its earlier ones), singles whose songs are all on its
        albums or EPs, what has no official release and what a user gave up on. Starred albums, albums with files and
        albums and EPs Lidarr looked for never are, given up or not."""
        groups = sorted(self.browse(db, item["mbid"]), key=lambda g: g.get("first-release-date") or "9999")
        editions = {g["id"]: self.edition(db, g["id"]) for g in groups}
        starred = {r[0] for r in db.execute("select mbid from lidarr_item where kind = 'album' and mbid is not null")}
        lidarrs = {r[0] for r in db.execute("select mbid from catalogue_search where mbid is not null")}  # Lidarr's, carried over once
        kept = lambda g: g["id"] not in why or g["id"] in starred or self.have_files(db, g["id"]) or \
            g["primary-type"] != "Single" and g["id"] in lidarrs
        seen, why = set(), {}
        for g in (g for g in groups if g["primary-type"] != "Single"):
            songs = editions[g["id"]]["songs"]
            if not editions[g["id"]]["tracks"]:
                why[g["id"]] = "no official release"
            elif REPACKAGE.search(g["title"]):
                why[g["id"]] = "a repackage"
            elif songs and len(songs & seen) >= REPEATS * len(songs):
                why[g["id"]] = "most of its songs are on earlier releases"
            seen |= songs
        on_albums = set().union(*(editions[g["id"]]["songs"] for g in groups if g["primary-type"] != "Single" and kept(g)))
        for g in (g for g in groups if g["primary-type"] == "Single"):
            if not editions[g["id"]]["tracks"]:
                why[g["id"]] = "no official release"
            elif editions[g["id"]]["songs"] <= on_albums:
                why[g["id"]] = "its songs are on the artist's albums"
        name = (db.execute("select name from mb_artist where mbid = ?", (item["mbid"],)).fetchone() or [None])[0] or \
            next((c["artist"]["name"] for g in groups for c in g.get("artist-credit") or [] if c["artist"]["id"] == item["mbid"]), None)
        given_up = {r[0] for r in db.execute("select rg from given_up")} - starred
        why.update({rg: "given up" for rg in given_up})
        skipped = {g["id"] for g in groups if not kept(g) or g["id"] in given_up}
        for g in groups:
            self.put(db, g, item["mbid"], name, "starred" if g["id"] in starred else "followed", why[g["id"]] if g["id"] in skipped else None)
        listed = {g["id"] for g in groups}
        db.executemany("delete from want_album where rg = ?", [(rg,) for (rg,) in db.execute(
            "select rg from want_album where artist_mbid = ? and reason = 'followed'", (item["mbid"],)).fetchall() if rg not in listed])
        db.execute("update lidarr_item set catalogued = ?, error = null, retry_at = null where kind = 'artist' and ref = ?", (time.time(), item["ref"]))
        log.info("catalogue of %s: %d release groups, %d left out", name or item["mbid"], len(groups), len(skipped))

    def have_files(self, db, rg: str) -> bool:
        return bool(db.execute("select 1 from track where mb_release_group = ? and source = 'local'", (rg,)).fetchone())

    def put(self, db, group: dict, artist_mbid: str, artist: str | None, reason: str, skip: str | None = None):
        """Adds or updates a release group's row. A new one carries over Lidarr's last search of it."""
        self.edition(db, group["id"])
        searched = db.execute("select max(searched) from (select searched from catalogue_search where mbid = ? union all "
                              "select searched from lidarr_item where kind = 'album' and mbid = ?)", (group["id"], group["id"])).fetchone()[0]
        db.execute("insert into want_album(rg, artist_mbid, artist, title, primary_type, secondary_types, first_date, reason, state, error, "
                   "searched, next_search, tries) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) on conflict(rg) do update set "
                   "artist_mbid = excluded.artist_mbid, artist = coalesce(excluded.artist, want_album.artist), title = excluded.title, "
                   "primary_type = excluded.primary_type, secondary_types = excluded.secondary_types, first_date = excluded.first_date, "
                   "reason = excluded.reason, state = case when excluded.state = 'skipped' then 'skipped' "
                   "when want_album.state = 'skipped' then 'missing' else want_album.state end, "
                   "error = case when excluded.state = 'skipped' then excluded.error when want_album.state = 'skipped' then null else want_album.error end",
                   (group["id"], artist_mbid, artist, group["title"], group.get("primary-type"), "; ".join(group.get("secondary-types") or []),
                    group.get("first-release-date"), reason, "skipped" if skip else "missing", skip, searched,
                    searched + RESEARCH if searched else None, int(bool(searched))))

    def map_releases(self, db):
        """Files tagged with a release but not its group get the group, from MusicBrainz, once."""
        for (release,) in db.execute("select distinct mb_release from track where source = 'local' and coalesce(mb_release, '') != '' "
                                     "and mb_release_group is null limit ?", (MAP_RELEASES,)).fetchall():
            try:
                group = musicbrainz.get(RELEASE.format(release))["release-group"]["id"]
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
                              "where t.mb_release_group = w.rg and t.source = 'local') n from want_album w left join mb_group g on g.rg = w.rg "
                              "where w.state != 'skipped'").fetchall():
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
        """One search, SEARCH_GAP apart: albums asked for on the dashboard first, then starred albums, then followed artists' albums and EPs, then their singles;
        each time those never searched first, then the newest. Then albums here only lossy, for a lossless copy weekly."""
        if time.time() - self.last_search < SEARCH_GAP:
            return
        if self.soon and (row := db.execute("select * from want_album where rg = ? and state = 'missing'", (self.soon.pop(),)).fetchone()):
            return self.search(db, row)
        if self.bundles and self.bundle_next(db):
            return
        now, queued = time.time(), self.downloads.groups(db)
        lossless = ", ".join(f"'{s}'" for s in LOSSLESS)
        rows = db.execute("select * from want_album w where (state = 'missing' and coalesce(next_search, 0) <= ?) or (state = 'have' and "
                          "coalesce(searched, 0) < ? and not exists (select 1 from track t where t.mb_release_group = w.rg and t.source = 'local' "
                          f"and t.suffix in ({lossless}))) order by state = 'have', reason != 'starred', primary_type = 'Single', "
                          "searched is not null, first_date desc limit 50", (now, now - UPGRADE_AGAIN)).fetchall()
        if row := next((r for r in rows if r["rg"] not in queued), None):
            self.search(db, row)

    def bundle_next(self, db) -> bool:
        """A bundle search instead of an album search, for an artist with two albums or EPs missing or one a search
        found nothing for, weekly at most. True when one ran."""
        row = db.execute("select artist_mbid, artist from want_album where state = 'missing' and primary_type != 'Single' and artist is not null "
                         "and artist_mbid not in (select artist_mbid from bundle_search where searched > ?) group by artist_mbid "
                         "having count(*) >= 2 or max(searched) is not null order by min(reason != 'starred') limit 1",
                         (time.time() - BUNDLE_AGAIN,)).fetchone()
        if row is None:
            return False
        mbid, sent = row["artist_mbid"], None
        try:
            groups, queued = self.browse(db, mbid), self.downloads.groups(db)
            wanted = {rg for (rg,) in db.execute("select rg from want_album where artist_mbid = ? and state = 'missing'", (mbid,)) if rg not in queued}
            titles = [g["title"] for g in groups if g["primary-type"] != "Single"]
            names = releases.aliases(db, row["artist"], mbid, titles)
            sent = self.downloads.bundle(db, {"artist": row["artist"], "names": names, "native": releases.native(names), "albums": titles},
                                         groups, wanted)
        except (OSError, LookupError) as e:
            log.warning("bundle search for %s failed: %s", row["artist"], e)
        self.last_search = now = time.time()
        # TorBox had no free slot: soon again.
        db.execute("insert or replace into bundle_search values (?, ?)", (mbid, now - BUNDLE_AGAIN + RETRY_FULL if sent is False else now))
        return True

    def search(self, db, row):
        """Never raises: a search that fails still counts, so the next one moves on to another album."""
        error = "searched, no release found yet"
        try:
            sent = self.downloads.find(db, self.target(db, row), row["rg"])
        except (OSError, LookupError) as e:
            log.warning("search for %s - %s failed: %s", row["artist"], row["title"], e)
            sent, error = None, f"search failed: {e}"[:300]
        self.last_search = now = time.time()
        if sent is False:  # TorBox had no free slot
            db.execute("update want_album set next_search = ? where rg = ?", (now + RETRY_FULL, row["rg"]))
            return
        first, most = (SINGLE_RESEARCH, SINGLE_MOST) if row["primary_type"] == "Single" else (RESEARCH, RESEARCH_MOST)
        db.execute("update want_album set searched = ?, tries = tries + 1, next_search = ?, error = ? where rg = ?",
                   (now, now + min(first * 2 ** row["tries"], most), None if sent else error, row["rg"]))

    def target(self, db, row) -> dict:
        """What releases.find looks for: the artist under all its names, the title, year, discs, the artist's other
        albums and EPs (a name holding one is a bundle), and whether the album is here lossy already (then only lossless will do)."""
        try:
            others = [g["title"] for g in self.browse(db, row["artist_mbid"]) if g["primary-type"] != "Single" and g["id"] != row["rg"]]
        except (OSError, LookupError):
            others = [r[0] for r in db.execute("select title from want_album where artist_mbid = ? and primary_type != 'Single' and rg != ?",
                                               (row["artist_mbid"], row["rg"]))]
        names = releases.aliases(db, row["artist"], row["artist_mbid"], [row["title"], *others])
        discs = (db.execute("select discs from mb_group where rg = ?", (row["rg"],)).fetchone() or [1])[0]
        return {"artist": row["artist"], "names": names, "title": row["title"], "year": (row["first_date"] or "")[:4],
                "native": releases.native(names), "discs": discs, "others": others, "upgrade": row["state"] == "have"}

    def still_to_find(self, db) -> int:
        return db.execute("select count(*) from want_album where state = 'missing'").fetchone()[0]


def artist_names(credit: str) -> list[str]:
    """Names to look a credit up by: the credit itself, then each artist of a collaboration, with and without
    a bracketed suffix: 'SawanoHiroyuki[nZk]:mizuki' -> SawanoHiroyuki[nZk]:mizuki, SawanoHiroyuki[nZk], SawanoHiroyuki, mizuki."""
    names = [credit]
    for part in re.split(r"\s*(?:,|&|:|×|/|\bfeat\.?|\bft\.?|\bwith\b)\s*", credit, flags=re.I):
        names += [part, re.sub(r"\s*[\[(][^\])]*[\])]\s*$", "", part)]
    return [n for n in dict.fromkeys(n.strip() for n in names) if n]


def song_key(title: str) -> str:
    """'Du hast (Remastered 2017)', 'Du Hast - Live' -> 'duhast'."""
    return fold(re.sub(r"\s+-\s+.*$", "", re.sub(r"\s*[(\[][^)\]]*[)\]]", "", title)))


def set_state(db, item, state: str, error: str | None, retry: float | None = None):
    db.execute("update lidarr_item set state = ?, error = ?, retry_at = ?, updated = ? where kind = ? and ref = ?",
               (state, error, time.time() + retry if retry else None, time.time(), item["kind"], item["ref"]))
