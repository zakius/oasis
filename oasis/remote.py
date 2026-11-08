"""Calling the services plays are forwarded to (Last.fm, ListenBrainz), and telling a failure worth retrying
(OSError: down, busy, rate-limited) from a refusal (Rejected)."""
import urllib.error
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
