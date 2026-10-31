"""Albums and artists users star go to Lidarr, which downloads them into the music folder; the library scan then
takes over. oasis keeps the desired state (who wants what) and reconciles Lidarr with it."""
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .ytm import fold

log = logging.getLogger(__name__)

RETRY_NO_MATCH = 1800   # seconds before looking an unmatched name up again
RETRY_ERROR = 300       # seconds before retrying after Lidarr or MusicBrainz errors
SEARCH_GAP = 180        # seconds between album searches, so a burst of stars doesn't trip the debrid service's rate limit
RESEARCH = 6 * 3600     # seconds before searching again for an album that got nothing
GRAB_WAIT = 600         # seconds after a search before "nothing grabbed" counts
MAX_ACTIVE = 3          # downloads in Lidarr's queue above which no new search starts
CATALOGUE_TYPES = {"Album", "EP"}  # what of a followed artist's back catalogue gets downloaded; singles repeat album tracks


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
        for candidate in artist_names(name):
            found = self.call("GET", "artist/lookup?" + urllib.parse.urlencode({"term": candidate}))
            if match := next((a for a in found if fold(a["artistName"]) == fold(candidate)), None):
                return match
        return None

    def find_album(self, artist: str, title: str) -> dict | None:
        for candidate in artist_names(artist):
            found = self.call("GET", "album/lookup?" + urllib.parse.urlencode({"term": f"{candidate} {title}"}))
            mine = [a for a in found if fold(a["artist"]["artistName"]) == fold(candidate)]
            if match := next((a for a in mine if fold(a["title"]) == fold(title)), None) or \
                    next((a for a in mine if fold(title) and (fold(title) in fold(a["title"]) or fold(a["title"]) in fold(title))), None):
                return match
        return None

    def artist(self, mbid: str) -> dict | None:
        return next(iter(self.call("GET", f"artist?mbId={mbid}") or []), None)

    def album(self, mbid: str) -> dict | None:
        return next(iter(self.call("GET", f"album?foreignAlbumId={mbid}") or []), None)

    def albums_of(self, artist_id: int) -> list[dict]:
        return self.call("GET", f"album?artistId={artist_id}") or []

    def add_artist(self, found: dict) -> dict:
        """Follows the artist for new releases; the back catalogue is monitored after Lidarr's first refresh, and searched paced."""
        body = {**found, **self.defaults(), "monitored": True, "monitorNewItems": "all",
                "addOptions": {"monitor": "none", "searchForMissingAlbums": False}}
        return self.call("POST", "artist", body)

    def add_album(self, found: dict) -> dict:
        """Adds the album monitored, under its artist with nothing else monitored; searching is paced separately."""
        artist = {**found["artist"], **self.defaults(), "monitored": True, "monitorNewItems": "none",
                  "addOptions": {"monitor": "none", "searchForMissingAlbums": False}}
        return self.call("POST", "album", {**found, "artist": artist, "monitored": True, "addOptions": {"searchForNewAlbum": False}})

    def set_albums(self, album_ids: list[int], monitored: bool):
        if album_ids:
            self.call("PUT", "album/monitor", {"albumIds": album_ids, "monitored": monitored})

    def search_album(self, album_id: int):
        self.call("POST", "command", {"name": "AlbumSearch", "albumIds": [album_id]})

    def follow_artist(self, artist: dict, follow: bool):
        """Following monitors the artist for new releases; unfollowing unmonitors it. Albums keep their own monitoring."""
        self.call("PUT", f"artist/{artist['id']}", {**artist, "monitored": follow, "monitorNewItems": "all" if follow else "none"})

    def missing(self) -> list[dict]:
        return self.call("GET", "wanted/missing?page=1&pageSize=5000&monitored=true&sortKey=releaseDate&sortDirection=descending")["records"]

    def queued(self) -> tuple[set[int], set[int]]:
        records = self.call("GET", "queue?pageSize=1000")["records"]
        return {r["albumId"] for r in records if r.get("albumId")}, {r["artistId"] for r in records if r.get("artistId")}

    def why_nothing(self, album_id: int) -> str:
        """The reason an album searched for isn't downloading, from Lidarr's history."""
        events = self.call("GET", f"history?albumId={album_id}&page=1&pageSize=5&sortKey=date&sortDirection=descending")
        for event in (events or {}).get("records", []):
            kind, data = event.get("eventType"), event.get("data") or {}
            if kind == "downloadFailed":
                return f"download failed: {data.get('message') or event.get('sourceTitle')}"
            if kind in ("albumImportIncomplete", "trackFileImportFailed", "downloadImportFailed"):
                return f"import failed: {data.get('statusMessages') or event.get('sourceTitle')}"
            if kind == "grabbed":
                return f"grabbed {event.get('sourceTitle')}, then it left the queue"
        return "searched, nothing grabbed yet: no release found, or the download client refused it"


def artist_names(credit: str) -> list[str]:
    """Names to look a credit up by: the credit itself, then each artist of a collaboration, with and without
    a bracketed suffix: 'SawanoHiroyuki[nZk]:mizuki' -> SawanoHiroyuki[nZk]:mizuki, SawanoHiroyuki[nZk], SawanoHiroyuki, mizuki."""
    names = [credit]
    for part in re.split(r"\s*(?:,|&|:|×|/|\bfeat\.?|\bft\.?|\bwith\b)\s*", credit, flags=re.I):
        names += [part, re.sub(r"\s*[\[(][^\])]*[\])]\s*$", "", part)]
    return [n for n in dict.fromkeys(n.strip() for n in names) if n]


class Library:
    """Reconciles Lidarr with what users starred: wanted albums get added and searched (paced), wanted artists
    followed for new releases, and items nobody wants any more unmonitored."""

    def __init__(self, connect, lidarr: Lidarr):
        self.connect, self.lidarr = connect, lidarr
        self.wake = threading.Event()
        self.last_search = 0.0

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        while True:
            try:
                self.reconcile()
            except Exception:
                log.exception("Lidarr sync failed")
            self.wake.wait(60)
            self.wake.clear()

    def reconcile(self):
        db = self.connect()
        try:
            for kind, ref in db.execute("select distinct kind, ref from wanted").fetchall():
                db.execute("insert or ignore into lidarr_item(kind, ref, state, updated) values (?, ?, 'wanted', ?)", (kind, ref, time.time()))
            queued_albums, queued_artists = self.lidarr.queued()
            for item in db.execute("select * from lidarr_item").fetchall():
                wanted = db.execute("select 1 from wanted where kind = ? and ref = ?", (item["kind"], item["ref"])).fetchone()
                if wanted and (item["retry_at"] or 0) > time.time():
                    continue
                try:
                    if wanted:
                        state, error = self.want(db, item, queued_albums, queued_artists)
                        set_state(db, item, state, error)
                    else:
                        self.release(db, item)
                        db.execute("delete from lidarr_item where kind = ? and ref = ?", (item["kind"], item["ref"]))
                except LookupError as e:
                    set_state(db, item, "failed", str(e), retry=RETRY_NO_MATCH)
                except Exception as e:  # Lidarr or MusicBrainz down, timeouts: soon again
                    log.warning("Lidarr: %s %s: %s", item["kind"], item["ref"], e)
                    set_state(db, item, "wanted" if not item["mbid"] else item["state"], str(e)[:300], retry=RETRY_ERROR)
            self.search_next(db, queued_albums)
        finally:
            db.close()

    def names(self, db, item) -> tuple[str, str | None]:
        column = "album_id" if item["kind"] == "album" else "artist_id"
        row = db.execute(f"select album_artist, album from track where {column} = ? limit 1", (item["ref"],)).fetchone() or \
            (db.execute("select a.name album_artist, r.title album from ytm_release r join ytm_artist a on a.artist_id = r.artist_id "
                        "where r.album_id = ?", (item["ref"],)).fetchone() if item["kind"] == "album" else None)
        if row is None:
            raise LookupError("not in oasis any more")
        return row["album_artist"], row["album"] if item["kind"] == "album" else None

    def want(self, db, item, queued_albums: set, queued_artists: set) -> tuple[str, str | None]:
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

        if not is_album:
            artist = self.lidarr.artist(mbid)
            if artist is None:
                self.lidarr.add_artist(found or lookup())
                return "followed", None
            if not artist["monitored"] or artist.get("monitorNewItems") != "all":
                self.lidarr.follow_artist(artist, True)
            db.execute("update lidarr_item set lidarr_id = ? where kind = ? and ref = ?", (artist["id"], item["kind"], item["ref"]))
            if not item["searched"]:
                self.monitor_catalogue(db, item, artist)
            return ("downloading" if artist["id"] in queued_artists else "followed"), None

        album = self.lidarr.album(mbid)
        if album is None:
            album = self.lidarr.add_album(found or lookup())
        elif not album["monitored"]:
            self.lidarr.set_albums([album["id"]], True)
        stats = album.get("statistics") or {}
        if stats.get("trackCount") and stats.get("trackFileCount", 0) >= stats["trackCount"]:
            return "have", None
        if album["id"] in queued_albums:
            return "downloading", None
        db.execute("update lidarr_item set lidarr_id = ? where kind = ? and ref = ?", (album["id"], item["kind"], item["ref"]))
        if item["searched"] and time.time() - item["searched"] > GRAB_WAIT:
            return "sent", self.lidarr.why_nothing(album["id"])
        return "sent", "waiting for its search" if not item["searched"] else "searching"

    def monitor_catalogue(self, db, item, artist):
        """Once Lidarr has the artist's albums, monitors its albums and EPs so the paced search downloads them.
        Done once per artist, so albums unmonitored in Lidarr afterwards stay that way."""
        albums = self.lidarr.albums_of(artist["id"])
        if not albums:
            return  # first refresh still pending
        self.lidarr.set_albums([a["id"] for a in albums if not a["monitored"] and a.get("albumType") in CATALOGUE_TYPES], True)
        db.execute("update lidarr_item set searched = ? where kind = ? and ref = ?", (time.time(), item["kind"], item["ref"]))

    def search_next(self, db, queued_albums: set):
        """Searches one album at a time, SEARCH_GAP apart and only while few downloads run: starred albums first, then
        followed artists' back catalogues, newest first; each time those never searched before ones that got nothing."""
        if time.time() - self.last_search < SEARCH_GAP or len(queued_albums) >= MAX_ACTIVE:
            return
        item = db.execute("select * from lidarr_item where kind = 'album' and state = 'sent' and lidarr_id is not null "
                          "and (searched is null or searched < ?) and exists (select 1 from wanted w where w.kind = 'album' "
                          "and w.ref = lidarr_item.ref) order by searched is not null, searched, rowid limit 1", (time.time() - RESEARCH,)).fetchone()
        if item is not None and item["lidarr_id"] not in queued_albums:
            self.lidarr.search_album(item["lidarr_id"])
            self.last_search = time.time()
            db.execute("update lidarr_item set searched = ?, error = 'searching' where kind = ? and ref = ?",
                       (self.last_search, item["kind"], item["ref"]))
        elif item is None:
            self.search_catalogue(db, queued_albums)

    def search_catalogue(self, db, queued_albums: set):
        followed = {r[0] for r in db.execute("select lidarr_id from lidarr_item where kind = 'artist' and lidarr_id is not null "
                                             "and exists (select 1 from wanted w where w.kind = 'artist' and w.ref = lidarr_item.ref)")}
        if not followed:
            return
        searched = dict(db.execute("select album_id, searched from catalogue_search").fetchall())
        due = [a for a in self.lidarr.missing() if a["artistId"] in followed and a["id"] not in queued_albums
               and searched.get(a["id"], 0) < time.time() - RESEARCH]
        if not due:
            return
        album = min(due, key=lambda a: a["id"] in searched)  # stable: keeps Lidarr's newest-first order otherwise
        self.lidarr.search_album(album["id"])
        self.last_search = time.time()
        db.execute("insert or replace into catalogue_search values (?, ?)", (album["id"], self.last_search))

    def release(self, db, item):
        """Nobody wants it any more: stop Lidarr looking for it, and for an artist's albums not downloaded yet. Files stay."""
        if not item["mbid"]:
            return
        if item["kind"] == "album":
            if album := self.lidarr.album(item["mbid"]):
                self.lidarr.set_albums([album["id"]], False)
        elif artist := self.lidarr.artist(item["mbid"]):
            self.lidarr.follow_artist(artist, False)
            starred = {r[0] for r in db.execute("select mbid from lidarr_item where kind = 'album' and mbid is not null")}
            self.lidarr.set_albums([a["id"] for a in self.lidarr.albums_of(artist["id"]) if a["monitored"]
                                    and a["foreignAlbumId"] not in starred and not (a.get("statistics") or {}).get("trackFileCount")], False)


def set_state(db, item, state: str, error: str | None, retry: float | None = None):
    db.execute("update lidarr_item set state = ?, error = ?, retry_at = ?, updated = ? where kind = ? and ref = ?",
               (state, error, time.time() + retry if retry else None, time.time(), item["kind"], item["ref"]))
