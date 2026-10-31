"""oasis's own import of a finished download: finds the MusicBrainz edition of the album the files are (by titles,
numbers and durations), writes that edition's tags into them the way Picard does, and places them in the library as
Lidarr names its files, "Artist/Album (Year)/NN - Title.ext", so they sit with what Lidarr imported before. Library
files are never overwritten or deleted: a lossy file a lossless copy replaces moves to .replaced."""
import fcntl
import logging
import os
import re
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path

import mutagen
from mutagen._vorbis import VCommentDict

from . import musicbrainz
from .dedupe import FICLONE
from .scan import COVER_NAMES, COVER_SUFFIXES, REPLACED
from .ytm import fold

log = logging.getLogger(__name__)

AUDIO = ("flac", "mp3", "m4a", "ogg", "opus", "wav", "ape", "wv", "aiff", "alac", "aac")
LOSSLESS = ("flac", "wav", "ape", "wv", "aiff", "alac")
EDITIONS = "https://musicbrainz.org/ws/2/release?release-group={}&inc=media+recordings+artist-credits+labels+release-groups+isrcs" \
           "&limit=100&offset={}&fmt=json"
KEEP_EDITIONS = 7 * 86400  # seconds a release group's editions stay cached
COVER_ART = "https://coverartarchive.org/release/{}/front"
SLACK = 5, 0.03            # a file's length may differ from the track's by 5 s or 3 %, whichever is more


def title_of(path: str, artist: str) -> str:
    """'01. Artist - Title (Album Version).flac' -> 'Title'."""
    name = re.sub(r"\.[^.]+$", "", os.path.basename(path))
    name = re.sub(r"^\s*(?:\d+[-.]?)?\d+\s*[.\-_)]*\s*", "", name)
    if name.casefold().startswith(artist.casefold()):
        name = name[len(artist):].lstrip(" -_.")
    return re.sub(r"\s*[(\[][^)\]]*[)\]]\s*$", "", name)


def number_of(path: str):
    """'2-05 Title.flac' -> (2, 5); '05 Title.flac' -> (1, 5)."""
    m = re.match(r"\s*(?:(\d)[-.])?(\d+)", os.path.basename(path))
    return (int(m[1] or 1), int(m[2])) if m else None


def key(title: str) -> str:
    return fold(re.sub(r"\s*[(\[][^)\]]*[)\]]\s*$", "", title))


def number(text) -> int | None:
    digits = str(text or "").split("/")[0].strip()
    return int(digits) if digits.isdigit() else None


def read(path: Path, artist: str) -> dict:
    """A downloaded file's title, (disc, track) and length: from its tags, else from its name."""
    audio = mutagen.File(path, easy=True)
    tags = audio.tags if audio is not None and audio.tags is not None else {}
    get = lambda name: (tags.get(name) or [None])[0] if name in tags else None
    named, track = number_of(str(path)), number(get("tracknumber"))
    return {"path": path, "key": key(get("title") or title_of(str(path), artist)),
            "number": (number(get("discnumber")) or 1, track) if track else named,
            "length": getattr(audio.info, "length", 0) if audio is not None else 0}


def close(length: float, ms) -> bool:
    return not ms or abs(length - ms / 1000) <= max(SLACK[0], SLACK[1] * ms / 1000)


def match(files: list[dict], tracks: list[tuple[dict, dict]]) -> dict | None:
    """Maps every (medium, track) of an edition to a distinct file whose length fits: by title, then by one title
    holding the other, then by disc and track number. None when some track has no file."""
    mapping, used = {}, set()
    for medium, track in tracks:
        fits = [f for f in files if f["path"] not in used and close(f["length"], track.get("length") or track["recording"].get("length"))]
        want = key(track["title"])
        found = [f for f in fits if f["key"] == want] or [f for f in fits if want and f["key"] and (want in f["key"] or f["key"] in want)] or \
                [f for f in fits if f["number"] == (medium["position"], track["position"])]
        if not found:
            return None
        found.sort(key=lambda f: f["number"] != (medium["position"], track["position"]))  # one title on two discs
        mapping[track["id"]] = found[0]
        used.add(found[0]["path"])
    return mapping


def pick(releases: list[dict], files: list[dict]) -> tuple[dict, dict]:
    """The edition the files cover: every track has its file. The most tracks wins, then the closest lengths.
    Pseudo-releases (transliterations) are left out: the release they translate has the same tracks."""
    best = None
    for release in releases:
        tracks = [(m, t) for m in release.get("media") or [] for t in m.get("tracks") or []]
        if release.get("status") == "Pseudo-Release" or not tracks or len(tracks) > len(files) or (mapping := match(files, tracks)) is None:
            continue
        lengths = {t["id"]: t.get("length") or t["recording"].get("length") for _, t in tracks}
        score = (len(tracks), -sum(abs(f["length"] - (lengths[i] or 0) / 1000) for i, f in mapping.items() if lengths[i]))
        if best is None or score > best[0]:
            best = score, release, mapping
    if best is None:
        raise LookupError("no edition matches")
    return best[1], best[2]


def editions(db, group: str) -> list[dict]:
    found = []
    while True:
        page = musicbrainz.get(EDITIONS.format(group, len(found)), db, KEEP_EDITIONS)
        found += page.get("releases") or []
        if not page.get("releases") or len(found) >= page.get("release-count", 0):
            return found


def joined(credit: list[dict]) -> str:
    return "".join(c["name"] + (c.get("joinphrase") or "") for c in credit)


def tags_for(release: dict, medium: dict, track: dict) -> dict[str, list[str]]:
    """Picard's tags for one track of an edition."""
    group, recording = release.get("release-group") or {}, track.get("recording") or {}
    credit = track.get("artist-credit") or recording.get("artist-credit") or release["artist-credit"]
    labels = release.get("label-info") or []
    return {
        "title": [track["title"]], "artist": [joined(credit)], "artists": [c["name"] for c in credit],
        "albumartist": [joined(release["artist-credit"])], "album": [release["title"]],
        "date": [release.get("date")], "originaldate": [group.get("first-release-date")],
        "tracknumber": [str(track["position"])], "totaltracks": [str(medium.get("track-count") or len(medium["tracks"]))],
        "discnumber": [str(medium["position"])], "totaldiscs": [str(len(release["media"]))],
        "media": [medium.get("format")], "releasecountry": [release.get("country")], "releasestatus": [(release.get("status") or "").lower()],
        "releasetype": [t.lower() for t in [group.get("primary-type"), *(group.get("secondary-types") or [])] if t],
        "label": list(dict.fromkeys(l["label"]["name"] for l in labels if l.get("label"))),
        "catalognumber": list(dict.fromkeys(l.get("catalog-number") for l in labels)), "barcode": [release.get("barcode")],
        "isrc": recording.get("isrcs") or [], "musicbrainz_albumid": [release["id"]], "musicbrainz_releasegroupid": [group.get("id")],
        "musicbrainz_releasetrackid": [track["id"]], "musicbrainz_trackid": [recording.get("id")],
        "musicbrainz_artistid": [c["artist"]["id"] for c in credit], "musicbrainz_albumartistid": [c["artist"]["id"] for c in release["artist-credit"]],
    }


def write(path: Path, tags: dict[str, list[str]]):
    """Replaces these tags with the edition's (an empty one is removed: it described another release); others stay.
    ID3 and MP4 hold the totals in the track and disc numbers."""
    audio = mutagen.File(path, easy=True)
    if audio is None:
        return
    if audio.tags is None:
        audio.add_tags()
    if isinstance(audio.tags, VCommentDict):  # the other names Vorbis comments use for the totals
        tags = {**tags, "tracktotal": tags["totaltracks"], "disctotal": tags["totaldiscs"]}
    else:
        tags = {k: v for k, v in tags.items() if k not in ("totaltracks", "totaldiscs")} | \
               {"tracknumber": [f"{tags['tracknumber'][0]}/{tags['totaltracks'][0]}"], "discnumber": [f"{tags['discnumber'][0]}/{tags['totaldiscs'][0]}"]}
    try:
        for name, values in tags.items():
            if values := [v for v in values if v]:
                audio[name] = values
            else:
                audio.pop(name, None)
    except (KeyError, ValueError, TypeError) as e:  # WAV and AIFF take only raw ID3 frames: such files stay as they came
        log.info("can't tag %s: %s", path.name, e)
        return
    audio.save()


def clean(name: str) -> str:
    """One folder or file name as Lidarr makes it with "replace illegal characters" and smart colon replacement: a run
    of one separator becomes one, and neither separators nor question marks can end it."""
    name = name.rstrip("?").replace(": ", " - ").replace(":", "-")
    for bad, good in zip('\\/<>?*|"', ("+", "+", "", "", "!", "-", "", "")):
        name = name.replace(bad, good)
    return re.sub(r"[- ._]+$", "", re.sub(r"([- ._])\1+", r"\1", name)).lstrip(" .")


def lossless(path: Path) -> bool:
    suffix = path.suffix.lower().lstrip(".")
    return suffix in LOSSLESS or suffix == "m4a" and getattr(getattr(mutagen.File(path), "info", None), "codec", "") == "alac"


def copy(source: Path, target: Path):
    """A reflink when the filesystem can, else a copy; synced, and never over an existing file."""
    part = target.with_name(f".{target.name}.oasis-part")
    try:
        with open(source, "rb") as src, open(part, "wb") as out:
            try:
                fcntl.ioctl(out.fileno(), FICLONE, src.fileno())
            except OSError:
                shutil.copyfileobj(src, out, 1 << 20)
            out.flush()
            os.fsync(out.fileno())
        os.link(part, target)
    finally:
        part.unlink(missing_ok=True)


class Importer:
    def __init__(self, connect, root: Path):
        self.connect, self.root = connect, Path(root)

    def place(self, folder: Path, group: str) -> Path:
        """Imports the download in folder as an edition of the release group; returns the album folder.
        LookupError when no edition matches."""
        db = self.connect()
        try:
            releases = editions(db, group)
            artist = releases[0]["artist-credit"][0]["name"] if releases else ""
            files = [read(p, artist) for p in sorted(folder.rglob("*")) if p.is_file() and p.suffix.lower().lstrip(".") in AUDIO]
            release, mapping = pick(releases, files)
            album = self.album_folder(db, release)
        finally:
            db.close()
        multi = len(release["media"]) > 1
        placed = 0
        for medium in release["media"]:
            for track in medium["tracks"]:
                source = mapping[track["id"]]["path"]
                write(source, tags_for(release, medium, track))
                where = album / clean(f"{medium.get('format') or 'Unknown'} {medium['position']:02}") if multi else album
                placed += self.put(source, where / (clean(f"{track['position']:02} - {track['title']}") + source.suffix.lower()))
        self.cover(release, {f["path"].parent for f in mapping.values()}, album)
        log.info("imported %d of %d tracks as %s (%s %s, %s) into %s", placed, len(mapping), release["title"], release.get("country"),
                 release.get("date"), release["id"], album)
        return album

    def album_folder(self, db, release: dict) -> Path:
        """<album artist>/<album> (<year>): the artist's folder where the library already has their files, else their name."""
        group = release.get("release-group") or {}
        artist = release["artist-credit"][0]["artist"]
        row = db.execute("select t.ref from track t join mb_artist m on m.artist_id = t.artist_id where m.mbid = ? and t.source = 'local' "
                         "and t.ref like ? limit 1", (artist["id"], f"{self.root}/%")).fetchone()
        home = Path(row[0]).relative_to(self.root).parts[0] if row else clean(artist["name"])
        year = (group.get("first-release-date") or release.get("date") or "")[:4]
        title = group.get("title") or release["title"]
        return self.root / home / clean(f"{title} ({year})" if year else title)

    def put(self, source: Path, target: Path) -> int:
        """Places one file unless the library has that track already (any format): a lossless file replaces a lossy
        one, which moves to .replaced; anything else stays as it is. 1 if placed."""
        target.parent.mkdir(parents=True, exist_ok=True)
        have = [p for p in target.parent.iterdir() if p.stem == target.stem and p.suffix.lower().lstrip(".") in AUDIO]
        if have and not (lossless(source) and not any(lossless(p) for p in have)):
            return 0
        for old in have:
            aside = self.root / REPLACED / old.relative_to(self.root)
            aside.parent.mkdir(parents=True, exist_ok=True)
            if aside.exists():
                aside = aside.with_name(f"{aside.stem}.{int(time.time())}{aside.suffix}")
            os.rename(old, aside)
            log.info("replaced %s; the old file is in %s", old, aside)
        copy(source, target)
        return 1

    def cover(self, release: dict, folders: set[Path], album: Path):
        """The download's cover image, else the Cover Art Archive's front cover, unless the album has one."""
        if any(p.stem.lower() == "cover" for p in album.iterdir()):
            return
        found = sorted(p for d in folders for p in d.iterdir() if p.suffix.lower() in COVER_SUFFIXES and p.stem.lower() in COVER_NAMES)
        if found:
            return copy(found[0], album / ("cover" + found[0].suffix.lower().replace("jpeg", "jpg")))
        try:
            with urllib.request.urlopen(urllib.request.Request(COVER_ART.format(release["id"]), headers={"User-Agent": musicbrainz.USER_AGENT}),
                                        timeout=60) as response:
                data, kind = response.read(), response.headers.get_content_type()
        except (urllib.error.URLError, OSError) as e:  # 404: no cover there
            log.info("no cover for %s: %s", release["id"], e)
            return
        (album / ("cover.png" if kind == "image/png" else "cover.jpg")).write_bytes(data)
