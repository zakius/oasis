"""A download's archives and disc images, made into a file per track before it is matched: archives are unpacked to
<download>/.oasis-unpacked/<archive>/ (ZIP by Python, the rest by unar), disc images split by their cue sheets to
<download>/.oasis-split/<disc>/ (by ffmpeg). Unpacking first makes sure the disk has room for it (files.NoSpace), and a
download's fetch asks for the same room ahead (download.py)."""
import json
import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

from .files import need_room, stderr_tail
from .formats import ARCHIVES, AUDIO, lossless, suffix
from .layout import clean

SPLIT = ".oasis-split"     # a download's disc images, split, in <download>/.oasis-split/<image>/
UNPACKED = ".oasis-unpacked"  # a download's archives, unpacked, in <download>/.oasis-unpacked/<archive>/
UNPACK_ROOM = 2            # free space an archive needs to unpack, in times what it holds: its music, then the library's copy
UNPACK_MOST = 20           # an archive holding more than this many times its size is a zip bomb: music hardly compresses
LATER_PART = re.compile(r"\.part0*([2-9]|[1-9]\d+)\.rar$", re.I)  # a multi-volume RAR unpacks from its first part
CUE_LINE = re.compile(r'\s*(FILE|TRACK|TITLE|INDEX)\s+(?:"(.*)"|(\S+))(?:\s+(\S+))?', re.I)


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
    if (folder / name).is_file() and suffix(folder / name) in AUDIO:
        return folder / name
    audio = [p for p in folder.iterdir() if p.is_file() and suffix(p) in AUDIO]
    stems = {Path(name.replace("\\", "/")).stem.casefold(), sheet.stem.casefold()}
    return next((p for p in audio if p.stem.casefold() in stems), None) or (audio[0] if len(audio) == 1 else None)


def unpacked_size(archive: Path) -> int:
    """The bytes an archive holds, as its listing says: ZIP's own, the rest's by lsar (unar's lister)."""
    try:
        if suffix(archive) == "zip":
            with zipfile.ZipFile(archive) as z:
                return sum(i.file_size for i in z.infolist())
        listed = subprocess.run(["lsar", "-j", str(archive)], check=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=600)
        return sum(e.get("XADFileSize", 0) for e in json.loads(listed.stdout)["lsarContents"])
    except subprocess.CalledProcessError as e:
        raise LookupError(f"can't list {archive.name}: {stderr_tail(e)}") from None
    except (zipfile.BadZipFile, ValueError, KeyError, TypeError) as e:
        raise LookupError(f"can't list {archive.name}: {e}") from None


def unpack(folder: Path):
    """Unpacks the archives below folder (an album uploaded as one .zip or .rar, a discography as one .zip): ZIP itself,
    the rest with unar. NoSpace when the disk hasn't UNPACK_ROOM for what one holds; LookupError for a zip bomb. The
    symbolic links an archive makes are deleted: they could point anywhere outside it."""
    for archive in sorted(p for p in folder.rglob("*") if p.is_file() and suffix(p) in ARCHIVES
                          and UNPACKED not in p.parts and not LATER_PART.search(p.name)):
        if (target := folder / UNPACKED / archive.stem).exists():
            continue
        size = sum(p.stat().st_size for p in archive.parent.iterdir() if p == archive or LATER_PART.search(p.name) and p.name.startswith(archive.name.split(".part")[0]))
        if (held := unpacked_size(archive)) > UNPACK_MOST * size:
            raise LookupError(f"can't unpack {archive.name}: it holds {held / max(size, 1):.0f} times its size")
        need_room(folder, UNPACK_ROOM * held, f"unpack {archive.name}")
        part = target.with_name(f"{target.name}.oasis-part")
        shutil.rmtree(part, ignore_errors=True)
        try:
            if suffix(archive) == "zip":
                with zipfile.ZipFile(archive) as z:
                    z.extractall(part)
            else:
                subprocess.run(["unar", "-q", "-f", "-D", "-o", str(part), str(archive)], check=True, capture_output=True,
                               stdin=subprocess.DEVNULL, timeout=1800)
        except subprocess.CalledProcessError as e:
            raise LookupError(f"can't unpack {archive.name}: {stderr_tail(e)}") from None
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
    for sheet in sorted(p for p in folder.rglob("*") if suffix(p) == "cue" and SPLIT not in p.parts):
        for name, tracks in cue(sheet):
            if len(tracks) > 1 and (image := image_of(sheet, name)) and image not in images:
                images[image] = tracks
    for disc, (image, tracks) in enumerate(images.items(), 1):
        kind = "flac" if lossless(image) else suffix(image)
        for track, after in zip(tracks, [*tracks[1:], None]):
            target = folder / SPLIT / str(disc) / f"{disc}-{track['number']:02} {clean(track['title'] or '')[:100]}.{kind}"
            target.parent.mkdir(parents=True, exist_ok=True)
            tags = [a for k, v in (("title", track["title"]), ("track", track["number"]), ("disc", disc if len(images) > 1 else None)) if v
                    for a in ("-metadata", f"{k}={v}")]
            try:
                subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-ss", f"{track['start']:.6f}", "-i", str(image),
                                *(["-t", f"{after['start'] - track['start']:.6f}"] if after else []), "-map", "0:a:0", "-map_metadata", "-1",
                                *tags, "-c:a", "flac" if kind == "flac" else "copy", str(target)], check=True, capture_output=True, timeout=1800)
            except subprocess.CalledProcessError as e:
                raise LookupError(f"can't split {image.name}: {stderr_tail(e)}") from None
    return set(images)
