"""oasis's own import of a finished download: unpacks its archives and splits its disc images (unpack.py), makes its
WAV and AIFF FLAC (pcm.py), finds the MusicBrainz edition of the album the files are (matching.py), writes that
edition's tags into them the way Picard does and places them in the library where layout.py says, with a cover. The download's FLACs are checked before anything
is placed, and a FLAC made from a lossy file fails the import (quality.LossySource), so another release is picked
(see place.py, whose rules uploads share). A track the library has already is skipped, unless the library's copy is
lossy and the download's lossless: then the lossy file is deleted once the new one is in place."""
import logging
from pathlib import Path

from . import musicbrainz, pcm
from .files import copy
from .formats import AUDIO, suffix
from .layout import edition_file, release_folder, tags_for
from .matching import pick, read, sides
from .place import cover, prepare, record, replaces, same_track
from .unpack import split, unpack

log = logging.getLogger(__name__)


class Importer:
    def __init__(self, connect, root: Path):
        self.connect, self.root = connect, Path(root)

    def place(self, folder: Path, group: str) -> Path:
        """Imports the download in folder as an edition of the release group; returns the album folder.
        LookupError when no edition matches, LossySource (one) when a FLAC of it was made from a lossy file."""
        unpack(folder)
        images = split(folder)
        pcm.convert_all([p for p in sorted(folder.rglob("*")) if p.is_file() and p not in images])
        db = self.connect()
        try:
            releases = musicbrainz.editions(group, db)
            artist = releases[0]["artist-credit"][0]["name"] if releases else ""
            files = sides([read(p, artist) for p in sorted(folder.rglob("*")) if p.is_file() and suffix(p) in AUDIO and p not in images], artist)
            release, mapping = pick(releases, files)
        finally:
            db.close()
        album = self.root / release_folder(release)
        tags, targets = {}, {}
        for medium in release["media"]:
            for track in medium["tracks"]:
                source = mapping[track["id"]]["path"]
                tags[source] = tags_for(release, medium, track)
                targets[source] = album / edition_file(release, medium, track, suffix(source))
        found = prepare(tags)
        placed = {}
        db = self.connect()
        try:
            for source, target in targets.items():
                if self.put(source, target):  # each recorded at once: a later placement may fail
                    placed[source] = target
                    record(db, found, {source: target})
        finally:
            db.close()
        cover(release["id"], {f["path"].parent for f in mapping.values()} | {i.parent for i in images}, album)
        log.info("imported %d of %d tracks as %s (%s %s, %s) into %s%s", len(placed), len(mapping), release["title"], release.get("country"),
                 release.get("date"), release["id"], album, f"; {len(mapping) - len(placed)} already here" if len(placed) < len(mapping) else "")
        return album

    def put(self, source: Path, target: Path) -> bool:
        """Places one file unless the library has that track already (any format): a lossless file replaces a lossy
        one, which is deleted once the new file is in place; anything else stays as it is. Whether it was placed."""
        target.parent.mkdir(parents=True, exist_ok=True)
        have = same_track(target)
        if have and not replaces([source], have):
            return False
        copy(source, target, over=target in have)  # an ALAC .m4a over a lossy one
        for old in have:
            if old != target:
                old.unlink()
            log.info("replaced %s with %s", old, target.name)
        return True
