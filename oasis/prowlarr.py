"""The few Prowlarr API calls oasis's own search needs: it asks the music indexers directly, so release names are read
by oasis (releases.py), not by Lidarr's parser."""
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger(__name__)

TIMEOUT = 90  # seconds: indexers behind FlareSolverr are slow


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

    def indexers(self) -> list[int]:
        return [i["id"] for i in self.call("indexer") if i["enable"] and self.tag in i["tags"] and i["protocol"] == "torrent"]

    def search(self, query: str) -> list[dict]:
        """Torrents in the audio categories: title, size, seeders, infoHash, magnetUrl/downloadUrl (Prowlarr's proxy), guid, indexerId.
        Each indexer is asked on its own, so a slow or broken one costs only its own results; OSError when all fail."""
        ask = lambda i: self.call("search?" + urllib.parse.urlencode([("query", query), ("type", "search"), ("categories", 3000),
                                                                       ("limit", 100), ("indexerIds", i)])) or []
        found, errors, ids = [], [], self.indexers()
        with ThreadPoolExecutor(8) as pool:
            for i, job in [(i, pool.submit(ask, i)) for i in ids]:
                try:
                    found += [r for r in job.result() if r.get("protocol") == "torrent"]
                except OSError as e:
                    log.info("Prowlarr indexer %s on %r: %s", i, query, e)
                    errors.append(e)
        if ids and len(errors) == len(ids):
            raise errors[0]
        return found
