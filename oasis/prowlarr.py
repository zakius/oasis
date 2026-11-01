"""The few Prowlarr API calls oasis's own search needs: it asks the music indexers directly, so release names are read
by oasis (releases.py), not by Lidarr's parser."""
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

log = logging.getLogger(__name__)

TIMEOUT = 90  # seconds: indexers behind FlareSolverr are slow
AUDIO = 3000
# Categories a search skips on an indexer without Audio (RuTor files music under Other): Movies, TV, XXX, by number or name.
VIDEO = re.compile(r"movie|tv|xxx|anime|video|hdtv|dvd|фильм|сериал|кино", re.I)


def music_categories(indexer: dict) -> list[int]:
    """Audio when the indexer has it; else its categories but video ones, where it files music among the rest."""
    found = indexer.get("capabilities", {}).get("categories", [])
    if any(c["id"] == AUDIO for c in found):
        return [AUDIO]
    return [c["id"] for c in found if not (2000 <= c["id"] < 3000 or 5000 <= c["id"] < 7000 or VIDEO.search(c["name"]))]


class Prowlarr:
    def __init__(self, url: str, api_key: str, tag: int = 2):
        """tag: the Prowlarr tag of the music indexers; anime's stay out of music searches."""
        self.url, self.key, self.tag = url.rstrip("/"), api_key, tag

    def call(self, path: str):
        request = urllib.request.Request(f"{self.url}/api/v1/{path}", headers={"X-Api-Key": self.key})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as e:
            raise OSError(f"Prowlarr {path.split('?')[0]}: {e.code} {e.read()[:200].decode(errors='replace')}") from e

    def indexers(self) -> dict[int, list[int]]:
        """The enabled music indexers Prowlarr hasn't paused after failures, with the categories to search them in."""
        now = datetime.now(timezone.utc).isoformat()
        paused = {s["indexerId"] for s in self.call("indexerstatus") if (s.get("disabledTill") or "") > now}
        return {i["id"]: music_categories(i) for i in self.call("indexer")
                if i["enable"] and self.tag in i["tags"] and i["protocol"] == "torrent" and i["id"] not in paused}

    def search(self, query: str) -> list[dict]:
        """Torrents in each indexer's music categories (music_categories): title, size, seeders, infoHash, magnetUrl/downloadUrl (Prowlarr's proxy), guid, indexerId.
        Each indexer is asked on its own, so a slow or broken one costs only its own results; OSError when all fail."""
        ids = self.indexers()
        ask = lambda i: self.call("search?" + urllib.parse.urlencode([("query", query), ("type", "search"), *(("categories", c) for c in ids[i]),
                                                                       ("limit", 100), ("indexerIds", i)])) or []
        found, errors = [], []
        with ThreadPoolExecutor(8) as pool:
            for i, job in [(i, pool.submit(ask, i)) for i in ids]:
                try:
                    found += [r for r in job.result() if r.get("protocol") == "torrent"]
                except OSError as e:
                    log.info("Prowlarr indexer %s on %r: %s", i, query, " ".join(str(e).split())[:160])
                    errors.append(e)
        if ids and len(errors) == len(ids):
            raise errors[0]
        return found
