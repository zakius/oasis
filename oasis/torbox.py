"""The few TorBox API calls oasis needs. TorBox downloads torrents on its own servers, so nothing seeds from here."""
import json
import urllib.error
import urllib.request

API = "https://api.torbox.app/v1/api/"


class TorBox:
    def __init__(self, key: str):
        self.key = key

    def call(self, path: str, body=None):
        request = urllib.request.Request(API + path, json.dumps(body).encode() if body else None,
                                         {"Authorization": "Bearer " + self.key, "Content-Type": "application/json", "User-Agent": "oasis/0.1"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read()).get("data")
        except urllib.error.HTTPError as e:
            raise OSError(f"TorBox {path.split('?')[0]}: {e.code} {e.read()[:200].decode(errors='replace')}") from e

    def torrents(self) -> list[dict]:
        return self.call("torrents/mylist?bypass_cache=true") or []

    def cached(self, hashes: list[str]) -> set[str]:
        """Of these hashes (at most 100), the ones TorBox has cached and would download at once."""
        found = self.call("torrents/checkcached?format=list&list_files=false&hash=" + ",".join(hashes)) if hashes else None
        return {c["hash"].lower() for c in found or []}

    def delete_torrent(self, torrent_id: int):
        self.call("torrents/controltorrent", {"torrent_id": torrent_id, "operation": "delete"})

    def queued(self) -> list[dict]:
        """Adds TorBox holds back until one of the plan's active slots is free."""
        return self.call("queued/getqueued?type=torrent&bypass_cache=true") or []

    def delete_queued(self, queued_id: int):
        self.call("queued/controlqueued", {"queued_id": queued_id, "operation": "delete"})
