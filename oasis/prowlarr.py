"""The few Prowlarr API calls oasis's own search needs: it asks the music indexers directly, so release names are read
by oasis (releases.py), not by Lidarr's parser."""
import ipaddress
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlparse

log = logging.getLogger(__name__)

TIMEOUT = 90  # seconds: indexers behind FlareSolverr are slow
AUDIO = 3000
# Categories a search skips on an indexer without Audio (some file music under Other): Movies, TV, XXX, by number or name.
VIDEO_CATEGORIES = re.compile(r"movie|tv|xxx|anime|video|hdtv|dvd|фильм|сериал|кино", re.I)


def music_categories(indexer: dict) -> list[int]:
    """Audio when the indexer has it; else its categories but video ones, where it files music among the rest."""
    found = indexer.get("capabilities", {}).get("categories", [])
    if any(c["id"] == AUDIO for c in found):
        return [AUDIO]
    return [c["id"] for c in found if not (2000 <= c["id"] < 3000 or 5000 <= c["id"] < 7000 or VIDEO_CATEGORIES.search(c["name"]))]


def site(url: str) -> str:
    """A URL's site by name, the same for each of its domains: 'example' for example.org and example.net alike."""
    parts = (urlparse(url).hostname or "").split(".")
    return parts[-2 if len(parts) > 1 else 0]


def local(indexer: dict) -> bool:
    """Whether the indexer runs on this network (Local-DHT's own DHT index at http://local-dht:3333): no tracker, no login,
    though Prowlarr's Generic Torznab reports every indexer private."""
    for url in indexer.get("indexerUrls") or []:
        host = urllib.parse.urlparse(url).hostname or ""
        try:
            if ipaddress.ip_address(host).is_private:
                return True
        except ValueError:
            if host == "localhost" or host and "." not in host:
                return True
    return False


def privacy(indexer: dict) -> str:
    """public, semiPrivate, private (a login), or local (see local)."""
    return "local" if local(indexer) else indexer.get("privacy") or "public"


def failures(text: bytes) -> str:
    """The error messages of a Prowlarr validation answer, without the attemptedValue each also holds."""
    try:
        found = json.loads(text)
    except ValueError:
        return ""
    found = found if isinstance(found, list) else [found]
    return "; ".join(dict.fromkeys(m for f in found if isinstance(f, dict) and (m := str(f.get("errorMessage") or f.get("message") or ""))))


class Prowlarr:
    def __init__(self, settings):
        """settings: config.Prowlarr; its tag marks the music indexers, so anime's stay out of music searches."""
        self.url, self.key, self.tag, self.ui_url = settings.url.rstrip("/"), settings.api_key, settings.tag, settings.ui_url
        self.names: dict[int, str] = {}   # indexer id -> name: see tracker
        self.logins: dict[int, str] = {}  # the same, of those needing a login (private, semi-private): their default seeding differs
        self.errors: dict[int, tuple[float, str]] = {}  # indexer id -> (when, what) of its last failed search, for the indexers page

    def call(self, path: str, body=None, method: str | None = None):
        """A write (body) that fails raises OSError of Prowlarr's error messages only: its validation answer also echoes
        the values sent, credentials among them, which must reach neither the page nor the log."""
        request = urllib.request.Request(f"{self.url}/api/v1/{path}", method=method, data=None if body is None else json.dumps(body).encode(),
                                         headers={"X-Api-Key": self.key, **({"Content-Type": "application/json"} if body is not None else {})})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return json.loads(response.read() or b"null")
        except urllib.error.HTTPError as e:
            text = e.read()
            if body is None:
                raise OSError(f"Prowlarr {path.split('?')[0]}: {e.code} {text[:200].decode(errors='replace')}") from None
            raise OSError(f"Prowlarr: {failures(text) or e.code}") from None

    def save(self, indexer: dict, force: bool = False) -> dict:
        """PUTs the indexer back as GET gave it, with the changes made: masked values go back as they came, which
        Prowlarr reads as unchanged. Prowlarr tests the indexer first unless force (its forceSave)."""
        return self.call(f"indexer/{indexer['id']}{'?forceSave=true' if force else ''}", indexer, "PUT")

    def test(self, indexer: dict):
        """Prowlarr's own test of the indexer as it is; OSError of why it fails."""
        self.call("indexer/test", indexer, "POST")

    def indexers(self) -> dict[int, list[int]]:
        """The enabled music indexers Prowlarr hasn't paused after failures, with the categories to search them in."""
        now = datetime.now(timezone.utc).isoformat()
        paused = {s["indexerId"] for s in self.call("indexerstatus") if (s.get("disabledTill") or "") > now}
        found = [i for i in self.call("indexer") if i["enable"] and self.tag in i["tags"] and i["protocol"] == "torrent"]
        self.names |= {i["id"]: i.get("name") for i in found}
        self.logins |= {i["id"]: i["name"] for i in found if privacy(i) not in ("public", "local")}
        return {i["id"]: music_categories(i) for i in found if i["id"] not in paused}

    def every(self) -> list[dict]:
        """Every indexer Prowlarr has, music or not, with its settings."""
        return self.call("indexer")

    def indexer(self, id: int) -> dict:
        """One indexer as Prowlarr holds it, its login fields as Prowlarr gives them (the password-type ones masked)."""
        return self.call(f"indexer/{id}")

    def overview(self) -> dict:
        """Prowlarr's view of every indexer, music or not, for the indexers page: indexers (their settings), status and
        stats (its health and counts, by indexer id), tags (id -> label)."""
        return {"indexers": self.every(), "status": {s["indexerId"]: s for s in self.call("indexerstatus")},
                "stats": {s["indexerId"]: s for s in (self.call("indexerstats") or {}).get("indexers") or []},
                "tags": {t["id"]: t["label"] for t in self.call("tag")}}

    def site_indexers(self, indexers: list[dict]) -> dict[str, int]:
        """Each site's indexer (site), of these indexers' URLs. A site several share (an anime site and its music twin)
        goes to an enabled music one: oasis searches only those."""
        return {site(u): i["id"] for i in sorted(indexers, key=lambda i: (i["enable"], self.tag in i.get("tags", [])))
                for u in [*(i.get("indexerUrls") or []), *(i.get("legacyUrls") or [])] if urlparse(u).hostname}

    def tracker(self, release: dict) -> str | None:
        """The name of the indexer a search result comes from, whose seeding rule it follows (see download.py)."""
        return self.names.get(release.get("indexerId"))

    def search(self, query: str) -> list[dict]:
        """Torrents in each indexer's music categories (music_categories): title, size, seeders, infoHash, magnetUrl/downloadUrl (Prowlarr's proxy), guid, indexerId.
        Each indexer is asked on its own, so a slow or broken one costs only its own results; OSError when all fail, or
        none is enabled and unpaused: a search that asked nobody found nothing."""
        if not (ids := self.indexers()):
            raise OSError("Prowlarr: no music indexer enabled and unpaused")
        ask = lambda i: self.call("search?" + urllib.parse.urlencode([("query", query), ("type", "search"), *(("categories", c) for c in ids[i]),
                                                                       ("limit", 100), ("indexerIds", i)])) or []
        found, errors = [], []
        with ThreadPoolExecutor(8) as pool:
            for i, job in [(i, pool.submit(ask, i)) for i in ids]:
                try:
                    found += [r for r in job.result() if r.get("protocol") == "torrent"]
                except OSError as e:
                    log.info("Prowlarr indexer %s on %r: %s", i, query, " ".join(str(e).split())[:160])
                    self.errors[i] = time.time(), " ".join(str(e).split())[:300]
                    errors.append(e)
        if len(errors) == len(ids):
            raise errors[0]
        return found
