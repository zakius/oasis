"""WAV and AIFF made FLAC: integer PCM becomes FLAC -8 at its rate and depth (8-bit as 16-bit: ffmpeg's FLAC has no
8-bit), its tags carried over, and replaces the original only when the FLAC decodes to the same samples. Float PCM stays
(FLAC holds integers only), as do DTS and AC3 (a surround disc's own format) and anything else ffmpeg can't name;
lossy audio in a WAV is remuxed, not re-encoded, to its own container, so it counts as the lossy file it is."""
import logging
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

log = logging.getLogger(__name__)

SUFFIXES = ("wav", "aif", "aiff")
INTEGER = ("pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_u8", "pcm_s16be", "pcm_s24be", "pcm_s32be")
LOSSY = {"mp3": ("mp3", "mp3"), "aac": ("m4a", "ipod"), "vorbis": ("ogg", "ogg"), "opus": ("opus", "opus")}  # codec: suffix, ffmpeg muxer
KEPT: set[str] = set()  # files looked at and left as they are, not probed again while oasis runs


def codec(path: Path) -> str:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, timeout=120)
    return out.stdout.strip()


def samples(path: Path) -> str:
    """The MD5 of the decoded samples, as 32-bit integers whatever the depth."""
    return subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-map", "0:a:0", "-c:a", "pcm_s32le", "-f", "hash", "-hash", "md5", "-"],
                          capture_output=True, check=True, text=True, timeout=1800).stdout.strip()


def convert(path: Path) -> Path | None:
    """The file that replaced path, or None when it stays (logged why). Never over an existing file."""
    if str(path) in KEPT:
        return None
    try:
        found = codec(path)
    except (subprocess.SubprocessError, OSError) as e:
        log.warning("%s stays: can't probe it: %s", path, e)
        KEPT.add(str(path))
        return None
    if found in INTEGER:
        suffix, args = "flac", ["-c:a", "flac", "-compression_level", "8", "-f", "flac"]  # ffmpeg keeps 16 and 24 bits; 32 bits as 24 fails the check
    elif found in LOSSY:
        suffix, args = LOSSY[found][0], ["-c:a", "copy", "-f", LOSSY[found][1]]
    else:
        log.info("%s stays as it is: %s", path, f"{found}, which FLAC can't hold losslessly" if found.startswith("pcm_f") else found or "no audio ffmpeg knows")
        KEPT.add(str(path))
        return None
    target, part = path.with_suffix("." + suffix), path.with_name(f".{path.name}.oasis-part")
    if target.exists():
        log.warning("%s stays: %s is there already", path, target.name)
        KEPT.add(str(path))
        return None
    try:
        subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(path), "-map", "0:a:0", "-map_metadata", "0", *args, str(part)],
                       capture_output=True, check=True, timeout=1800)
        if suffix == "flac" and (want := samples(path)) != (got := samples(part)):
            log.warning("%s stays: its FLAC decodes to other samples (%s, not %s)", path, got, want)
            KEPT.add(str(path))
            return None
        os.link(part, target)
    except subprocess.CalledProcessError as e:
        log.warning("%s stays: ffmpeg failed: %s", path, e.stderr.decode(errors="replace").strip()[-200:] if isinstance(e.stderr, bytes) else e.stderr)
        KEPT.add(str(path))
        return None
    except (subprocess.SubprocessError, OSError) as e:
        log.warning("%s stays: %s", path, e)
        KEPT.add(str(path))
        return None
    finally:
        part.unlink(missing_ok=True)
    before, after = path.stat().st_size, target.stat().st_size
    path.unlink()
    log.info("%s: %s %s, %.1f MB to %.1f MB%s", path.name, found, "to FLAC, same samples" if suffix == "flac" else f"remuxed to .{suffix}",
             before / 1e6, after / 1e6, "" if suffix == "flac" else " (lossy in a WAV)")
    return target


def convert_all(paths: list[Path], jobs: int = 4) -> dict[Path, Path]:
    """Each WAV or AIFF of paths converted (convert), in parallel: old path -> new."""
    todo = [p for p in paths if p.suffix.lower().lstrip(".") in SUFFIXES]
    with ThreadPoolExecutor(jobs) as pool:
        return {p: new for p, new in zip(todo, pool.map(convert, todo)) if new}
