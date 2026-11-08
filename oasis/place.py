"""How files enter the library, for the importer and uploads alike: their FLACs are checked first (quality.py: fake
hi-res becomes what it is, outliers are recompressed, a FLAC made from a lossy file stops it all with LossySource),
then they are tagged, placed by the caller under layout.py's names, and quality's findings stored once they are in
place. A library file is never replaced unless asked, but for a lossless copy of a track the library has only lossy
(same_track, replaces). The album gets a cover if it has none. Whether a track the library has already is skipped or
stops the whole lot is the caller's: the importer skips it, an upload is refused."""
from pathlib import Path

from . import audiorecords, quality
from .cover import release_front
from .files import copy
from .formats import AUDIO, is_cover, lossless, suffix
from .tags import write


def same_track(target: Path) -> list[Path]:
    """The library's files of the track target names: of its name in its folder, in any audio format."""
    if not target.parent.is_dir():
        return []
    return [p for p in target.parent.iterdir() if p.stem == target.stem and suffix(p) in AUDIO]


def replaces(new: list[Path], old: list[Path]) -> bool:
    """Whether new files take the place of the library's old ones without being asked to: they are lossless, the old lossy."""
    return all(map(lossless, new)) and not any(map(lossless, old))


def prepare(tags: dict[Path, dict[str, list[str]]]) -> dict[Path, dict]:
    """Readies files for the library: their FLACs examined and acted on (LossySource when one was made from a lossy
    file), then each given its tags. Returns quality's findings by file, for record once the files are placed."""
    found = quality.import_check(sorted(p for p in tags if suffix(p) == "flac"))
    for path, given in tags.items():
        write(path, given)
    return found


def record(db, found: dict[Path, dict], placed: dict[Path, Path]):
    """Stores quality's findings of the files placed (file -> its library path) under their library paths. Only once
    they are there: a finding holds the library file's mtime."""
    for source, target in placed.items():
        if source in found:
            audiorecords.store_finding(db, target, found[source])


def cover(release_id: str | None, folders: set[Path], album: Path):
    """The cover image among the files, else the Cover Art Archive's front cover of the release if any, unless the
    album has one."""
    if any(is_cover(p) for p in album.iterdir()):
        return
    found = sorted(p for d in folders for p in d.iterdir() if is_cover(p))
    if found:
        return copy(found[0], album / f"cover.{suffix(found[0]).replace('jpeg', 'jpg')}")
    if release_id is None:
        return
    if fetched := release_front(release_id):
        data, kind = fetched
        (album / ("cover.png" if kind == "image/png" else "cover.jpg")).write_bytes(data)
