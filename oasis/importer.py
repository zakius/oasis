"""oasis's own import of a finished download: finds the MusicBrainz edition of the album the files are (by titles,
numbers and durations), writes that edition's tags into them the way Picard does, and places them in the library as
"Artist/Year - Album/NN - Title.ext" (see album_folder and track_file). Library files are never overwritten: a
lossless copy of a track the library has only lossy replaces it, the lossy file deleted once the new one is in place.
A disc image (one audio file and a cue sheet) is split into a file per track first. The download's FLACs are checked
before anything is placed (see quality.py): fake hi-res becomes what it is, outliers are recompressed, and a FLAC made
from a lossy file fails the import (LossySource), so another release is picked."""
import fcntl
import json
import logging
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import mutagen
from mutagen._vorbis import VCommentDict

from . import musicbrainz, quality
from .dedupe import FICLONE
from .scan import COVER_NAMES, COVER_SUFFIXES
from .ytm import fold

log = logging.getLogger(__name__)

AUDIO = ("flac", "mp3", "m4a", "ogg", "opus", "wav", "ape", "wv", "aiff", "alac", "aac", "mpc")
LOSSLESS = ("flac", "wav", "ape", "wv", "aiff", "alac")
EDITIONS = "https://musicbrainz.org/ws/2/release?release-group={}&inc=media+recordings+artist-credits+labels+release-groups+isrcs" \
           "&limit=100&offset={}&fmt=json"
KEEP_EDITIONS = 7 * 86400  # seconds a release group's editions stay cached
COVER_ART = "https://coverartarchive.org/release/{}/front"
SLACK = 5, 0.03            # a file's length may differ from the track's by 5 s or 3 %, whichever is more
NAMED_SLACK = 5, 0.1       # and by 10 % when the file has the track's title and number, or a neighbour's (a tape's other order)
SPLIT = ".oasis-split"     # a download's disc images, split, in <download>/.oasis-split/<image>/
ARCHIVES = ("zip", "rar", "7z")
UNPACKED = ".oasis-unpacked"  # a download's archives, unpacked, in <download>/.oasis-unpacked/<archive>/
UNPACK_ROOM = 2            # free space an archive needs to unpack, in times what it holds: its music, then the library's copy
UNPACK_MOST = 20           # an archive holding more than this many times its size is a zip bomb: music hardly compresses
LATER_PART = re.compile(r"\.part0*([2-9]|[1-9]\d+)\.rar$", re.I)  # a multi-volume RAR unpacks from its first part
SIDE = re.compile(r"([A-H])(\d{0,2})[\s._-]+")  # a vinyl side's position opening a file's name: 'A Amerika', 'B2. Title'
CUE_LINE = re.compile(r'\s*(FILE|TRACK|TITLE|INDEX)\s+(?:"(.*)"|(\S+))(?:\s+(\S+))?', re.I)


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
        if release.get("status") == "Pseudo-Release" or not tracks or len(tracks) > len(files) or (mapping := match(files, tracks)) is None:
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


def cue_text(data: bytes) -> str:
    """Cue sheets are UTF-8 (perhaps with a BOM), UTF-16, Shift-JIS, CP1251 or CP1250. CP1251 Cyrillic reads as Shift-JIS
    too, but as half-width kana, which Japanese cue sheets don't use."""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    try:
        if not re.search("[\uff61-\uff9f]", text := data.decode("cp932")):
            return text
    except UnicodeDecodeError:
        pass
    text = data.decode("cp1251", errors="replace")
    # Central European (Polish 'Żółć') read as Cyrillic leaves words of Latin and Cyrillic letters mixed: 'iЇуіж'.
    return data.decode("cp1250", errors="replace") if re.search(r"(?=\w*[a-z])(?=\w*[а-яёіїєў])\w+", text, re.I) else text


def cue(path: Path) -> list[tuple[str, list[dict]]]:
    """A cue sheet's files, each with its tracks: number, title and start (seconds; INDEX 01, frames of 1/75 s)."""
    files = []
    for line in cue_text(path.read_bytes()).splitlines():
        if not (m := CUE_LINE.match(line)):
            continue
        command, value = m[1].upper(), m[2] if m[2] is not None else m[3]
        tracks = files[-1][1] if files else []
        if command == "FILE":
            files.append((value, []))
        elif command == "TRACK" and files and value.isdigit():
            tracks.append({"number": int(value), "title": None, "start": None})
        elif command == "TITLE" and tracks:
            tracks[-1]["title"] = value
        elif command == "INDEX" and tracks and value == "01" and (t := re.fullmatch(r"(\d+):(\d+):(\d+)", m[4] or "")):
            tracks[-1]["start"] = int(t[1]) * 60 + int(t[2]) + int(t[3]) / 75
    return [(name, [t for t in tracks if t["start"] is not None]) for name, tracks in files]


def image_of(sheet: Path, name: str) -> Path | None:
    """The audio file a cue sheet names: as named, else of its name or the sheet's with another suffix (sheets often
    name the .wav the image was ripped to), else the folder's only audio file."""
    folder = sheet.parent
    if (folder / name).is_file() and (folder / name).suffix.lower().lstrip(".") in AUDIO:
        return folder / name
    audio = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower().lstrip(".") in AUDIO]
    stems = {Path(name.replace("\\", "/")).stem.casefold(), sheet.stem.casefold()}
    return next((p for p in audio if p.stem.casefold() in stems), None) or (audio[0] if len(audio) == 1 else None)


class NoSpace(OSError):
    """Too little disk space: the download waits for more, and its release isn't at fault."""


def unpacked_size(archive: Path) -> int:
    """The bytes an archive holds, as its listing says: ZIP's own, the rest's by lsar (unar's lister)."""
    try:
        if archive.suffix.lower() == ".zip":
            with zipfile.ZipFile(archive) as z:
                return sum(i.file_size for i in z.infolist())
        listed = subprocess.run(["lsar", "-j", str(archive)], check=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=600)
        return sum(e.get("XADFileSize", 0) for e in json.loads(listed.stdout)["lsarContents"])
    except subprocess.CalledProcessError as e:
        raise LookupError(f"can't list {archive.name}: {e.stderr.decode(errors='replace').strip()[-200:]}") from None
    except (zipfile.BadZipFile, ValueError, KeyError, TypeError) as e:
        raise LookupError(f"can't list {archive.name}: {e}") from None


def unpack(folder: Path):
    """Unpacks the archives below folder (an album uploaded as one .zip or .rar, a discography as one .zip): ZIP itself,
    the rest with unar. NoSpace when the disk hasn't UNPACK_ROOM for what one holds; LookupError for a zip bomb. The
    symbolic links an archive makes are deleted: they could point anywhere outside it."""
    for archive in sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower().lstrip(".") in ARCHIVES
                          and UNPACKED not in p.parts and not LATER_PART.search(p.name)):
        if (target := folder / UNPACKED / archive.stem).exists():
            continue
        size = sum(p.stat().st_size for p in archive.parent.iterdir() if p == archive or LATER_PART.search(p.name) and p.name.startswith(archive.name.split(".part")[0]))
        if (held := unpacked_size(archive)) > UNPACK_MOST * size:
            raise LookupError(f"can't unpack {archive.name}: it holds {held / max(size, 1):.0f} times its size")
        if (free := shutil.disk_usage(folder).free) < UNPACK_ROOM * held:
            raise NoSpace(f"too little disk space to unpack {archive.name}: {UNPACK_ROOM * held / 1e9:.1f} GB needed, {free / 1e9:.1f} GB free")
        part = target.with_name(f"{target.name}.oasis-part")
        shutil.rmtree(part, ignore_errors=True)
        try:
            if archive.suffix.lower() == ".zip":
                with zipfile.ZipFile(archive) as z:
                    z.extractall(part)
            else:
                subprocess.run(["unar", "-q", "-f", "-D", "-o", str(part), str(archive)], check=True, capture_output=True,
                               stdin=subprocess.DEVNULL, timeout=1800)
        except subprocess.CalledProcessError as e:
            raise LookupError(f"can't unpack {archive.name}: {e.stderr.decode(errors='replace').strip()[-200:]}") from None
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as e:  # RuntimeError: a password
            raise LookupError(f"can't unpack {archive.name}: {e}") from None
        for top, dirs, files in os.walk(part):
            for name in dirs + files:
                if os.path.islink(link := os.path.join(top, name)):
                    os.unlink(link)
        part.rename(target)


def split(folder: Path) -> set[Path]:
    """Splits the disc images below folder by their cue sheets; returns the images. Lossless images become FLAC
    (sample-exact cuts need a decode); lossy ones are cut as they are. Each image is a disc of its own, in order."""
    images = {}
    for sheet in sorted(p for p in folder.rglob("*") if p.suffix.lower() == ".cue" and SPLIT not in p.parts):
        for name, tracks in cue(sheet):
            if len(tracks) > 1 and (image := image_of(sheet, name)) and image not in images:
                images[image] = tracks
    for disc, (image, tracks) in enumerate(images.items(), 1):
        suffix = ".flac" if lossless(image) else image.suffix.lower()
        for track, after in zip(tracks, [*tracks[1:], None]):
            target = folder / SPLIT / str(disc) / f"{disc}-{track['number']:02} {clean(track['title'] or '')[:100]}{suffix}"
            target.parent.mkdir(parents=True, exist_ok=True)
            tags = [a for k, v in (("title", track["title"]), ("track", track["number"]), ("disc", disc if len(images) > 1 else None)) if v
                    for a in ("-metadata", f"{k}={v}")]
            try:
                subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-ss", f"{track['start']:.6f}", "-i", str(image),
                                *(["-t", f"{after['start'] - track['start']:.6f}"] if after else []), "-map", "0:a:0", "-map_metadata", "-1",
                                *tags, "-c:a", "flac" if suffix == ".flac" else "copy", str(target)], check=True, capture_output=True, timeout=1800)
            except subprocess.CalledProcessError as e:
                raise LookupError(f"can't split {image.name}: {e.stderr.decode(errors='replace').strip()[-200:]}") from None
    return set(images)


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


def album_folder(artist: str, year: str, title: str) -> Path:
    """An album's folder in the library: "<artist>/<year> - <album>", or "<artist>/<album>" without a year. A name that
    cleans to nothing (such as "..") is "Unknown Artist" or "Unknown Album", so the album never lands above its folder."""
    return Path(clean(artist) or "Unknown Artist", clean(f"{year} - {title}" if year else title) or "Unknown Album")


def release_folder(release: dict) -> Path:
    """An edition's album folder: its first artist's MusicBrainz name, the release group's year (else its own)."""
    group = release.get("release-group") or {}
    return album_folder(release["artist-credit"][0]["artist"]["name"], (group.get("first-release-date") or release.get("date") or "")[:4], release["title"])


def track_file(disc: int, discs: int, track: int, tracks: int, title: str, suffix: str) -> str:
    """A track's file name, "07 - Title.flac"; on an album of several discs "1-07 - Title.flac", so one folder keeps
    the discs in order. Each number is as wide as the album's widest one, a track's at least two digits."""
    number = f"{track:0{max(2, len(str(tracks)))}}"
    return clean(f"{disc:0{len(str(discs))}}-{number} - {title}" if discs > 1 else f"{number} - {title}") + suffix.lower()


def lossless(path: Path) -> bool:
    suffix = path.suffix.lower().lstrip(".")
    return suffix in LOSSLESS or suffix == "m4a" and getattr(getattr(mutagen.File(path), "info", None), "codec", "") == "alac"


def copy(source: Path, target: Path, over: bool = False):
    """A reflink when the filesystem can, else a copy; synced, and never over an existing file unless over."""
    part = target.with_name(f".{target.name}.oasis-part")
    try:
        with open(source, "rb") as src, open(part, "wb") as out:
            try:
                fcntl.ioctl(out.fileno(), FICLONE, src.fileno())
            except OSError:
                shutil.copyfileobj(src, out, 1 << 20)
            out.flush()
            os.fsync(out.fileno())
        os.replace(part, target) if over else os.link(part, target)
    finally:
        part.unlink(missing_ok=True)


class Importer:
    def __init__(self, connect, root: Path):
        self.connect, self.root = connect, Path(root)

    def place(self, folder: Path, group: str) -> Path:
        """Imports the download in folder as an edition of the release group; returns the album folder.
        LookupError when no edition matches, LossySource (one) when a FLAC of it was made from a lossy file."""
        unpack(folder)
        images = split(folder)
        db = self.connect()
        try:
            releases = editions(db, group)
            artist = releases[0]["artist-credit"][0]["name"] if releases else ""
            files = sides([read(p, artist) for p in sorted(folder.rglob("*")) if p.is_file() and p.suffix.lower().lstrip(".") in AUDIO and p not in images], artist)
            release, mapping = pick(releases, files)
        finally:
            db.close()
        checked = quality.import_check(sorted({f["path"] for f in mapping.values() if f["path"].suffix.lower() == ".flac"}))
        album = self.root / release_folder(release)
        discs, tracks = len(release["media"]), max(t["position"] for m in release["media"] for t in m["tracks"])
        placed = 0
        db = self.connect()
        try:
            for medium in release["media"]:
                for track in medium["tracks"]:
                    source = mapping[track["id"]]["path"]
                    write(source, tags_for(release, medium, track))
                    target = album / track_file(medium["position"], discs, track["position"], tracks, track["title"], source.suffix)
                    if self.put(source, target):
                        placed += 1
                        if source in checked:
                            quality.store(db, target, checked[source])
        finally:
            db.close()
        self.cover(release, {f["path"].parent for f in mapping.values()} | {i.parent for i in images}, album)
        log.info("imported %d of %d tracks as %s (%s %s, %s) into %s", placed, len(mapping), release["title"], release.get("country"),
                 release.get("date"), release["id"], album)
        return album

    def put(self, source: Path, target: Path) -> int:
        """Places one file unless the library has that track already (any format): a lossless file replaces a lossy
        one, which is deleted once the new file is in place; anything else stays as it is. 1 if placed."""
        target.parent.mkdir(parents=True, exist_ok=True)
        have = [p for p in target.parent.iterdir() if p.stem == target.stem and p.suffix.lower().lstrip(".") in AUDIO]
        if have and not (lossless(source) and not any(lossless(p) for p in have)):
            return 0
        copy(source, target, over=target in have)  # an ALAC .m4a over a lossy one
        if target.stat().st_size != source.stat().st_size:
            raise OSError(f"{target} isn't the {source.name} it was copied from")
        for old in have:
            if old != target:
                old.unlink()
            log.info("replaced %s with %s", old, target.name)
        return 1

    def cover(self, release: dict | None, folders: set[Path], album: Path):
        """The download's cover image, else the Cover Art Archive's front cover of the release if any, unless the album has one."""
        if any(p.stem.lower() == "cover" for p in album.iterdir()):
            return
        found = sorted(p for d in folders for p in d.iterdir() if p.suffix.lower() in COVER_SUFFIXES and p.stem.lower() in COVER_NAMES)
        if found:
            return copy(found[0], album / ("cover" + found[0].suffix.lower().replace("jpeg", "jpg")))
        if release is None:
            return
        try:
            with urllib.request.urlopen(urllib.request.Request(COVER_ART.format(release["id"]), headers={"User-Agent": musicbrainz.USER_AGENT}),
                                        timeout=60) as response:
                data, kind = response.read(), response.headers.get_content_type()
        except (urllib.error.URLError, OSError) as e:  # 404: no cover there
            log.info("no cover for %s: %s", release["id"], e)
            return
        (album / ("cover.png" if kind == "image/png" else "cover.jpg")).write_bytes(data)
