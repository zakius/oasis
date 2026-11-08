"""Torrents as oasis reads them without a client: magnet links by info hash, and .torrent files' bencoded data, for the
hash TorBox will know a release by and the files it will list."""
import hashlib
import re


def magnet(hash: str) -> str:
    return f"magnet:?xt=urn:btih:{hash}"


def info_hash(found: dict) -> str:
    """The hash of a magnet link or .torrent ({magnet} or {torrent}, as download.source finds them): the link's, or
    the hash of the .torrent's info dictionary."""
    if m := re.search(r"btih:([0-9a-f]{40})", found.get("magnet") or "", re.I):
        return m[1].lower()
    data, i = found.get("torrent") or b"", 1
    try:
        while data[:1] == b"d" and data[i:i + 1] != b"e":
            key_end = bencoded_end(data, i)
            value_end = bencoded_end(data, key_end)
            if data[i:key_end] == b"4:info":
                return hashlib.sha1(data[key_end:value_end]).hexdigest()
            i = value_end
    except (ValueError, IndexError):  # not a .torrent
        pass
    return ""


def bencoded_end(data: bytes, i: int) -> int:
    """Where the bencoded value starting at i ends."""
    kind = data[i:i + 1]
    if kind == b"i":
        return data.index(b"e", i) + 1
    if kind in (b"l", b"d"):
        i += 1
        while data[i:i + 1] != b"e":
            i = bencoded_end(data, i)
        return i + 1
    colon = data.index(b":", i)
    return colon + 1 + int(data[i:colon])


def bdecoded(data: bytes, i: int = 0):
    """The bencoded value starting at i: ints, bytes, lists and dicts with bytes keys."""
    kind, end = data[i:i + 1], bencoded_end(data, i)
    if kind == b"i":
        return int(data[i + 1:end - 1])
    if kind in (b"l", b"d"):
        items, i = [], i + 1
        while i < end - 1:
            items.append(bdecoded(data, i))
            i = bencoded_end(data, i)
        return dict(zip(items[::2], items[1::2])) if kind == b"d" else items
    return data[data.index(b":", i) + 1:end]


def torrent_files(data: bytes) -> list[dict]:
    """A .torrent's files as TorBox lists them, {name, size}, the torrent's name their first folder; [] when it isn't one."""
    try:
        info = bdecoded(data)[b"info"]
        text = lambda key, d: (d.get(key + b".utf-8") or d[key]).decode(errors="replace")
        name = text(b"name", info)
        if b"files" not in info:
            return [{"name": name, "size": info[b"length"]}]
        return [{"name": "/".join([name, *(p.decode(errors="replace") for p in f.get(b"path.utf-8") or f[b"path"])]), "size": f[b"length"]}
                for f in info[b"files"]]
    except (ValueError, IndexError, KeyError, TypeError, AttributeError):
        return []
