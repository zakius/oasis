"""oasis's own downloads: the release a search picks goes to TorBox, which downloads the torrent on its servers; oasis
fetches the files over HTTP into its staging folder and imports them. rdtclient isn't involved, so
TorBox's slots and queue are read directly, files keep their paths inside the torrent, and oasis keeps its own record
of which hash is which album. oasis places the files itself (importer.py), or with import = "lidarr" has Lidarr
import them. A cached bundle serves several albums: only their files are fetched, each album into
<hash>/<release group>/, and each is imported on its own (download_target). Nothing seeds from here."""
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
from .doctor import CACHE_IGNORES
from .importer import AUDIO, editions
from .lidarr import GRAB_TRIES, unparsed_match
from .releases import hash_of
from .torbox import RateLimited

log = logging.getLogger(__name__)

INTERVAL = 60            # seconds between polls of TorBox
KEEP = (*AUDIO, "cue", "jpg", "jpeg", "png", "webp", "gif")  # music, cue sheets and cover art; logs, nfo and the rest stay on TorBox
PACE = 3                 # seconds between file download requests: bursts of them get TorBox's 429
PARALLEL = 2             # files fetched at once
PAUSE = 300              # seconds without file downloads after a 429
FETCH_TRIES = 5          # polls in a row a file may fail to download before the release counts as broken
KEEP_FAILED = 7 * 86400  # seconds a failed download's files stay for inspection


def choose(offers: list[dict], artist: str, title: str, blocked: set, cached) -> list[dict]:
    """The releases to try for an album, in turn: those TorBox has cached (it downloads them at once, seeders or not)
    at least as good as the best eligible one, then those Lidarr accepts, best first, then one it can't read by name.
    A release whose only faults are few seeders or a copy in Lidarr's queue is eligible only when cached."""
    fresh = [r for r in offers if r.get("protocol") != "usenet" and not {hash_of(r), r.get("guid")} & blocked]
    accepted = [r for r in fresh if not r.get("rejections")]
    seedless = [r for r in fresh if r.get("rejections") and all(x.startswith(CACHE_IGNORES) for x in r["rejections"])]
    have = cached(list(dict.fromkeys(h for r in accepted + seedless if (h := hash_of(r))))[:100])
    eligible = [r for r in fresh if any(r is a for a in accepted) or (any(r is s for s in seedless) and hash_of(r) in have)]
    best = max((r["qualityWeight"] for r in eligible), default=0)
    picks = [r for r in eligible if hash_of(r) in have and r["qualityWeight"] >= best]
    picks += [r for r in accepted if not any(r is p for p in picks)]
    if (unparsed := unparsed_match(fresh, artist, title)) and not any(unparsed is p for p in picks):
        picks.append(unparsed)
    return picks


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

    def __init__(self, connect, lidarr, importer, downloads: str, max_active: int = 8, stall_hours: float = 1, imported=lambda: None, own=None,
                 prowlarr=None):
        """importer: the doctor, for the TorBox key and Lidarr's import. own: oasis's Importer, which imports instead.
        imported: called after each import. Lidarr tells oasis to scan after its own imports, not after ManualImport.
        prowlarr: oasis searches the indexers itself; without, Lidarr's search lists the releases."""
        self.connect, self.lidarr, self.importer, self.folder, self.imported, self.own = connect, lidarr, importer, Path(downloads), imported, own
        self.prowlarr = prowlarr
        self.max_active, self.stall = max_active, stall_hours * 3600
        self.client = None
        self.seen = {}       # hash -> (progress, since when)
        self.failures = {}   # hash -> polls in a row its files failed to download
        self.pace, self.last_request, self.paused_until = threading.Lock(), 0.0, 0.0

    def torbox(self):
        self.client = self.client or self.importer.torbox()  # the key rdtclient holds
        return self.client

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        while True:
            try:
                self.cycle()
            except Exception:  # TorBox or Lidarr down: next poll
                log.exception("downloads poll failed")
            time.sleep(INTERVAL)

    def room(self) -> bool:
        """Whether another download may start. Every active torrent of the account takes one of the plan's slots,
        anime's and seeding ones too; max_active of them leaves the rest for anime."""
        return sum(bool(t["active"]) for t in self.torbox().torrents()) < self.max_active

    def albums(self, db) -> set[int]:
        return {r[0] for r in db.execute("select album_id from download where state not in ('done', 'failed')")}

    def groups(self, db) -> set[str]:
        return {r[0] for r in db.execute("select album_mbid from download where state not in ('done', 'failed') and album_mbid is not null union "
                                         "select t.rg from download_target t join download d on d.hash = t.hash where d.state not in ('done', 'failed')")}

    def search(self, db, album_id: int) -> bool | None:
        """Searches the indexers (through Prowlarr, else Lidarr) for Lidarr's album and sends the best pick to TorBox;
        see send."""
        album, tb, blocked = self.lidarr.album_by_id(album_id), self.torbox(), self.blocked(db)
        picks = releases.find(self.prowlarr, releases.target(db, self.lidarr, album), blocked, tb.cached) if self.prowlarr else \
            choose(self.lidarr.releases(album_id), album["artist"]["artistName"], album["title"], blocked, tb.cached)
        return self.send(db, picks, blocked, album_id, album.get("foreignAlbumId"), f"{album['artist']['artistName']} - {album['title']}")

    def find(self, db, want: dict, group: str) -> bool | None:
        """Searches Prowlarr for an album of oasis's own catalogue (acquire.py); see send."""
        blocked = self.blocked(db)
        return self.send(db, releases.find(self.prowlarr, want, blocked, self.torbox().cached), blocked, 0, group,
                         f"{want['artist']} - {want['title']}")

    def bundle(self, db, want: dict, groups: list[dict], wanted: set[str]) -> bool | None:
        """Sends the cached bundle of the artist that holds the most wanted albums (release group ids) to TorBox, to
        fetch only theirs; see releases.bundles and send."""
        blocked = self.blocked(db)
        picks = releases.bundles(self.prowlarr, want, groups, wanted, blocked, self.torbox().cached_files, lambda rg: editions(db, rg))
        for release, targets in picks[:GRAB_TRIES]:
            if (sent := self.send(db, [release], blocked, 0, None, f"{want['artist']}: {len(targets)} albums", targets)) is not None:
                return sent
        return None

    def blocked(self, db) -> set[str]:
        return {r[0] for r in db.execute("select hash from release_block")}

    def send(self, db, picks: list[dict], blocked: set, album_id: int, group: str | None, label: str, targets: dict | None = None) -> bool | None:
        """Sends the first pick TorBox takes. True when one went, None when none would, False when TorBox had no free
        slot and queued it: the queued copy goes, the release isn't at fault, and the album is due again soon.
        album_id: Lidarr's, 0 for albums only oasis's catalogue has. targets: a bundle's albums, {group: files}."""
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
                       "values (?, ?, ?, ?, ?, ?, 'added', ?, ?)", (added["hash"].lower(), release.get("guid"), album_id,
                                                                    group, release["title"], added["torrent_id"], now, now))
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
            torrents = {t["id"]: t for t in self.torbox().torrents()}
            for row in rows:
                try:
                    self.step(db, row, torrents.get(row["torbox_id"]))
                except Exception as e:  # Lidarr down mid-import, disk full: next poll
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
                if not torrent["seeds"] and time.time() - since > self.stall:
                    return self.fail(db, row, "dead torrent: no seeds, no progress")
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

    def fetch(self, db, row, torrent) -> bool:
        """Fetches the chosen files. Files already there at full size are kept; others are fetched again. True once
        all are there."""
        if time.time() < self.paused_until:
            return False
        if (todo := self.chosen(db, row, torrent)) is None:
            self.fail(db, row, "a bundle's files differ from what TorBox had cached")
            return False
        if not any(f["name"].rsplit(".", 1)[-1].lower() in AUDIO for f, _ in todo):
            self.fail(db, row, "no audio files in the torrent")
            return False
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
        if self.own:
            return self.place(db, row)
        self.lidarr_import(db, row)

    def place(self, db, row):
        """oasis's own import. A download no edition matches fails; Lidarr rescans the album folder so it counts the
        album present and doesn't search it again."""
        folder = self.folder / row["hash"]
        try:
            album = self.own.place(folder, row["album_mbid"])
        except LookupError as e:
            return self.fail(db, row, f"import failed: {e}")
        self.done(db, row, folder)
        try:
            self.lidarr.rescan([str(album)])
        except OSError as e:
            log.warning("Lidarr rescan of %s failed: %s", album, e)

    def place_targets(self, db, row):
        """A bundle's albums, each imported on its own. One no edition matches is left out; the bundle fails, and is
        blocked, only when none matched."""
        folder = self.folder / row["hash"]
        for (rg,) in db.execute("select rg from download_target where hash = ? and state = 'pending'", (row["hash"],)).fetchall():
            try:
                album = self.own.place(folder / rg, rg)
            except LookupError as e:
                log.info("bundle %s: %s isn't there: %s", row["title"], rg, e)
                db.execute("update download_target set state = 'failed' where hash = ? and rg = ?", (row["hash"], rg))
                continue
            db.execute("update download_target set state = 'done' where hash = ? and rg = ?", (row["hash"], rg))
            try:
                self.lidarr.rescan([str(album)])
            except OSError as e:
                log.warning("Lidarr rescan of %s failed: %s", album, e)
        if db.execute("select 1 from download_target where hash = ? and state = 'done'", (row["hash"],)).fetchone():
            self.done(db, row, folder)
        else:
            self.fail(db, row, "import failed: no album of the bundle matched")

    def lidarr_import(self, db, row):
        """Imports the folder with the doctor's matcher, moving the files into the library. Lidarr can fail an import
        after moving the files, and a restart can come between: an album complete in Lidarr counts as imported."""
        folder = self.folder / row["hash"]
        item = {"downloadId": None, "albumId": row["album_id"], "title": row["title"]}
        if self.importer.try_import(item, str(folder), mode="move") or self.complete(row["album_id"]):
            self.done(db, row, folder)
        else:
            self.fail(db, row, "import failed: the files match no edition of the album")

    def done(self, db, row, folder: Path):
        self.delete(row)
        shutil.rmtree(folder, ignore_errors=True)
        self.set(db, row, "done")
        log.info("imported %s", row["title"])
        self.imported()

    def complete(self, album_id: int) -> bool:
        stats = self.lidarr.album_by_id(album_id).get("statistics") or {}
        return stats.get("trackFileCount", 0) >= stats.get("trackCount", 0) > 0

    def fail(self, db, row, why: str, block: bool = True):
        """Deletes the torrent and makes the album due for another search. A blocked release is never picked again.
        The files stay KEEP_FAILED for inspection."""
        self.delete(row)
        now = time.time()
        if block:
            db.executemany("insert or replace into release_block values (?, ?, ?)", [(k, why, now) for k in (row["hash"], row["guid"]) if k])
        self.set(db, row, "failed", why)
        db.execute("update lidarr_item set searched = null where kind = 'album' and lidarr_id = ?", (row["album_id"],))
        db.execute("delete from catalogue_search where album_id = ?", (row["album_id"],))
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
