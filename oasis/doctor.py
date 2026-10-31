"""Gets Lidarr's stuck downloads into the library: imports failed downloads against whichever edition of the album
their files match, and otherwise blocklists the release so Lidarr grabs another one. Also keeps TorBox's few active
slots free of torrents nothing will ever finish, and imports albums rdtclient finished but never marks done.
Each rule works around a Lidarr or rdtclient fault, until oasis downloads and imports on its own."""
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone

from .importer import AUDIO, number_of, title_of
from .lidarr import Lidarr
from .torbox import TorBox
from .ytm import fold

log = logging.getLogger(__name__)

INTERVAL = 300                          # seconds between cycles
SWAP_AFTER = timedelta(minutes=10)      # a download TorBox hasn't finished by then isn't cached: look for a cached copy
SWAP_RECHECK = timedelta(hours=1)
# Rejections a release TorBox has cached gets past: it needs no seeders, and the slow copy in the queue is what it replaces.
CACHE_IGNORES = ("Not enough seeders", "Release in queue")
ORPHAN_AFTER = timedelta(minutes=10)    # rdtclient tracks its adds by then
STUCK_AFTER = timedelta(minutes=30)     # every audio file downloaded this long ago, but the torrent still "downloading"
EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def match(files: list, tracks: list, artist: str):
    """Maps every track of an edition to a file, by title, else by disc and track number. None when it can't."""
    by_title = {}
    for f in files:
        by_title.setdefault(fold(title_of(f["path"], artist)), []).append(f)
    by_number = {number_of(f["path"]): f for f in files}
    mapping, used = {}, set()
    for t in tracks:
        key = fold(re.sub(r"\s*[(\[][^)\]]*[)\]]\s*$", "", t["title"]))
        candidates = [f for f in by_title.get(key, []) if f["path"] not in used] or \
                     [f for k, fs in by_title.items() if key and k and (key in k or k in key) for f in fs if f["path"] not in used]
        f = candidates[0] if candidates else by_number.get((t["mediumNumber"], t["absoluteTrackNumber"])) or \
            (by_number.get((1, t["absoluteTrackNumber"])) if len({x["mediumNumber"] for x in tracks}) == 1 else None)
        if not f or f["path"] in used:
            return None
        mapping[t["id"]] = f
        used.add(f["path"])
    return mapping


def read_copy(path: str, sql: str, params=()) -> list:
    """Reads another app's SQLite database from a copy, so its own writes never wait on us and it is never written."""
    with tempfile.TemporaryDirectory() as tmp:
        for suffix in ("", "-wal"):
            if os.path.exists(path + suffix):
                shutil.copy(path + suffix, os.path.join(tmp, "db" + suffix))
        db = sqlite3.connect(os.path.join(tmp, "db"))
        try:
            return db.execute(sql, params).fetchall()
        finally:
            db.close()


class Doctor:
    """The rules, run every INTERVAL. dry_run logs what each would do and changes nothing."""

    def __init__(self, connect, lidarr: Lidarr, rdt_db: str, stall_hours: float = 1, dry_run: bool = False, torbox=TorBox):
        self.connect, self.lidarr, self.rdt_db, self.torbox_for = connect, lidarr, rdt_db, torbox
        self.stall, self.dry = timedelta(hours=stall_hours), dry_run
        self.checked = {}        # queue id -> when it was last checked for a cached copy
        self.progress_seen = {}  # TorBox hash -> (progress, when it was first seen at that progress)

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        while True:
            try:
                self.cycle()
            except Exception:  # Lidarr or TorBox restarts: next cycle
                log.exception("doctor cycle failed")
            time.sleep(INTERVAL)

    def say(self, *parts):
        log.info("%s%s", "[dry] " if self.dry else "", " ".join(str(p) for p in parts))

    def remove(self, item, blocklist=False, search=False):
        if not self.dry:
            self.lidarr.remove(item["id"], blocklist, search)

    def torbox(self):
        rows = read_copy(self.rdt_db, "select Value from Settings where SettingId = 'Provider:ApiKey'")  # rdtclient's TorBox key
        return self.torbox_for(rows[0][0]) if rows and rows[0][0] else None

    def cycle(self):
        queue = self.lidarr.queue()
        now, searched = datetime.now(timezone.utc), set()
        # Releases oasis grabbed for an album although Lidarr couldn't read their names: Lidarr ties them to no album.
        db = self.connect()
        try:
            rescued = dict(db.execute("select hash, album_id from lidarr_rescue").fetchall())
            ours = {h for (h,) in db.execute("select hash from download")}  # oasis's own TorBox downloads (download.py)
        finally:
            db.close()
        for item in queue:
            if not item.get("albumId") and (album_id := rescued.get((item.get("downloadId") or "").lower())):
                item["albumId"] = album_id
        # Imports first, so albums they complete don't trigger searches below.
        for item in queue:
            state = item.get("trackedDownloadState")
            messages = " ".join(m for s in item.get("statusMessages", []) for m in s.get("messages", []))
            if state in ("importFailed", "importBlocked") and item.get("albumId"):
                try:
                    if self.try_import(item):
                        continue
                except Exception as e:
                    self.say("import error", item["title"][:70], e)
                self.replace(item, messages[:80] or state, queue, searched)
            elif state == "downloading" and item.get("albumId"):
                done, folder = self.audio_done(item["downloadId"])
                if done and now - done > STUCK_AFTER and self.try_import(item, folder) and not self.dry:
                    self.say("done", item["title"][:70], "- imported what rdtclient finished; the rest were duplicate extras")
                    self.remove(item)
        queue = self.lidarr.queue() if not self.dry else queue
        torbox = self.torbox()
        waiting = {(q.get("hash") or "").lower() for q in torbox.queued()} if torbox else set()
        for item in queue:
            if item["status"] == "warning" and item.get("trackedDownloadState") == "downloading":
                added = datetime.fromisoformat(item["added"].replace("Z", "+00:00")) if item.get("added") else None
                error = "error" in (item.get("errorMessage") or "").lower()
                stalled = "stalled" in (item.get("errorMessage") or "").lower() and (added is None or now - added > self.stall)
                if error and (item.get("downloadId") or "").lower() in waiting:
                    # TorBox had no free slot: nothing wrong with the release. The next search, once a slot is free, finds it.
                    self.say("drop", item["title"][:70], "- TorBox is full, not the release's fault")
                    self.remove(item)
                elif error or stalled:
                    self.replace(item, item.get("errorMessage") or "stalled", queue, searched)
        torrents = self.sweep(torbox, queue, searched, now, ours) if torbox else []
        log.info("doctor: %d downloads in Lidarr's queue, %d TorBox torrents", len(queue), len(torrents))

    def try_import(self, item, folder=None, mode="auto") -> bool:
        """folder: where the files are, for downloads Lidarr still thinks are running (it proposes nothing by id then),
        or doesn't know (oasis's own, with downloadId None). mode: Lidarr's importMode."""
        where = {"folder": folder} if folder else {"downloadId": item["downloadId"]}
        proposals = self.lidarr.proposals(**where)
        if not proposals:
            return False
        if not self.dry:
            self.fix_extensions(os.path.commonpath([os.path.dirname(p["path"]) for p in proposals]))
            proposals = self.lidarr.proposals(**where)

        # Lidarr's own match is clean: import it as proposed.
        if all(p.get("tracks") and not p.get("rejections") and (p.get("album") or {}).get("id") == item["albumId"] for p in proposals):
            files = [{"path": p["path"], "artistId": p["artist"]["id"], "albumId": p["album"]["id"], "albumReleaseId": p["albumReleaseId"],
                      "trackIds": [t["id"] for t in p["tracks"]], "quality": p["quality"], "downloadId": item["downloadId"],
                      "indexerFlags": 0, "disableReleaseSwitching": False} for p in proposals]
            return self.import_files(item, files, proposals[0]["albumReleaseId"], mode)

        # Otherwise find an edition the files cover completely; prefer the one with the most tracks.
        album = self.lidarr.album_by_id(item["albumId"])
        artist = album["artist"]["artistName"]
        for release in sorted(album["releases"], key=lambda r: -r["trackCount"]):
            if release["trackCount"] > len(proposals):
                continue
            if mapping := match(proposals, self.lidarr.release_tracks(release["id"]), artist):
                files = [{"path": f["path"], "artistId": album["artistId"], "albumId": album["id"], "albumReleaseId": release["id"],
                          "trackIds": [track_id], "quality": f["quality"], "downloadId": item["downloadId"], "indexerFlags": 0,
                          "disableReleaseSwitching": False} for track_id, f in mapping.items()]
                self.say("matched", item["title"][:70], "to", release["trackCount"], "track edition",
                         release.get("format"), release.get("country"), release.get("disambiguation") or "")
                return self.import_files(item, files, release["id"], mode)
        return False

    def fix_extensions(self, folder: str):
        """Hardlinks files like '13. P.L.U.C.Kflac' to a name with a real extension, so Lidarr sees them as audio."""
        for root, _, names in os.walk(folder):
            for name in names:
                for ext in AUDIO:
                    if name.lower().endswith(ext) and not name.lower().endswith("." + ext) and len(name) > len(ext):
                        target = os.path.join(root, name[:-len(ext)].rstrip(".") + "." + ext)
                        if not os.path.exists(target) and not self.dry:
                            os.link(os.path.join(root, name), target)
                        self.say("renamed", name, "->", os.path.basename(target))

    def import_files(self, item, files, release_id, mode="auto") -> bool:
        if self.dry:
            self.say("import", item["title"][:70], f"({len(files)} files) as release", release_id)
            return True
        cmd = self.lidarr.manual_import(files, mode)
        for _ in range(150):
            status = self.lidarr.command(cmd["id"])
            if status["status"] in ("completed", "failed", "aborted"):
                break
            time.sleep(2)
        # Lidarr can fail a ManualImport after the files are in (a NullReferenceException when it marks a download it still
        # thinks is running as imported), so judge by the files: each one imported now has a track file added since.
        added = [f for f in self.lidarr.track_files(item["albumId"]) if f["dateAdded"] >= cmd["queued"][:19]]
        ok = len(added) >= len(files)
        self.say("import", "ok" if ok else f"failed ({status.get('message')})", item["title"][:70], "as release", release_id)
        return ok

    def replace(self, item, why: str, queue: list, searched: set):
        """Removes the download and blocklists the release; Lidarr searches again unless another copy is coming,
        the album is complete, or another release of it already triggered a search this round."""
        album_id = item.get("albumId")
        other = any(q["id"] != item["id"] and q.get("albumId") == album_id and q.get("trackedDownloadStatus") == "ok"
                    and q["status"] in ("downloading", "queued", "completed") for q in queue)
        album = self.lidarr.album_by_id(album_id) if album_id else None
        have = album and album["statistics"]["trackFileCount"] >= album["statistics"]["trackCount"] > 0
        skip = bool(album_id and (other or have or album_id in searched))
        if not skip and album_id:
            searched.add(album_id)
        reason = "another copy is coming" if other else "the album is in the library" if have else "already searching"
        self.say("replace", item["title"][:70] or item["downloadId"], f"({why})", f"- no new search, {reason}" if skip else "- searching again")
        self.remove(item, blocklist=True, search=not skip)

    def audio_done(self, download_id):
        """When rdtclient finished every audio file of a torrent it still calls unfinished: the time the last one finished,
        and the torrent's folder. It never finishes the second of two files with one name in different folders
        (folder.jpg, rip logs), so such a download stays at 99% forever and Lidarr never imports it."""
        rows = read_copy(self.rdt_db, "select d.FileName, d.DownloadFinished, t.Category, t.RdName from Downloads d "
                         "join Torrents t on t.TorrentId = d.TorrentId where lower(t.Hash) = lower(?) and t.Completed is null", (download_id,))
        audio = [finished for name, finished, *_ in rows if name.rsplit(".", 1)[-1].lower() in AUDIO]
        if not audio or None in audio or all(r[1] for r in rows):
            return None, None
        return datetime.fromisoformat(max(audio)[:19]).replace(tzinfo=timezone.utc), f"/downloads/{rows[0][2]}/{rows[0][3]}"

    def prefer_cached(self, torbox, item, torrent, now):
        """Swaps a slow (uncached) download for a copy of the album TorBox has cached, at the same quality or better."""
        if now - self.checked.get(item["id"], EPOCH) < SWAP_RECHECK:
            return
        self.checked[item["id"]] = now
        releases = self.lidarr.releases(item["albumId"])
        current = max((r["qualityWeight"] for r in releases if (r.get("infoHash") or "").lower() == torrent["hash"].lower()), default=None)
        if current is None:
            return
        candidates = [r for r in releases if r.get("infoHash") and r["infoHash"].lower() != torrent["hash"].lower()
                      and r["qualityWeight"] >= current and all(x.startswith(CACHE_IGNORES) for x in r["rejections"])]
        cached = torbox.cached(list(dict.fromkeys(r["infoHash"].lower() for r in candidates))[:100])
        if not (best := next((r for r in candidates if r["infoHash"].lower() in cached), None)):  # Lidarr lists releases best first
            return
        self.say("swap to cached", item["title"][:60], "->", best["title"][:60], best["quality"]["quality"]["name"])
        if not self.dry:
            self.lidarr.grab(best)
            self.remove(item)

    def sweep(self, torbox, queue, searched, now, ours=frozenset()) -> list[dict]:
        """TorBox queues adds beyond the plan's active slots, and rdtclient reports those as failed: queued ones are orphans
        that would take the slots later, and so are active ones rdtclient no longer tracks (TorBox started them after the
        add was reported failed). Dead torrents (no seeds, no progress for stall_hours, at any percentage) hold slots
        forever: Lidarr's get replaced, others (Sonarr's) are deleted, which rdtclient reports to their owner as failed.
        oasis's own downloads (ours) are left to download.py."""
        for q in torbox.queued():
            self.say("drop queued orphan", (q.get("name") or q.get("hash") or "")[:70])
            if not self.dry:
                torbox.delete_queued(q["id"])
        mine = {(item.get("downloadId") or "").lower(): item for item in queue}
        tracked = {h.lower() for (h,) in read_copy(self.rdt_db, "select Hash from Torrents where Completed is null and Hash is not null")}
        torrents = torbox.torrents()
        for t in torrents:
            if t["hash"].lower() in ours:
                continue
            age = now - datetime.fromisoformat(t["created_at"].replace("Z", "+00:00"))
            item = mine.get(t["hash"].lower())
            running = t["active"] and t["progress"] < 1
            if running and t["hash"].lower() not in tracked and age > ORPHAN_AFTER:
                self.say("delete orphan", t["name"][:70], "- rdtclient doesn't track it")
                if not self.dry:
                    torbox.delete_torrent(t["id"])
                continue
            seen = self.progress_seen.get(t["hash"])
            if not seen or seen[0] != t["progress"]:
                # A torrent still at 0% has made no progress since it was added, restart or not.
                seen = self.progress_seen[t["hash"]] = (t["progress"], now - age if not seen and not t["progress"] else now)
            if running and age > SWAP_AFTER and item and item.get("albumId"):
                try:
                    self.prefer_cached(torbox, item, t, now)
                except Exception as e:
                    self.say("cache check failed", item["title"][:60], e)
            if not (running and not t["seeds"] and now - seen[1] > self.stall):
                continue
            if item:
                self.replace(item, "dead torrent, no seeds", queue, searched)
            else:
                self.say("delete dead torrent", t["name"][:70])
                if not self.dry:
                    torbox.delete_torrent(t["id"])
        return torrents
