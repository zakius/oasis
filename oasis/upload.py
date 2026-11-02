"""The OpenSubsonic extension upload (docs/upload-extension.md): a client sends a ripped CD or an album's files, and
oasis tags them from a MusicBrainz release or the client's tags, checks them as it checks downloads (quality.py),
names and places them as the importer does, and adds the cover. An upload's state is in the upload tables, its files
in <database folder>/uploads/<id>/ until it is committed or cancelled, or KEEP after its last request.

A commit is all or nothing: the files are tagged in a staging folder, the library files they replace are renamed
aside, and a failure removes what was placed and puts those back. Only once all are in are the old ones deleted."""
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager, suppress
from pathlib import Path
from urllib.parse import quote, quote_plus

import mutagen
from mutagen.flac import FLAC
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen.oggflac import OggFLAC
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

from . import musicbrainz, quality
from .db import make_id
from .importer import AUDIO, KEEP_EDITIONS, Importer, album_folder, copy, joined, lossless, release_folder, tags_for, track_file, write

log = logging.getLogger(__name__)

KEEP = 86400  # seconds an upload stays after its last request
MAX_TRACK = 4 << 30  # bytes a track's file may have: 24-bit/192 kHz FLAC of over an hour
MAX_COVER = 50 << 20  # bytes a cover may have
SUFFIXES = {"flac": (FLAC,), "opus": (OggOpus,), "mp3": (MP3,), "m4a": (MP4,), "ogg": (OggVorbis, OggOpus, OggFLAC)}
DISC = "https://musicbrainz.org/ws/2/discid/{}?inc=artist-credits+recordings+labels&cdstubs=no&fmt=json"
RELEASE = "https://musicbrainz.org/ws/2/release/{}?inc=media+recordings+artist-credits+labels+release-groups+isrcs&fmt=json"


class Refused(Exception):
    """A request the upload can't take: a Subsonic error code and a message."""
    def __init__(self, message: str, code: int = 0, duplicate: bool = False):
        super().__init__(message)
        self.code, self.duplicate = code, duplicate


def is_format(path: Path, suffix: str) -> bool:
    try:
        return isinstance(mutagen.File(path), SUFFIXES[suffix])
    except mutagen.MutagenError:
        return False


def decodes(path: Path) -> bool:
    """flac -t for a FLAC when flac is installed, else a decode by ffmpeg that reports no error; without ffmpeg, whether
    mutagen reads it as its suffix says."""
    if path.suffix == ".flac" and shutil.which("flac"):
        return subprocess.run(["flac", "-t", "-s", str(path)], capture_output=True, timeout=600).returncode == 0
    if shutil.which("ffmpeg"):
        run = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-f", "null", "-"], capture_output=True, timeout=600)
        return run.returncode == 0 and not run.stderr.strip()
    return is_format(path, path.suffix.lstrip("."))


def text(value) -> bool:
    """Whether a tag is absent or text a file name and tag can hold."""
    return value is None or isinstance(value, str) and "\0" not in value


def compact(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


def disc_track(release: dict, track: dict) -> dict:
    recording = track.get("recording") or {}
    length = track.get("length") or recording.get("length")
    return compact({"number": track["position"], "title": track["title"],
                    "artist": joined(track.get("artist-credit") or recording.get("artist-credit") or release.get("artist-credit") or []),
                    "duration": round(length / 1000) if length else None, "musicBrainzId": recording.get("id")})


def release_of(release: dict, disc_id: str, count: int | None, cover: str | None) -> dict:
    """lookupDisc's release: the disc is the medium holding the disc id, else the first with the TOC's track count."""
    media, labels = release.get("media") or [], [l for l in release.get("label-info") or [] if l.get("label")]
    medium = next((m for m in media if any(d.get("id") == disc_id for d in m.get("discs") or [])), None) or \
        next((m for m in media if m.get("track-count") == count), None) or (media or [{}])[0]
    date = release.get("date") or ""
    return compact({
        "id": release["id"], "title": release["title"], "artist": joined(release.get("artist-credit") or []),
        "year": int(date[:4]) if date[:4].isdigit() else None, "date": date or None, "country": release.get("country"),
        "label": labels[0]["label"]["name"] if labels else None,
        "catalogNumber": next((l["catalog-number"] for l in labels if l.get("catalog-number")), None),
        "barcode": release.get("barcode") or None, "discCount": len(media), "discNumber": medium.get("position"), "coverArt": cover,
        "track": [disc_track(release, t) for t in medium.get("tracks") or []]})


class Uploads:
    def __init__(self, connect, root: Path, folder: Path):
        self.connect, self.importer, self.folder = connect, Importer(connect, root), Path(folder)
        self.lock = threading.Lock()  # one commit at a time
        self.busy, self.guard = set(), threading.Lock()  # (upload, a (disc, number), "cover" or "commit") in progress, guarded

    @contextmanager
    def holding(self, id: str, part):
        """Holds one of an upload's files while its body arrives, or the whole upload while it is committed: a request
        for a file held, or any while a commit is, is refused (a commit too while a file is held), and clean leaves the
        upload alone."""
        with self.guard:
            if (id, part) in self.busy or (id, "commit") in self.busy or part == "commit" and any(b[0] == id for b in self.busy):
                raise Refused("Another request is sending this file or committing the upload")
            self.busy.add((id, part))
        try:
            yield
        finally:
            with self.guard:
                self.busy.discard((id, part))

    def lookup(self, db, disc_id: str, toc: str | None) -> list[dict]:
        """MusicBrainz's releases with the disc, else (with the TOC) its fuzzy matches; exact matches first."""
        toc = re.split(r"[\s+]+", toc.strip()) if toc else []
        try:
            found = musicbrainz.get(DISC.format(quote(disc_id, safe="")) + (f"&toc={quote_plus(' '.join(toc))}" if toc else ""))
        except LookupError:
            return []
        except OSError as e:
            raise Refused(f"MusicBrainz is unreachable: {e}")
        count = len(toc) - 3 if toc else None
        covers = lambda id: (r := db.execute("select album_id from track where source = 'local' and mb_release = ? and cover is not null limit 1",
                                             (id,)).fetchone()) and r[0]
        releases = sorted(found.get("releases") or [], key=lambda r: not any(d.get("id") == disc_id for m in r.get("media") or [] for d in m.get("discs") or []))
        return [release_of(r, disc_id, count, covers(r["id"])) for r in releases]

    def create(self, db, user: str, release_id: str | None, disc: int, tags: str | None) -> dict:
        if release_id:
            try:
                release = musicbrainz.get(RELEASE.format(quote(release_id, safe="")), db, KEEP_EDITIONS)
            except LookupError:
                raise Refused("Release not found", 70)
            except OSError as e:
                raise Refused(f"MusicBrainz is unreachable: {e}")
            medium = next((m for m in release.get("media") or [] if m["position"] == disc), None)
            if medium is None:
                raise Refused(f"The release has no disc {disc}", 70)
            slots, release, tags = [(disc, t["position"]) for t in medium["tracks"]], json.dumps(release), None
        else:
            try:
                given = json.loads(tags)
                slots = [(int(t.get("disc") or 1), int(t["number"])) for t in given["track"] if t["title"]]
            except (ValueError, TypeError, KeyError, AttributeError):
                raise Refused("tags must be a JSON object with track[] of disc, number and title")
            names = [given.get(k) for k in ("album", "albumArtist", "genre")] + [v for t in given["track"] for v in (t["title"], t.get("artist"))]
            if not all(map(text, names)) or not all(a is None or isinstance(a, list) and all(isinstance(x, str) and text(x) for x in a)
                                                    for a in (t.get("artists") for t in given["track"])):
                raise Refused("tags must be text without NUL characters")
            if not slots or len(set(slots)) < len(slots) or len(slots) < len(given["track"]):
                raise Refused("tags must list each disc and number once, each with a title")
            if not (given.get("albumArtist") or all(t.get("artist") or t.get("artists") for t in given["track"])):
                raise Refused("tags need an albumArtist, or an artist for each track")
            if not str(given.get("year") or "0").isdigit():
                raise Refused("year must be a number")
            release = disc = None
        id = uuid.uuid4().hex
        db.execute("insert into upload values (?, ?, ?, ?, ?, ?)", (id, user, release, disc, tags, time.time()))
        db.executemany("insert into upload_track(upload, disc, number) values (?, ?, ?)", [(id, *s) for s in slots])
        return self.describe(db, user, id, False)

    def row(self, db, user: str, id: str):
        row = db.execute("select * from upload where id = ? and user = ?", (id, user)).fetchone()
        if row is None:
            raise Refused("Upload not found", 70)
        db.execute("update upload set touched = ? where id = ?", (time.time(), id))
        return row

    def describe(self, db, user: str, id: str, progress: bool = True) -> dict:
        self.row(db, user, id)
        tracks = db.execute("select * from upload_track where upload = ? order by disc, number", (id,)).fetchall()
        return {"id": id, "expires": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + KEEP)), "track": [
            {"disc": t["disc"], "number": t["number"], **({"received": t["received"], **({"size": t["size"]} if t["size"] is not None else {})}
                                                         if progress else {})} for t in tracks]}

    def receive(self, db, user: str, id: str, disc: int, number: int, offset: int, size: int, suffix: str, body) -> int:
        """Stores the body at offset of the track's file (an earlier offset sends again what follows it); returns the
        bytes stored. The whole file must be the format its suffix says, else it is dropped."""
        self.row(db, user, id)
        with self.holding(id, (disc, number)):
            return self.store_track(db, id, disc, number, offset, size, suffix, body)

    def store_track(self, db, id: str, disc: int, number: int, offset: int, size: int, suffix: str, body) -> int:
        slot = db.execute("select * from upload_track where upload = ? and disc = ? and number = ?", (id, disc, number)).fetchone()
        if slot is None:
            raise Refused(f"The upload has no track {disc}-{number}", 70)
        if suffix not in SUFFIXES:
            raise Refused(f"Suffix '{suffix}' isn't accepted; send one of {', '.join(SUFFIXES)}")
        if not 0 <= offset <= slot["received"]:
            raise Refused(f"Offset {offset} isn't within the {slot['received']} bytes received")
        if offset and (suffix, size) != (slot["suffix"], slot["size"]):
            raise Refused("A resumed file keeps its suffix and size")
        if size > MAX_TRACK:
            raise Refused(f"A track may have {MAX_TRACK} bytes at most")
        self.folder.mkdir(parents=True, exist_ok=True)
        if (free := shutil.disk_usage(self.folder).free) < size - offset:
            raise Refused(f"Too little disk space: {size - offset} bytes needed, {free} free")
        if slot["suffix"] and slot["suffix"] != suffix:
            (self.folder / id / f"{disc}-{number}.{slot['suffix']}").unlink(missing_ok=True)
        path, received = self.folder / id / f"{disc}-{number}.{suffix}", offset
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(path, "r+b" if path.exists() else "wb") as out:
                out.truncate(offset)
                out.seek(offset)
                for chunk in body:
                    if received + len(chunk) > size:
                        out.truncate(received := offset)
                        raise Refused(f"More than the {size} bytes of size")
                    out.write(chunk)
                    received += len(chunk)
        finally:  # a client gone midway resumes from what arrived
            db.execute("update upload_track set suffix = ?, size = ?, received = ? where upload = ? and disc = ? and number = ?",
                       (suffix, size, received, id, disc, number))
            db.execute("update upload set touched = ? where id = ?", (time.time(), id))
        if received == size and not is_format(path, suffix):
            path.unlink()
            db.execute("update upload_track set received = 0 where upload = ? and disc = ? and number = ?", (id, disc, number))
            raise Refused(f"Track {disc}-{number} isn't a {suffix} file")
        return received

    def cover(self, db, user: str, id: str, body):
        """The album's cover, JPEG or PNG; the release's from the Cover Art Archive without it."""
        self.row(db, user, id)
        with self.holding(id, "cover"):
            self.store_cover(self.folder / id, body)

    def store_cover(self, folder: Path, body):
        folder.mkdir(parents=True, exist_ok=True)
        with open(part := folder / "cover.part", "wb") as out:
            for chunk in body:
                if out.tell() + len(chunk) > MAX_COVER:
                    out.close()
                    part.unlink()
                    raise Refused(f"A cover may have {MAX_COVER} bytes at most")
                out.write(chunk)
        with open(part, "rb") as f:
            head = f.read(8)
        suffix = ".jpg" if head.startswith(b"\xff\xd8\xff") else ".png" if head.startswith(b"\x89PNG\r\n\x1a\n") else None
        if suffix is None:
            part.unlink()
            raise Refused("The cover isn't a JPEG or PNG image")
        for old in folder.glob("cover.*"):
            if old != part:
                old.unlink()
        part.rename(folder / f"cover{suffix}")

    def cancel(self, db, user: str, id: str):
        self.row(db, user, id)
        self.remove(db, id)

    def remove(self, db, id: str):
        db.execute("delete from upload where id = ?", (id,))
        shutil.rmtree(self.folder / id, ignore_errors=True)

    def clean(self, db):
        """Deletes uploads abandoned KEEP ago, but none with a file being received or being committed."""
        for (id,) in db.execute("select id from upload where touched < ?", (time.time() - KEEP,)).fetchall():
            with self.guard:
                if any(b[0] == id for b in self.busy) or not db.execute("delete from upload where id = ? and touched < ?", (id, time.time() - KEEP)).rowcount:
                    continue
            shutil.rmtree(self.folder / id, ignore_errors=True)

    def layout(self, row) -> list[tuple[Path, str | None, dict]]:
        """The albums the upload makes: folder, release group, and per (disc, number) slot its tags and file name. One
        album, or with no album name each track a single of its own."""
        if row["release"]:
            release = json.loads(row["release"])
            medium = next(m for m in release["media"] if m["position"] == row["disc"])
            discs, widest = len(release["media"]), max(t["position"] for m in release["media"] for t in m["tracks"])
            return [(release_folder(release), (release.get("release-group") or {}).get("id"), {
                (medium["position"], t["position"]): (tags_for(release, medium, t), track_file(medium["position"], discs, t["position"], widest, t["title"], ""))
                for t in medium["tracks"]})]
        given = json.loads(row["tags"])
        tracks, year = given["track"], str(given.get("year") or "")[:4]
        slot = lambda t: (int(t.get("disc") or 1), int(t["number"]))
        artists = lambda t: t.get("artists") or [t.get("artist") or given["albumArtist"]]

        def tags(t, album, artist, d, n, discs, total):
            return {"title": [t["title"]], "artist": [t.get("artist") or ", ".join(artists(t))], "artists": artists(t), "albumartist": [artist],
                    "album": [album], "date": [year], "genre": [given.get("genre")], "tracknumber": [str(n)], "totaltracks": [str(total)],
                    "discnumber": [str(d)], "totaldiscs": [str(discs)]}
        if not given.get("album"):  # loose singles
            return [(album_folder(artist := given.get("albumArtist") or tags(t, "", "", 1, 1, 1, 1)["artist"][0], year, t["title"]), None,
                     {slot(t): (tags(t, t["title"], artist, 1, 1, 1, 1), track_file(1, 1, 1, 1, t["title"], ""))}) for t in tracks]
        artist, discs, widest = given.get("albumArtist") or tags(tracks[0], "", "", 1, 1, 1, 1)["artist"][0], max(slot(t)[0] for t in tracks), max(slot(t)[1] for t in tracks)
        return [(album_folder(artist, year, given["album"]), None, {
            (d, n): (tags(t, given["album"], artist, d, n, discs, sum(slot(u)[0] == d for u in tracks)), track_file(d, discs, n, widest, t["title"], ""))
            for t in tracks for d, n in [slot(t)]})]

    def commit(self, db, user: str, id: str, replace: bool) -> list[Path]:
        """Places the upload in the library; returns a placed file per album. Refused when it isn't complete or doesn't
        decode, when a FLAC of it was made from a lossy file, and when the library has an album's discs already, unless
        replace or the upload is lossless and they are lossy: those files are deleted once the upload is in."""
        with self.lock:
            row = self.row(db, user, id)
            with self.holding(id, "commit"):
                return self.place_upload(db, id, row, replace)

    def place_upload(self, db, id: str, row, replace: bool) -> list[Path]:
        slots = db.execute("select * from upload_track where upload = ? order by disc, number", (id,)).fetchall()
        for s in slots:
            if not s["size"] or s["received"] < s["size"]:
                raise Refused(f"Track {s['disc']}-{s['number']} isn't complete")
            if not decodes(self.folder / id / f"{s['disc']}-{s['number']}.{s['suffix']}"):
                raise Refused(f"Track {s['disc']}-{s['number']} doesn't decode")
        albums, staging = self.layout(row), self.folder / id / "staging"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir()
        by_slot = {(s["disc"], s["number"]): s for s in slots}
        staged, targets, old, firsts = {}, {}, set(), []
        for i, (folder, group, tracks) in enumerate(albums):
            album, mine = self.importer.root / folder, [by_slot[k] for k in tracks if k in by_slot]
            (staging / str(i)).mkdir()
            for s in mine:
                tags, name = tracks[s["disc"], s["number"]]
                copy(self.folder / id / f"{s['disc']}-{s['number']}.{s['suffix']}", source := staging / str(i) / f"{name}.{s['suffix']}")
                staged[source], targets[source] = tags, album / source.name
            tags = tracks[mine[0]["disc"], mine[0]["number"]][0]
            firsts.append(album / f"{tracks[mine[0]['disc'], mine[0]['number']][1]}.{mine[0]['suffix']}")
            uploaded = {(int(t["discnumber"][0]), int(t["tracknumber"][0])) for t in (tracks[s["disc"], s["number"]][0] for s in mine)}
            discs = {d for d, _ in uploaded}
            rows = db.execute(
                f"select ref, coalesce(disc_no, 1), track_no from track where source = 'local' and (album_id = ? or mb_release_group = ?) and "
                f"coalesce(disc_no, 1) in ({','.join('?' * len(discs))})",
                (make_id("al-", tags["albumartist"][0], tags["album"][0]), group, *discs)).fetchall()
            # Only the tracks the upload replaces go: a deluxe edition's extra tracks stay.
            theirs = {Path(r[0]) for r in rows if (r[1], r[2]) in uploaded}
            theirs |= {p for t in (targets[x] for x in staged if x.parent == staging / str(i)) if t.parent.is_dir()
                       for p in t.parent.iterdir() if p.stem == t.stem and p.suffix.lower().lstrip(".") in AUDIO}
            theirs = {p for p in theirs if p.is_file()}
            present = theirs | {p for r in rows if (p := Path(r[0])).is_file()}
            mine_staged = [x for x in staged if x.parent == staging / str(i)]
            if present and not replace and not (all(map(lossless, mine_staged)) and not any(map(lossless, present))):
                raise Refused("already in the library", duplicate=True)
            old |= theirs
        try:
            checked = quality.import_check(sorted(p for p in staged if p.suffix == ".flac"))
        except quality.LossySource as e:
            raise Refused(str(e))
        for source, tags in staged.items():
            write(source, tags)
        try:
            self.place(targets, old)
        except OSError as e:
            raise Refused(f"Couldn't place the upload: {e}")
        try:  # the upload is in: what's left only adds to it, and a retry would be refused as a duplicate
            for source, found in checked.items():
                quality.store(db, targets[source], found)
            for folder, _, _ in albums:
                self.importer.cover(json.loads(row["release"]) if row["release"] else None, {self.folder / id}, self.importer.root / folder)
        except Exception:
            log.exception("upload %s is in the library, but its cover or quality findings aren't", id)
        self.remove(db, id)
        return firsts

    def place(self, targets: dict[Path, Path], old: set[Path]):
        """Copies each source to its target, the old files renamed aside meanwhile: deleted when all are in, else
        back in place with what was placed removed."""
        aside, placed = [(p, p.with_name(f".{p.name}.oasis-old")) for p in sorted(old)], []
        try:
            for p, kept in aside:
                os.rename(p, kept)
            for source, target in targets.items():
                target.parent.mkdir(parents=True, exist_ok=True)
                copy(source, target)
                placed.append(target)
                if target.stat().st_size != source.stat().st_size:
                    raise OSError(f"{target} isn't the {source.name} it was copied from")
        except BaseException:
            for target in placed:
                target.unlink(missing_ok=True)
            for p, kept in aside:
                with suppress(OSError):
                    os.rename(kept, p)
            for folder in {t.parent for t in targets.values()}:
                with suppress(OSError):
                    folder.rmdir()  # a new album's folder, empty again
            raise
        for _, kept in aside:
            kept.unlink()
