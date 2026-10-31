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
from pathlib import Path

from .db import artist_id
from .ytm import fold

log = logging.getLogger(__name__)

RETRY_NO_MATCH = 1800   # seconds before looking an unmatched name up again
RETRY_ERROR = 300       # seconds before retrying after Lidarr or MusicBrainz errors
SEARCH_GAP = 60         # seconds between album searches, so a burst of stars doesn't trip the debrid service's rate limit
RESEARCH = 6 * 3600     # seconds before searching again for an album that got nothing
GRAB_WAIT = 600         # seconds after a search before "nothing grabbed" counts
GRAB_TRIES = 5          # accepted releases tried in turn when grabbing fails
MAX_ACTIVE = 8          # running downloads at which no new search starts: TorBox Pro has 10 slots, two stay free for anime
RETRY_FULL = 600        # seconds before an album TorBox had no free slot for is searched again
UPGRADE_AGAIN = 7 * 86400  # seconds between searches for a lossless copy of an album downloaded lossy
CATALOGUE_TYPES = {"Album", "EP"}  # what of a followed artist's back catalogue gets downloaded; singles repeat album tracks
RECHECK_CATALOGUE = 86400  # seconds between passes dropping repackages Lidarr monitored as new releases
REPEATS = 0.6              # share of an album's songs already on the artist's earlier releases that makes it a repackage
REPACKAGE = re.compile(r"\b(best of|greatest hits|very best|the essential|anthology|collection|singles|remaster(ed)?|anniversary|"
                       r"deluxe|bonus ?tracks?|re-?recorded|reissue|remixes|instrumentals?|karaoke)\b", re.I)


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

    def artists(self) -> list[dict]:
        return self.call("GET", "artist") or []

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

    def search_album(self, album_id: int) -> str | None:
        """Searches and grabs the best release Lidarr accepts. When it accepts none only because it can't read the
        names (most Japanese releases: '[2022.09.07] Creepy Nuts 3rdアルバム「アンサンブル・プレイ」[FLAC]'), grabs one
        whose name has the artist and the album title; returns its hash, since Lidarr won't import it on its own."""
        releases = self.releases(album_id)
        # Lidarr lists best first. A release whose torrent the download client can't fetch fails to grab: the next one.
        for release in [r for r in releases if not r.get("rejections")][:GRAB_TRIES]:
            if self.try_grab(release):
                return None
        album = self.album_by_id(album_id)
        if (pick := unparsed_match(releases, album["artist"]["artistName"], album["title"])) \
                and self.try_grab(pick, albumId=album_id, artistId=album["artistId"]):
            log.info("grabbed %s for %s - %s, which Lidarr can't read", pick["title"], album["artist"]["artistName"], album["title"])
            return (pick.get("infoHash") or "").lower() or None
        return None

    def try_grab(self, release: dict, **ids) -> bool:
        try:
            self.grab(release, **ids)
            return True
        except OSError as e:
            log.info("couldn't grab %s: %s", release["title"], e)
            return False

    def tracks(self, album: dict) -> list[dict]:
        """The tracks of the album's smallest edition: bonus discs of live versions don't make it a repackage."""
        releases = [r for r in album.get("releases") or [] if r.get("trackCount")]
        if not releases:
            return self.call("GET", f"track?albumId={album['id']}") or []
        return self.call("GET", f"track?albumReleaseId={min(releases, key=lambda r: r['trackCount'])['id']}") or []

    def rescan(self, folders: list[str]):
        self.call("POST", "command", {"name": "RescanFolders", "folders": folders})

    def follow_artist(self, artist: dict, follow: bool):
        """Following monitors the artist for new releases; unfollowing unmonitors it. Albums keep their own monitoring."""
        self.call("PUT", f"artist/{artist['id']}", {**artist, "monitored": follow, "monitorNewItems": "all" if follow else "none"})

    def missing(self) -> list[dict]:
        return self.call("GET", "wanted/missing?page=1&pageSize=5000&monitored=true&sortKey=releaseDate&sortDirection=descending")["records"]

    def cutoff_unmet(self) -> list[dict]:
        return self.call("GET", "wanted/cutoff?page=1&pageSize=5000&monitored=true&sortKey=releaseDate&sortDirection=descending")["records"]

    def queued(self) -> tuple[set[int], set[int], int]:
        """Albums and artists in Lidarr's queue, and how many of its downloads still run."""
        records = self.call("GET", "queue?pageSize=1000")["records"]
        return ({r["albumId"] for r in records if r.get("albumId")}, {r["artistId"] for r in records if r.get("artistId")},
                sum(r.get("trackedDownloadState") == "downloading" for r in records))

    def queue(self) -> list[dict]:
        """Every download, with those Lidarr ties to no artist."""
        return self.call("GET", "queue?pageSize=500&includeUnknownArtistItems=true")["records"]

    def remove(self, queue_id: int, blocklist: bool, search: bool):
        """Removes a download from the queue and the download client; blocklisting makes the next search pick another release."""
        self.call("DELETE", f"queue/{queue_id}?" + urllib.parse.urlencode(
            {"removeFromClient": "true", "blocklist": str(blocklist).lower(), "skipRedownload": str(not search).lower()}))

    def releases(self, album_id: int) -> list[dict]:
        """What the indexers have for the album, best first, with Lidarr's rejections."""
        return self.call("GET", f"release?albumId={album_id}") or []

    def grab(self, release: dict, **ids):
        """ids (albumId, artistId) tie a release Lidarr can't read by name to its album."""
        self.call("POST", "release", {"guid": release["guid"], "indexerId": release["indexerId"], **ids})

    def album_by_id(self, album_id: int) -> dict:
        return self.call("GET", f"album/{album_id}")

    def release_tracks(self, release_id: int) -> list[dict]:
        return self.call("GET", f"track?albumReleaseId={release_id}") or []

    def proposals(self, **where) -> list[dict]:
        """How Lidarr would import a download's files: where is downloadId, or folder (ignored when an id is given)."""
        return self.call("GET", "manualimport?" + urllib.parse.urlencode({**where, "filterExistingFiles": "false"})) or []

    def manual_import(self, files: list[dict], mode: str = "auto") -> dict:
        return self.call("POST", "command", {"name": "ManualImport", "importMode": mode, "replaceExistingFiles": False, "files": files})

    def command(self, command_id: int) -> dict:
        return self.call("GET", f"command/{command_id}")

    def track_files(self, album_id: int) -> list[dict]:
        return self.call("GET", f"trackfile?albumId={album_id}") or []

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


UNPARSED = "Unable to parse"
BUNDLES = re.compile(r"discograph|collection|complete|box ?set|anthology|全集|ディスコグラフィ", re.I)


def unparsed_match(releases: list[dict], artist: str, title: str) -> dict | None:
    """Of the releases Lidarr rejected only for names it can't read, the best whose name has the artist and the album
    title: lossless first, then the most seeders. Titles too short to tell apart ('V', '1') are left alone, and so
    are discographies, which hold the album but aren't it."""
    artist, title = fold(artist), fold(title)
    if len(artist) < 2 or len(title) < 4 or title.isdigit():
        return None
    found = [r for r in releases if r.get("rejections") and all(x.startswith(UNPARSED) for x in r["rejections"])
             and artist in fold(r["title"]) and title in fold(r["title"]) and not BUNDLES.search(r["title"])
             and 20e6 < (r.get("size") or 0) < 5e9]
    lossless = lambda r: "flac" in r["title"].lower() or "FLAC" in r["quality"]["quality"]["name"]
    return max(found, key=lambda r: (lossless(r), r.get("seeders") or 0), default=None)


def artist_names(credit: str) -> list[str]:
    """Names to look a credit up by: the credit itself, then each artist of a collaboration, with and without
    a bracketed suffix: 'SawanoHiroyuki[nZk]:mizuki' -> SawanoHiroyuki[nZk]:mizuki, SawanoHiroyuki[nZk], SawanoHiroyuki, mizuki."""
    names = [credit]
    for part in re.split(r"\s*(?:,|&|:|×|/|\bfeat\.?|\bft\.?|\bwith\b)\s*", credit, flags=re.I):
        names += [part, re.sub(r"\s*[\[(][^\])]*[\])]\s*$", "", part)]
    return [n for n in dict.fromkeys(n.strip() for n in names) if n]


class Library:
    """Reconciles Lidarr with what users starred: wanted albums get added and searched (paced), wanted artists
    followed for new releases, and items nobody wants any more unmonitored. With downloads (download.py), oasis
    downloads what the searches find through TorBox itself; without, Lidarr grabs it through its download client."""

    def __init__(self, connect, lidarr: Lidarr, downloads=None):
        self.connect, self.lidarr, self.downloads = connect, lidarr, downloads
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
            queued_albums, queued_artists, running = self.lidarr.queued()
            if self.downloads:
                queued_albums |= self.downloads.albums(db)
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
            if self.room(running):
                self.search_next(db, queued_albums)
        finally:
            db.close()

    def room(self, running: int) -> bool:
        """Whether a search may start: few enough downloads in Lidarr's queue, or with oasis's own downloads, few
        enough active TorBox torrents."""
        if not self.downloads:
            return running < MAX_ACTIVE
        try:
            return self.downloads.room()
        except OSError as e:
            log.warning("TorBox: %s", e)
            return False

    def names(self, db, item) -> tuple[str, str | None]:
        column = "album_id" if item["kind"] == "album" else "artist_id"
        row = db.execute(f"select album_artist, album from track where {column} = ? limit 1", (item["ref"],)).fetchone() or \
            (db.execute("select a.name album_artist, r.title album from ytm_release r join ytm_artist a on a.artist_id = r.artist_id "
                        "where r.album_id = ?", (item["ref"],)).fetchone() if item["kind"] == "album" else None)
        if row is None and item["kind"] == "artist" and item["mbid"]:  # followed before any of its music arrived
            known = db.execute("select name from mb_artist where mbid = ?", (item["mbid"],)).fetchone()
            if name := known[0] if known else (self.lidarr.artist(item["mbid"]) or {}).get("artistName"):
                return name, None
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
            if (item["searched"] or 0) < time.time() - RECHECK_CATALOGUE:
                self.monitor_catalogue(db, item, artist, first=not item["searched"])
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
            return "sent", self.why_nothing(db, album["id"])
        return "sent", "waiting for its search" if not item["searched"] else "searching"

    def why_nothing(self, db, album_id: int) -> str:
        failed = db.execute("select error from download where album_id = ? and state = 'failed' order by updated desc limit 1",
                            (album_id,)).fetchone() if self.downloads else None
        return f"download failed: {failed[0]}" if failed else self.lidarr.why_nothing(album_id)

    def monitor_catalogue(self, db, item, artist, first: bool):
        """Monitors the artist's albums and EPs so the paced search downloads them, except repackages: best-ofs,
        deluxe and anniversary editions, re-recordings, whose songs are mostly on its earlier releases. Monitoring
        happens once per artist, so albums unmonitored in Lidarr afterwards stay that way; dropping repackages
        repeats daily, for those Lidarr monitors as new releases. Albums with files or starred are left alone."""
        albums = sorted((a for a in self.lidarr.albums_of(artist["id"]) if a.get("albumType") in CATALOGUE_TYPES),
                        key=lambda a: a.get("releaseDate") or "")
        if not albums:
            return  # first refresh still pending
        starred = {r[0] for r in db.execute("select mbid from lidarr_item where kind = 'album' and mbid is not null")}
        seen, keep = set(), set()
        for album in albums:
            songs = {song_key(t["title"]) for t in self.lidarr.tracks(album)}
            if not REPACKAGE.search(album["title"]) and not (songs and len(songs & seen) >= REPEATS * len(songs)):
                keep.add(album["id"])
            seen |= songs
        if first:
            self.lidarr.set_albums([a["id"] for a in albums if a["id"] in keep and not a["monitored"]], True)
        self.lidarr.set_albums([a["id"] for a in albums if a["id"] not in keep and a["monitored"] and a["foreignAlbumId"] not in starred
                                and not (a.get("statistics") or {}).get("trackFileCount")], False)
        db.execute("update lidarr_item set searched = ? where kind = ? and ref = ?", (time.time(), item["kind"], item["ref"]))

    def search_next(self, db, queued_albums: set):
        """Searches one album at a time, SEARCH_GAP apart and only while few downloads run: starred albums first, then
        followed artists' back catalogues, newest first; each time those never searched before ones that got nothing."""
        if time.time() - self.last_search < SEARCH_GAP:
            return
        item = db.execute("select * from lidarr_item where kind = 'album' and state = 'sent' and lidarr_id is not null "
                          "and (searched is null or searched < ?) and exists (select 1 from wanted w where w.kind = 'album' "
                          "and w.ref = lidarr_item.ref) order by searched is not null, searched, rowid limit 1", (time.time() - RESEARCH,)).fetchone()
        if item is not None and item["lidarr_id"] not in queued_albums:
            searched = self.search(db, item["lidarr_id"])
            self.last_search = time.time()
            db.execute("update lidarr_item set searched = ?, error = 'searching' where kind = ? and ref = ?",
                       (searched, item["kind"], item["ref"]))
        elif item is None:
            self.search_catalogue(db, queued_albums)

    def search(self, db, album_id: int) -> float:
        """Never raises: a search that fails still counts, so the next search moves on to another album. Returns the
        time to record as the album's last search: one TorBox had no free slot for is due again after RETRY_FULL."""
        now = time.time()
        try:
            if self.downloads:
                return now if self.downloads.search(db, album_id) else now - RESEARCH + RETRY_FULL
            hash = self.lidarr.search_album(album_id)
        except OSError as e:
            log.warning("search for Lidarr album %d failed: %s", album_id, e)
            return now
        if hash:  # the doctor imports it into this album
            db.execute("insert or replace into lidarr_rescue values (?, ?, ?)", (hash, album_id, now))
        return now

    def search_catalogue(self, db, queued_albums: set):
        followed = {r[0] for r in db.execute("select lidarr_id from lidarr_item where kind = 'artist' and lidarr_id is not null "
                                             "and exists (select 1 from wanted w where w.kind = 'artist' and w.ref = lidarr_item.ref)")}
        if not followed:
            return
        searched = dict(db.execute("select album_id, searched from catalogue_search").fetchall())
        due = lambda albums, every: [a for a in albums if a["artistId"] in followed and a["id"] not in queued_albums
                                     and searched.get(a["id"], 0) < time.time() - every]
        due = due(self.lidarr.missing(), RESEARCH) or due(self.lidarr.cutoff_unmet(), UPGRADE_AGAIN)  # then lossy ones, for lossless
        if not due:
            return
        # Never searched first, then albums and EPs before singles; stable, so Lidarr's newest-first order otherwise.
        album = min(due, key=lambda a: (a["id"] in searched, a.get("albumType") == "Single"))
        searched = self.search(db, album["id"])
        self.last_search = time.time()
        db.execute("insert or replace into catalogue_search values (?, ?)", (album["id"], searched))

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


def adoptable(db, lidarr: Lidarr) -> list[dict]:
    """Artists followed in Lidarr itself, which oasis doesn't follow. Those Lidarr has only for a starred album aren't
    followed: they are monitored for no new releases."""
    followed = {r[0] for r in db.execute("select mbid from lidarr_item where kind = 'artist' and mbid is not null")}
    return [a for a in lidarr.artists() if a["foreignArtistId"] not in followed and a["monitored"] and a.get("monitorNewItems") == "all"]


def adopt(db, lidarr: Lidarr, user: str, artists: list[dict], snapshot: Path):
    """Makes the user follow the artists in oasis, as if starred, so the paced search covers their missing albums. Their
    Lidarr monitoring is saved to snapshot first, for rollback: the first reconcile monitors their albums and EPs and
    drops repackages. The MusicBrainz id decides who each artist is: names like 'Hunter' are ambiguous."""
    snapshot.write_text(json.dumps([{**{k: a.get(k) for k in ("id", "artistName", "foreignArtistId", "monitored", "monitorNewItems")},
                                     "monitoredAlbumIds": [b["id"] for b in lidarr.albums_of(a["id"]) if b["monitored"]]}
                                    for a in artists], indent=1))
    db.execute("begin immediate")  # reconcile releases an item nobody wants: the row and its want land together
    try:
        for a in artists:
            id, now = artist_id(db, a["artistName"], a["foreignArtistId"]), time.time()
            db.execute("insert into lidarr_item(kind, ref, state, mbid, lidarr_id, updated) values ('artist', ?, 'followed', ?, ?, ?) "
                       "on conflict(kind, ref) do update set state = excluded.state, mbid = excluded.mbid, lidarr_id = excluded.lidarr_id, "
                       "error = null, retry_at = null, searched = null where lidarr_item.mbid is null", (id, a["foreignArtistId"], a["id"], now))
            db.execute("insert or ignore into wanted values (?, 'artist', ?)", (user, id))
            db.execute("insert into stat(id, starred) values (?, ?) on conflict(id) do update set starred = coalesce(stat.starred, excluded.starred)",
                       (id, now))
        db.execute("commit")
    except BaseException:
        db.execute("rollback")
        raise


def song_key(title: str) -> str:
    """'Du hast (Remastered 2017)', 'Du Hast - Live' -> 'duhast'."""
    return fold(re.sub(r"\s+-\s+.*$", "", re.sub(r"\s*[(\[][^)\]]*[)\]]", "", title)))


def set_state(db, item, state: str, error: str | None, retry: float | None = None):
    db.execute("update lidarr_item set state = ?, error = ?, retry_at = ?, updated = ? where kind = ? and ref = ?",
               (state, error, time.time() + retry if retry else None, time.time(), item["kind"], item["ref"]))
