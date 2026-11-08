"""Cover art: the picture of a track, album, artist or playlist, wherever it is kept: in a file's tags, an image
file beside it, YouTube Music, or the Cover Art Archive for an album only MusicBrainz has."""
import logging
import urllib.request
from pathlib import Path

import mutagen

from .formats import IMAGES, suffix
from .musicbrainz import USER_AGENT
from .tags import pictures
from .tracks import IDS, MB_IDS, current_id, id_column, mbid_of

log = logging.getLogger(__name__)
# The Cover Art Archive's front images: a release's, and a release group's at 500 px.
RELEASE_COVER = "https://coverartarchive.org/release/{}/front"
GROUP_COVER = "https://coverartarchive.org/release-group/{}/front-500"


def fetch(url: str, timeout: int = 15) -> tuple[bytes, str]:
    """An image from the web, as (image, media type)."""
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=timeout) as response:
        return response.read(), response.headers.get_content_type()


def release_front(mbid: str) -> tuple[bytes, str] | None:
    """The Cover Art Archive's front cover of a MusicBrainz release; None when it has none (404) or can't be reached."""
    try:
        return fetch(RELEASE_COVER.format(mbid), timeout=60)
    except OSError as e:
        log.info("no cover for %s: %s", mbid, e)
        return None


def embedded(path: str) -> tuple[bytes, str] | None:
    found = pictures(mutagen.File(path))
    return (found[0].data, found[0].mime) if found else None


def load(track_path: str, cover: str) -> tuple[bytes, str] | None:
    if cover == "embedded":
        return embedded(track_path)
    if cover.startswith("https://"):
        return fetch(cover)
    image = Path(cover)
    return image.read_bytes(), IMAGES.get(suffix(image), "image/jpeg")


def find(db, id: str) -> tuple[bytes, str] | None:
    """The cover of a track, album, artist (its album's) or playlist by id, as (image, media type)."""
    id = current_id(db, id)
    if id.startswith(MB_IDS["album"]):  # a MusicBrainz-only album until it arrives
        r = {"ref": None, "cover": GROUP_COVER.format(mbid_of(id))}
    elif id.startswith(IDS["playlist"]):
        r = db.execute("select null as ref, cover from playlist where id = ? and cover is not null and hidden = 0", (id.removeprefix(IDS["playlist"]),)).fetchone()
    else:
        r = db.execute(f"select ref, cover from track where {id_column(id)} = ? and cover is not null "
                       "order by source != 'local', cover = 'embedded', disc_no, track_no limit 1", (id,)).fetchone() or \
            db.execute("select null as ref, cover from ytm_release where album_id = ? and cover is not null", (id,)).fetchone()
    if r is None:
        return None
    try:
        return load(r["ref"], r["cover"])
    except (OSError, mutagen.MutagenError) as e:  # a cover that can't be read is no cover
        log.warning("cover %s of %s unavailable: %s", r["cover"], r["ref"] or id, e)
        return None
