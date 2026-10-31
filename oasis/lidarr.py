"""Albums and artists users star go to Lidarr, which downloads them into the music folder; the library scan then
takes over. oasis keeps the desired state (who wants what) and reconciles Lidarr with it."""
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .ytm import fold

log = logging.getLogger(__name__)

RETRY = 1800  # seconds between attempts for items that failed


class Lidarr:
    """The few Lidarr API calls oasis needs."""

    def __init__(self, url: str, api_key: str, root_folder: str | None = None,
                 quality_profile: int | None = None, metadata_profile: int | None = None):
        self.url, self.key = url.rstrip("/"), api_key
        self.root_folder, self.quality_profile, self.metadata_profile = root_folder, quality_profile, metadata_profile

    def call(self, method: str, path: str, body=None):
        request = urllib.request.Request(f"{self.url}/api/v1/{path}", json.dumps(body).encode() if body is not None else None,
                                         {"X-Api-Key": self.key, "Content-Type": "application/json"}, method=method)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                data = response.read()
                return json.loads(data) if data else None
        except urllib.error.HTTPError as e:
            raise OSError(f"Lidarr {method} {path.split('?')[0]}: {e.code} {e.read()[:200].decode(errors='replace')}") from e

    def defaults(self) -> dict:
        """Root folder and profiles: from oasis.toml, else the root folder's own defaults."""
        roots = self.call("GET", "rootfolder")
        root = next((r for r in roots if r["path"] == self.root_folder), roots[0])
        return {"rootFolderPath": root["path"], "qualityProfileId": self.quality_profile or root["defaultQualityProfileId"],
                "metadataProfileId": self.metadata_profile or root["defaultMetadataProfileId"]}

    def find_artist(self, name: str) -> dict | None:
        found = self.call("GET", "artist/lookup?" + urllib.parse.urlencode({"term": name}))
        return next((a for a in found if fold(a["artistName"]) == fold(name)), None)

    def find_album(self, artist: str, title: str) -> dict | None:
        found = self.call("GET", "album/lookup?" + urllib.parse.urlencode({"term": f"{artist} {title}"}))
        mine = [a for a in found if fold(a["artist"]["artistName"]) == fold(artist)]
        return next((a for a in mine if fold(a["title"]) == fold(title)), None) or \
            next((a for a in mine if fold(title) and (fold(title) in fold(a["title"]) or fold(a["title"]) in fold(title))), None)

    def artist(self, mbid: str) -> dict | None:
        return next(iter(self.call("GET", f"artist?mbId={mbid}") or []), None)

    def album(self, mbid: str) -> dict | None:
        return next(iter(self.call("GET", f"album?foreignAlbumId={mbid}") or []), None)

    def add_artist(self, found: dict, monitor_all: bool) -> dict:
        body = {**found, **self.defaults(), "monitored": True, "monitorNewItems": "all" if monitor_all else "none",
                "addOptions": {"monitor": "all" if monitor_all else "none", "searchForMissingAlbums": monitor_all}}
        return self.call("POST", "artist", body)

    def add_album(self, found: dict) -> dict:
        artist = {**found["artist"], **self.defaults(), "monitored": True, "monitorNewItems": "none",
                  "addOptions": {"monitor": "none", "searchForMissingAlbums": False}}
        return self.call("POST", "album", {**found, "artist": artist, "monitored": True, "addOptions": {"searchForNewAlbum": True}})

    def set_album(self, album_id: int, monitored: bool, search: bool = False):
        self.call("PUT", "album/monitor", {"albumIds": [album_id], "monitored": monitored})
        if search:
            self.call("POST", "command", {"name": "AlbumSearch", "albumIds": [album_id]})

    def set_artist(self, artist: dict, monitored: bool):
        artist = {**artist, "monitored": monitored, "monitorNewItems": "all" if monitored else "none"}
        self.call("PUT", f"artist/{artist['id']}", artist)
        if monitored:
            self.call("POST", "command", {"name": "ArtistSearch", "artistId": artist["id"]})

    def queued(self) -> tuple[set[int], set[int]]:
        records = self.call("GET", "queue?pageSize=1000")["records"]
        return {r["albumId"] for r in records if r.get("albumId")}, {r["artistId"] for r in records if r.get("artistId")}


class Library:
    """Reconciles Lidarr with what users starred: wanted items get added and monitored, unwanted ones unmonitored."""

    def __init__(self, connect, lidarr: Lidarr):
        self.connect, self.lidarr = connect, lidarr
        self.wake = threading.Event()

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        while True:
            try:
                self.reconcile()
            except Exception:
                log.exception("Lidarr sync failed")
            self.wake.wait(300)
            self.wake.clear()

    def reconcile(self):
        db = self.connect()
        try:
            for kind, ref in db.execute("select distinct kind, ref from wanted").fetchall():
                db.execute("insert or ignore into lidarr_item(kind, ref, state, updated) values (?, ?, 'wanted', ?)", (kind, ref, time.time()))
            queued_albums, queued_artists = self.lidarr.queued()
            for item in db.execute("select * from lidarr_item").fetchall():
                wanted = db.execute("select 1 from wanted where kind = ? and ref = ?", (item["kind"], item["ref"])).fetchone()
                if wanted and item["state"] == "failed" and item["updated"] > time.time() - RETRY:
                    continue  # failed recently; try again later instead of hammering Lidarr
                try:
                    if wanted:
                        state = self.want(db, item, queued_albums, queued_artists)
                        db.execute("update lidarr_item set state = ?, error = null, updated = ? where kind = ? and ref = ?",
                                   (state, time.time(), item["kind"], item["ref"]))
                    else:
                        self.release(item)
                        db.execute("delete from lidarr_item where kind = ? and ref = ?", (item["kind"], item["ref"]))
                except Exception as e:
                    log.warning("Lidarr: %s %s: %s", item["kind"], item["ref"], e)
                    db.execute("update lidarr_item set state = 'failed', error = ?, updated = ? where kind = ? and ref = ?",
                               (str(e)[:300], time.time(), item["kind"], item["ref"]))
        finally:
            db.close()

    def names(self, db, item) -> tuple[str, str | None]:
        column = "album_id" if item["kind"] == "album" else "artist_id"
        row = db.execute(f"select album_artist, album from track where {column} = ? limit 1", (item["ref"],)).fetchone()
        if row is None:
            raise LookupError("not in oasis any more")
        return row["album_artist"], row["album"] if item["kind"] == "album" else None

    def want(self, db, item, queued_albums: set, queued_artists: set) -> str:
        artist_name, album_title = self.names(db, item)
        is_album = item["kind"] == "album"
        lookup = (lambda: self.lidarr.find_album(artist_name, album_title)) if is_album else (lambda: self.lidarr.find_artist(artist_name))
        found, mbid = None, item["mbid"]
        if not mbid:
            found = lookup()
            if not found:
                raise LookupError(f"no MusicBrainz album {album_title!r} by {artist_name}" if is_album
                                  else f"no MusicBrainz artist named {artist_name}")
            mbid = found["foreignAlbumId" if is_album else "foreignArtistId"]
            db.execute("update lidarr_item set mbid = ? where kind = ? and ref = ?", (mbid, item["kind"], item["ref"]))

        if is_album:
            album = self.lidarr.album(mbid)
            if album is None:
                album = self.lidarr.add_album(found or lookup())
            elif not album["monitored"]:
                self.lidarr.set_album(album["id"], True, search=True)
            if album["id"] in queued_albums:
                return "downloading"
            stats = album.get("statistics") or {}
            return "have" if stats.get("trackCount") and stats.get("trackFileCount", 0) >= stats["trackCount"] else "sent"

        artist = self.lidarr.artist(mbid)
        if artist is None:
            artist = self.lidarr.add_artist(found or lookup(), monitor_all=True)
        elif not artist["monitored"] or artist.get("monitorNewItems") != "all":
            self.lidarr.set_artist(artist, True)
        if artist["id"] in queued_artists:
            return "downloading"
        stats = artist.get("statistics") or {}
        return "have" if stats.get("trackCount") and stats.get("trackFileCount", 0) >= stats["trackCount"] else "sent"

    def release(self, item):
        """Nobody wants it any more: stop Lidarr looking for it. Files already downloaded stay."""
        if not item["mbid"]:
            return
        if item["kind"] == "album":
            if album := self.lidarr.album(item["mbid"]):
                self.lidarr.set_album(album["id"], False)
        elif artist := self.lidarr.artist(item["mbid"]):
            self.lidarr.set_artist(artist, False)
