"""Files on disk: copying them into the library, a reflink where the filesystem lets files share data (btrfs, XFS),
else a copy of the bytes (dedupe.py's clones are reflinks only, since a copy of the bytes would save nothing); the room
a disk has for more (NoSpace); and what a failed command run on them (unar, ffmpeg, flac) said."""
import fcntl
import os
import shutil
import subprocess
from pathlib import Path

FICLONE = 0x40049409  # the ioctl that makes one file share another's data


def reflink(source, target):
    """Makes target (open for writing) share source's data (open for reading); OSError where the filesystem can't."""
    fcntl.ioctl(target.fileno(), FICLONE, source.fileno())


def copy(source: Path, target: Path, over: bool = False):
    """A reflink when the filesystem can, else a copy; synced, checked to be source's size, and never over an existing
    file unless over."""
    part = target.with_name(f".{target.name}.oasis-part")
    try:
        with open(source, "rb") as src, open(part, "wb") as out:
            try:
                reflink(src, out)
            except OSError:
                shutil.copyfileobj(src, out, 1 << 20)
            out.flush()
            os.fsync(out.fileno())
        if part.stat().st_size != source.stat().st_size:
            raise OSError(f"{target} isn't the {source.name} it was copied from")
        os.replace(part, target) if over else os.link(part, target)
    finally:
        part.unlink(missing_ok=True)


class NoSpace(OSError):
    """Too little disk space: the download waits for more, and its release isn't at fault."""


def need_room(folder: Path, need: int, task: str = ""):
    """NoSpace unless the disk holding folder has need bytes free; task says what for ('unpack x.zip')."""
    if (free := shutil.disk_usage(folder).free) < need:
        raise NoSpace(f"too little disk space{' to ' + task if task else ''}: {need / 1e9:.1f} GB needed, {free / 1e9:.1f} GB free")


def stderr_tail(e: subprocess.CalledProcessError) -> str:
    """The end of a failed command's error output, short enough for a message."""
    return e.stderr.decode(errors="replace").strip()[-200:]
