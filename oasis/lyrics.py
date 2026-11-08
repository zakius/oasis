"""Lyrics from the track's own files and from the providers roma uses, chosen by roma's rules (YouTube Music's text is
the reference). Lines are (start ms or None, text); words, when the source has karaoke timing, are each line's
[start ms, end ms, text] words, which join into the line's text."""
import base64
import html
import json
import logging
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import mutagen

from . import romanize
from .romanize import U
from .text import fold
from .tracks import SAME_SONG_SECONDS, outside_names
from .youtube import video_of

log = logging.getLogger(__name__)

LRC_TIME = re.compile(r"\[(\d+):(\d+(?:[.:]\d+)?)\]")
WORD_TIME = re.compile(r"<(\d+):(\d+(?:[.:]\d+)?)>")
# Above this, two texts are the same lyrics: the same lyrics from two providers score 0.77-1.0, another language
# under 0.1, a different song in the same language around 0.3 (measured for roma).
SAME_LYRICS = 0.6
TIMEOUT = 5
MISS_KEPT = 86400  # seconds a track no provider had lyrics for is left before asking again


@dataclass
class Lyrics:
    source: str
    lines: list[tuple[int | None, str]]
    synced: bool
    word_level: bool = False
    words: list[list[list]] | None = None
    lang: str | None = None  # a file's language tag for its lyrics (ISO 639: "jpn", "zho"); providers give none
    translations: dict[str, list[str]] | None = None  # language ("zh", "en") -> a translation of each line, "" for none

    def text(self) -> str:
        return "\n".join(text for _, text in self.lines)

    def subset(self, keep: list[int]) -> "Lyrics":
        return Lyrics(self.source, [self.lines[i] for i in keep], self.synced, self.word_level, [self.words[i] for i in keep] if self.words else None,
                      self.lang, self.translations and {lang: [t[i] for i in keep] for lang, t in self.translations.items()})


# Translations providers merge into the lyrics: a line with the same start as the line before, in another script

FAMILIES = ((U.HAN, "han"), (U.HANGUL, "hangul"), (U.CYRILLIC, "cyrillic"), (U.GREEK, "greek"), (U.ARABIC, "arabic"), (U.HEBREW, "hebrew"),
            (U.THAI, "thai"), (U.DEVANAGARI, "devanagari"))
LANGS = {"kana": "ja", "han": "zh", "hangul": "ko", "greek": "el", "arabic": "ar", "hebrew": "he", "thai": "th", "devanagari": "hi", "latin": "en"}
READING = 0.5  # Latin lines this like the generated reading of their lines are a reading, not a translation


def family(text: str) -> str | None:
    """A line's writing: kana (Japanese), han, hangul, cyrillic, ..., latin; None without letters."""
    if romanize.has_kana(text):
        return "kana"
    scripts = {romanize.script(c) for c in text if c.isalpha()}
    return next((name for code, name in FAMILIES if code in scripts), "latin" if scripts else None)


def language_of(texts: list[str]) -> str:
    """The language of a translation, from its writing: Chinese providers translate to Chinese, rarely to English."""
    found = Counter(f for t in texts if (f := family(t))).most_common(1)
    if not found:
        return "und"
    return romanize.cyrillic_language("\n".join(texts)) if found[0][0] == "cyrillic" else LANGS.get(found[0][0], "und")


def separate(found: Lyrics) -> Lyrics:
    """Takes out the translation lines a provider merged into the lyrics (NetEase, KuGou, QQ and LRCLIB uploads do),
    each becoming the translation of the song's line it follows:
    - a line with the same start as the line before, written in a script other than both that line's and the song's;
    - a script that interleaves with the song's: its lines come one at a time after a line in another script (80% of
      them), about as many as the song's own lines (60%), and the lyrics don't start with it.
    Latin lines that read as the line they follow are a reading (romaji, pinyin) and go, as oasis makes its own."""
    lines = found.lines
    if len(lines) < 2:
        return found
    families = [family(text) for _, text in lines]
    owner = {}  # a translation line -> the song's line it translates
    # Same start as the line before
    first = []  # each line's first line of its start time
    for i, (start, _) in enumerate(lines):
        first.append(first[-1] if found.synced and i and start is not None and start == lines[i - 1][0] else i)
    later = [i for i in range(len(lines)) if first[i] != i]
    song = Counter(families[i] for i in set(first) if families[i]).most_common(1)
    song = song[0][0] if song else None
    other = {families[i] for i in later if families[i] and families[i] not in (song, families[first[i]])}
    owner |= {i: first[i] for i in later if families[i] in other}
    # Interleaved: each line after a song line
    worded = [i for i, f in enumerate(families) if f]
    before = {i: worded[n - 1] for n, i in enumerate(worded) if n}
    for f in {families[i] for i in worded} - {families[worded[0]] if worded else None}:
        rows = [i for i in worded if families[i] == f]
        after = [i for i in rows if i in before and families[before[i]] != f and i not in owner]
        if len(rows) >= 2 and len(after) >= 0.8 * len(rows) and len(rows) >= 0.6 * (len(worded) - len(rows)):
            owner |= {i: before[i] for i in after}
            other.add(f)
    if not owner:
        return found
    gone = set(owner) | {i for i in later if families[i] is None}  # empty lines under a translated one go with them
    keep = [i for i in range(len(lines)) if i not in gone]
    result = found.subset(keep)
    translations = dict(result.translations or {})
    for f in other:
        rows = [i for i in owner if families[i] == f]
        if f == "latin" and sum(similarity(romanize.text(lines[owner[i]][1]), lines[i][1]) for i in rows) / len(rows) >= READING:
            continue  # a reading
        by_line = {}
        for i in sorted(rows):
            by_line[owner[i]] = " ".join(x for x in (by_line.get(owner[i]), lines[i][1].strip()) if x)
        translations.setdefault(language_of(list(by_line.values())), [by_line.get(i, "") for i in keep])
    result.translations = translations or None
    return result


def attach(found: Lyrics, starts_texts: list[tuple[int, str]], near: int = 0) -> Lyrics:
    """found with a provider's separate translation (start, text) rows as a translation, each on the line starting
    within near ms of it."""
    rows = [(start, text.strip()) for start, text in starts_texts if text.strip() and text.strip() != "//"]
    if not rows or not found.synced:
        return found
    values = []
    for start, _ in found.lines:
        best = min(rows, key=lambda r: abs(r[0] - (start or 0)))
        values.append(best[1] if abs(best[0] - (start or 0)) <= near else "")
    if any(values):
        found.translations = {**(found.translations or {}), language_of([v for v in values if v]): values}
    return found


def timed(source: str, rows: list, word_level: bool) -> Lyrics:
    """Synced lyrics from (start, text, words) rows, in time order. A word without an end runs to the next line (roma's
    closeWords)."""
    rows = sorted(rows, key=lambda row: row[0])
    for i, (start, _, words) in enumerate(rows):
        for w in words:
            if w[1] is None:
                w[1] = rows[i + 1][0] if i + 1 < len(rows) else words[-1][0] + 1000
    words = [words for _, _, words in rows]
    return Lyrics(source, [(start, text) for start, text, _ in rows], True, word_level, words if any(words) else None)


@dataclass
class Query:
    title: str
    artist: str
    duration: int


def ms(minutes: str, seconds: str) -> int:
    return int(minutes) * 60_000 + round(float(seconds.replace(":", ".")) * 1000)


def parse(text: str, source: str = "file") -> Lyrics:
    """LRC (also with <mm:ss.xx> word stamps) or plain text. A line with several timestamps repeats. An [offset:ms]
    tag is applied to the times (positive shows lines sooner), so oasis always sends offset 0."""
    lines, rows, word_level = [], [], False
    offset = int(m.group(1)) if (m := re.search(r"^\[offset:\s*([+-]?\d+)\s*\]", text, re.M | re.I)) else 0
    for raw in text.splitlines():
        rest, stamps = raw.strip(), []
        while m := LRC_TIME.match(rest):
            stamps.append(max(0, ms(*m.groups()) - offset))
            rest = rest[m.end():]
        words, marks = [], list(WORD_TIME.finditer(rest))
        if marks:
            word_level = True
            if stamps and rest[:marks[0].start()].strip():  # text before the first word stamp starts with the line
                words.append([stamps[0], None, rest[:marks[0].start()]])
            for i, m in enumerate(marks):
                start = max(0, ms(*m.groups()) - offset)
                if words:
                    words[-1][1] = start
                if w := rest[m.end():marks[i + 1].start() if i + 1 < len(marks) else len(rest)]:
                    words.append([start, None, w])
            rest = WORD_TIME.sub("", rest)
        if stamps:
            rows += [(start, rest.strip(), words if len(stamps) == 1 and stamps else []) for start in stamps]
        elif not re.fullmatch(r"\[\w+:.*\]", raw.strip()):  # LRC headers like [ar:...]
            lines.append((None, raw.strip()))
    if rows:
        return timed(source, rows, word_level)
    while lines and not lines[-1][1]:
        lines.pop()
    return Lyrics(source, lines, False)


def local(path: str) -> Lyrics | None:
    """Lyrics from a sidecar .lrc file, synced ID3 SYLT, or embedded lyrics tags. Their language is the file's language
    tag (ID3 TLAN, Vorbis LANGUAGE: Picard writes the release's), else the ID3 lyrics frame's, often a default "eng"."""
    sidecar = Path(path).with_suffix(".lrc")
    tags = getattr(mutagen.File(path), "tags", None) or {}
    language = tags.get("TLAN") or tags.get("language")
    lang = str(language[0]) if language else None
    if sidecar.exists():
        return replace(parse(sidecar.read_text(errors="replace")), lang=lang)
    for key in tags.keys():
        if key.startswith("SYLT") and tags[key].format == 2:  # timestamps in milliseconds
            return Lyrics("file", [(start, text) for text, start in tags[key].text], True, lang=lang or tags[key].lang)
    for key in tags.keys():
        if key.startswith("USLT"):
            return replace(parse(tags[key].text), lang=lang or tags[key].lang)
        if key.lower() in ("lyrics", "unsyncedlyrics", "©lyr"):
            return replace(parse(str(tags[key][0])), lang=lang)
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
    found = separate(found)
    found = found.subset([i for i, (_, text) in enumerate(found.lines)
                          if not CREDIT.search(text) and not (i < 3 and " - " in text and same_title(text.split(" - ", 1)[1], query.title))])
    found.lines = [(start, unescape(text)) for start, text in found.lines]
    found.translations = found.translations and {lang: [unescape(t) for t in values] for lang, values in found.translations.items()}
    found.words = found.words and [[[a, b, unescape(t)] for a, b, t in words] for words in found.words]
    return found


def fold_title(s: str) -> str:
    return fold(re.sub(r"[(\[（【].*?[)\]）】]", "", unicodedata.normalize("NFKD", s)))


def same_title(a: str, b: str) -> bool:
    """Same song title, ignoring case, spaces, punctuation and bracketed extras such as "(TV Size)"."""
    x, y = fold_title(a), fold_title(b)
    return bool(x and y and (x in y or y in x))


def closest(items: list, duration: int, duration_of):
    """The candidate whose length is closest, within SAME_SONG_SECONDS when the song's length is known."""
    if duration <= 0:
        return items[0] if items else None
    near = [i for i in items if abs(duration_of(i) - duration) <= SAME_SONG_SECONDS]
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


def parse_timed(text: str, word: re.Pattern, source: str, relative=False) -> Lyrics | None:
    """NetEase YRC "[start,dur](wordStart,dur,0)word" and KuGou KRC "[start,dur]<offset,dur,0>word" (relative: word
    starts count from the line's)."""
    rows = []
    for raw in text.splitlines():
        if not (head := re.match(r"\[(\d+),(\d+)\]", raw)):
            continue
        base = int(head[1]) if relative else 0
        words = [[base + int(m[1]), base + int(m[1]) + int(m[2]), m[3]] for m in word.finditer(raw[head.end():]) if m[3]]
        rows.append((int(head[1]), "".join(w[2] for w in words).strip(), words))
    if not rows:
        return None
    found = timed(source, rows, True)
    translation = krc_translation(text)
    if translation and len(translation) == len(rows) and [r[0] for r in rows] == sorted(r[0] for r in rows) and any(translation):
        found.translations = {language_of([t for t in translation if t]): translation}
    return found


def krc_translation(text: str) -> list[str] | None:
    """A KRC file's translation, one per line: its [language:] block is base64 JSON whose type 1 content is the
    translation (type 0 is a reading, which oasis makes itself)."""
    if not (m := re.search(r"^\[language:([^\]]*)\]", text, re.M)) or not m[1].strip():
        return None
    try:
        content = json.loads(base64.b64decode(m[1]))["content"]
    except (ValueError, KeyError, TypeError) as e:
        log.info("KRC language block unreadable: %s", e)
        return None
    found = next((c.get("lyricContent") for c in content if c.get("type") == 1), None)
    return ["".join(x for x in line if x).strip() for line in found] if found else None


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
    translation = (lyric.get("tlyric") or {}).get("lyric")  # LRC with the lrc's start times
    translation = parse(translation).lines if translation else []
    if (yrc := (lyric.get("yrc") or {}).get("lyric")) and (timed := parse_timed(yrc, YRC_WORD, "NetEase")):
        return attach(timed, translation, near=1000)  # word timing starts lines a little off the lrc's
    lrc = (lyric.get("lrc") or {}).get("lyric")
    if not lrc:
        return None
    result = parse(lrc, "NetEase")
    result = result.subset([i for i, (_, text) in enumerate(result.lines) if " : " not in text and "：" not in text])  # credits like "作词 : X"
    return attach(result, translation) if result.lines else None


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
    return parse_timed(kugou_decrypt(download["content"]), KRC_WORD, "KuGou", relative=True) if download.get("content") else None


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
    lines, words, end = [], [], 0
    for row in rows:
        syllables = [s for s in row.get("syllabus") or [] if s.get("text")]
        words.append([[int(float(s.get("time") or 0)), int(float(s.get("time") or 0)) + int(float(s.get("duration") or 0)), s["text"]] for s in syllables])
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
    synced = any(start > 0 for start, _ in lines)
    return Lyrics(source, lines, synced, any(words), words if synced and any(words) else None)


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


def youtube_music(db, ytm, r):
    """A YouTube Music provider for find: the track's own lyrics there, by its video or the same song's. None when
    YouTube Music has no such song."""
    try:
        video_id = video_of(db, r, ytm)
    except Exception as e:
        log.info("YouTube Music match for %s failed: %s", r["id"], e)
        return None

    def youtube():
        found = ytm.lyrics(video_id)
        if found is None:
            return None
        return Lyrics(found["source"], found["lines"], found["synced"])
    return youtube if video_id else None


def for_track(db, r, ytm=None) -> Lyrics | None:
    """The song's lines, with the translations a file or provider merged into them taken out (separate): files, and
    lyrics cached before oasis did that, still have them in."""
    found = found_for_track(db, r, ytm)
    return separate(found) if found else None


def found_for_track(db, r, ytm=None) -> Lyrics | None:
    """The track's own lyrics first; otherwise the providers' pick, cached in the lyrics table (misses for MISS_KEPT)."""
    if r["source"] == "local" and (found := local(r["ref"])):
        return found
    cached = db.execute("select found, fetched from lyrics where track_id = ?", (r["id"],)).fetchone()
    if cached and (cached["found"] or cached["fetched"] > time.time() - MISS_KEPT):
        return Lyrics(**json.loads(cached["found"])) if cached["found"] else None
    artist, _ = outside_names(r)
    found = find(Query(r["title"], artist, r["duration"] or 0), youtube_music(db, ytm, r) if ytm else None)
    db.execute("insert or replace into lyrics values (?, ?, ?)",
               (r["id"], json.dumps(asdict(found)) if found else None, time.time()))
    return found
