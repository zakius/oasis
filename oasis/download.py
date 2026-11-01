"""oasis's own downloads: the release a search picks goes to TorBox, which downloads the torrent on its servers; oasis
fetches the files over HTTP into its staging folder and places them itself (importer.py). rdtclient isn't involved,
so TorBox's slots and queue are read directly, files keep their paths inside the torrent, and oasis keeps its own
record of which hash is which album. A cached bundle serves several albums: only their files are fetched, each album
into <hash>/<release group>/, and each is imported on its own (download_target). Nothing seeds from here."""
import hashlib
import json
import logging
import posixpath
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath

from . import releases
from .importer import ARCHIVES, AUDIO, editions
from .releases import hash_of
from .torbox import RateLimited

log = logging.getLogger(__name__)

INTERVAL = 60            # seconds between polls of TorBox
SLOWEST = 12 * 3600     # seconds a download may take at its pace so far
DEAD_AFTER = 20 * 60     # seconds a torrent may sit at 0% without seeders
UNRELIABLE = 5, 1 / 3    # an indexer with this many downloads, this share of them dead, is tried last
KEEP = (*AUDIO, *ARCHIVES, "cue", "jpg", "jpeg", "png", "webp", "gif")  # music (perhaps packed), cue sheets and cover art; logs, nfo and the rest stay on TorBox
PACE = 3                 # seconds between file download requests: bursts of them get TorBox's 429
PARALLEL = 2             # files fetched at once
PAUSE = 300              # seconds without file downloads after a 429
FETCH_TRIES = 5          # polls in a row a file may fail to download before the release counts as broken
KEEP_FAILED = 7 * 86400  # seconds a failed download's files stay for inspection
GRAB_TRIES = 5           # picks tried in turn when TorBox won't take one


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
        self.pace, self.last_request, self.paused_until = threading.Lock(), 0.0, 0.0

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

    def groups(self, db) -> set[str]:
        return {r[0] for r in db.execute("select album_mbid from download where state not in ('done', 'failed') and album_mbid is not null union "
                                         "select t.rg from download_target t join download d on d.hash = t.hash where d.state not in ('done', 'failed')")}

    def find(self, db, want: dict, group: str) -> bool | None:
        """Searches Prowlarr for an album of oasis's own catalogue (acquire.py); see send."""
        blocked = self.blocked(db)
        return self.send(db, releases.find(self.prowlarr, want, blocked, self.torbox().cached, self.unreliable(db), self.slow(db)), blocked, group,
                         f"{want['artist']} - {want['title']}")

    def bundle(self, db, want: dict, groups: list[dict], wanted: set[str]) -> bool | None:
        """Sends the cached bundle of the artist that holds the most wanted albums (release group ids) to TorBox, to
        fetch only theirs; see releases.bundles and send."""
        blocked = self.blocked(db)
        picks = releases.bundles(self.prowlarr, want, groups, wanted, blocked, self.torbox().cached_files, lambda rg: editions(db, rg))
        for release, targets in picks[:GRAB_TRIES]:
            if (sent := self.send(db, [release], blocked, None, f"{want['artist']}: {len(targets)} albums", targets)) is not None:
                return sent
        return None

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

    def send(self, db, picks: list[dict], blocked: set, group: str | None, label: str, targets: dict | None = None) -> bool | None:
        """Sends the first pick TorBox takes. True when one went, None when none would, False when TorBox had no free
        slot and queued it: the queued copy goes, the release isn't at fault, and the album is due again soon.
        group: the album's release group. targets: a bundle's albums, {group: files}."""
        tb = self.torbox()
        for release in picks[:GRAB_TRIES]:
            try:
                found = source(release)
            except OSError as e:
                log.info("couldn't add %s to TorBox: %s", release["title"], e)
                continue
            if info_hash(found) in blocked:  # listed without its hash, it is one that failed before
                log.info("%s failed before: next", release["title"])
                continue
            try:
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
            db.execute("insert or replace into download(hash, guid, album_id, album_mbid, title, torbox_id, state, created, updated) "
                       "values (?, ?, 0, ?, ?, ?, 'added', ?, ?)", (added["hash"].lower(), release.get("guid"),
                                                                    group, release["title"], added["torrent_id"], now, now))
            self.seen.pop(added["hash"].lower(), None)  # added again after a failure: its stall timer starts anew
            self.failures.pop(added["hash"].lower(), None)
            db.execute("delete from download_target where hash = ?", (added["hash"].lower(),))
            db.executemany("insert into download_target(hash, rg, files, state) values (?, ?, ?, 'pending')",
                           [(added["hash"].lower(), rg, json.dumps(files)) for rg, files in (targets or {}).items()])
            log.info("sent %s to TorBox for %s", release["title"], label)
            return True
        return None

    def cycle(self):
        db = self.connect()
        try:
            for (hash,) in db.execute("select hash from download where state = 'failed' and updated < ?", (time.time() - KEEP_FAILED,)).fetchall():
                shutil.rmtree(self.folder / hash, ignore_errors=True)
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
            # TorBox lists a torrent it is checking (one re-added, say) with no files, even when it calls it finished.
            if not (torrent["download_finished"] and torrent["download_present"] and torrent.get("files")):
                progress, since = self.seen.get(row["hash"], (None, 0))
                if progress != torrent["progress"]:  # a torrent still at 0% has made no progress since it was added, restart or not
                    since = row["created"] if progress is None and not torrent["progress"] else time.time()
                    self.seen[row["hash"]] = (torrent["progress"], since)
                # Not a byte from no seeder: dead soon. Stalled part way: the usual wait, as seeders come and go.
                if not torrent["seeds"] and time.time() - since > (DEAD_AFTER if not torrent["progress"] else self.stall):
                    return self.fail(db, row, "dead torrent: no seeds, no progress")
                # A trickle from one seeder: past the usual wait, a torrent on course to take longer than SLOWEST makes
                # way when others wait for its slot. It is tried again last (slow), when nothing better turns up.
                if self.crowded and (took := time.time() - row["created"]) > self.stall and torrent["progress"] < took / SLOWEST:
                    return self.fail(db, row, f"too slow: {torrent['progress']:.0%} in {took / 3600:.0f} h")
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

    def chosen(self, db, row, torrent) -> list[tuple[dict, Path]] | None:
        """The files to fetch and where to: the torrent's music, cue sheets and cover art to <hash>/<path inside the
        torrent>, so same-named files in different folders both arrive; a bundle's targets only, to <hash>/<release
        group>/<path inside the album's folder>. None when TorBox doesn't list a target's file."""
        files = [f for f in torrent.get("files") or [] if f["name"].rsplit(".", 1)[-1].lower() in KEEP]
        targets = db.execute("select rg, files from download_target where hash = ?", (row["hash"],)).fetchall()
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
        if db.execute("select 1 from download_target where hash = ?", (row["hash"],)).fetchone():
            return self.place_targets(db, row)
        self.place(db, row)

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
        blocked, only when none matched."""
        folder = self.folder / row["hash"]
        for (rg,) in db.execute("select rg from download_target where hash = ? and state = 'pending'", (row["hash"],)).fetchall():
            try:
                self.own.place(folder / rg, rg)
            except LookupError as e:
                log.info("bundle %s: %s isn't there: %s", row["title"], rg, e)
                db.execute("update download_target set state = 'failed' where hash = ? and rg = ?", (row["hash"], rg))
                continue
            db.execute("update download_target set state = 'done' where hash = ? and rg = ?", (row["hash"], rg))
        if db.execute("select 1 from download_target where hash = ? and state = 'done'", (row["hash"],)).fetchone():
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
        now = time.time()
        if block:
            db.executemany("insert or replace into release_block values (?, ?, ?)", [(k, why, now) for k in (row["hash"], row["guid"]) if k])
        self.set(db, row, "failed", why)
        db.execute("update want_album set next_search = null, error = ? where rg = ?", (f"download failed: {why}", row["album_mbid"]))
        log.info("download %s failed: %s", row["title"], why)

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
