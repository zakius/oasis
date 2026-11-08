"""The kinds of file oasis handles, told by their suffixes: the audio the library holds (and the MIME type it is
served as), which of it is lossless, the archives a download may pack it in, and images, of which a cover is one named
as COVER_NAMES. ALAC lives in .m4a, so whether an .m4a is lossless takes a look inside (lossless); a decoder tells by
the codec (lossless_codec)."""
import os
from pathlib import Path

import mutagen

AUDIO = {
    "mp3": "audio/mpeg", "flac": "audio/flac", "ogg": "audio/ogg", "oga": "audio/ogg", "opus": "audio/ogg",
    "m4a": "audio/mp4", "aac": "audio/aac", "wav": "audio/wav", "wv": "audio/x-wavpack",
    "ape": "audio/x-ape", "mpc": "audio/x-musepack", "aiff": "audio/aiff", "aif": "audio/aiff",
}
LOSSLESS = ("flac", "wav", "ape", "wv", "aiff", "aif")
PCM = ("wav", "aif", "aiff")  # made FLAC on import and in the library (pcm.py)
LOSSLESS_CODECS = ("flac", "alac", "ape", "wavpack")  # as ffmpeg names them, and mutagen ALAC's; PCM's are pcm_s16le and the like
ARCHIVES = ("zip", "rar", "7z")
IMAGES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif"}
COVERS = ("jpg", "jpeg", "png", "webp")
COVER_NAMES = ("cover", "folder", "front", "album")


def suffix(name) -> str:
    """'01 Title.FLAC' -> 'flac': a file name's or path's suffix, lowercase and without its dot; '' when it has none."""
    return os.path.splitext(name)[1].lower().lstrip(".")


def is_cover(path: Path) -> bool:
    """An image named as covers are: 'cover.jpg', 'Folder.png'."""
    return suffix(path) in COVERS and path.stem.lower() in COVER_NAMES


def lossless_codec(codec: str) -> bool:
    return codec in LOSSLESS_CODECS or codec.startswith("pcm_")


def lossless(path: Path) -> bool:
    return suffix(path) in LOSSLESS or suffix(path) == "m4a" and lossless_codec(getattr(getattr(mutagen.File(path), "info", None), "codec", ""))
