"""The few TorBox API calls oasis needs, with the account's key, which rdtclient holds. TorBox downloads torrents on
its own servers, and seeds there as oasis asks (each indexer's seeding mode, see download.py); nothing seeds or uploads
from here."""
import json
import os
import shutil
import sqlite3
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid

API = "https://api.torbox.app/v1/api/"
CHECK_AT_ONCE = 100  # hashes TorBox checks in one call


class RateLimited(OSError):
    """TorBox answered 429: wait before asking again."""


SEED = {"torbox": "1", "seed": "2", "off": "3"}  # createtorrent's seed: auto (the account's setting), always, never


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
        """Of these hashes, the ones TorBox has cached and would download at once."""
        return set(self.cached_files(hashes, files=False))

    def cached_files(self, hashes: list[str], files: bool = True) -> dict[str, list[dict]]:
        """Of these hashes, the ones TorBox has cached, with their files: {hash: [{name, size}]}; files False leaves
        those out."""
        found = []
        for i in range(0, len(hashes), CHECK_AT_ONCE):
            found += self.call(f"torrents/checkcached?format=list&list_files={str(files).lower()}&hash=" + ",".join(hashes[i:i + CHECK_AT_ONCE])) or []
        return {c["hash"].lower(): c.get("files") or [] for c in found}

    def add(self, name: str, magnet: str | None = None, torrent: bytes | None = None, mode: str = "off") -> dict:
        """Adds a magnet link or .torrent file: {torrent_id, hash}, or {queued_id, hash} when no active slot was free.
        mode: torbox leaves seeding to the account's setting (1), seed seeds it once downloaded (2), off never (3). It
        doesn't zip it, so files stay separate."""
        return self.call("torrents/createtorrent", form={"name": name, "seed": SEED[mode], "allow_zip": "false",
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


def active(torrents: list[dict]) -> int:
    """How many of the account's torrents (TorBox.torrents) take one of the plan's active slots."""
    return sum(bool(t["active"]) for t in torrents)


def read_copy(path: str, sql: str, params=()) -> list:
    """Reads another app's SQLite database from a copy, so its own writes never wait on us and it is never written."""
    with tempfile.TemporaryDirectory() as tmp:
        for suffix in ("", "-wal"):
            if os.path.exists(path + suffix):
                shutil.copy(path + suffix, os.path.join(tmp, "db" + suffix))
        db = sqlite3.connect(os.path.join(tmp, "db"))
        try:
            return db.execute(sql, params).fetchall()
        finally:
            db.close()


def from_rdtclient(rdt_db: str, make=TorBox):
    """A client (make(key)) with the TorBox key rdtclient's database holds; None while it holds none."""
    rows = read_copy(rdt_db, "select Value from Settings where SettingId = 'Provider:ApiKey'")
    return make(rows[0][0]) if rows and rows[0][0] else None


def multipart(fields: dict, boundary: str) -> bytes:
    """Form fields as multipart/form-data; bytes values go as a file."""
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"'.encode() +
             (b'; filename="release.torrent"\r\nContent-Type: application/x-bittorrent\r\n\r\n' + v if isinstance(v, bytes)
              else b"\r\n\r\n" + str(v).encode()) + b"\r\n" for k, v in fields.items()]
    return b"".join(parts) + f"--{boundary}--\r\n".encode()
