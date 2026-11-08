"""The albums oasis looks for (want_album, which acquire.py fills and searches for) as downloads see them: whether a
search is due, which albums are still wanted and whose they are, and a failed download's albums due for a search again.
The SQL saying an album is due or still wanted, how a failure's error opens, and what a search's send came to
(Outcome) are shared with acquire.py."""
import json
import time
from enum import Enum

DUE = "state = 'missing' and coalesce(next_search, 0) <= ?"  # SQL: an album due for a search, given the time
STILL_WANTED = "state in ('missing', 'downloading')"  # SQL: an album not here yet, being downloaded or not
# How want_album.error opens after a failure, which the dashboard shows apart from "not found yet".
DOWNLOAD_FAILED, SEARCH_FAILED = "download failed: ", "search failed: "


def search_again(db, rgs: list[str], error: str | None = None):
    """The albums are due for a search at the next pass, a download of theirs having failed or been blocked; error:
    why, shown till then."""
    for rg in rgs:
        db.execute("update want_album set next_search = null, error = coalesce(?, error) where rg = ?", (error, rg))


def bundles_again(db, rgs: list[str]):
    """The next bundle search of the albums' artists is due, for another bundle of them."""
    db.execute(f"update bundle_search set searched = 0 where artist_mbid in (select artist_mbid from want_album where rg in ({','.join('?' * len(rgs))}))", rgs)


def searches_due(db) -> bool:
    return bool(db.execute(f"select 1 from want_album where {DUE} limit 1", (time.time(),)).fetchone())


def still_wanted(db, artist: str | None = None) -> set[str]:
    """The release groups not here yet, being downloaded or not; artist: of that MusicBrainz artist only."""
    if artist:
        return {r[0] for r in db.execute(f"select rg from want_album where artist_mbid = ? and {STILL_WANTED}", (artist,))}
    return {r[0] for r in db.execute(f"select rg from want_album where {STILL_WANTED}")}


def here(db) -> set[str]:
    """The release groups oasis looked for that are here now."""
    return {r[0] for r in db.execute("select rg from want_album where state = 'have'")}


def wanted_since(db, artist: str, wanted: list[str]) -> bool:
    """Whether the MusicBrainz artist has an album missing that isn't among these."""
    return bool(db.execute("select 1 from want_album where artist_mbid = ? and state = 'missing' and rg not in (select value from json_each(?))",
                           (artist, json.dumps(wanted))).fetchone())


def artist_of(db, rgs: list[str]) -> str | None:
    """The MusicBrainz artist of one of these albums oasis looks for."""
    row = db.execute(f"select artist_mbid from want_album where rg in ({','.join('?' * len(rgs))}) and artist_mbid is not null", rgs).fetchone()
    return row and row[0]


class Outcome(Enum):
    """What a search's send to TorBox came to: a release; nothing, as none would do; nothing for now, as TorBox had no
    free slot (it is due again soon); or nothing, TorBox having been asked and taken none."""
    SENT = "sent"
    NOTHING_SENT = "nothing sent"
    NO_SLOT = "no slot"
    REFUSED = "refused"
