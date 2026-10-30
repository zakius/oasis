"""Song recommendations from YouTube Music radio and Last.fm, and radio playlists that grow as they are played."""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from itertools import chain, zip_longest

from .db import remember

log = logging.getLogger(__name__)

AHEAD = 30      # unplayed tracks a radio keeps queued
KEEP = 300      # older radio entries beyond this are dropped
RECENT = 100    # recently played tracks a radio won't repeat


class Recommender:
    def __init__(self, connect, ytm, lastfm):
        self.connect, self.ytm, self.lastfm = connect, ytm, lastfm
        self.wake = threading.Event()

    def similar(self, db, seed, count: int, deep: bool = False) -> list[str]:
        """Track ids like the seed track, local copies standing in for streaming ones. deep adds Last.fm's picks."""
        sources = []
        if self.ytm:
            video_id = seed["ytm_id"] or self.ytm.match(seed)
            if video_id:
                db.execute("update track set ytm_id = ? where id = ?", (video_id, seed["id"]))
                sources.append([remember(db, row) for row in self.ytm.radio(video_id, count)])
        if deep and self.lastfm:
            sources.append(self.from_lastfm(db, seed, min(count, 15)))
        picks = [id for id in chain.from_iterable(zip_longest(*sources)) if id and id != seed["id"]]
        return list(dict.fromkeys(picks))[:count]

    def from_lastfm(self, db, seed, count: int) -> list[str]:
        artist = seed["album_artist"] if seed["source"] == "ytm" else seed["artist"]
        try:
            pairs = self.lastfm.similar(artist, seed["title"], count)
        except Exception as e:
            log.info("Last.fm similar failed: %s", e)
            return []
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
                for playlist in db.execute("select * from playlist where radio > 0").fetchall():
                    self.top_up(db, playlist)
            except Exception:
                log.exception("radio update failed")
            finally:
                db.close()

    def played(self, db, owner: str, track_ids: list[str]):
        """Moves radios past the played tracks and makes sure the owner has a radio that follows all listening."""
        if not db.execute("select 1 from playlist where owner = ? and radio = 2", (owner,)).fetchone():
            now = time.time()
            db.execute("insert into playlist(name, owner, comment, created, changed, radio) values (?, ?, ?, ?, ?, 2)",
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
        if playlist["radio"] == 2:
            seed_ids = [r[0] for r in db.execute("select id from stat where played is not null order by played desc limit 3")]
        else:
            seed_ids = list(reversed(entries[max(0, played_pos - 2):played_pos + 1])) or entries[:3]
        seeds = [r for id in seed_ids if (r := db.execute("select * from track where id = ?", (id,)).fetchone())]
        if not seeds:
            return
        skip = set(entries) | {r[0] for r in db.execute("select id from stat where played is not null order by played desc limit ?", (RECENT,))}
        added = []
        for seed in seeds:
            for id in self.similar(db, seed, AHEAD + 20, deep=True):
                if id not in skip:
                    skip.add(id)
                    added.append(id)
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


def set_entries(db, playlist_id, track_ids):
    db.execute("delete from playlist_entry where playlist_id = ?", (playlist_id,))
    db.executemany("insert into playlist_entry values (?, ?, ?)", [(playlist_id, i, t) for i, t in enumerate(track_ids)])
    db.execute("update playlist set changed = ? where id = ?", (time.time(), playlist_id))
