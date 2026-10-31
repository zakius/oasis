"""The one way oasis asks MusicBrainz anything. MusicBrainz allows one request a second per client, so every request
of the process (credits, imports) waits its turn on one lock; when it answers 503 (busy) or another 5xx, everyone
backs off. Answers can be kept in mb_cache for callers that ask the same thing again."""
import json
import threading
import time
import urllib.error
import urllib.request

USER_AGENT = "oasis/0.1 ( https://github.com/zakius/oasis )"
GAP = 1.1                 # seconds between requests
BACKOFF = (5, 15, 45)     # seconds every request waits after a 5xx, before each retry

lock = threading.Lock()
next_at = 0.0             # earliest time of the next request


def get(url: str, db=None, keep: float = 0) -> dict:
    """The JSON at url. LookupError when MusicBrainz doesn't have it, OSError when it stays unreachable or busy.
    db and keep: an answer cached up to keep seconds ago is used, and a fresh one stored."""
    global next_at
    if db is not None and keep:
        row = db.execute("select body from mb_cache where url = ? and fetched > ?", (url, time.time() - keep)).fetchone()
        if row:
            return json.loads(row[0])
    for backoff in (*BACKOFF, None):
        with lock:
            time.sleep(max(0.0, next_at - time.time()))
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}),
                                            timeout=60) as response:
                    body = response.read()
                break
            except urllib.error.HTTPError as e:
                if e.code in (400, 404):  # 400: not a valid id
                    raise LookupError(url) from e
                if e.code < 500 or backoff is None:
                    raise OSError(f"{url}: {e.code}") from e
                next_at = time.time() + backoff
                continue
            finally:
                next_at = max(next_at, time.time() + GAP)
    if db is not None and keep:
        db.execute("insert or replace into mb_cache values (?, ?, ?)", (url, body.decode(), time.time()))
    return json.loads(body)
