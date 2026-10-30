import re
from pathlib import Path

import mutagen

LRC_TIME = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")


def parse(text: str) -> tuple[list[tuple[int | None, str]], bool]:
    """Lines as (start ms, text); LRC timestamps make them synced. Lines with several timestamps repeat."""
    lines = []
    for raw in text.splitlines():
        times = LRC_TIME.findall(raw)
        words = LRC_TIME.sub("", raw).strip()
        if times:
            lines += [(round((int(m) * 60 + float(s)) * 1000), words) for m, s in times]
        elif not re.fullmatch(r"\[\w+:.*\]", raw.strip()):  # skip LRC headers like [ar:...]
            lines.append((None, raw.strip()))
    synced = any(start is not None for start, _ in lines)
    if synced:
        lines = sorted((line for line in lines if line[0] is not None), key=lambda line: line[0])
    while lines and not lines[-1][1] and not synced:
        lines.pop()
    return lines, synced


def local(path: str) -> tuple[list[tuple[int | None, str]], bool] | None:
    """Lyrics from a sidecar .lrc file, synced ID3 SYLT, or embedded lyrics tags."""
    sidecar = Path(path).with_suffix(".lrc")
    if sidecar.exists():
        return parse(sidecar.read_text(errors="replace"))
    tags = getattr(mutagen.File(path), "tags", None) or {}
    for key in tags.keys():
        if key.startswith("SYLT") and tags[key].format == 2:  # timestamps in milliseconds
            return [(start, text) for text, start in tags[key].text], True
    for key in tags.keys():
        if key.startswith("USLT"):
            return parse(tags[key].text)
        if key.lower() in ("lyrics", "unsyncedlyrics", "©lyr"):
            return parse(str(tags[key][0]))
    return None
