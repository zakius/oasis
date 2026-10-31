"""Keeps TorBox's few active slots free, for oasis's downloads and anime's (Sonarr through rdtclient): deletes the
account's queued items, active torrents nobody tracks, and dead ones. Each rule works around a TorBox or rdtclient fault."""
import logging
import os
import shutil
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone

from .torbox import TorBox

log = logging.getLogger(__name__)

INTERVAL = 300                          # seconds between cycles
ORPHAN_AFTER = timedelta(minutes=10)    # rdtclient tracks its adds by then


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

    def __init__(self, connect, rdt_db: str, stall_hours: float = 1, dry_run: bool = False, torbox=TorBox):
        self.connect, self.rdt_db, self.torbox_for = connect, rdt_db, torbox
        self.stall, self.dry = timedelta(hours=stall_hours), dry_run
        self.progress_seen = {}  # TorBox hash -> (progress, when it was first seen at that progress)

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        while True:
            try:
                self.cycle()
            except Exception:  # TorBox restarts: next cycle
                log.exception("doctor cycle failed")
            time.sleep(INTERVAL)

    def say(self, *parts):
        log.info("%s%s", "[dry] " if self.dry else "", " ".join(str(p) for p in parts))

    def torbox(self):
        rows = read_copy(self.rdt_db, "select Value from Settings where SettingId = 'Provider:ApiKey'")  # rdtclient's TorBox key
        return self.torbox_for(rows[0][0]) if rows and rows[0][0] else None

    def cycle(self):
        db = self.connect()
        try:
            ours = {h for (h,) in db.execute("select hash from download")}  # oasis's own TorBox downloads (download.py)
        finally:
            db.close()
        torrents = self.sweep(torbox, datetime.now(timezone.utc), ours) if (torbox := self.torbox()) else []
        log.info("doctor: %d TorBox torrents", len(torrents))

    def sweep(self, torbox, now, ours=frozenset()) -> list[dict]:
        """TorBox queues adds beyond the plan's active slots, and rdtclient reports those as failed: queued ones are orphans
        that would take the slots later, and so are active ones rdtclient no longer tracks (TorBox started them after the
        add was reported failed). Dead torrents (no seeds, no progress for stall_hours, at any percentage) hold slots
        forever: they are deleted, which rdtclient reports to their owner (Sonarr) as failed. oasis's own downloads
        (ours) are left to download.py."""
        for q in torbox.queued():
            self.say("drop queued orphan", (q.get("name") or q.get("hash") or "")[:70])
            if not self.dry:
                torbox.delete_queued(q["id"])
        tracked = {h.lower() for (h,) in read_copy(self.rdt_db, "select Hash from Torrents where Completed is null and Hash is not null")}
        torrents = torbox.torrents()
        for t in torrents:
            if t["hash"].lower() in ours:
                continue
            age = now - datetime.fromisoformat(t["created_at"].replace("Z", "+00:00"))
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
            if running and not t["seeds"] and now - seen[1] > self.stall:
                self.say("delete dead torrent", t["name"][:70])
                if not self.dry:
                    torbox.delete_torrent(t["id"])
        return torrents
