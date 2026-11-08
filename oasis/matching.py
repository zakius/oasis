"""Which downloaded file is which track of a MusicBrainz edition, from what the files themselves say: a title and a
(disc, track) number from their tags, else from their names, and their lengths. pick chooses the edition a download's
files cover; releases.py reads torrent file lists the same way before anything is downloaded, with no lengths yet."""
import os
import re
from pathlib import Path

import mutagen

from . import musicbrainz
from .tags import first, number
from .text import fold

SLACK = 5, 0.03            # a file's length may differ from the track's by 5 s or 3 %, whichever is more
NAMED_SLACK = 5, 0.1       # and by 10 % when the file has the track's title and number, or a neighbour's (a tape's other order)
SIDE = re.compile(r"([A-H])(\d{0,2})[\s._-]+")  # a vinyl side's position opening a file's name: 'A Amerika', 'B2. Title'


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


def read(path: Path, artist: str) -> dict:
    """A downloaded file's title, (disc, track) and length: from its tags, else from its name."""
    audio = mutagen.File(path, easy=True)
    track = number(first(audio, "tracknumber"))
    return {"path": path, "key": key(first(audio, "title") or title_of(str(path), artist)),
            "number": (number(first(audio, "discnumber")) or 1, track) if track else number_of(str(path)),
            "length": getattr(audio.info, "length", 0) if audio is not None else 0}


def sides(files: list[dict], artist: str) -> list[dict]:
    """Files numbered only by a vinyl side's position in their names ('A Amerika.flac', 'B Wilder Wein.flac'), a folder
    of them all from side A, are numbered in side order, and their titles read without it."""
    folders = {}
    for f in files:
        folders.setdefault(f["path"].parent, []).append(f)
    for group in folders.values():
        found = [(m[1], int(m[2] or 0), f, m) for f in group if not f["number"] and (m := SIDE.match(f["path"].name))]
        if len(found) < max(2, len(group)) or len({s[:2] for s in found}) < len(found) or min(s[0] for s in found) != "A":
            continue
        for n, (_, _, f, m) in enumerate(sorted(found, key=lambda s: s[:2]), 1):
            f["number"] = 1, n
            if f["key"] == key(title_of(f["path"].name, artist)):  # not a title tag's
                f["key"] = key(title_of(f["path"].name[m.end():], artist))
    return files


def near(number, place) -> bool:
    return bool(number) and number[0] == place[0] and abs(number[1] - place[1]) <= 1


def close(length: float, ms, slack=SLACK) -> bool:
    return not ms or abs(length - ms / 1000) <= max(slack[0], slack[1] * ms / 1000)


def match(files: list[dict], tracks: list[tuple[dict, dict]]) -> dict | None:
    """Maps every (medium, track) of an edition to a distinct file whose length fits: by title, then by one title
    holding the other, then by disc and track number. None when some track has no file. A length of None isn't
    checked: a bundle's files are matched by name before they are downloaded."""
    mapping, used = {}, set()
    for medium, track in tracks:
        length = track.get("length") or track["recording"].get("length")
        want, place = key(track["title"]), (medium["position"], track["position"])
        # A file named and numbered as the track (or its neighbour) may differ more: another master, a longer fade, a tape.
        fits = [f for f in files if f["path"] not in used and (f["length"] is None or close(f["length"], length)
                                                                or f["key"] == want and near(f["number"], place) and close(f["length"], length, NAMED_SLACK))]
        found = [f for f in fits if f["key"] == want] or [f for f in fits if want and f["key"] and (want in f["key"] or f["key"] in want)] or \
                [f for f in fits if f["number"] == place]
        if not found:
            return None
        found.sort(key=lambda f: f["number"] != place)  # one title on two discs
        mapping[track["id"]] = found[0]
        used.add(found[0]["path"])
    return mapping


def pick(releases: list[dict], files: list[dict]) -> tuple[dict, dict]:
    """The edition the files cover: every track has its file. The most tracks wins, then the closest lengths, then
    the official earliest.
    Pseudo-releases (transliterations) are left out: the release they translate has the same tracks."""
    best = None
    for release in releases:
        tracks = [(m, t) for m in release.get("media") or [] for t in m.get("tracks") or []]
        if musicbrainz.is_pseudo(release) or not tracks or len(tracks) > len(files) or (mapping := match(files, tracks)) is None:
            continue
        lengths = {t["id"]: t.get("length") or t["recording"].get("length") for _, t in tracks}
        # A track without a length in MusicBrainz counts as off by the most: an edition that has them is surer.
        # Identical editions (the same digital release in many countries) fall to the official, earliest one: the original.
        score = (len(tracks), -sum(abs(f["length"] - lengths[i] / 1000) if lengths[i] else SLACK[0] for i, f in mapping.items()),
                 release.get("status") == "Official", -int(re.sub(r"\D", "", release.get("date") or "").ljust(8, "9")[:8]))
        if best is None or score > best[0]:
            best = score, release, mapping
    if best is None:
        raise LookupError("no edition matches")
    return best[1], best[2]

