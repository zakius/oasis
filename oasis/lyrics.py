"""Lyrics from the track's own files and from the providers roma uses, chosen by roma's rules (YouTube Music's text is
the reference). Lines are (start ms or None, text)."""
import base64
import html
import json
import logging
import re
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path

import mutagen

log = logging.getLogger(__name__)

LRC_TIME = re.compile(r"\[(\d+):(\d+(?:[.:]\d+)?)\]")
WORD_TIME = re.compile(r"<(\d+):(\d+(?:[.:]\d+)?)>")
# Above this, two texts are the same lyrics: the same lyrics from two providers score 0.77-1.0, another language
# under 0.1, a different song in the same language around 0.3 (measured for roma).
SAME_LYRICS = 0.6
TIMEOUT = 5


@dataclass
class Lyrics:
    source: str
    lines: list[tuple[int | None, str]]
    synced: bool
    word_level: bool = False

    def text(self) -> str:
        return "\n".join(text for _, text in self.lines)


@dataclass
class Query:
    title: str
    artist: str
    album: str | None
    duration: int


def ms(minutes: str, seconds: str) -> int:
    return int(minutes) * 60_000 + round(float(seconds.replace(":", ".")) * 1000)


def parse(text: str, source: str = "file") -> Lyrics:
    """LRC (also with <mm:ss.xx> word stamps) or plain text. A line with several timestamps repeats. An [offset:ms]
    tag is applied to the times (positive shows lines sooner), so oasis always sends offset 0."""
    lines, word_level = [], False
    offset = int(m.group(1)) if (m := re.search(r"^\[offset:\s*([+-]?\d+)\s*\]", text, re.M | re.I)) else 0
    for raw in text.splitlines():
        rest, stamps = raw.strip(), []
        while m := LRC_TIME.match(rest):
            stamps.append(max(0, ms(*m.groups()) - offset))
            rest = rest[m.end():]
        if WORD_TIME.search(rest):
            word_level = True
            rest = WORD_TIME.sub("", rest)
        if stamps:
            lines += [(start, rest.strip()) for start in stamps]
        elif not re.fullmatch(r"\[\w+:.*\]", raw.strip()):  # LRC headers like [ar:...]
            lines.append((None, raw.strip()))
    synced = any(start is not None for start, _ in lines)
    if synced:
        lines = sorted((line for line in lines if line[0] is not None), key=lambda line: line[0])
    else:
        while lines and not lines[-1][1]:
            lines.pop()
    return Lyrics(source, lines, synced, word_level and synced)


def local(path: str) -> Lyrics | None:
    """Lyrics from a sidecar .lrc file, synced ID3 SYLT, or embedded lyrics tags."""
    sidecar = Path(path).with_suffix(".lrc")
    if sidecar.exists():
        return parse(sidecar.read_text(errors="replace"))
    tags = getattr(mutagen.File(path), "tags", None) or {}
    for key in tags.keys():
        if key.startswith("SYLT") and tags[key].format == 2:  # timestamps in milliseconds
            return Lyrics("file", [(start, text) for text, start in tags[key].text], True)
    for key in tags.keys():
        if key.startswith("USLT"):
            return parse(tags[key].text)
        if key.lower() in ("lyrics", "unsyncedlyrics", "©lyr"):
            return parse(str(tags[key][0]))
    return None


# Cleaning, as roma does

# "作词 : X", "作曲：X", "Lyrics by X": a role, then a colon. Chinese providers put these at the top.
CREDIT = re.compile(r"^\s*(作词|作詞|作曲|编曲|編曲|词|詞|曲|制作人|制作|製作|监制|監修|混音|混缩|母带|和声|吉他|贝斯|鼓|弦乐|录音|出品|发行|企划|OP|SP|"
                    r"Lyrics|Lyricist|Music|Composer|Arranger|Arrangement|Producer|Produced by|Written by)\s*(by)?\s*[:：]", re.I)


def cleaned(found: Lyrics, query: "Query") -> Lyrics:
    """Unescapes HTML entities and backslashed quotes, and drops credit lines and a leading "Artist - Title" line
    (KuGou starts with one): they aren't sung."""
    def unescape(text):
        return re.sub(r"\\(['\"])", r"\1", html.unescape(text))
    lines = [(start, unescape(text)) for i, (start, text) in enumerate(found.lines)
             if not CREDIT.search(text) and not (i < 3 and " - " in text and same_title(text.split(" - ", 1)[1], query.title))]
    return Lyrics(found.source, lines, found.synced, found.word_level)


# Matching

def fold_title(s: str) -> str:
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)).lower()
    s = re.sub(r"[(\[（【].*?[)\]）】]", "", s)
    return re.sub(r"[\s\W_]+", "", s)


def same_title(a: str, b: str) -> bool:
    """Same song title, ignoring case, spaces, punctuation and bracketed extras such as "(TV Size)"."""
    x, y = fold_title(a), fold_title(b)
    return bool(x and y and (x in y or y in x))


def closest(items: list, duration: int, duration_of):
    """The candidate whose length is closest, within 5 s when the song's length is known."""
    if duration <= 0:
        return items[0] if items else None
    near = [i for i in items if abs(duration_of(i) - duration) <= 5]
    return min(near, key=lambda i: abs(duration_of(i) - duration), default=None)


def similarity(a: str, b: str) -> float:
    """Dice coefficient of character bigrams over lowercase NFKC letters and digits."""
    def grams(s):
        s = "".join(c for c in unicodedata.normalize("NFKC", s).lower() if c.isalnum())
        counts = {}
        for i in range(len(s) - 1):
            counts[s[i:i + 2]] = counts.get(s[i:i + 2], 0) + 1
        return counts
    x, y = grams(a), grams(b)
    total = sum(x.values()) + sum(y.values())
    return 2 * sum(min(n, y.get(g, 0)) for g, n in x.items()) / total if total else 0.0


def choose(ytm: Lyrics | None, others: list[Lyrics]) -> Lyrics | None:
    """roma's rules: 1. word-timed that matches YouTube Music (any word-timed when it has none), 2. YouTube Music
    line-timed, 3. other synced that matches it, 4. YouTube Music plain, 5. other synced, then plain."""
    if ytm is None:
        return next((l for l in others if l.word_level), None) or next((l for l in others if l.synced), None) \
            or (others[0] if others else None)
    reference = ytm.text()

    def best(candidates):
        scored = [(similarity(l.text(), reference), l) for l in candidates]
        scored = [s for s in scored if s[0] > SAME_LYRICS]
        return max(scored, key=lambda s: s[0])[1] if scored else None
    if found := best([l for l in others if l.word_level]):
        return found
    if ytm.synced:
        return ytm
    return best([l for l in others if l.synced]) or ytm


# Providers: each returns Lyrics, None when it has nothing, or raises when it couldn't answer.

def get(url: str, headers: dict | None = None, data: bytes | None = None) -> bytes:
    request = urllib.request.Request(url, data, {"User-Agent": "Mozilla/5.0 oasis", **(headers or {})})
    with urllib.request.urlopen(request, timeout=12) as response:
        return response.read()


def q(s) -> str:
    return urllib.parse.quote(str(s))


def lrclib(query: Query) -> Lyrics | None:
    """LRCLIB: free, no key. Line-synced LRC, sometimes with word stamps."""
    items = json.loads(get(f"https://lrclib.net/api/search?track_name={q(query.title)}&artist_name={q(query.artist)}"))
    items = [i for i in items if same_title(i.get("trackName") or "", query.title)]
    synced = closest([i for i in items if i.get("syncedLyrics")], query.duration, lambda i: i.get("duration") or 0)
    if synced:
        return parse(synced["syncedLyrics"], "LRCLIB")
    plain = closest([i for i in items if i.get("plainLyrics")], query.duration, lambda i: i.get("duration") or 0)
    return parse(plain["plainLyrics"], "LRCLIB") if plain else None


def parse_timed(text: str, word: re.Pattern, source: str) -> Lyrics | None:
    """NetEase YRC "[start,dur](wordStart,dur,0)word" and KuGou KRC "[start,dur]<offset,dur,0>word"."""
    lines = []
    for raw in text.splitlines():
        if not (head := re.match(r"\[(\d+),(\d+)\]", raw)):
            continue
        words = "".join(m[3] for m in word.finditer(raw[head.end():]))
        lines.append((int(head[1]), words.strip()))
    return Lyrics(source, lines, True, True) if lines else None


YRC_WORD = re.compile(r"\((\d+),(\d+),\d+\)(.*?)(?=\(\d+,\d+,\d+\)|$)")
KRC_WORD = re.compile(r"<(\d+),(\d+),\d+>([^<]*)")


def netease(query: Query) -> Lyrics | None:
    """NetEase Cloud Music: word timing for much C/J-pop, line LRC otherwise. Unofficial API."""
    referer = {"Referer": "https://music.163.com/"}
    found = json.loads(get(f"https://music.163.com/api/cloudsearch/pc?s={q(query.title + ' ' + query.artist)}&type=1&limit=10", referer))
    songs = (found.get("result") or {}).get("songs") or []
    song = closest([s for s in songs if same_title(s.get("name") or "", query.title)], query.duration, lambda s: (s.get("dt") or 0) / 1000)
    if not song:
        return None
    lyric = json.loads(get(f"https://music.163.com/api/song/lyric?id={song['id']}&lv=-1&tv=-1&yv=-1", referer))
    if (yrc := (lyric.get("yrc") or {}).get("lyric")) and (timed := parse_timed(yrc, YRC_WORD, "NetEase")):
        return timed
    lrc = (lyric.get("lrc") or {}).get("lyric")
    if not lrc:
        return None
    result = parse(lrc, "NetEase")
    result.lines = [line for line in result.lines if " : " not in line[1] and "：" not in line[1]]  # credits like "作词 : X"
    return result if result.lines else None


KUGOU_KEY = bytes([64, 71, 97, 119, 94, 50, 116, 71, 81, 54, 49, 45, 206, 210, 110, 105])


def kugou_decrypt(content: str) -> str:
    raw = base64.b64decode(content)[4:]
    data = bytes(b ^ KUGOU_KEY[i % len(KUGOU_KEY)] for i, b in enumerate(raw))
    return zlib.decompress(data).decode("utf-8").removeprefix("﻿")


def kugou(query: Query) -> Lyrics | None:
    """KuGou: KRC word timing, encrypted with a well-known key. Unofficial API."""
    keyword = q(f"{query.artist} - {query.title}")
    found = json.loads(get(f"https://mobileservice.kugou.com/api/v3/search/song?version=9108&plat=0&pagesize=8&showtype=0&keyword={keyword}"))
    songs = (found.get("data") or {}).get("info") or []
    song = closest([s for s in songs if same_title(s.get("songname") or "", query.title)], query.duration, lambda s: s.get("duration") or 0)
    if not song:
        return None
    candidates = json.loads(get(f"https://krcs.kugou.com/search?ver=1&man=yes&client=mobi&keyword={keyword}"
                                f"&duration={query.duration * 1000}&hash={song['hash']}")).get("candidates") or []
    if not candidates:
        return None
    c = candidates[0]
    download = json.loads(get(f"https://krcs.kugou.com/download?ver=1&man=yes&client=pc&fmt=krc&charset=utf8&id={c['id']}&accesskey={c['accesskey']}"))
    return parse_timed(kugou_decrypt(download["content"]), KRC_WORD, "KuGou") if download.get("content") else None


def lyricsplus(query: Query) -> Lyrics | None:
    """LyricsPlus (YouLy+ backend): searches Apple Music, QQ Music, Musixmatch and others; word timing as syllables."""
    try:
        body = json.loads(get(f"https://lyricsplus.prjktla.my.id/v2/lyrics/get?title={q(query.title)}"
                              f"&artist={q(query.artist)}&duration={query.duration}"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    meta = body.get("metadata") or {}
    if meta.get("title") and not same_title(meta["title"], query.title):
        return None
    rows = body.get("lyrics") or []
    lines, word_level, end = [], False, 0
    for row in rows:
        syllables = [s for s in row.get("syllabus") or [] if s.get("text")]
        word_level |= bool(syllables)
        text = "".join(s["text"] for s in syllables).strip() if syllables else (row.get("text") or "").strip()
        start = int(float(row.get("time") or 0))
        end = max(end, start + int(float(row.get("duration") or 0)))
        lines.append((start, text))
    if not lines:
        return None
    # The server matched the song itself: its timing must fit this recording (LastWave's bounds, as in roma).
    song = query.duration * 1000
    if song >= 60_000 and end > 0 and not (min(song // 2, song - 90_000) <= end <= song + 45_000):
        return None
    source = f"LyricsPlus · {meta['source']}" if meta.get("source") else "LyricsPlus"
    return Lyrics(source, lines, any(start > 0 for start, _ in lines), word_level)


PROVIDERS = {"lrclib": lrclib, "netease": netease, "kugou": kugou, "lyricsplus": lyricsplus}


def find(query: Query, youtube=None) -> Lyrics | None:
    """Asks YouTube Music (youtube: a callable) and every provider at once, each for at most TIMEOUT seconds."""
    pool = ThreadPoolExecutor(len(PROVIDERS) + 1)
    jobs = {pool.submit(fn, query): name for name, fn in PROVIDERS.items()}
    ytm_job = pool.submit(youtube) if youtube else None
    wait([*jobs, *([ytm_job] if ytm_job else [])], timeout=TIMEOUT)
    pool.shutdown(wait=False, cancel_futures=True)

    def result(job, name):
        if not job.done():
            log.info("lyrics from %s timed out", name)
            return None
        try:
            return job.result()
        except Exception as e:
            log.info("lyrics from %s failed: %s", name, e)
            return None
    others = [cleaned(found, query) for job, name in jobs.items() if (found := result(job, name))]
    ytm = result(ytm_job, "YouTube Music") if ytm_job else None
    return choose(cleaned(ytm, query) if ytm else None, others)
