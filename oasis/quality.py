"""What a library FLAC really holds, read from its decoded samples, and what oasis does about it:

- padded: a 24-bit file whose samples all fit in 16 bits (the low 8 are zero) becomes the 16-bit FLAC it is, sample
  for sample;
- upsampled: a rate above 48 kHz whose spectrum ends in a brick wall at 22.05 or 24 kHz is resampled to 44.1 kHz
  16-bit (a CD) or 48 kHz 24-bit;
- lossy: a hard cutoff between 14 and 19.6 kHz, sustained across the file, is an MP3 or AAC decoded into FLAC. Such a
  download fails the import (the importer raises LossySource); the backlog deletes such library files, and
  library.check_quality wants their album again, so a proper release (even an MP3 one) replaces them;
- anything else is recompressed with flac -8 when that saves at least 3 %.

Every rewrite goes to a temporary file in the file's folder, keeps every metadata block (tags, pictures, cue sheets,
application blocks) byte for byte, and is checked before it renames over the original, so the path and the track id
stay: identical audio by MD5 or decoded samples for recompression and padding, length, rate and waveform for a
resample. Files that share their audio with another (dedupe.py's reflinks) are left as they are: rewriting one would
store its audio twice.

The spectrum is averaged over loud frames only (silence and quiet passages say nothing about the top octave), over the
whole file and per stretch of it; a cutoff counts only when every stretch loud enough shows it. Cases near the
thresholds are recorded as borderline and never acted on."""
import logging
import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np

from . import audiorecords
from .audio import COMPARE_RATE, check_decodes, chunks, decode
from .audiorecords import CONVERTED, LOSSY, PADDED, RESAMPLED, UPSAMPLED
from .files import stderr_tail

log = logging.getLogger(__name__)

FRAME_SECONDS = 0.1
BIN_HZ = 100               # the spectrum is read in bands this wide
LOUD_DB = 15               # frames within this of the loud end (95th percentile) are analysed
QUIET_DBFS = -50           # and never quieter than this
MIN_LOUD = 30              # loud frames a verdict needs: 3 s of music
STRETCHES = 6              # a cutoff must show in every stretch of the file with STRETCH_LOUD loud frames, at least 3 of them
STRETCH_LOUD = 10
GAP_HZ, SIDE_HZ = 1000, 1000  # a cliff compares the band SIDE_HZ wide GAP_HZ below the edge with the one above it
CLIFF_DB = 40              # a drop this steep is a cutoff (resamplers and lossy encoders: 60-110 dB; CD masters' own lowpass: 25-40)
BORDER_DB = 25             # steeper than this but less than CLIFF_DB: borderline
FLOOR_DB = 10              # above a cutoff nothing may rise more than this over the level just past it
LOSSY_HZ = 14000, 19600    # lossy encoders' lowpass (128 kbps MP3 ~16.5 kHz, AAC 192 ~19.3); CD masters' own end at 19.8-21 kHz
BORDER_LOSSY_HZ = 20300    # a 320 kbps MP3's ~20.1 kHz is here, among CD masters: borderline
FROM_44K_HZ = 22600        # an upsampled file's cutoff below this came from 44.1 kHz, else from 48 kHz
UPSAMPLED_HZ = 24600
NEAR_16 = 0.01             # a 24-bit file whose low 8 bits are used this rarely is effectively 16-bit: borderline
SMALLER = 0.03             # recompression is kept when it saves this much
SOXR = "resampler=soxr:precision=28"
JOBS = 8


class LossySource(LookupError):
    """A download holds a FLAC made from a lossy file."""


class Unverified(Exception):
    """A rewritten file failed its checks; the original stays."""


def analyse(path: str) -> dict:
    """rate, bits, channels; bits_used and low_share (samples using the low 8 of 24 bits); the spectrum's cutoff (Hz),
    its drop (dB) and whether every loud stretch shows it (sustained). cutoff is None below BORDER_DB."""
    rate, channels, bits, decoded = chunks(path)
    n = 1 << round(np.log2(rate * FRAME_SECONDS))
    window = np.hanning(n)
    starts = np.arange(0, n // 2 + 1, max(1, round(BIN_HZ * n / rate)))
    used, low, samples, levels, spectra, rest = 0, 0, 0, [], [], np.zeros(0)
    for chunk in decoded:
        used |= int(np.bitwise_or.reduce(chunk, axis=None))
        low += int(np.count_nonzero(chunk & 0xFFFF))
        samples += chunk.size
        mono = np.concatenate([rest, chunk.mean(1, dtype=np.float64) / 2 ** 31])
        k = len(mono) // n
        frames, rest = mono[:k * n].reshape(k, n), mono[k * n:]
        if k:
            levels.append(10 * np.log10((frames ** 2).mean(1) + 1e-20))
            spectra.append(np.add.reduceat(np.abs(np.fft.rfft(frames * window)) ** 2, starts, axis=1).astype(np.float32))
    result = {"rate": rate, "bits": bits, "channels": channels, "bits_used": bits_used(used, bits),
              "low_share": low / samples if samples else 0.0, "cutoff": None, "drop": None, "sustained": False}
    if not levels:
        return result
    levels, spectra = np.concatenate(levels), np.concatenate(spectra)
    loud = np.flatnonzero((levels >= np.percentile(levels, 95) - LOUD_DB) & (levels >= QUIET_DBFS))
    if len(loud) < MIN_LOUD:
        return result
    hz = starts * rate / n
    edge, drop = cliff(spectrum(spectra[loud]), hz)
    if drop < BORDER_DB:
        return result
    result["cutoff"], result["drop"] = round(hz[edge]), round(float(drop), 1)
    stretches = [loud[loud * STRETCHES // len(levels) == s] for s in range(STRETCHES)]
    found = [cliff(spectrum(spectra[i]), hz) for i in stretches if len(i) >= STRETCH_LOUD]
    result["sustained"] = len(found) >= 3 and all(e is not None and abs(hz[e] - hz[edge]) <= GAP_HZ and d >= BORDER_DB for e, d in found)
    return result


def bits_used(used: int, bits: int) -> int:
    """The file's depth less the low bits that are zero in every sample (used: all samples OR-ed, left-aligned)."""
    return max(0, min(bits, 32 - ((used & -used).bit_length() - 1))) if used else 0


def spectrum(power: np.ndarray) -> np.ndarray:
    return 10 * np.log10(power.mean(0) + 1e-30)


def cliff(level: np.ndarray, hz: np.ndarray) -> tuple[int | None, float]:
    """The steepest drop above 12 kHz with nothing rising again above it: (its edge band, dB), or (None, 0)."""
    step = hz[1] - hz[0]
    gap, side = round(GAP_HZ / step), round(SIDE_HZ / step)
    best, best_drop = None, 0.0
    for e in range(np.searchsorted(hz, 12000), len(level) - gap - side):
        below, above = level[e - gap - side:e - gap].mean(), level[e + gap:e + gap + side].mean()
        if below - above > best_drop and np.percentile(level[e + gap:], 95) <= above + FLOOR_DB:
            best, best_drop = e, below - above
    if best is None:
        return None, 0.0
    middle = level[best - gap - side:best - gap].mean() - best_drop / 2  # the edge: the last band above the drop's midpoint
    over = np.flatnonzero(level[best - gap:best + gap + 1] > middle)
    return best - gap + (int(over[-1]) if len(over) else gap), best_drop


def classify(a: dict) -> dict:
    """verdict: padded, upsampled (source_rate: what the cutoff implies), lossy, or None; borderline: the same kinds
    near the thresholds, only recorded."""
    cut, drop = a["cutoff"], a["drop"] or 0
    strong = cut is not None and drop >= CLIFF_DB and a["sustained"]
    verdict = borderline = source_rate = None
    if cut and LOSSY_HZ[0] <= cut <= LOSSY_HZ[1] and strong:
        verdict = LOSSY
    elif a["rate"] > 48000 and cut and cut <= UPSAMPLED_HZ and strong:
        verdict, source_rate = UPSAMPLED, 44100 if cut < FROM_44K_HZ else 48000
    elif a["bits"] > 16 and 0 < a["bits_used"] <= 16:
        verdict = PADDED
    if verdict != LOSSY and cut and LOSSY_HZ[0] <= cut <= BORDER_LOSSY_HZ:
        borderline = LOSSY
    elif verdict is None and a["rate"] > 48000 and cut and cut <= UPSAMPLED_HZ:
        borderline = UPSAMPLED
    elif verdict is None and a["bits"] > 16 and a["low_share"] < NEAR_16:
        borderline = PADDED
    return {"verdict": verdict, "borderline": borderline, "source_rate": source_rate}


def examine(path: Path) -> dict:
    return (a := analyse(str(path))) | classify(a)


def blocks(path: Path) -> tuple[list[tuple[int, bytes]], int]:
    """A FLAC's metadata blocks, (type, data), and where its audio frames start."""
    with open(path, "rb") as f:
        if f.read(4) != b"fLaC":
            raise Unverified(f"{path.name} doesn't start as a FLAC (an ID3 tag before it?)")
        found = []
        while True:
            head = f.read(4)
            found.append((head[0] & 0x7f, f.read(int.from_bytes(head[1:], "big"))))
            if head[0] & 0x80:
                return found, f.tell()


def stream(info: bytes) -> tuple[int, int, int, int]:
    """STREAMINFO's rate, channels, bits per sample and total samples."""
    v = int.from_bytes(info[10:18], "big")
    return v >> 44, (v >> 41 & 7) + 1, (v >> 36 & 31) + 1, v & (1 << 36) - 1


def kept(found: list[tuple[int, bytes]]) -> list[tuple[int, bytes]]:
    """The blocks a rewrite keeps as they are: all but STREAMINFO, PADDING and SEEKTABLE, which describe the audio."""
    return [(k, d) for k, d in found if k not in (0, 1, 3)]


def rebuild(original: Path, encoded: Path, out: Path):
    """The encoded audio under the original's metadata: the new STREAMINFO and SEEKTABLE, the original's other blocks
    and padding."""
    old, _ = blocks(original)
    new, start = blocks(encoded)
    padding = sum(len(d) for k, d in old if k == 1)
    chosen = [b for b in new if b[0] == 0] + [b for b in new if b[0] == 3] + kept(old) + ([(1, bytes(padding))] if padding else [])
    with open(out, "wb") as f, open(encoded, "rb") as audio:
        f.write(b"fLaC")
        for i, (kind, data) in enumerate(chosen):
            f.write(bytes([kind | (0x80 if i == len(chosen) - 1 else 0)]) + len(data).to_bytes(3, "big") + data)
        audio.seek(start)
        shutil.copyfileobj(audio, f, 1 << 20)
        f.flush()
        os.fsync(f.fileno())


def run(*args, **kwargs):
    try:
        return subprocess.run([str(a) for a in args], check=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=3600, **kwargs)
    except subprocess.CalledProcessError as e:
        raise Unverified(f"{args[0]}: {stderr_tail(e)}") from None


def pcm_hash(path: Path) -> str:
    """The decoded samples' hash, at 32 bits whatever the depth: equal for 16 bits padded to 24 and the 16 bits."""
    return run("ffmpeg", "-v", "error", "-i", path, "-map", "0:a:0", "-c:a", "pcm_s32le", "-f", "hash", "-hash", "sha256", "-").stdout.decode().strip()


def same_waveform(a: Path, b: Path) -> bool:
    """A resample's audio is the original's: the residual at dedupe's comparison rate is far below the music."""
    x, y = decode(str(a), COMPARE_RATE), decode(str(b), COMPARE_RATE)
    n = min(len(x), len(y))
    x, y = x[:n].astype(np.float64), y[:n].astype(np.float64)
    return bool(np.dot(x, x)) and 10 * np.log10(np.sum((x - y) ** 2) / np.dot(x, x) + 1e-30) < -40


def rewrite(path: Path, rate: int | None = None, bits: int | None = None, dither: bool = False) -> int | None:
    """Re-encodes path with flac -8, at another rate or depth when given; replaces it when checked (a recompression
    only when SMALLER). Returns the bytes saved, None when it isn't worth it."""
    before = path.stat()
    temps = [path.with_name(f".{path.name}.oasis-{kind}") for kind in ("pcm", "encoded", "new")]
    pcm, encoded, new = temps
    try:
        if rate or bits:
            fmt = "s16" if bits == 16 else "s32"
            filters = ":".join(filter(None, [f"{rate}" if rate else "", SOXR if rate else "", f"osf={fmt}", "dither_method=triangular" if dither else ""]))
            run("ffmpeg", "-v", "error", "-nostdin", "-y", "-i", path, "-map", "0:a:0", "-map_metadata", "-1", "-af", f"aresample={filters}",
                "-c:a", "flac", "-compression_level", "0", "-sample_fmt", fmt, *(["-bits_per_raw_sample", "24"] if bits == 24 else []), "-f", "flac", pcm)
        run("flac", "-8", "-s", "-f", "--no-padding", "-o", encoded, pcm if rate or bits else path)
        rebuild(path, encoded, new)
        if not (rate or bits) and new.stat().st_size > before.st_size * (1 - SMALLER):
            return None
        check(path, new, rate, bits)
        now = path.stat()
        if (now.st_ino, now.st_size, now.st_mtime_ns) != (before.st_ino, before.st_size, before.st_mtime_ns):
            raise Unverified(f"{path.name} changed meanwhile")
        shutil.copymode(path, new)
        os.replace(new, path)
        return before.st_size - path.stat().st_size
    finally:
        for temp in temps:
            temp.unlink(missing_ok=True)


def check(original: Path, new: Path, rate: int | None, bits: int | None):
    """Raises Unverified unless new holds the original's metadata and its audio: identical, or resampled."""
    old_blocks, _ = blocks(original)
    new_blocks, _ = blocks(new)
    if kept(old_blocks) != kept(new_blocks):
        raise Unverified("the metadata differs")
    old_info, new_info = next(d for k, d in old_blocks if k == 0), next(d for k, d in new_blocks if k == 0)
    (old_rate, channels, old_bits, samples), (new_rate, new_channels, new_bits, new_samples) = stream(old_info), stream(new_info)
    check_decodes(new, "flac")
    if rate:
        if (new_rate, new_channels, new_bits) != (rate, channels, bits) or abs(new_samples / rate - samples / old_rate) > 0.05:
            raise Unverified(f"resampled to {new_rate} Hz, {new_bits} bits, {new_samples / new_rate:.2f} s")
        if not same_waveform(original, new):
            raise Unverified("the resampled audio differs")
        return
    if (new_rate, new_channels, new_bits if bits else old_bits, new_samples) != (old_rate, channels, bits or old_bits, samples):
        raise Unverified("the stream's format differs")
    md5 = old_info[18:34]
    if not bits and any(md5):
        if new_info[18:34] != md5:
            raise Unverified("the MD5 differs")
    elif pcm_hash(original) != pcm_hash(new):
        raise Unverified("the decoded samples differ")


def act(path: Path, found: dict, shared: bool) -> dict:
    """Converts, resamples or recompresses one checked file (never a lossy one: see purge); found gains action
    (resampled, converted, recompressed, kept, shared, failed), was and saved."""
    found = found | {"action": None, "was": None, "saved": 0}
    if found["verdict"] == LOSSY:
        return found
    if shared:
        return found | {"action": "shared"}
    was = f'{found["bits"]}-bit {found["rate"] / 1000:g} kHz'
    try:
        if found["verdict"] == UPSAMPLED:
            bits = 16 if found["source_rate"] == 44100 or found["bits"] == 16 else 24
            saved = rewrite(path, found["source_rate"], bits, dither=bits == 16)
            found |= {"action": RESAMPLED, "was": was, "rate": found["source_rate"], "bits": bits}
        elif found["verdict"] == PADDED:
            saved = rewrite(path, bits=16)
            found |= {"action": CONVERTED, "was": was, "bits": 16}
        else:
            saved = rewrite(path)
            found["action"] = "recompressed" if saved is not None else "kept"
        found["saved"] = saved or 0
    except (Unverified, OSError, subprocess.TimeoutExpired) as e:
        log.warning("quality: %s left as it is: %s", path, e)
        found["action"] = "failed"
    if found["action"] not in ("kept", "failed"):
        log.info("quality: %s %s (%s), %+.1f MB", found["action"], path, found["verdict"] or "outlier", -found["saved"] / 1e6)
    return found


def backlog(db, jobs: int = JOBS, delete_lossy: bool = False, deleted=lambda db, rg: None) -> dict:
    """Checks every library FLAC not checked since it last changed, acts on it, then purges lossy-sourced ones (only
    listed unless delete_lossy; see purge for deleted). Resumable: each file's result is stored as it is done."""
    checked = audiorecords.checked(db)
    files = [Path(r[0]) for r in db.execute("select ref from track where source = 'local' and lower(suffix) = 'flac' order by ref")]
    todo = [p for p in files if p.exists() and checked.get(str(p)) != p.stat().st_mtime]
    shared, done, failed, started = audiorecords.shared_paths(db), 0, 0, time.time()
    log.info("quality: %d of %d FLACs to check, %d jobs", len(todo), len(files), jobs)
    with ThreadPoolExecutor(jobs) as pool:
        futures = {pool.submit(lambda p: act(p, examine(p), str(p) in shared), p): p for p in todo}
        for future in as_completed(futures):
            path = futures[future]
            try:
                found = future.result()
            except Exception as e:
                log.warning("quality: can't check %s: %s", path, e)
                failed += 1
                continue
            audiorecords.store_finding(db, path, found)
            if found["action"] == "recompressed":
                audiorecords.retagged(db, str(path))  # the same audio: dedupe's verdicts on it stay
            done += 1
            if done % 50 == 0:
                log.info("quality: %d of %d checked, %.0f s", done, len(todo), time.time() - started)
    log.info("quality: %d checked, %d couldn't be, in %.0f s", done, failed, time.time() - started)
    return {"checked": done, "unreadable": failed, "purged": purge(db, delete_lossy, deleted)}


def purge(db, delete: bool, deleted) -> list[dict]:
    """Lossy-sourced library FLACs, by album: deleted (with the rest of the album when most of it is lossy-sourced),
    then deleted(db, rg) with the album's release group, in the same commit, so it is wanted again however a later
    album goes. Files no release group ties to an album stay, and are only listed."""
    lossy = audiorecords.with_verdict(db, LOSSY)
    if not lossy:
        return []
    albums: dict[str, list] = {}
    for r in db.execute(f"select t.* from track t where t.source = 'local' and t.album_id in "
                        f"(select album_id from track where ref in ({','.join('?' * len(lossy))}))", list(lossy)):
        albums.setdefault(r["album_id"], []).append(r)
    report = []
    for tracks in albums.values():
        bad = [t for t in tracks if t["ref"] in lossy]
        groups = [t["mb_release_group"] for t in tracks if t["mb_release_group"]]
        rg = max(set(groups), key=groups.count) if groups else None
        doomed = tracks if len(bad) * 2 > len(tracks) else bad
        entry = {"artist": tracks[0]["album_artist"], "album": tracks[0]["album"], "rg": rg, "lossy": len(bad), "tracks": len(tracks),
                 "deleted": 0, "files": [t["ref"] for t in doomed]}
        report.append(entry)
        if not rg:
            log.warning("quality: %s - %s: %d of %d tracks lossy-sourced, but no release group: left in place", entry["artist"], entry["album"],
                        len(bad), len(tracks))
            continue
        if not delete:
            log.info("quality: %s - %s: %d of %d tracks lossy-sourced; --delete-lossy deletes %d", entry["artist"], entry["album"], len(bad),
                     len(tracks), len(doomed))
            continue
        for t in doomed:
            Path(t["ref"]).unlink(missing_ok=True)
        audiorecords.gone(db, [t["ref"] for t in doomed])
        entry["deleted"] = len(doomed)
        deleted(db, rg)
        db.commit()
        log.info("quality: %s - %s: deleted %d files (%d lossy-sourced)", entry["artist"], entry["album"],
                 len(doomed), len(bad))
    return report


def import_check(files: list[Path], jobs: int = JOBS) -> dict[Path, dict]:
    """A download's FLACs examined and acted on before they are placed; LossySource when any is lossy-sourced. A file
    that can't be read is left to the import as it is."""
    def safely(f, *args):
        try:
            return f(*args)
        except Exception as e:
            log.warning("quality: can't check %s: %s", args[0], e)
            return None
    with ThreadPoolExecutor(jobs) as pool:
        found = {p: r for p, r in zip(files, pool.map(lambda p: safely(examine, p), files)) if r}
        if lossy := sorted((p, r) for p, r in found.items() if r["verdict"] == LOSSY):
            raise LossySource(f"lossy source in a FLAC: {lossy[0][0].name} is cut off at {lossy[0][1]['cutoff'] / 1000:.1f} kHz"
                              + (f", and {len(lossy) - 1} more" if len(lossy) > 1 else ""))
        return {p: r for p, r in zip(found, pool.map(lambda p: safely(act, p, found[p], False), found)) if r}
