"""The few TorBox API calls oasis needs. TorBox downloads torrents on its own servers, and seeds there the ones oasis
asks it to (login trackers'); nothing seeds or uploads from here."""
import json
import urllib.error
import urllib.parse
import urllib.request
import uuid

API = "https://api.torbox.app/v1/api/"


class RateLimited(OSError):
    """TorBox answered 429: wait before asking again."""


class TorBox:
    def __init__(self, key: str):
        self.key = key

    def call(self, path: str, body=None, form: dict | None = None):
        data, kind = (multipart(form, boundary := uuid.uuid4().hex), f"multipart/form-data; boundary={boundary}") if form else \
            (json.dumps(body).encode() if body else None, "application/json")
        request = urllib.request.Request(API + path, data, {"Authorization": "Bearer " + self.key, "Content-Type": kind, "User-Agent": "oasis/0.1"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                answer = json.loads(response.read())
        except urllib.error.HTTPError as e:
            raise (RateLimited if e.code == 429 else OSError)(f"TorBox {path.split('?')[0]}: {e.code} {e.read()[:200].decode(errors='replace')}") from e
        if answer.get("success") is False:
            raise OSError(f"TorBox {path.split('?')[0]}: {answer.get('error')} {answer.get('detail')}")
        return answer.get("data")

    def torrents(self) -> list[dict]:
        return self.call("torrents/mylist?bypass_cache=true") or []

    def cached(self, hashes: list[str]) -> set[str]:
        """Of these hashes (at most 100), the ones TorBox has cached and would download at once."""
        found = self.call("torrents/checkcached?format=list&list_files=false&hash=" + ",".join(hashes)) if hashes else None
        return {c["hash"].lower() for c in found or []}

    def cached_files(self, hashes: list[str]) -> dict[str, list[dict]]:
        """Of these hashes (at most 100), the ones TorBox has cached, with their files: {hash: [{name, size}]}."""
        found = self.call("torrents/checkcached?format=list&list_files=true&hash=" + ",".join(hashes)) if hashes else None
        return {c["hash"].lower(): c.get("files") or [] for c in found or []}

    def add(self, name: str, magnet: str | None = None, torrent: bytes | None = None, seed: bool = False) -> dict:
        """Adds a magnet link or .torrent file: {torrent_id, hash}, or {queued_id, hash} when no active slot was free.
        seed: TorBox seeds it once downloaded (2), else never (3), so it leaves the active slots then. It doesn't zip
        it, so files stay separate."""
        return self.call("torrents/createtorrent", form={"name": name, "seed": "2" if seed else "3", "allow_zip": "false",
                                                         **({"magnet": magnet} if magnet else {"file": torrent})})

    def link(self, torrent_id: int, file_id: int) -> str:
        """A download URL for one file of a finished torrent."""
        return self.call("torrents/requestdl?" + urllib.parse.urlencode({"token": self.key, "torrent_id": torrent_id, "file_id": file_id}))

    def delete_torrent(self, torrent_id: int):
        self.call("torrents/controltorrent", {"torrent_id": torrent_id, "operation": "delete"})

    def queued(self) -> list[dict]:
        """Adds TorBox holds back until one of the plan's active slots is free."""
        return self.call("queued/getqueued?type=torrent&bypass_cache=true") or []

    def delete_queued(self, queued_id: int):
        self.call("queued/controlqueued", {"queued_id": queued_id, "operation": "delete"})


def multipart(fields: dict, boundary: str) -> bytes:
    """Form fields as multipart/form-data; bytes values go as a file."""
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"'.encode() +
             (b'; filename="release.torrent"\r\nContent-Type: application/x-bittorrent\r\n\r\n' + v if isinstance(v, bytes)
              else b"\r\n\r\n" + str(v).encode()) + b"\r\n" for k, v in fields.items()]
    return b"".join(parts) + f"--{boundary}--\r\n".encode()
