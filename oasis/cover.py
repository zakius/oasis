import base64
import urllib.request
from pathlib import Path

import mutagen
from mutagen.flac import Picture

MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


def embedded(path: str) -> tuple[bytes, str] | None:
    audio = mutagen.File(path)
    if audio is None:
        return None
    if getattr(audio, "pictures", None):
        return audio.pictures[0].data, audio.pictures[0].mime
    tags = audio.tags or {}
    for key in tags.keys():
        if key.startswith("APIC"):
            return tags[key].data, tags[key].mime
        if key == "covr":
            cover = tags[key][0]
            return bytes(cover), "image/png" if cover.imageformat == cover.FORMAT_PNG else "image/jpeg"
        if key.lower() == "metadata_block_picture":
            picture = Picture(base64.b64decode(tags[key][0]))
            return picture.data, picture.mime
    return None


def load(track_path: str, cover: str) -> tuple[bytes, str] | None:
    if cover == "embedded":
        return embedded(track_path)
    if cover.startswith("https://"):
        with urllib.request.urlopen(cover, timeout=15) as response:
            return response.read(), response.headers.get_content_type()
    image = Path(cover)
    return image.read_bytes(), MIME.get(image.suffix.lower(), "image/jpeg")
