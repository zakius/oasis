"""Keeps one copy of each audio stream in the library. Files of one song (same artist and title, lengths within
LENGTH_SLACK) are compared by their decoded audio, and match when they are

1. identical samples, however they were encoded;
2. identical samples shifted or trimmed: different rips of one master;
3. one master at another resolution, or lossy: lossless copies by their waveform after aligning and level matching,
   lossy ones by tonal balance and loudness over time, which codecs keep and remastering changes.

Versions tagged as such (live, demo, acoustic, remix, edit...) never take part. MusicBrainz recording ids aren't
consulted: the same audio often has several, from singles and compilations nobody merged. Of matching files the best is kept (lossless, then resolution, then the most
complete, then the smallest FLAC), and every other one becomes a copy-on-write clone of it (btrfs/XFS reflink) with
its own tags written back, so each edition keeps its file, folder and tags while the audio is stored once."""
import errno
import itertools
import logging
import os
import re
from collections import defaultdict
from pathlib import Path

import mutagen
import numpy as np
from mutagen._vorbis import VCommentDict
from mutagen.flac import FLAC
from mutagen.id3 import ID3
from mutagen.mp3 import MP3

from . import audiorecords
from .audio import COMPARE_RATE, decode, stream_info
from .files import reflink
from .formats import lossless_codec, suffix
from .tags import pictures, values
from .text import fold, song_key

log = logging.getLogger(__name__)

LENGTH_SLACK = 15          # seconds copies of one song may differ by
LOSSLESS_SAME_DB = -50     # waveform residual of lossless copies of one master (measured: -74 same, -27 for a 1 dB EQ)
LOSSY_SAME_DB = 0.5        # tonal balance and loudness deviation allowed with lossy copies (codecs <= 0.27, remasters >= 0.8)
KEEPABLE = {"flac", "mp3"}  # formats the kept copy may have: tags are rewritten in place over the shared audio
VERSION = re.compile(r"\b(live|demo|acoustic|unplugged|re-?mix(es)?|mix|edit|instrumental|karaoke|session|rehearsal|a ?cappella|"
                     r"orchestral|piano|cover|(?<!album )(?<!original )(?<!single )version)\b", re.I)


def probe(path: str) -> dict:
    info = stream_info(path)
    number = lambda key: int(info[key]) if info.get(key, "").isdigit() else 0
    codec = info.get("codec_name", "")
    tags, audio = mutagen.File(path, easy=True), mutagen.File(path)
    return {"path": path, "codec": codec, "rate": number("sample_rate"), "channels": number("channels"),
            "bits": number("bits_per_raw_sample") or {"s16": 16, "s16p": 16, "s32": 32, "s32p": 32}.get(info.get("sample_fmt"), 0),
            "bitrate": number("bit_rate"), "lossless": lossless_codec(codec),
            "length": getattr(audio.info, "length", 0) if audio else 0, "size": os.path.getsize(path),
            "md5": getattr(audio.info, "md5_signature", 0) if isinstance(audio, FLAC) else 0,
            "release_type": "; ".join(values(tags, "releasetype"))}


def offset(a: np.ndarray, b: np.ndarray) -> int:
    """How many samples later the audio is in b than in a, from cross-correlating a stretch of a's middle."""
    window = min(len(a), len(b), 1 << 18) // 2
    start = max(0, len(a) // 2 - window // 2)
    piece = a[start:start + window].astype(np.float64)
    lo = max(0, start - window)
    region = b[lo:lo + 3 * window].astype(np.float64)
    n = 1 << (len(region) + len(piece)).bit_length()
    corr = np.fft.irfft(np.fft.rfft(region, n) * np.conj(np.fft.rfft(piece, n)), n)[:len(region) - len(piece) + 1]
    return lo + int(np.argmax(corr)) - start


def overlap(a: np.ndarray, b: np.ndarray, shift: int) -> tuple[np.ndarray, np.ndarray]:
    a, b = (a, b[shift:]) if shift >= 0 else (a[-shift:], b)
    n = min(len(a), len(b))
    return a[:n], b[:n]


def compare(x: dict, y: dict) -> int:
    """The level (1-3) at which two files are one stream, 0 when they aren't."""
    if x["md5"] and x["md5"] == y["md5"] and (x["rate"], x["bits"], x["channels"]) == (y["rate"], y["bits"], y["channels"]):
        return 1
    if x["lossless"] and y["lossless"] and (x["rate"], x["channels"]) == (y["rate"], y["channels"]):
        a, b = decode(x["path"]), decode(y["path"])
        if len(a) == len(b) and np.array_equal(a, b):
            return 1
        ch = x["channels"] or 1
        oa, ob = overlap(a, b, offset(a[::ch], b[::ch]) * ch)
        if len(oa) > 0.95 * min(len(a), len(b)) and np.array_equal(oa, ob):
            return 2
        del a, b, oa, ob
    a, b = decode(x["path"], COMPARE_RATE), decode(y["path"], COMPARE_RATE)
    oa, ob = overlap(a, b, offset(a, b))
    edge = COMPARE_RATE  # fades and silence at the ends say little
    oa, ob = oa[edge:-edge].astype(np.float64), ob[edge:-edge].astype(np.float64)
    if len(oa) < 0.8 * min(len(a), len(b)) - 2 * edge or not oa.any() or not ob.any():
        return 0
    gain = np.dot(oa, ob) / np.dot(ob, ob)
    residual = 10 * np.log10(np.sum((oa - gain * ob) ** 2) / np.sum(oa ** 2))
    if x["lossless"] and y["lossless"]:
        return 3 if residual < LOSSLESS_SAME_DB else 0
    return 3 if residual < -10 and max(deviations(oa, ob)) < LOSSY_SAME_DB else 0


def deviations(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    """How far b's tonal balance (third-octave bands) and loudness over time (128 ms frames) stray from a's, in dB,
    after the overall level difference: what remastering changes and lossy coding doesn't."""
    n = 2048
    frames = len(a) // n
    fa, fb = a[:frames * n].reshape(frames, n), b[:frames * n].reshape(frames, n)
    window = np.hanning(n)
    sa, sb = np.abs(np.fft.rfft(fa * window)) ** 2, np.abs(np.fft.rfft(fb * window)) ** 2
    freqs = np.fft.rfftfreq(n, 1 / COMPARE_RATE)
    edges = 60 * 2 ** np.arange(0, np.log2(7000 / 60), 1 / 3)
    bands = [(freqs >= lo) & (freqs < hi) for lo, hi in zip(edges, edges[1:])]
    tone = 10 * np.log10(np.array([sa[:, m].sum() for m in bands]) / np.array([sb[:, m].sum() for m in bands]))
    ea, eb = 10 * np.log10((fa ** 2).mean(1) + 1e-12), 10 * np.log10((fb ** 2).mean(1) + 1e-12)
    loud = ea > ea.max() - 30
    envelope = ea[loud] - eb[loud]
    return float(np.abs(tone - np.median(tone)).max()), float(np.percentile(np.abs(envelope - np.median(envelope)), 95))


def rank(f: dict) -> tuple:
    """Lossless first, then resolution, the most complete, the best lossy bitrate, then the smallest FLAC. Lossless
    lengths count in samples, since rips one CD frame (13 ms) apart differ by less than any rounding; lossy ones in
    tenths of a second, as encoders pad them differently."""
    return (f["lossless"], f["bits"] if f["lossless"] else 0, f["rate"], round(f["length"] * f["rate"]) if f["lossless"] else round(f["length"], 1),
            0 if f["lossless"] else f["bitrate"], f["codec"] == "flac", -f["size"])


def clone(kept: Path, replaced: Path) -> Path:
    """Replaces `replaced` with a reflink of `kept` carrying `replaced`'s tags; the file takes `kept`'s extension."""
    target = replaced.with_suffix(kept.suffix)
    tmp = replaced.with_name(f".oasis-{replaced.stem}{kept.suffix}")
    try:
        with open(kept, "rb") as source, open(tmp, "wb") as copy:
            reflink(source, copy)
        copy_tags(replaced, tmp, suffix(kept))
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    if target != replaced:
        replaced.unlink()
    return target


def keep_size(info):
    """Writes tags into the existing padding, so only the first blocks of the clone stop being shared."""
    return info.padding if info.padding >= 0 else 1 << 16


def copy_tags(source: Path, target: Path, kind: str):
    """Writes source's tags into target, a clone of kind ('flac' or 'mp3')."""
    original, easy = mutagen.File(source), mutagen.File(source, easy=True)
    if kind == "mp3":
        if isinstance(original, MP3) and original.tags is not None:
            original.tags.save(target, padding=keep_size)  # ID3 holds no stream data: it saves onto any MP3 as is
            return
        tags = ID3()
        tags.save(target)
        out = mutagen.File(target, easy=True)
        for key in easy.keys() if easy else ():
            out[key] = easy[key]
        out.save(padding=keep_size)
        return
    out = FLAC(target)
    out.clear()
    out.clear_pictures()
    if isinstance(original, FLAC):
        out.tags.extend(list(original.tags or []))
        for picture in original.pictures:
            out.add_picture(picture)
    else:
        for key in easy.keys() if easy else ():
            out[key] = easy[key]
        if not isinstance(original.tags, VCommentDict):  # Vorbis comments' pictures came with the tags above
            for picture in pictures(original):
                out.add_picture(picture)
    out.save(padding=keep_size)


def pad(kept: Path, replaced: list[Path]):
    """Gives the kept FLAC room for the biggest tags among its clones, so writing them never moves the audio."""
    if suffix(kept) != "flac":
        return
    needed = max(sum(len(p.data) for p in pictures(mutagen.File(path))) for path in replaced) + (1 << 16)
    audio = FLAC(kept)
    current = sum(len(p.data) for p in audio.pictures) + next((b.length for b in audio.metadata_blocks if b.code == 1), 0)
    if current < needed:
        audio.save(padding=lambda info: needed)


def run(db) -> list[tuple[str, str]]:
    """One pass over the library; returns the files it replaced, as (old path, new path)."""
    songs = defaultdict(list)
    for row in db.execute("select ref, title, album, album_artist, artist, duration, mtime from track where source = 'local'"):
        songs[(fold(row["album_artist"] or row["artist"] or ""), song_key(row["title"]))].append(row)
    changed = []
    for rows in songs.values():
        rows = [r for r in rows if os.path.exists(r["ref"])]
        if len(rows) < 2:
            continue
        try:
            changed += dedupe_song(db, rows)
        except OSError as e:
            if e.errno in (errno.EOPNOTSUPP, errno.EXDEV, errno.ENOTTY):  # no reflinks here, nothing can be saved
                log.warning("dedupe: the library's filesystem can't share data between files (%s); dedupe is off", e)
                return changed
            log.warning("dedupe: %s: %s", rows[0]["title"], e)
        except Exception as e:
            log.warning("dedupe: %s: %s", rows[0]["title"], e)
    if changed:
        log.info("dedupe: %d files now share their audio with another copy", len(changed))
    return changed


def dedupe_song(db, rows) -> list[tuple[str, str]]:
    mtimes = {r["ref"]: os.path.getmtime(r["ref"]) for r in rows}
    shared = audiorecords.clones(db, list(mtimes))

    def is_clone(path, of):
        s = shared.get(path)
        return s is not None and s["kept"] == of and s["mtime"] == mtimes[path] and s["kept_mtime"] == mtimes.get(of)

    infos, levels = {}, {}

    def info(path):
        if path not in infos:
            infos[path] = probe(path)
        return infos[path]

    def level(x, y):
        a, b = sorted((x, y))
        if (a, b) in levels:
            return levels[a, b]
        result = audiorecords.pair_level(db, a, b, mtimes[a], mtimes[b])
        if result is None:
            if is_clone(a, b) or is_clone(b, a):
                result = 1
            else:
                ia, ib = info(a), info(b)
                live = "live" in (ia["release_type"] + ib["release_type"]).lower()
                result = 0 if live else compare(ia, ib)
            audiorecords.store_pair(db, a, b, mtimes[a], mtimes[b], result)
            db.commit()
        levels[a, b] = result
        return result

    pool = [r for r in rows if not VERSION.search(r["title"] or "") and not VERSION.search(r["album"] or "")]
    pairs = [(x, y) for x, y in itertools.combinations(pool, 2) if abs((x["duration"] or 0) - (y["duration"] or 0)) <= LENGTH_SLACK]
    matched = {(x["ref"], y["ref"]) for x, y in pairs if level(x["ref"], y["ref"])}
    if not matched:
        return []
    changed, done = [], set()
    # A copy already kept stays kept when the rest ties: its padding for the clones' tags mustn't make a clone look smaller.
    already_kept = {r["kept"] for r in shared.values()}
    key = lambda p: (*rank(info(p))[:-1], p in already_kept, rank(info(p))[-1])
    for kept in sorted({p for pair in matched for p in pair}, key=key, reverse=True):
        if kept in done:
            continue
        copies = [p for p in {q for pair in matched if kept in pair for q in pair} - {kept} - done]
        done |= {kept, *copies}
        if info(kept)["codec"] not in KEEPABLE:
            continue
        todo = [p for p in copies if not is_clone(p, kept)]
        if not todo:
            continue
        pad(Path(kept), [Path(p) for p in todo])
        kept_mtime = os.path.getmtime(kept)
        for path in todo:
            new = clone(Path(kept), Path(path))
            audiorecords.store_clone(db, path, str(new), kept, os.path.getmtime(new), kept_mtime)
            changed.append((path, str(new)))
        db.commit()
        log.info("dedupe: %s: %d copies now share %s", Path(kept).stem, len(todo), Path(kept).name)
    return changed
