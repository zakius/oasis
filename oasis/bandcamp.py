"""Each user's Bandcamp, with their sign-in (the identity cookie of bandcamp.com), while they can use its account
features: their purchases imported into the library as FLAC, each matched to a MusicBrainz edition as downloads are
(importer.py), and the albums on their wishlist wanted for them (acquire.py). Both are read every REFRESH, through the
fan collection API Bandcamp's own fan pages use. bandcamp_item keeps what became of each item: one imported, wanted or
already here is left alone; one MusicBrainz doesn't know, or whose import failed, is tried again after RETRY."""
import json
import logging
import re
import shutil
import time
from html import unescape
from pathlib import Path

from . import musicbrainz, worker
from .acquire import LOSSLESS_TRACK
from .musicbrainz import lucene
from .sources import request, save
from .text import fold
from .tracks import MB_IDS

log = logging.getLogger(__name__)

API = "https://bandcamp.com/api/"
REFRESH = 6 * 3600
RETRY = 7 * 86400
PAGE = 100      # items asked for at a time
STAT_TRIES = 3  # times a download is asked to be readied, each with the fresh link the last answer gave


class Fan:
    """A Bandcamp fan's collection, with their sign-in."""

    def __init__(self, identity: str):
        self.headers = {"Cookie": "identity=" + identity}

    def summary(self) -> dict:
        """The fan's id and name: fan_id, and collection_summary's username. ValueError when the sign-in is refused."""
        found = request("GET", API + "fan/2/collection_summary", headers=self.headers).json()
        if found.get("error") or not found.get("fan_id"):
            raise ValueError(f"Bandcamp didn't accept that sign-in ({found.get('error_message') or 'no fan'}): copy it again.")
        return found

    def items(self, fan_id: int, kind: str) -> tuple[list[dict], dict[str, str]]:
        """The fan's collection (purchases) or wishlist, newest first, page by page, and the collection's download
        pages by sale ('<sale_item_type><sale_item_id>')."""
        token, items, pages = f"{int(time.time())}::a::", [], {}
        while True:
            body = json.dumps({"fan_id": fan_id, "older_than_token": token, "count": PAGE}).encode()
            response = request("POST", f"{API}fancollection/1/{kind}_items", body=body, headers={**self.headers, "Content-Type": "application/json"})
            if response.status != 200:
                raise OSError(f"HTTP {response.status} from Bandcamp's {kind}")
            found = response.json()
            items += found.get("items") or []
            pages |= found.get("redownload_urls") or {}
            if not found.get("more_available") or not found.get("last_token"):
                return items, pages
            token = found["last_token"]

    def flac(self, page: str) -> str:
        """The link a purchase's FLAC downloads from. Its download page's data names it; Bandcamp readies the file
        first, and statdownload says when it is ready, with the link to fetch, or gives a fresh one to ask again with
        (the page's has expired)."""
        html = request("GET", page, headers=self.headers).data.decode(errors="replace")
        blob = re.search(r'id="pagedata" data-blob="([^"]+)"', html)
        if not blob:
            raise LookupError("Bandcamp's download page has no data")
        items = json.loads(unescape(blob[1])).get("digital_items") or [{}]
        url = ((items[0].get("downloads") or {}).get("flac") or {}).get("url")
        if not url:
            raise LookupError("Bandcamp has no FLAC of this purchase")
        for _ in range(STAT_TRIES):
            text = request("GET", url.replace("/download/", "/statdownload/", 1) + "&.vrs=1", headers=self.headers).data.decode(errors="replace")
            if (at := text.find('{"result"')) < 0:
                raise LookupError("Bandcamp didn't say whether the download is ready")
            stat = json.JSONDecoder().raw_decode(text, at)[0]
            if stat.get("result") == "ok" and stat.get("download_url"):
                return stat["download_url"]
            if not stat.get("retry_url"):
                break
            url = stat["retry_url"]
        raise LookupError(f"Bandcamp didn't ready the download ({stat.get('errortype')})")


def release_group(db, artist: str, title: str, page: str | None) -> str | None:
    """The MusicBrainz release group of a Bandcamp album or track: that of a release MusicBrainz links to its page,
    else the one of that title by the artist of that name or alias (as acquire.py finds a starred album)."""
    for release in musicbrainz.releases_at(page, db) if page else []:
        return musicbrainz.release(release, db)["release-group"]["id"]
    for mbid in musicbrainz.search_artist(artist):
        groups = musicbrainz.search("release-group", f"releasegroup:{lucene(title)} AND arid:{mbid}", db)
        if found := next((g["id"] for g in groups if fold(g["title"]) == fold(title)), None):
            return found
    return None


def fetch(url: str, folder: Path) -> Path:
    """Downloads a purchase into folder: an album comes as a zip, which the import unpacks, a track as its FLAC."""
    part = folder / "purchase"
    save(url, part)
    with part.open("rb") as f:
        kind = {b"PK\x03\x04": "zip", b"fLaC": "flac"}.get(f.read(4))
    if not kind:
        raise LookupError("Bandcamp's download is neither a zip nor a FLAC")
    return part.rename(folder / f"purchase.{kind}")


class Bandcamp:
    def __init__(self, connect, services, importer, staging: Path, want, imported=lambda: None):
        """services: each user's services (sources.Sources). importer: importer.py's, for purchases. staging: where
        purchases download to. want(db, user, ids): the user wants these albums (Oasis.want). imported(): a purchase
        is in the library."""
        self.connect, self.services, self.importer, self.staging = connect, services, importer, staging
        self.want, self.imported = want, imported

    def start(self):
        worker.start("bandcamp", self.refresh_all, REFRESH)

    def refresh_all(self):
        db = self.connect()
        users = [r[0] for r in db.execute("select user from user_service where service = 'bandcamp' and account is not null")]
        db.close()
        for user in users:
            self.refresh(user)

    def refresh(self, user: str):
        """Imports the user's new purchases and wants their wishlist's new albums. A failure is logged; an item that
        failed on the network is tried at the next refresh."""
        db = self.connect()
        try:
            account = self.services.account(db, user, "bandcamp")
            if not account:
                return
            fan = Fan(account["identity"])
            purchases, pages = fan.items(account["fan_id"], "collection")
            for item in purchases:
                key = f"{item.get('sale_item_type')}{item.get('sale_item_id')}"
                if key in pages and self.due(db, user, "purchase", key):
                    self.purchase(db, user, fan, item, key, pages[key])
            wishes, _ = fan.items(account["fan_id"], "wishlist")
            for item in wishes:
                key = f"{item.get('tralbum_type')}{item.get('tralbum_id')}"
                if self.due(db, user, "wish", key):
                    self.wish(db, user, item, key)
        except Exception as e:
            log.warning("bandcamp for %s failed: %s", user, e)
        finally:
            db.close()

    @staticmethod
    def due(db, user: str, kind: str, key: str) -> bool:
        row = db.execute("select state, tried from bandcamp_item where user = ? and kind = ? and id = ?", (user, kind, key)).fetchone()
        return row is None or row["state"] in ("unknown", "failed") and row["tried"] < time.time() - RETRY

    @staticmethod
    def note(db, user: str, kind: str, key: str, item: dict, title: str, rg: str | None, state: str, error: str | None = None):
        db.execute("insert or replace into bandcamp_item values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (user, kind, key, item.get("band_name"), title, rg, state, error, time.time()))

    def purchase(self, db, user: str, fan: Fan, item: dict, key: str, page: str):
        """Imports a purchase as an edition of its release group, unless the library has that album lossless."""
        title = item.get("item_title") or ""
        try:
            rg = release_group(db, item.get("band_name") or "", title, item.get("item_url"))
            if rg is None:
                return self.note(db, user, "purchase", key, item, title, None, "unknown")
            if db.execute(f"select 1 from track t where t.mb_release_group = ? and t.source = 'local' and {LOSSLESS_TRACK}", (rg,)).fetchone():
                return self.note(db, user, "purchase", key, item, title, rg, "here")
            folder = self.staging / key
            shutil.rmtree(folder, ignore_errors=True)
            folder.mkdir(parents=True)
            try:
                fetch(fan.flac(page), folder)
                self.importer.place(folder, rg)
            finally:
                shutil.rmtree(folder, ignore_errors=True)
        except OSError as e:
            log.warning("bandcamp purchase %s for %s: %s", title, user, e)
        except LookupError as e:
            log.info("bandcamp purchase %s for %s failed: %s", title, user, e)
            self.note(db, user, "purchase", key, item, title, None, "failed", str(e)[:300])
        else:
            log.info("bandcamp purchase %s for %s imported", title, user)
            self.note(db, user, "purchase", key, item, title, rg, "imported")
            self.imported()

    def wish(self, db, user: str, item: dict, key: str):
        """Wants a wished album for the user; a wished track, its album, or the single when it is on none."""
        on_album = item.get("tralbum_type") == "t" and item.get("album_title")
        title = (item.get("album_title") if on_album else item.get("item_title")) or ""
        try:
            rg = release_group(db, item.get("band_name") or "", title, None if on_album else item.get("item_url"))
        except (OSError, LookupError) as e:
            log.warning("bandcamp wish %s for %s: %s", title, user, e)
            return
        if rg:
            self.want(db, user, [MB_IDS["album"] + rg])
        self.note(db, user, "wish", key, item, title, rg, "wanted" if rg else "unknown")
