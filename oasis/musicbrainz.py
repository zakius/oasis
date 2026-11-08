"""The one way oasis asks MusicBrainz anything: callers ask for an artist, a release, a release group's editions...
by name, and only this module knows the URLs and how long each answer is kept in mb_cache. MusicBrainz allows one
request a second per client, so every request of the process (credits, imports) waits its turn on one lock; when it
answers 503 (busy) or another 5xx, everyone backs off. A lookup given no db asks afresh and keeps nothing. The
vocabulary of its answers that oasis reads in several places is here too: ids, release group types, medium formats,
aliases, artist credits."""
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .text import fold

USER_AGENT = "oasis/0.1 ( https://github.com/zakius/oasis )"
GAP = 1.1
BACKOFF = (5, 15, 45)     # seconds every request waits after a 5xx, before each retry

MB = "https://musicbrainz.org/ws/2/"
ARTIST = MB + "artist/{}?fmt=json&inc=aliases+artist-rels"
RELEASE = MB + "release/{}?inc=media+recordings+artist-credits+labels+release-groups+isrcs+release-rels&fmt=json"
RELEASE_CREDITS = MB + "release/{}?fmt=json&inc=artist-credits+recordings+isrcs+labels+genres+" \
                  "release-groups+artist-rels+recording-level-rels+work-rels+work-level-rels"
GROUP = MB + "release-group/{}?inc=artist-credits&fmt=json"
EDITIONS = MB + "release?release-group={}&inc=media+recordings+artist-credits+labels+release-groups+isrcs&limit=100&offset={}&fmt=json"
OFFICIAL_EDITIONS = MB + "release?release-group={}&status=official&inc=recordings&limit=100&fmt=json"
CATALOGUE = MB + "release-group?artist={}&type=album|ep|single&inc=artist-credits+aliases&limit=100&offset={}&fmt=json"
PSEUDO = MB + "release?artist={}&status=pseudo-release&inc=release-groups&limit=100&offset={}&fmt=json"
SEARCH = MB + "{}?query={}&limit=25&fmt=json"
DISC = MB + "discid/{}?inc=artist-credits+recordings+labels&cdstubs=no&fmt=json"

KEEP_ARTIST = 30 * 86400    # seconds each answer stays in mb_cache, for lookups given a db
KEEP_RELEASE = 7 * 86400
KEEP_GROUP = 30 * 86400
KEEP_EDITIONS = 7 * 86400
KEEP_CATALOGUE = 23 * 3600  # acquire reads a followed artist's catalogue daily: fresh then, and searches in between ask nothing
KEEP_SEARCH = 7 * 86400

MBID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
TYPES = ("Album", "EP", "Single")  # the primary types oasis wants and shows
VIDEO_MEDIA = re.compile(r"video|^dvd$|blu-ray|hd-dvd|vhs|^s?vcd$|laserdisc|umd", re.I)  # medium formats; DVD-Audio is audio

lock = threading.Lock()
next_at = 0.0


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


def pages(url: str, kind: str, db, keep: float) -> list[dict]:
    """All of a browse's kind ('release-group'), page by page; url has {} for the offset."""
    found = []
    while True:
        page = get(url.format(len(found)), db, keep)
        found += page.get(kind + "s") or []
        if not page.get(kind + "s") or len(found) >= page.get(kind + "-count", 0):
            return found


def aliases(entity: dict) -> list[str]:
    """An artist's or release group's other names: its aliases, but not search hints ('LP' for Linkin Park), which
    aren't names."""
    return [a["name"] for a in entity.get("aliases") or [] if a.get("type") != "Search hint"]


def credited(credit: list[dict]) -> str:
    """An artist credit as MusicBrainz writes it: 'Beyoncé feat. Jay-Z'."""
    return "".join(c["name"] + (c.get("joinphrase") or "") for c in credit)


def by_date(groups: list[dict]) -> list[dict]:
    """The albums, EPs and singles (TYPES) among the release groups, oldest first, undated ones last."""
    return sorted((g for g in groups if g.get("primary-type") in TYPES), key=lambda g: g.get("first-release-date") or "9999")


def type_name(kind: str) -> str:
    """A release type as MusicBrainz writes it ('Album', 'EP', 'Single', 'Compilation'), from a tag's lower case ('ep')."""
    return "EP" if kind.lower() == "ep" else kind.title()


def is_pseudo(release: dict) -> bool:
    """Whether the release is a pseudo-release: a transliteration or translation of another, with the same tracks."""
    return release.get("status") == "Pseudo-Release"


def artist(mbid: str, db=None) -> dict:
    """The artist with its aliases and its relationships to other artists (members, other names)."""
    return get(ARTIST.format(mbid), db, KEEP_ARTIST)


def release(mbid: str, db=None) -> dict:
    """The release with its media, tracks, recordings, credits, labels, release group, ISRCs and links to other
    releases (transliterations)."""
    return get(RELEASE.format(urllib.parse.quote(mbid, safe="")), db, KEEP_RELEASE)


def release_credits(mbid: str) -> dict:
    """The release with everything credits.py reads: also genres and the relationships of its recordings and works."""
    return get(RELEASE_CREDITS.format(mbid))


def release_group(mbid: str, db=None) -> dict:
    return get(GROUP.format(mbid), db, KEEP_GROUP)


def editions(group: str, db=None) -> list[dict]:
    """Every release of the release group, of any status, with media, tracks, credits, labels and ISRCs."""
    return pages(EDITIONS.replace("{}", group, 1), "release", db, KEEP_EDITIONS)


def official_editions(group: str) -> list[dict]:
    """The release group's first 100 official releases, with their tracks."""
    return get(OFFICIAL_EDITIONS.format(group)).get("releases") or []


def release_groups(artist: str, db=None) -> list[dict]:
    """All the artist's albums, EPs and singles, with their aliases."""
    return pages(CATALOGUE.replace("{}", artist, 1), "release-group", db, KEEP_CATALOGUE)


def pseudo_releases(artist: str, db=None) -> list[dict]:
    """All the artist's pseudo-releases (transliterations, translations), with their release groups."""
    return pages(PSEUDO.replace("{}", artist, 1), "release", db, KEEP_CATALOGUE)


def search(kind: str, query: str, db=None) -> list[dict]:
    """The best 25 of kind ('artist', 'release-group') matching the Lucene query."""
    return get(SEARCH.format(kind, urllib.parse.quote(query)), db, KEEP_SEARCH).get(kind + "s") or []


def lucene(text: str) -> str:
    """The text as one Lucene phrase."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def find(kind: str, text: str, db=None) -> list[dict]:
    """Artists (kind 'artist': by any name or alias, in any script) or release groups named like text, best first."""
    words = re.sub(r'[-+&|!(){}\[\]^"~*?:\\/]', " ", text).casefold().split()
    if not words:
        return []
    path = "artist" if kind == "artist" else "release-group"
    return search(path, " ".join(words), db)


def search_artist(name: str) -> list[str]:
    """The ids of artists named so, by name or alias: exact matches only, case, accents and punctuation aside."""
    if not fold(name):
        return []
    found = search("artist", lucene(name))
    return [a["id"] for a in found if fold(name) in {fold(a["name"]), *(fold(x["name"]) for x in a.get("aliases") or [])}]


def disc(disc_id: str, toc: list[str]) -> list[dict]:
    """The releases with the disc, else (with its TOC) its fuzzy matches."""
    url = DISC.format(urllib.parse.quote(disc_id, safe=""))
    if toc:
        url += "&toc=" + urllib.parse.quote_plus(" ".join(toc))
    return get(url).get("releases") or []
