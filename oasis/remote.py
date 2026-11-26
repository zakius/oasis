"""Calling the services plays are forwarded to (Last.fm, ListenBrainz), and telling a failure worth retrying
(OSError: down, busy, rate-limited) from a refusal (Rejected)."""
import urllib.error
import urllib.parse
import urllib.request


class Rejected(Exception):
    """The service refused this call or play for good; retrying won't help."""


def post(url: str, data: bytes, headers: dict) -> tuple[int, bytes]:
    return send(urllib.request.Request(url, data, headers))


def get(url: str) -> tuple[int, bytes]:
    return send(urllib.request.Request(url))


def send(request) -> tuple[int, bytes]:
    """The status and body, whatever the status: check tells what a failure was."""
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def check(status: int, body: bytes, service: str):
    if status == 429 or status >= 500:
        raise OSError(f"{service}: HTTP {status}")
    if status >= 400:
        raise Rejected(f"{service}: HTTP {status} {body[:200]!r}")


def web_address(url: str) -> str:
    """An address of a user's own server, which must be http or https with a host; ValueError otherwise."""
    url = url.strip()
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("Enter the server's address, starting with http:// or https://.")
    return url


def api_root(url: str) -> str:
    """The root of a user's own server's API (web_address), ending in / for the paths of its calls."""
    return web_address(url).rstrip("/") + "/"
