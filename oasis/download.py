"""oasis's own downloads: the release a search picks goes to TorBox, which downloads the torrent on its servers; oasis
fetches the files over HTTP into its staging folder and places them itself (importer.py). rdtclient isn't involved,
so TorBox's slots and queue are read directly, files keep their paths inside the torrent, and oasis keeps its own
record of which hash is which album. A bundle serves several albums: only their files are fetched, each album into
<hash>/<release group>/, and each is imported on its own (download_target). A bundle whose files TorBox doesn't list
yet (uncached, or cached as one zip) holds the albums it likely holds till it does (resolve); one whose music is packed
is fetched whole and unpacked here first (unpacked). TorBox downloads a torrent whole, but hands out its files one by
one. Nothing seeds from here. A bundle imported is remembered with all it holds (bundle_kept): an album searched
later, one starred since too, comes from it again while TorBox has it cached, before any Prowlarr search."""
import hashlib
import json
import logging
import os
import posixpath
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

from . import releases
from .importer import ARCHIVES, AUDIO, UNPACK_ROOM, UNPACKED, NoSpace, editions, unpack
from .quality import LossySource
from .releases import hash_of
from .torbox import RateLimited

log = logging.getLogger(__name__)

INTERVAL = 60            # seconds between polls of TorBox
SLOWEST = 12 * 3600     # seconds a download may take at its pace so far
WARM_UP = 30 * 60        # seconds a download runs before its pace counts: finding peers takes a while
PATIENT = 30 * 60        # seconds those waits last while TorBox has slots free: a dead torrent keeps its album from another release
EMPTY_SWARM = 10 * 60    # seconds a torrent may go without progress, seeders and a full copy among its peers
DEAD_AFTER = 20 * 60     # seconds a torrent may sit at 0% without seeders
UNRELIABLE = 5, 1 / 3    # an indexer with this many downloads, this share of them dead, is tried last
KEEP = (*AUDIO, *ARCHIVES, "cue", "jpg", "jpeg", "png", "webp", "gif")  # music (perhaps packed), cue sheets and cover art; logs, nfo and the rest stay on TorBox
PACE = 3                 # seconds between file download requests: bursts of them get TorBox's 429
PARALLEL = 2             # files fetched at once
PAUSE = 300              # seconds without file downloads after a 429
FETCH_TRIES = 5          # polls in a row a file may fail to download before the release counts as broken
KEEP_FAILED = 2 * 86400  # seconds a failed download's files stay for inspection
KEPT_UNSEEN = 90 * 86400 # seconds a remembered bundle stays unused; TorBox's cache check when one is used is its real expiry
GRAB_TRIES = 5           # picks tried in turn when TorBox won't take one
FRESH = 3600             # seconds Prowlarr's answers to an artist's bundle searches and the .torrents read stay: its bundles go a pass apart


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def source(release: dict) -> dict:
    """What TorBox gets: the magnet link, else the .torrent through Prowlarr (which may answer with a magnet link
    instead), else a magnet link of the bare hash. Prowlarr's own search gives its proxy for the magnet link too.
    Proxy URLs carry Prowlarr's key: errors name only the path."""
    if (release.get("magnetUrl") or "").startswith("magnet:"):
        return {"magnet": release["magnetUrl"]}
    if url := release.get("magnetUrl") or release.get("downloadUrl"):
        try:
            with urllib.request.build_opener(NoRedirect).open(url, timeout=60) as response:
                return {"torrent": response.read()}
        except urllib.error.HTTPError as e:
            if (location := e.headers.get("Location") or "").startswith("magnet:"):
                return {"magnet": location}
            if not hash_of(release):
                raise OSError(f"{url.split('?')[0]}: {e.code}") from None
        except OSError:
            if not hash_of(release):
                raise OSError(f"{url.split('?')[0]}: unreachable") from None
    return {"magnet": f"magnet:?xt=urn:btih:{hash_of(release)}"}


def info_hash(found: dict) -> str:
    """The hash of what source() found: from the magnet link, or the .torrent's info dictionary."""
    if m := re.search(r"btih:([0-9a-f]{40})", found.get("magnet") or "", re.I):
        return m[1].lower()
    data, i = found.get("torrent") or b"", 1
    try:
        while data[:1] == b"d" and data[i:i + 1] != b"e":
            key_end = bencoded_end(data, i)
            value_end = bencoded_end(data, key_end)
            if data[i:key_end] == b"4:info":
                return hashlib.sha1(data[key_end:value_end]).hexdigest()
            i = value_end
    except (ValueError, IndexError):  # not a .torrent
        pass
    return ""


def bencoded_end(data: bytes, i: int) -> int:
    """Where the bencoded value starting at i ends."""
    kind = data[i:i + 1]
    if kind == b"i":
        return data.index(b"e", i) + 1
    if kind in (b"l", b"d"):
        i += 1
        while data[i:i + 1] != b"e":
            i = bencoded_end(data, i)
        return i + 1
    colon = data.index(b":", i)
    return colon + 1 + int(data[i:colon])


def bdecoded(data: bytes, i: int = 0):
    """The bencoded value starting at i: ints, bytes, lists and dicts with bytes keys."""
    kind, end = data[i:i + 1], bencoded_end(data, i)
    if kind == b"i":
        return int(data[i + 1:end - 1])
    if kind in (b"l", b"d"):
        items, i = [], i + 1
        while i < end - 1:
            items.append(bdecoded(data, i))
            i = bencoded_end(data, i)
        return dict(zip(items[::2], items[1::2])) if kind == b"d" else items
    return data[data.index(b":", i) + 1:end]


def torrent_files(data: bytes) -> list[dict]:
    """A .torrent's files as TorBox lists them, {name, size}, the torrent's name their first folder; [] when it isn't one."""
    try:
        info = bdecoded(data)[b"info"]
        text = lambda key, d: (d.get(key + b".utf-8") or d[key]).decode(errors="replace")
        name = text(b"name", info)
        if b"files" not in info:
            return [{"name": name, "size": info[b"length"]}]
        return [{"name": "/".join([name, *(p.decode(errors="replace") for p in f.get(b"path.utf-8") or f[b"path"])]), "size": f[b"length"]}
                for f in info[b"files"]]
    except (ValueError, IndexError, KeyError, TypeError, AttributeError):
        return []


def archives(files: list[dict]) -> list[dict]:
    return [f for f in files if f["name"].rsplit(".", 1)[-1].lower() in ARCHIVES]


def inside(path: str) -> Path:
    """A path inside the torrent, kept below the staging folder."""
    return Path(*[p for p in PurePosixPath(path).parts if p not in ("/", "..", ".")])


class Downloads:
    """Sends searches' picks to TorBox and walks each download through added -> downloading -> fetching ->
    importing -> done, or failed. The state is in the download table, so a restart resumes where it was."""

    def __init__(self, connect, torbox, importer, prowlarr, downloads: str, max_active: int = 8, stall_hours: float = 1, imported=lambda: None):
        """torbox: makes the TorBox client (the doctor's, with the key rdtclient holds). importer: importer.py's.
        imported: called after each import."""
        self.connect, self.torbox_for, self.own, self.prowlarr, self.folder, self.imported = connect, torbox, importer, prowlarr, Path(downloads), imported
        self.max_active, self.stall = max_active, stall_hours * 3600
        self.client = None
        self.seen = {}       # hash -> (progress, since when)
        self.failures = {}   # hash -> polls in a row its files failed to download
        self.copying = {}    # hash -> [(path, size)]: the files being fetched here, for copied()
        self.crowded = False  # TorBox's slots are all taken and albums wait: see step
        self.added = 0.0     # when send last asked TorBox to add a torrent, for acquire's gap between searches
        self.pace, self.last_request, self.paused_until = threading.Lock(), 0.0, 0.0
        self.kept = {}       # (kind, key) -> (when, value): see recall

    def torbox(self):
        self.client = self.client or self.torbox_for()
        return self.client

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        while True:
            try:
                self.cycle()
            except Exception:  # TorBox down: next poll
                log.exception("downloads poll failed")
            time.sleep(INTERVAL)

    def room(self) -> bool:
        """Whether another download may start. Every active torrent of the account takes one of the plan's slots,
        anime's and seeding ones too; max_active of them leaves the rest for anime."""
        return sum(bool(t["active"]) for t in self.torbox().torrents()) < self.max_active

    def groups(self, db, but: str = "") -> set[str]:
        """The release groups being downloaded, a bundle's expected ones too; but: of downloads other than that one."""
        return {r[0] for r in db.execute("select album_mbid from download where state not in ('done', 'failed') and album_mbid is not null and hash != ? union "
                                         "select t.rg from download_target t join download d on d.hash = t.hash where d.state not in ('done', 'failed') "
                                         "and d.hash != ?", (but, but))}

    def recall(self, kind: str, key: str, make):
        """make(), kept FRESH: an artist's bundle searches run a pass apart while they send one bundle each."""
        now = time.time()
        self.kept = {k: v for k, v in self.kept.items() if now - v[0] < FRESH}
        if (kind, key) not in self.kept:
            self.kept[kind, key] = now, make()
        return self.kept[kind, key][1]

    def torrent(self, release: dict) -> dict | None:
        """The hash and files of a release's .torrent, through Prowlarr's proxy; files [] when that gives a magnet link.
        None when it fails."""
        def read():
            try:
                found = source({k: v for k, v in release.items() if k != "magnetUrl"})
            except OSError as e:
                log.info("no .torrent for %s: %s", release["title"], e)
                return None
            return {"hash": info_hash(found), "files": torrent_files(found.get("torrent") or b"")}
        return self.recall("torrent", release.get("guid") or release["downloadUrl"], read)

    def find(self, db, want: dict, group: str) -> bool | None:
        """Searches Prowlarr for an album of oasis's own catalogue (acquire.py), after the bundles oasis has had (remembered);
        see send."""
        if not want["upgrade"] and (sent := self.remembered(db, {group}, f"{want['artist']} - {want['title']}")) is not None:
            return sent
        blocked = self.blocked(db)
        return self.send(db, releases.find(self.prowlarr, want, blocked, self.torbox().cached, self.unreliable(db), self.slow(db)), blocked, group,
                         f"{want['artist']} - {want['title']}")

    def bundle(self, db, want: dict, groups: list[dict], wanted: set[str]) -> bool | None:
        """Sends the artist's bundle that holds the most wanted albums (release group ids) to TorBox: a cached one,
        to fetch only theirs, else while slots are free an uncached one with seeders; see releases.bundles. One a
        search: its albums count as downloading, so the next search (acquire.py) sends the bundle that holds the most
        of the rest, until none holds any; their albums' own searches come after. A bundle whose files TorBox doesn't
        list yet is resolved once it does; see send."""
        if (sent := self.remembered(db, wanted, want["artist"])) is not None:
            return sent
        blocked = self.blocked(db) | {r[0] for r in db.execute("select hash from download where state not in ('done', 'failed')")}
        prowlarr = SimpleNamespace(search=lambda query: self.recall("search", query, lambda: self.prowlarr.search(query)))
        picks = releases.bundles(prowlarr, want, groups, wanted, blocked, self.torbox().cached_files, lambda rg: editions(db, rg),
                                 not self.crowded, self.slow(db), self.torrent)
        search = {"want": want, "wanted": sorted(wanted), "groups": [{k: g[k] for k in ("id", "title", "other_titles", "first-release-date", "primary-type")
                                                                       if k in g} for g in groups]}
        for release, targets, holds in picks[:GRAB_TRIES]:
            label = f"{want['artist']}: {len(targets)} albums" if targets else f"{want['artist']}: a bundle that may hold {len(holds)} albums"
            if (sent := self.send(db, [release], blocked, None, label, targets, None if targets else search, holds, catalogue=search)) is not None:
                return sent
        return None

    def remembered(self, db, wanted: set[str], label: str) -> bool | None:
        """Sends the remembered bundle (bundle_kept) that holds the most of these albums, when TorBox still has it
        cached, to fetch only theirs; see send. Not for an album it was tried for already. One TorBox no longer has is forgotten: None when none went, and the
        albums are searched as usual."""
        avoid = self.blocked(db) | {r[0] for r in db.execute("select hash from download where state not in ('done', 'failed')")}
        found = [(r["hash"], r["title"], held) for r in db.execute("select * from bundle_kept order by seen desc") if r["hash"] not in avoid
                 and (held := {rg: f for rg, f in json.loads(r["groups"]).items() if rg in wanted and rg not in json.loads(r["tried"])})][:100]
        listed = self.torbox().cached([h for h, _, _ in found]) if found else set()
        for hash, title, held in sorted(found, key=lambda k: len(k[2]), reverse=True):
            if hash not in listed:
                db.execute("delete from bundle_kept where hash = ?", (hash,))
                log.info("remembered bundle %s is no longer cached on TorBox: forgotten", title)
                continue
            release = {"title": title, "infoHash": hash, "magnetUrl": f"magnet:?xt=urn:btih:{hash}"}
            # Its remembered paths: TorBox lists the files of one of 100 or more as one zip till it is added.
            if (sent := self.send(db, [release], avoid, None, f"{label}: {len(held)} albums from a remembered bundle", held, kept=True)) is not None:
                db.execute("update bundle_kept set seen = ?, tried = (select json_group_array(value) from (select value from json_each(tried) union "
                           "select value from json_each(?))) where hash = ?", (time.time(), json.dumps(list(held)), hash))
                return sent
        return None

    def remember(self, db, row, files: list[dict]):
        """A bundle imported, with its files and every release group its folders hold (releases.targets, over all the
        artist's groups), wanted or not; one sent from memory is just seen again. Not one whose music came packed: its
        albums can't be fetched alone."""
        row = db.execute("select * from download where hash = ?", (row["hash"],)).fetchone()
        targets = {rg: json.loads(f) for rg, f in db.execute("select rg, files from download_target where hash = ? and state = 'done'", (row["hash"],))}
        names = {f["name"] for f in files}
        if row["state"] != "done" or not targets or not all(n in names for t in targets.values() for n in t):
            return
        if row["kept"] or not row["catalogue"]:
            db.execute("update bundle_kept set seen = ? where hash = ?", (time.time(), row["hash"]))
            return
        artist = db.execute(f"select artist_mbid from want_album where rg in ({','.join('?' * len(targets))}) and artist_mbid is not null", list(targets)).fetchone()
        tried = [rg for (rg,) in db.execute("select rg from download_target where hash = ?", (row["hash"],))]
        db.execute("insert or replace into bundle_kept values (?, ?, ?, ?, ?, ?, ?)", (row["hash"], row["title"], artist and artist[0],
                   json.dumps([{"name": f["name"], "size": f.get("size")} for f in files]), json.dumps(targets), time.time(), json.dumps(tried)))
        # The rest of its groups: a MusicBrainz lookup or more each, at one a second, so not in the downloads' poll.
        threading.Thread(target=self.map_kept, args=(row["hash"], row["title"], files, json.loads(row["catalogue"])), daemon=True).start()

    def map_kept(self, hash: str, title: str, files: list[dict], search: dict):
        db = self.connect()
        try:
            held = releases.targets(files, search["want"], search["groups"], {g["id"] for g in search["groups"]}, lambda rg: editions(db, rg))
            for (groups,) in db.execute("select groups from bundle_kept where hash = ?", (hash,)).fetchall():
                db.execute("update bundle_kept set groups = ? where hash = ?", (json.dumps(held | json.loads(groups)), hash))
                log.info("remembered bundle %s: it holds %d of the artist's release groups", title, len(held | json.loads(groups)))
        except (OSError, LookupError) as e:
            log.info("remembered bundle %s: only its imported albums: %s", title, e)
        finally:
            db.close()

    def unreliable(self, db) -> frozenset:
        """Indexers whose seed counts don't hold up: a third or more of what oasis took from them was dead."""
        seen = db.execute("select guid, coalesce(error like 'dead torrent%', 0) from download where guid is not null and (state = 'done' or error like 'dead torrent%')").fetchall()
        tally = {}
        for guid, dead in seen:
            tally.setdefault(releases.site(guid), []).append(dead)
        return frozenset(s for s, deaths in tally.items() if len(deaths) >= UNRELIABLE[0] and sum(deaths) >= UNRELIABLE[1] * len(deaths))

    def blocked(self, db) -> set[str]:
        return {r[0] for r in db.execute("select hash from release_block where reason not like 'too slow%'")}

    def slow(self, db) -> frozenset:
        """Releases that made way for others while slow: tried last, not never."""
        return frozenset(r[0] for r in db.execute("select hash from release_block where reason like 'too slow%'"))

    def send(self, db, picks: list[dict], blocked: set, group: str | None, label: str, targets: dict | None = None,
             search: dict | None = None, holds: set = frozenset(), catalogue: dict | None = None, kept: bool = False) -> bool | None:
        """Sends the first pick TorBox takes. True when one went, None when none would, False when TorBox had no free
        slot and queued it: the queued copy goes, the release isn't at fault, and the album is due again soon.
        group: the album's release group. targets: a bundle's albums, {group: files}. search: a bundle's whose files
        TorBox doesn't list yet, for resolve; holds: the albums it likely holds, which count as downloading till then.
        catalogue: a bundle's artist search, for remember. kept: sent from a remembered bundle."""
        tb = self.torbox()
        for i, release in enumerate(picks[:GRAB_TRIES]):
            try:
                found = source(release)
            except OSError as e:
                log.info("couldn't add %s to TorBox: %s", release["title"], e)
                continue
            if info_hash(found) in blocked:  # listed without its hash, it is one that failed before
                log.info("%s failed before: next", release["title"])
                continue
            try:
                self.added = time.time()
                added = tb.add(release["title"], **found)
            except OSError as e:
                log.info("couldn't add %s to TorBox: %s", release["title"], e)
                continue
            if not added.get("torrent_id"):
                queued = added.get("queued_id") or next((q["id"] for q in tb.queued() if (q.get("hash") or "").lower() == added["hash"].lower()), None)
                if queued:
                    tb.delete_queued(queued)
                log.info("TorBox has no free slot for %s: later", release["title"])
                return False
            now = time.time()
            later = [{k: r[k] for k in ("title", "guid", "infoHash", "magnetUrl", "downloadUrl", "seeders") if k in r} for r in picks[i + 1:i + 1 + GRAB_TRIES]]
            db.execute("insert or replace into download(hash, guid, album_id, album_mbid, title, torbox_id, state, created, updated, bundle, picks, catalogue, kept) "
                       "values (?, ?, 0, ?, ?, ?, 'added', ?, ?, ?, ?, ?, ?)", (added["hash"].lower(), release.get("guid"), group, release["title"],
                                                                                added["torrent_id"], now, now, search and json.dumps(search),
                                                                                json.dumps(later) if group and later else None,
                                                                                catalogue and json.dumps(catalogue), int(kept)))
            self.seen.pop(added["hash"].lower(), None)  # added again after a failure: its stall timer starts anew
            self.failures.pop(added["hash"].lower(), None)
            db.execute("delete from download_target where hash = ?", (added["hash"].lower(),))
            db.executemany("insert into download_target(hash, rg, files, state) values (?, ?, ?, 'pending')",
                           [(added["hash"].lower(), rg, json.dumps(files)) for rg, files in (targets or {}).items()])
            db.executemany("insert into download_target(hash, rg, files, state) values (?, ?, '[]', 'expected')",
                           [(added["hash"].lower(), rg) for rg in sorted(holds) if not targets])
            log.info("sent %s to TorBox for %s", release["title"], label)
            return True
        return None

    def cycle(self):
        db = self.connect()
        try:
            for (hash,) in db.execute("select hash from download where state = 'failed' and updated < ?", (time.time() - KEEP_FAILED,)).fetchall():
                shutil.rmtree(self.folder / hash, ignore_errors=True)
            db.execute("delete from bundle_kept where seen < ?", (time.time() - KEPT_UNSEEN,))
            rows = db.execute("select * from download where state not in ('done', 'failed') order by created").fetchall()
            if not rows:
                return
            listed = self.torbox().torrents()
            torrents = {t["id"]: t for t in listed}
            # Slots all taken while albums wait for a search: slow downloads make way.
            self.crowded = sum(bool(t["active"]) for t in listed) >= self.max_active and bool(db.execute(
                "select 1 from want_album where state = 'missing' and coalesce(next_search, 0) <= ? limit 1", (time.time(),)).fetchone())
            for row in rows:
                try:
                    self.step(db, row, torrents.get(row["torbox_id"]))
                except Exception as e:  # disk full, MusicBrainz down mid-import: next poll
                    log.warning("download %s: %s", row["title"], e)
                    db.execute("update download set error = ?, updated = ? where hash = ?", (str(e)[:300], time.time(), row["hash"]))
        finally:
            db.close()

    def step(self, db, row, torrent):
        state = row["state"]
        if state in ("added", "downloading"):
            if torrent is None:
                return self.fail(db, row, "gone from TorBox", block=False)
            if "error" in torrent["download_state"].lower():
                return self.fail(db, row, f"TorBox: {torrent['download_state']}")
            if row["bundle"] and torrent.get("files"):
                if not self.resolve(db, row, torrent["files"]):
                    return self.fail(db, row, "the bundle holds no wanted album")
                row = db.execute("select * from download where hash = ?", (row["hash"],)).fetchone()
            # TorBox lists a torrent it is checking (one re-added, say) with no files, even when it calls it finished.
            if not (torrent["download_finished"] and torrent["download_present"] and torrent.get("files")):
                progress, since = self.seen.get(row["hash"], (None, 0))
                if progress != torrent["progress"]:  # a torrent still at 0% has made no progress since it was added, restart or not
                    since = row["created"] if progress is None and not torrent["progress"] else time.time()
                    self.seen[row["hash"]] = (torrent["progress"], since)
                # Waits for seeders are short while others need the slot, and PATIENT while slots are free: a late seeder
                # (someone's PC that is on in the evening) still has a chance then, and costs nothing.
                wait = lambda short: short if self.crowded else max(short, PATIENT)
                # Nobody to get it from, or peers holding only part of it: its slot is freed.
                if not torrent["seeds"] and time.time() - since > wait(EMPTY_SWARM):
                    if not torrent.get("peers"):
                        return self.fail(db, row, "dead torrent: nobody shares it")
                    if (have := torrent.get("availability") or 0) < 1:
                        return self.fail(db, row, f"dead torrent: its peers have only {have:.0%} of it")
                # Not a byte from no seeder: dead soon. Stalled part way: the usual wait, as seeders come and go.
                if not torrent["seeds"] and time.time() - since > wait(DEAD_AFTER if not torrent["progress"] else self.stall):
                    return self.fail(db, row, "dead torrent: no seeds, no progress")
                # A trickle from one seeder: a torrent on course to take longer than SLOWEST, at its pace so far or
                # TorBox's speed now, whichever is faster, makes way: past the usual wait when others wait for its
                # slot, past WARM_UP for another pick of its search. It is tried again last (slow), when nothing better turns up.
                took, speed = time.time() - row["created"], (torrent.get("download_speed") or 0) / (torrent.get("size") or float("inf"))
                if took > WARM_UP and 1 - torrent["progress"] > (SLOWEST - took) * max(torrent["progress"] / took, speed):
                    why = f"too slow: {torrent['progress']:.0%} in {took / 3600:.0f} h"
                    if self.crowded and took > self.stall:
                        return self.fail(db, row, why)
                    if row["picks"] and self.switch(db, row, why):
                        return
                return self.set(db, row, "downloading")
            state = self.set(db, row, "fetching")
        if state == "fetching":
            if torrent is None:
                return self.fail(db, row, "gone from TorBox before its files were fetched", block=False)
            if not self.fetch(db, row, torrent):
                return
            state = self.set(db, row, "importing")
        if state == "importing":
            self.finish(db, row)
            if torrent:
                self.remember(db, row, torrent.get("files") or [])

    def switch(self, db, row, why: str) -> bool:
        """Sends the next of the album's search's picks in place of a slow download, cached ones first, and blocks the
        slow one as too slow. The picks are tried once: False, and they're dropped, when none went."""
        avoid = self.blocked(db) | self.slow(db) | {row["hash"]}
        picks = [r for r in json.loads(row["picks"]) if not {hash_of(r), r.get("guid")} & avoid]
        cached = self.torbox().cached([h for r in picks if (h := hash_of(r))]) if picks else set()
        if not self.send(db, sorted(picks, key=lambda r: hash_of(r) not in cached), avoid, row["album_mbid"], f"{row['title']} (too slow)"):
            db.execute("update download set picks = null where hash = ?", (row["hash"],))
            return False
        self.delete(row)
        self.block_release(db, row, why)
        self.set(db, row, "failed", why)
        log.info("download %s made way: %s", row["title"], why)
        return True

    def resolve(self, db, row, files: list[dict], packed: bool = True) -> bool:
        """A bundle's targets, from the files TorBox lists once it has the torrent (when it has downloaded it, as it
        seems), or unpacked here: of the albums wanted when it was sent, those still missing and not downloading
        otherwise. They replace the albums it was expected to hold. One whose music is packed (packed: in archives TorBox
        lists) stays unresolved: its archives are fetched, and unpacked holds the rest. False when it holds none."""
        search = json.loads(row["bundle"])
        missing = {r[0] for r in db.execute("select rg from want_album where state in ('missing', 'downloading')")} - self.groups(db, but=row["hash"])
        found = releases.targets(files, search["want"], search["groups"], set(search["wanted"]) & missing, lambda rg: editions(db, rg))
        if not found and packed and archives(files):
            return True
        db.execute("delete from download_target where hash = ?", (row["hash"],))
        db.executemany("insert into download_target(hash, rg, files, state) values (?, ?, ?, 'pending')",
                       [(row["hash"], rg, json.dumps(names)) for rg, names in found.items()])
        db.execute("update download set bundle = null where hash = ?", (row["hash"],))
        log.info("bundle %s holds %d wanted albums", row["title"], len(found))
        return bool(found)

    def chosen(self, db, row, torrent) -> list[tuple[dict, Path]] | None:
        """The files to fetch and where to: the torrent's music, cue sheets and cover art to <hash>/<path inside the
        torrent>, so same-named files in different folders both arrive; a bundle's targets only, to <hash>/<release
        group>/<path inside the album's folder>. None when TorBox doesn't list a target's file."""
        files = [f for f in torrent.get("files") or [] if f["name"].rsplit(".", 1)[-1].lower() in KEEP]
        if row["bundle"]:  # music in archives, to unpack: those named after a wanted album, else all
            search = json.loads(row["bundle"])
            named = [f for f in archives(files) if set(releases.folder_groups(PurePosixPath(f["name"]).stem, search["want"], search["groups"])) & set(search["wanted"])]
            return [(f, self.folder / row["hash"] / inside(f["name"])) for f in named or archives(files)]
        targets = db.execute("select rg, files from download_target where hash = ? and state != 'expected'", (row["hash"],)).fetchall()
        if not targets:
            return [(f, self.folder / row["hash"] / inside(f["name"])) for f in files]
        listed, todo = {f["name"]: f for f in files}, []
        for rg, names in targets:
            names = json.loads(names)
            top = posixpath.commonpath([posixpath.dirname(n) for n in names])
            if not all(n in listed for n in names):
                return None
            todo += [(listed[n], self.folder / row["hash"] / rg / inside(posixpath.relpath(n, top))) for n in names]
        return todo

    def copied(self, hash: str) -> float | None:
        """The share of a download's files fetched here so far, partly fetched ones too; None when none is being fetched."""
        if not (files := self.copying.get(hash)):
            return None
        size = lambda p: p.stat().st_size if p.exists() else 0
        return sum(min(total, size(path) or size(path.with_name(path.name + ".part"))) for path, total in files) / (sum(t for _, t in files) or 1)

    def fetch(self, db, row, torrent) -> bool:
        """Fetches the chosen files. Files already there at full size are kept; others are fetched again. True once
        all are there."""
        if time.time() < self.paused_until:
            return False
        if (todo := self.chosen(db, row, torrent)) is None:
            self.fail(db, row, "a bundle's files differ from what TorBox had cached")
            return False
        if not any(f["name"].rsplit(".", 1)[-1].lower() in (*AUDIO, *ARCHIVES) for f, _ in todo):
            self.fail(db, row, "no audio files in the torrent")
            return False
        self.copying[row["hash"]] = [(path, f["size"]) for f, path in todo]
        todo = [(f, path) for f, path in todo if not (path.exists() and path.stat().st_size == f["size"])]
        self.folder.mkdir(parents=True, exist_ok=True)
        need = sum(f["size"] for f, _ in todo) + UNPACK_ROOM * sum(f["size"] for f in archives([f for f, _ in todo]))
        if todo and (free := shutil.disk_usage(self.folder).free) < need:
            raise NoSpace(f"too little disk space: {need / 1e9:.1f} GB needed, {free / 1e9:.1f} GB free")
        with ThreadPoolExecutor(PARALLEL) as pool:
            errors = [e for e in pool.map(lambda job: self.fetch_file(torrent["id"], *job), todo) if e]
        if not errors:
            self.failures.pop(row["hash"], None)
            return True
        if not all(isinstance(e, RateLimited) for e in errors):
            self.failures[row["hash"]] = tries = self.failures.get(row["hash"], 0) + 1
            log.info("download %s: %d files failed (%s)", row["title"], len(errors), errors[0])
            if tries >= FETCH_TRIES:
                self.fail(db, row, f"files failed to download: {errors[0]}")
        return False

    def fetch_file(self, torrent_id: int, file: dict, path: Path) -> Exception | None:
        try:
            with self.pace:
                if time.time() < self.paused_until:
                    raise RateLimited("TorBox rate limit: paused")
                time.sleep(max(0.0, self.last_request + PACE - time.time()))
                self.last_request = time.time()
                url = self.torbox().link(torrent_id, file["id"])
            path.parent.mkdir(parents=True, exist_ok=True)
            part = path.with_name(path.name + ".part")
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "oasis/0.1"}), timeout=120) as response, \
                        open(part, "wb") as out:
                    shutil.copyfileobj(response, out, 1 << 20)
            except urllib.error.HTTPError as e:
                raise (RateLimited if e.code == 429 else OSError)(f"{file['name']}: {e.code}") from None
            if (size := part.stat().st_size) != file["size"]:
                part.unlink()
                raise OSError(f"{file['name']}: {size} bytes, not {file['size']}")
            part.rename(path)
            return None
        except RateLimited as e:
            self.paused_until = time.time() + PAUSE
            return e
        except OSError as e:
            return e

    def finish(self, db, row):
        if row["bundle"]:
            return self.unpacked(db, row)
        if db.execute("select 1 from download_target where hash = ?", (row["hash"],)).fetchone():
            return self.place_targets(db, row)
        self.place(db, row)

    def unpacked(self, db, row):
        """A bundle whose music came packed: its archives unpacked here (NoSpace: it waits), read as resolve reads
        TorBox's files, and each wanted album's files linked into <hash>/<release group>/ for place_targets."""
        folder = self.folder / row["hash"]
        unpack(folder)
        files = [{"name": p.relative_to(folder).as_posix(), "size": p.stat().st_size} for p in sorted((folder / UNPACKED).rglob("*")) if p.is_file()]
        if not self.resolve(db, row, files, packed=False):
            return self.fail(db, row, "the bundle holds no wanted album")
        for rg, names in db.execute("select rg, files from download_target where hash = ?", (row["hash"],)).fetchall():
            names = json.loads(names)
            top = posixpath.commonpath([posixpath.dirname(n) for n in names])
            for name in names:
                (target := folder / rg / inside(posixpath.relpath(name, top))).parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    os.link(folder / name, target)
        self.place_targets(db, row)

    def place(self, db, row):
        """A download no edition matches fails."""
        folder = self.folder / row["hash"]
        try:
            self.own.place(folder, row["album_mbid"])
        except LookupError as e:
            return self.fail(db, row, f"import failed: {e}")
        self.done(db, row, folder)

    def place_targets(self, db, row):
        """A bundle's albums, each imported on its own. One no edition matches is left out; the bundle fails, and is
        blocked, only when none matched. One made from lossy files blocks it too, so that album's next search finds another."""
        folder, lossy = self.folder / row["hash"], None
        for (rg,) in db.execute("select rg from download_target where hash = ? and state = 'pending'", (row["hash"],)).fetchall():
            try:
                self.own.place(folder / rg, rg)
            except LookupError as e:
                log.info("bundle %s: %s isn't there: %s", row["title"], rg, e)
                db.execute("update download_target set state = 'failed' where hash = ? and rg = ?", (row["hash"], rg))
                lossy = lossy or (e if isinstance(e, LossySource) else None)
                continue
            db.execute("update download_target set state = 'done' where hash = ? and rg = ?", (row["hash"], rg))
        if db.execute("select 1 from download_target where hash = ? and state = 'done'", (row["hash"],)).fetchone():
            if lossy:  # the albums it holds lossy-sourced are searched again: not in this bundle
                self.block_release(db, row, f"import failed: {lossy}")
            self.done(db, row, folder)
        else:
            self.fail(db, row, "import failed: no album of the bundle matched")

    def done(self, db, row, folder: Path):
        self.delete(row)
        shutil.rmtree(folder, ignore_errors=True)
        self.set(db, row, "done")
        log.info("imported %s", row["title"])
        self.imported()

    def fail(self, db, row, why: str, block: bool = True):
        """Deletes the torrent and makes the album due for another search. A blocked release is never picked again.
        The files stay KEEP_FAILED for inspection."""
        self.delete(row)
        if block:
            self.block_release(db, row, why)
        self.set(db, row, "failed", why)
        db.execute("update want_album set next_search = null, error = ? where rg = ?", (f"download failed: {why}", row["album_mbid"]))
        # A bundle's albums: the artist's next bundle search is due, for another bundle of them.
        db.execute("update bundle_search set searched = 0 where artist_mbid in (select artist_mbid from want_album where rg in "
                   "(select rg from download_target where hash = ?))", (row["hash"],))
        log.info("download %s failed: %s", row["title"], why)

    def block_release(self, db, row, why: str):
        db.executemany("insert or replace into release_block values (?, ?, ?)", [(k, why, time.time()) for k in (row["hash"], row["guid"]) if k])

    def block(self, db, rg: str) -> bool:
        """Blocks the release TorBox is downloading for the album, so its next search picks another; a bundle's albums
        all search again. False when none is (files already being fetched or imported are let finish)."""
        rows = db.execute("select * from download where state in ('added', 'downloading') and (album_mbid = ? or hash in "
                          "(select hash from download_target where rg = ?))", (rg, rg)).fetchall()
        for row in rows:
            self.fail(db, row, "blocked by a user")
            db.execute("update want_album set next_search = null where rg in (select rg from download_target where hash = ?)", (row["hash"],))
        return bool(rows)

    def delete(self, row):
        try:
            self.torbox().delete_torrent(row["torbox_id"])
        except OSError as e:
            log.info("couldn't delete %s from TorBox: %s", row["title"], e)

    def set(self, db, row, state: str, error: str | None = None) -> str:
        db.execute("update download set state = ?, error = ?, updated = ? where hash = ?", (state, error, time.time(), row["hash"]))
        return state
