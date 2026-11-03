"""oasis's own reading of release names for its Prowlarr search: which torrents are the album, and which to try first.
Names come in any script and shape ('[마신] [2022.09.07] Creepy Nuts 3rdアルバム「アンサンブル・プレイ」[FLAC 48kHz/24bit]'),
so the name and the album are both folded to words and compared word by word; each kana, kanji or Hangul syllable
is a word of its own, as those scripts don't space words. Uploads may name the album by another of its titles in
MusicBrainz ('Ensemble Play'), which count as its own. A wrong album is worse than none: variants (live,
instrumentals, deluxe...), bundles and videos are left out, and so are short or self-titled names that don't pin the
album down. Disc images are tried after a file per track. The importer's edition match comes after, as a second check.
Discographies, singles collections and other bundles are read by their file lists: each folder that is one wanted
album, EP or single becomes a target, by its name and its songs, and only its files are fetched; a single may be loose
tracks too (see loose). A folder of disc images is taken by its name and size alone. File lists are TorBox's of bundles
it has cached, the .torrent's, or TorBox's once it has the torrent (download.py); without one, a bundle's name tells
what it likely holds (expected)."""
import logging
import re
import unicodedata
from pathlib import PurePosixPath
from urllib.parse import urlparse

from . import musicbrainz
from .importer import ARCHIVES, AUDIO, LOSSLESS as LOSSLESS_FILES, key, match, number_of, title_of

log = logging.getLogger(__name__)

CJK = re.compile("[぀-ヺー-ヿ㐀-䶿一-鿿가-힯豈-﫿]")  # kana, kanji, Hangul; not ・
LETTERS = str.maketrans({"æ": "ae", "œ": "oe", "ø": "o", "ł": "l", "đ": "d", "ð": "d", "þ": "th", "ı": "i"})  # no accent to strip
BRACKETS = re.compile(r"[\[\]「」『』\"“”„«»()〈〉《》【】/／]")  # and slashes, which break some indexers' search URLs
BRACKETED = re.compile(r"\s*[\[(（【][^\])）】]*[\])）】]")
QUOTED = re.compile(r"[「『\"“]([^」』\"”]+)[」』\"”]")
# Words that make a release something else than the album, unless its title says so. Remasters are the album.
VARIANTS = re.compile(r"\b(best of|greatest hits|very best|the essential|anthology|singles|anniversary|deluxe|expanded|tour edition|bonus tracks?|"
                      r"re ?recorded|reissue|remix(es|ed)?|mix ?tapes?|instrumentals?|inst|off vocal|no vocals?|karaoke|acapellas?|a ?cappellas?|live|unplugged|"
                      r"acoustic|demos?|tribute|orchestral|symphonic|piano|lullaby|8 ?bit|sped up|slowed|nightcore|tv size)\b")
CREDITED_AS_SOURCE = re.compile(r"\b(based on (the )?music of|music of|tribute to|covers? of|in the style of|songs of|plays|presents?)( the)?$")  # "Funk Daddy Present Jay-Z": a mixtape of theirs
VARIANTS_JA = re.compile(r"インスト|カラオケ|ライ[ブヴ]|リミックス|オフボーカル")
BUNDLES = re.compile(r"discograph|collection|complete|box ?set|anthology|\bsingles\b|全集|ディスコグラフィ", re.I)
DISCOGRAPHY = re.compile(r"discograph|дискограф|complete|box ?set|全集|ディスコグラフィ", re.I)  # more than singles
VIDEO = re.compile(r"\b(dvd\d*|dvdrip|bdmv|blu ?ray|bd ?rip|bd|hdtv|web ?rip|\d{3,4}[pi]|x26[45]|h ?26[45]|hevc|xvid|divx|mkv|avi|mpe?g|mp4|vob|videos?)\b")
TYPES = {"single": "Single", "maxi": "Single", "ep": "EP"}  # words naming a release's type
IMAGE = re.compile(r"\b(image|img) cue\b")  # one file for the whole disc, which the importer splits: a file per track is surer
LOSSLESS = re.compile(r"\b(flac|alac|wav|lossless|24 ?bits?|hi ?res|ape)\b")
MP3_320 = re.compile(r"\b320( ?k| ?kbps| ?kbit)?\b")
V0 = re.compile(r"\bv0\b")
SIZES = 20e6, 5e9      # bytes a release may have, the most per disc
RATE = 12e3, 1.2e6     # bytes per second of music a disc image may hold: a 96 kbps MP3 to a 24-bit 192 kHz WAV
ALIASES = "https://musicbrainz.org/ws/2/artist/{}?inc=aliases&fmt=json"
KEEP_ALIASES = 30 * 86400
NATIVE_QUERIES = 2     # searches by the artist's names in other scripts
OTHER_TITLE_QUERIES = 2  # searches by the album's other titles (romanised, translated)
DISC = re.compile(r"^(?:cd|disc|disk)\s*(\d+)\b", re.I)  # a disc's folder inside its album's
LEAD = re.compile(r"(\s*(\[[^]]*]|\([^)]*\)|\{[^}]*}|(19|20)\d\d)[\s\-–—_.:|/]*)*")  # years and bracketed tags opening a name
YEAR = re.compile(r"^(19|20)\d\d$")
# Where a release's name splits into parts: separators, brackets and quotes, and runs of spaces where an uploader dropped
# them ('Linkin Park   Jay Z  Collision Course'); and words joining artists in a credit.
SEPARATOR = re.compile(r"[\s_]-+[\s_]*|-+[\s_]|[\s_]{2,}|[–—|:：,/／+\[\](){}（）【】「」『』\"“”]")
CONNECTORS = {"and", "feat", "ft", "featuring", "with", "vs"}
IMAGES = ("jpg", "jpeg", "png", "webp", "gif")
NUMBERS = {w: str(n) for n, w in enumerate("one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
                                            "seventeen eighteen nineteen twenty".split(), 1)}
ROMAN = re.compile(r"x{0,3}(ix|iv|v?i{0,3})")
RELEASES = re.compile(r"\b(\d+) (releases|релиз)")  # '(126 Releases)': a bundle's own count
BUNDLE_WORDS = {"singles", "anthology", "demo", "demos", "live", "bonustrack", "bonustracks", "deluxe", "expanded"}  # variants a discography holds besides the albums
BUNDLE_SEEDERS = 3       # seeders an uncached bundle needs: it downloads whole, gigabytes, from them
TORRENTS = 20            # .torrent files read through Prowlarr per bundle search for hashes, as many for file lists, best seeded first: each asks an indexer


def plain(text: str) -> str:
    """Casefolded, full-width forms and accents gone; kana keep their marks (バ isn't ハ)."""
    text = unicodedata.normalize("NFKC", text).casefold().translate(LETTERS)
    return "".join(c if CJK.match(c) else "".join(d for d in unicodedata.normalize("NFKD", c) if not unicodedata.combining(d)) for c in text)


def words(text: str) -> list[str]:
    """'Hardwired…To Self-Destruct' -> hardwired to self destruct; 'アンサンブル・プレイ' -> ア ン サ ン ブ ル プ レ イ."""
    text = re.sub(r"['’`´]", "", plain(text).replace("&", " and "))
    return re.sub(r"[\W_]+", " ", CJK.sub(r" \g<0> ", text)).split()


def spans(tokens: list[str], phrase: list[str]) -> list[tuple[int, int]]:
    """Where the phrase is in the name, as runs of whole words, spacing aside: 'self destruct' is 'selfdestruct'."""
    want, found = "".join(phrase), []
    for i in range(len(tokens) if want else 0):
        joined = ""
        for j in range(i, len(tokens)):
            joined += tokens[j]
            if not want.startswith(joined):
                break
            if joined == want:
                found.append((i, j + 1))
                break
    return found


def leads(title: str, want: dict) -> list[tuple[int, int]]:
    """Where the artist's names are in a release's name when one opens it: only years and bracketed tags ('[2010]',
    '(Pol)') may come first, never another word ('Tab Hunter', 'Demon Hunter' aren't Hunter). The spans count in the
    name's words, as spans()."""
    tokens, lead = words(title), words(LEAD.match(title)[0])
    found = [s for n in want["names"] for s in spans(tokens, words(n))]
    return found if any(a == len(lead) for a, _ in found) else []


def alone(title: str, want: dict) -> bool:
    """Whether the artist's name, opening a release's name (leads), stands alone: a separator, bracket, run of spaces,
    joining word, year or bundle word follows, not another word ('Hunter - Discography', 'Hunter (Poland) Discography',
    'Linkin Park   Papercuts', but not 'Hunter Tom - Discography', 'Hunter Hayes Discography', 'Hunter x Hunter OST')."""
    rest = re.sub(r"['’`´]", "", plain(title[len(LEAD.match(title)[0]):]).replace("&", " and "))  # as words() reads it
    return any(re.match(r"[\W_]*".join(map(re.escape, words(n))) + r"(?![^\W_])(\s{2,}|[\s.\]]*($|[-–—(\[{:：|/／,+]|(and|feat|ft|with|vs|(19|20)\d\d)\b|"
                        r"discograph|дискограф|complete|collection|box|anthology|singles|全集|ディスコグラフィ))", rest)
               for n in want["names"]) and bool(leads(title, want))


def hits(pattern, text: str) -> set[str]:
    return {m[0].replace(" ", "") for m in pattern.finditer(text)}


def unique(items: list, key) -> list:
    seen = set()
    return [i for i in items if (k := key(i)) not in seen and not seen.add(k)]


def hash_of(release: dict) -> str:
    return (release.get("infoHash") or "").lower()


def quality(name: str) -> int:
    """From the name: 3 lossless, 2 MP3 320, 1 V0, 0 anything else."""
    text = " ".join(words(name))
    return 3 if LOSSLESS.search(text) else 2 if MP3_320.search(text) else 1 if V0.search(text) else 0


def native(names: list[str]) -> list[str]:
    """The names written wholly in another script than Latin, to search by too."""
    return [n for n in names if all(ord(c) > 0x24f for c in n if c.isalpha())][:NATIVE_QUERIES]


def aliases(db, name: str, mbid: str | None, titles: list[str]) -> list[str]:
    """The artist's name and its MusicBrainz name and aliases; titles: its albums and EPs."""
    names, titles = [name], {"".join(words(t)) for t in titles}
    try:
        if not mbid:
            raise LookupError("no MusicBrainz id")
        found = musicbrainz.get(ALIASES.format(mbid), db, KEEP_ALIASES)
        # Search hints ('LP' for Linkin Park), very short aliases and former names that are album titles ('Hybrid
        # Theory') would match names that aren't the artist's.
        names += [found["name"], *(a["name"] for a in found.get("aliases") or [] if a.get("type") != "Search hint"
                                   and len(n := "".join(words(a["name"]))) >= (2 if CJK.search(a["name"]) else 3) and n not in titles)]
    except (OSError, LookupError, KeyError) as e:
        log.info("no MusicBrainz aliases for %s: %s", name, e)
    return unique([n for n in names if words(n)], lambda n: "".join(words(n)))


def other_titles(title: str, names: list[str], others: list[str]) -> list[str]:
    """Of an album's other whole titles in MusicBrainz (names), those a release may be named by instead: not
    another album's title (others) and no other variant ('BIRIKEN (Instrumental)' isn't ビリケン)."""
    variant = lambda t: (hits(VARIANTS, " ".join(words(t))), hits(VARIANTS_JA, plain(t)))
    taken = {"".join(words(t)) for t in [title, *others]}
    return unique([n for n in names if (j := "".join(words(n))) and j not in taken and variant(n) == variant(title)], lambda n: "".join(words(n)))


def reject(release: dict, want: dict) -> str | None:
    """Why the release isn't the album, or None when it is, by its title or one of its other titles (other_titles)."""
    whys = [reject_title(release, {**want, "title": t}) for t in [want["title"], *want["other_titles"]]]
    return None if None in whys else next((w for w in whys if w != "no title"), whys[0])


def reject_title(release: dict, want: dict) -> str | None:
    name, title = release["title"], want["title"]
    tokens = words(name)
    text, title_text = " ".join(tokens), " ".join(words(title))
    if not SIZES[0] < (release.get("size") or 0) < SIZES[1] * want["discs"]:
        return "size"
    artists = [s for n in want["names"] for s in spans(tokens, words(n))]
    if not artists:
        return "no artist"
    variants = [words(title), *([short] if (short := words(BRACKETED.sub(" ", title))) and short != words(title) else [])]
    # The artist and the title must be in different places, even when they are the same word (self-titled albums).
    apart = lambda found: [t for t in found if any(t[1] <= a or b <= t[0] for a, b in artists)]
    titles = apart([t for v in variants for t in spans(tokens, v)])
    if not titles:
        return "no title"
    # 'Torsten Rasch - Mein Herz Brennt (Based on Music of Rammstein)': the artist is only the source of another's work.
    if all(m := CREDITED_AS_SOURCE.search(" ".join(tokens[:a])) for a, _ in artists):
        return f"another artist's: {m[1]}"
    allowed = hits(VARIANTS, " ".join([title_text, *(" ".join(words(n)) for n in want["names"])]))
    if extra := hits(VARIANTS, text) - allowed or hits(VARIANTS_JA, plain(name)) - hits(VARIANTS_JA, plain(title)):
        return f"variant: {sorted(extra)[0]}"
    if missing := hits(VARIANTS, title_text) - hits(VARIANTS, text):
        return f"not the variant: {sorted(missing)[0]}"
    if VIDEO.search(text) and not VIDEO.search(title_text):
        return "video"
    if BUNDLES.search(name) and not BUNDLES.search(title):
        return "bundle"
    if {"various", "va"} & set(tokens) - set(words(" ".join([title, *want["names"]]))):
        return "various artists"
    compact, names = "".join(words(title)), {"".join(words(n)) for n in want["names"]}
    for other in want["others"]:  # 'Korn III: Remember Who You Are' holds 'Korn', and is another album
        if len(o := words(other)) > len(words(title)) and spans(o, words(title)) and apart(spans(tokens, o)):
            return f"another album: {other}"
    if not placed(name, tokens, artists, titles, variants):
        return "the artist's name isn't on its own"
    if why := namesake(tokens, title_text, want):
        return why
    for other in want["others"]:
        o = words(other)
        if len(joined := "".join(o)) >= 4 and not joined.isdigit() and joined not in names and not spans(words(title), o) \
                and not spans(o, words(title)) and apart(spans(tokens, o)):
            return f"bundle: also {other}"
    if len(compact) < 4 or compact in names:
        # Right after the artist, perhaps past the year: 'Rammstein - (2019) Rammstein', not 'This Is Rammstein (2019)'.
        after = any(start == end or start == end + 1 and tokens[end] == want["year"] for _, end in artists for start, _ in titles)
        quoted = any("".join(words(m[1])) == compact for m in QUOTED.finditer(unicodedata.normalize("NFKC", name)))
        if compact in names and not (after or quoted):
            return "self-titled: the title isn't right after the artist"
        if not (want["year"] and want["year"] in tokens or after or quoted):
            return "title too short: no year"
    return None


def placed(name: str, tokens: list[str], artists: list[tuple[int, int]], titles: list[tuple[int, int]], variants: list[list[str]]) -> bool:
    """Whether the artist's name stands on its own, as in a release of theirs, not as part of another's ('Tab Hunter',
    'Demon Hunter', 'Hunter Hayes', 'Hunter x Hunter' aren't Hunter): opening the name (leads) or after a separator,
    bracket or joining word ('Kanye West & Jay-Z'), and followed by one, or by the title, perhaps past the year
    ('Linkin_Park-Meteora-2003'). Opening the name, also when the title is quoted ('Creepy Nuts 3rdアルバム「アンサンブル・プレイ」').
    Spans as spans()."""
    texts = SEPARATOR.split(name)
    part = [i for i, text in enumerate(texts) for _ in words(text)]  # the part each word is in
    if len(part) != len(tokens):  # can't tell
        return True
    lead, quoted = len(words(LEAD.match(name)[0])), any(words(m[1]) in variants for m in QUOTED.finditer(unicodedata.normalize("NFKC", name)))
    apart = lambda i: i in (0, len(tokens)) or part[i] != part[i - 1]
    return any((a == lead or apart(a) or tokens[a - 1] in CONNECTORS)
               and (apart(b) or tokens[b] in CONNECTORS or s == b or s == b + 1 and YEAR.match(tokens[b]) or quoted and a == lead)
               for a, b in artists for s, _ in titles)


def namesake(tokens: list[str], title_text: str, want: dict) -> str | None:
    """Why a release is another of the artist's releases with the same title (the EP Hybrid Theory, the album Hybrid
    Theory), or None. A type word in the name tells them apart, then the year; else it is the album."""
    if not (namesakes := want.get("namesakes")):
        return None
    if named := [TYPES[t] for t in tokens if t in TYPES and t not in title_text.split()]:
        return None if named[0] == want["type"] or all(n["type"] != named[0] for n in namesakes) else f"namesake: the {named[0]}"
    if want["year"] in tokens and all(n["year"] != want["year"] and n["year"] not in tokens for n in namesakes):
        return None
    if want["year"] not in tokens and (dated := next((n for n in namesakes if n["year"] in tokens), None)):
        return f"namesake: the {dated['type']} of {dated['year']}"
    album = next((n for n in namesakes if n["type"] == "Album"), None)
    return None if want["type"] == "Album" or not album else f"namesake: the Album of {album['year']}"


def site(guid: str) -> str:
    """The indexer a release came from, by its guid: its host, or 'other' for guids that are magnets."""
    return urlparse(guid).hostname or "other" if guid.startswith("http") else "other"


def rank(releases: list[dict], have: set[str], upgrade: bool = False, unreliable: frozenset = frozenset(), slow: frozenset = frozenset(),
         low: frozenset = frozenset()) -> list[dict]:
    """Best first: what oasis can read before it goes (cached on TorBox, or a .torrent through Prowlarr) before every
    magnet link, which goes only when nothing readable is left; then what will download (cached, or seeded per an
    indexer whose seed counts held up), then lossless, then a file per track over a disc image, then cached, then
    quality, then seeders.
    A release TorBox hasn't cached needs a seeder. The same torrent from several indexers is tried once. Slow ones,
    which made way for others before, come last; ahead of them, those of indexers whose ratio for us is low (low: ids)."""
    usable = [r for r in releases if hash_of(r) in have or (r.get("seeders") or 0) >= 1]
    if upgrade:
        usable = [r for r in usable if quality(r["title"]) == 3]
    usable.sort(key=lambda r: (hash_of(r) in have or bool(r.get("downloadUrl")), hash_of(r) not in slow, r.get("indexerId") not in low, hash_of(r) in have or site(r.get("guid") or "") not in unreliable, quality(r["title"]) == 3, not IMAGE.search(" ".join(words(r["title"]))), hash_of(r) in have, quality(r["title"]),
                               r.get("seeders") or 0), reverse=True)
    return unique(usable, lambda r: hash_of(r) or r["guid"])


def clean(text: str) -> str:
    """A query's words: 'The Blueprint²' -> 'The Blueprint 2', as release names write it."""
    return " ".join(BRACKETS.sub(" ", unicodedata.normalize("NFKC", re.sub(r"(?<=[^\W\d])([¹²³⁰⁴-⁹])(?!\w)", r" \1", text))).split())


def spellings(artist: str) -> list[str]:
    """The artist's name for queries: without accents first when that leaves it plain ASCII, as release names mostly
    spell it ('JAY-Z' for 'JAŸ-Z', which indexers don't fold), then as it is."""
    artist = clean(artist)
    folded = "".join(c for c in unicodedata.normalize("NFKD", artist) if not unicodedata.combining(c))
    return list(dict.fromkeys([folded, artist] if folded.isascii() else [artist]))


def queries(want: dict) -> list[str]:
    """Artist and album; the album alone when distinctive; the album with the artist's names in other scripts; the
    artist and the album's other titles. Brackets and quotes go: indexers find nothing for '[Rammstein]'."""
    title, artists = clean(want["title"]), spellings(want["artist"])
    return list(dict.fromkeys([*(f"{a} {title}" for a in artists), *([title] if len(title.split()) >= 2 or len(title) >= 8 else []),
                               *(f"{clean(n)} {title}" for n in want["native"]),
                               *(f"{artists[0]} {clean(t)}" for t in want["other_titles"][:OTHER_TITLE_QUERIES])]))


def find(prowlarr, want: dict, blocked: set, cached, unreliable: frozenset = frozenset(), slow: frozenset = frozenset(),
         low: frozenset = frozenset()) -> list[dict]:
    """The releases to try, best first, from the first query that finds a usable one; unreliable, slow, low: see rank."""
    for query in queries(want):
        found = prowlarr.search(query)
        fits = [r for r in found if not {hash_of(r), r.get("guid")} & blocked and not reject(r, want)]
        hashes = list(dict.fromkeys(h for r in fits if (h := hash_of(r))))[:100]
        picks = rank(fits, cached(hashes) if hashes else set(), want["upgrade"], unreliable, slow, low)
        log.info("Prowlarr %r: %d results, %d the album, %d usable", query, len(found), len(fits), len(picks))
        if picks:
            return picks
    return []


def is_bundle(release: dict, want: dict) -> bool:
    """A discography or bundle of the artist, whose name opens it (leads): its name says so, the artist's name alone
    ('Hunter Hayes - Discography' is another's), or holds two of the artist's album titles.
    want: artist, names, native, albums (the artist's album and EP titles)."""
    tokens, artists = words(release["title"]), leads(release["title"], want)
    if not artists or VIDEO.search(" ".join(tokens)):
        return False
    names = {"".join(words(n)) for n in want["names"]}
    held = {"".join(words(t)) for t in want["albums"] if len(j := "".join(words(t))) >= 4 and j not in names
            and any(all(e <= a or b <= s for a, b in artists) for s, e in spans(tokens, words(t)))}
    return bool(BUNDLES.search(release["title"])) and alone(release["title"], want) or len(held) >= 2


def bundle_queries(want: dict) -> list[str]:
    artist = spellings(want["artist"])[0]
    return list(dict.fromkeys([f"{artist} discography", artist, f"{artist} singles", f"{artist} complete singles", *map(clean, want["native"])]))


def roman(token: str) -> int:
    values = [{"i": 1, "v": 5, "x": 10}[c] for c in token]
    return sum(-v if v < after else v for v, after in zip(values, [*values[1:], 0]))


def numbered(tokens: list[str]) -> list[str]:
    """Numbers as digits, however written: 'Underground XIV', 'Underground Eleven', 'Underground V2.0' -> underground
    14, underground 11, underground 2. A '.0' goes ('6.0' is 6); other decimals stay ('2.5' is 2 5)."""
    found = []
    for t in tokens:
        t = NUMBERS.get(t) or (str(roman(t)) if t and ROMAN.fullmatch(t) else re.sub(r"^v(?=\d)", "", t))
        if not (t == "0" and found and found[-1].isdigit()):
            found.append(t)
    return found


def forms(title: str, want: dict) -> list[str]:
    """The ways a bundle's folder may name a title: as it is, without its subtitle ('LP Underground 9: Demos' ->
    LP Underground 9), and either without the artist's initials opening it ('Underground 9: Demos', 'Underground 9')."""
    initials = {"".join(w[0] for w in words(n)) for n in want["names"] if len(words(n)) > 1}
    found = [title, *([title.split(":")[0]] if ":" in title else [])]
    return found + [" ".join(w[1:]) for t in found if len(w := words(t)) > 1 and w[0] in initials]


def folder_groups(name: str, want: dict, groups: list[dict]) -> list[str]:
    """The release groups an album folder inside a bundle may be by its name: its name holds the title (one of its
    forms, or one of the group's other_titles) apart from the artist's name, numbers however written, and the year when
    it has one besides the title's own; variants and videos aren't the album. Of titles inside one another only the
    longest counts ('One More Light', not 'One'). Of groups of one title a single loses to the album or EP unless the
    name says single (a title track's single doesn't take its album's folder), and a name that says EP or single keeps
    that type. The folder's files tell the rest (targets)."""
    tokens, plain_tokens = numbered(words(name)), words(name)
    text = " ".join(plain_tokens)
    artists = [s for n in want["names"] for s in spans(tokens, numbered(words(n)))]
    names = {"".join(words(n)) for n in want["names"]}
    fits = {}
    for g, named in ((g, f) for g in groups for t in [g["title"], *g.get("other_titles", ())] for f in forms(t, want)):
        title, year = numbered(words(named)), (g.get("first-release-date") or "")[:4]
        compact, title_text = "".join(words(named)), " ".join(words(named))
        found = [(s, e) for s, e in spans(tokens, title) if all(e <= a or b <= s for a, b in artists)]
        # A year in the name, not the title's own ('2011 - North American Tour EP' for '2011 North American Tour EP' of 2010), must be the group's.
        dated = [{t for i, t in enumerate(tokens) if YEAR.match(t) and not s <= i < e} for s, e in found]
        if not compact or not any(year in years or not years and len(compact) >= 4 and compact not in names for years in dated):
            continue
        allowed = hits(VARIANTS, " ".join([title_text, *(" ".join(words(n)) for n in want["names"])]))
        if hits(VARIANTS, text) - allowed or hits(VARIANTS_JA, plain(name)) - hits(VARIANTS_JA, plain(named)) or \
                hits(VARIANTS, title_text) - hits(VARIANTS, text) or VIDEO.search(text) and not VIDEO.search(title_text):
            continue
        fits[g["id"]] = max(fits.get(g["id"], 0), len("".join(title)))
    best = [rg for rg, n in fits.items() if n == max(fits.values())]
    if len(best) > 1:
        kinds, named = {g["id"]: g.get("primary-type") for g in groups}, next((TYPES[t] for t in plain_tokens if t in TYPES), None)
        best = [rg for rg in best if kinds[rg] == named] if named else [rg for rg in best if kinds[rg] != "Single"]
    return best


def folder_album(name: str, want: dict, groups: list[dict]) -> str | None:
    """The release group an album folder inside a bundle is by its name alone, when that tells (folder_groups)."""
    found = folder_groups(name, want, groups)
    return found[0] if len(found) == 1 else None


def covers(audio: list[dict], editions: list[dict], artist: str, titled: bool = False) -> int:
    """The most tracks of an edition the files' names cover, every one of them: by title, or by disc and track number
    unless titled; 0 when they cover none. Their lengths are checked when they are imported."""
    def number(name):
        found, disc = number_of(name), DISC.match(PurePosixPath(name).parent.name)
        return (int(disc[1]), found[1]) if found and disc else found
    files = [{"path": f["name"], "key": key(title_of(f["name"], artist)), "number": None if titled else number(f["name"]), "length": None} for f in audio]
    return max((len(tracks) for r in editions if r.get("status") != "Pseudo-Release" and (tracks := [(m, t) for m in r.get("media") or [] for t in m.get("tracks") or []])
                and len(tracks) <= len(files) and match(files, tracks) is not None), default=0)


def disc_images(audio: list[dict], sheets: set[PurePosixPath]) -> bool:
    """Whether an album folder's music is disc images: each of its folders with music holds one audio file and a cue sheet."""
    folders = [PurePosixPath(f["name"]).parent for f in audio]
    return len(set(folders)) == len(folders) and set(folders) <= sheets


def sized(audio: list[dict], editions: list[dict]) -> bool:
    """Whether disc images are the size of an edition's music, as far as it can tell: their cue sheets, which tell the
    tracks, aren't readable before download. The importer splits them and matches the edition after."""
    lengths = [sum(t.get("length") or t["recording"].get("length") or 0 for m in r.get("media") or [] for t in m.get("tracks") or []) / 1000
               for r in editions if r.get("status") != "Pseudo-Release"]
    size = sum(f["size"] for f in audio)
    return any(RATE[0] <= size / s <= RATE[1] for s in lengths if s) or not any(lengths)


def targets(files: list[dict], want: dict, groups: list[dict], wanted: set[str], editions) -> dict[str, list[str]]:
    """The wanted albums, EPs and singles a bundle's file list ({name, size}) holds, by release group, each with its files to fetch:
    one album folder's music (its discs' folders too), cue sheets and images. A folder must cover a whole edition by
    its file names, or be disc images of an edition's size. Of several groups its name may be (folder_groups), it is the
    one whose songs it holds by title, the most of them ('Hybrid Theory' with the EP's songs is the EP). A folder whose
    name is none of the artist's groups ('CD 2', 'Fan Club 2005') and no variant is the one wanted group an edition of
    which it holds exactly, song for song by title.
    Lossless folders are tried first, a file per track before images; then singles in loose tracks. groups: the
    artist's release groups; wanted: those missing. editions(rg): the release group's editions with their tracks."""
    folders, sheets = {}, {PurePosixPath(f["name"]).parent for f in files if f["name"].lower().endswith(".cue")}
    for f in files:
        if f["name"].rsplit(".", 1)[-1].lower() in AUDIO:
            parent = PurePosixPath(f["name"]).parent
            folders.setdefault(parent.parent if DISC.match(parent.name) else parent, []).append(f)
    lossless = lambda audio: sum(f["name"].rsplit(".", 1)[-1].lower() in LOSSLESS_FILES for f in audio)
    known, allowed = {}, hits(VARIANTS, " ".join(words(" ".join(want["names"]))))

    def fits(rg, audio, titled=False, exact=False) -> int:
        try:
            if rg not in known:
                known[rg] = editions(rg)
            if disc_images(audio, sheets):
                return int(not titled and sized(audio, known[rg]))
            return covers(audio, [r for r in known[rg] if not exact or sum(len(m.get("tracks") or []) for m in r.get("media") or []) == len(audio)],
                          want["artist"], titled)
        except (OSError, LookupError) as e:
            log.info("editions of %s: %s", rg, e)
            known[rg] = []
            return 0

    found = {}
    for folder, audio in sorted(folders.items(), key=lambda item: (lossless(item[1]) > 0, not disc_images(item[1], sheets), lossless(item[1]),
                                                                    len(item[1])), reverse=True):
        if len(named := folder_groups(folder.name, want, groups)) == 1:
            rg = named[0] if named[0] in wanted and fits(named[0], audio) else None
        elif named:
            rg = max(named, key=lambda rg: fits(rg, audio, titled=True)) if any(fits(rg, audio, titled=True) for rg in named) else None
        elif not hits(VARIANTS, " ".join(words(folder.name))) - allowed and not VIDEO.search(" ".join(words(folder.name))):
            by_songs = [rg for rg in sorted(wanted - set(found)) if fits(rg, audio, titled=True, exact=True)]
            rg = by_songs[0] if len(by_songs) == 1 else None
        else:
            rg = None
        if rg in wanted and rg not in found:
            extras = [f["name"] for f in files if f["name"].rsplit(".", 1)[-1].lower() in (*IMAGES, "cue") and PurePosixPath(f["name"]).is_relative_to(folder)]
            found[rg] = [f["name"] for f in audio] + extras
        elif set(named) & wanted - set(found):
            log.info("%s holds only part of %s", folder, named)
    singles = [g["id"] for g in groups if g.get("primary-type") == "Single" and g["id"] in wanted and g["id"] not in found]
    return found | loose([f for a in folders.values() if not disc_images(a, sheets) for f in a], want, singles, lambda rg: known.get(rg) or editions(rg))


def loose(audio: list[dict], want: dict, singles: list[str], editions) -> dict[str, list[str]]:
    """The singles a bundle holds as loose tracks, in any folder: each track of one of a single's editions has a file
    named exactly as it, lossless first. Exactly: brackets aside, as the importer reads a file's name, but neither a
    title holding the other nor track numbers, which count within other releases; nor a variant (live, remix...) the
    track isn't. A file may serve an album's target and a single's both: each gets its own copy, so a single never
    takes an album's files. The importer's edition match, by length too, is the real check."""
    named = {}
    for f in sorted(audio, key=lambda f: f["name"].rsplit(".", 1)[-1].lower() not in LOSSLESS_FILES):
        if title := key(title_of(f["name"], want["artist"])):
            named.setdefault(title, []).append((f["name"], hits(VARIANTS, " ".join(words(PurePosixPath(f["name"]).stem)))))
    found = {}
    for rg in singles:
        try:
            official = [r for r in editions(rg) if r.get("status") != "Pseudo-Release"]
        except (OSError, LookupError) as e:
            log.info("editions of %s: %s", rg, e)
            continue
        for r in sorted(official, key=lambda r: sum(len(m.get("tracks") or []) for m in r.get("media") or []), reverse=True):
            picked = []
            for t in (t for m in r.get("media") or [] for t in m.get("tracks") or []):
                allowed = hits(VARIANTS, " ".join(words(" ".join([t["title"], *want["names"]]))))
                picked.append(next((n for n, v in named.get(key(t["title"]), []) if v <= allowed and n not in picked), None))
            if picked and None not in picked:
                found[rg] = picked
                break
    return found


def expected(release: dict, want: dict, groups: list[dict], wanted: set[str]) -> set[str]:
    """The wanted release groups a bundle likely holds, by its name alone: those it names; for a discography or
    collection, the albums and EPs of the years it spans (all without years), and singles too when it counts more
    releases than the artist has albums and EPs; a singles collection, the singles. None when the artist doesn't open
    the name ('Mike Shinoda (from Linkin Park) - Discography') or it is a variant (instrumentals, a tribute)."""
    tokens, artists = words(release["title"]), leads(release["title"], want)
    if not artists or hits(VARIANTS, " ".join(tokens)) - BUNDLE_WORDS:
        return set()
    names = {"".join(words(n)) for n in want["names"]}
    found = {g["id"] for g in groups if g["id"] in wanted and len(j := "".join(words(g["title"]))) >= 4 and j not in names
             and any(all(e <= a or b <= s for a, b in artists) for s, e in spans(tokens, words(g["title"])))}
    if not BUNDLES.search(release["title"]):
        return found
    years, count = sorted(int(t) for t in tokens if YEAR.match(t)), RELEASES.search(" ".join(tokens))
    only = "singles" in tokens and not DISCOGRAPHY.search(release["title"])
    singles = "singles" in tokens or bool(count) and int(count[1]) > sum(g.get("primary-type") != "Single" for g in groups)
    spanned = lambda g: not years or years[0] <= int((g.get("first-release-date") or "0")[:4] or 0) <= years[-1]
    return found | {g["id"] for g in groups if g["id"] in wanted and spanned(g) and (singles if g.get("primary-type") == "Single" else not only)}


def bundles(prowlarr, want: dict, groups: list[dict], wanted: set[str], blocked: set, cached_files, editions, uncached: bool = False,
            slow: frozenset = frozenset(), torrent=lambda release: None) -> list[tuple[dict, dict | None, set[str]]]:
    """The artist's bundles that hold wanted release groups, best first, each with its targets when TorBox's file list
    gives them (to fetch only theirs), else None (resolve, in download.py, reads them once TorBox has it), and the
    groups it holds or likely holds. What it holds is read from a file list: TorBox's when it has it cached file by
    file (it lists a cached torrent of 100 files or more as one zip), else the .torrent's through Prowlarr
    (torrent(release): {hash, files}, or None for a magnet link), which also gives the hash of a release listed without;
    else its name tells (expected); a magnet link's only when the artist's name stands alone (alone). Those whose files
    oasis read come before every one it couldn't: what they hold is sure. Then cached ones: they download at once. With
    uncached, while slots are free, those with BUNDLE_SEEDERS follow: they download whole and hold a slot for long. Then
    the most groups, quality and seeders; slow ones (see rank) last."""
    found = unique([r for q in bundle_queries(want) for r in prowlarr.search(q)], lambda r: hash_of(r) or r["guid"])
    candidates = sorted((r for r in found if not {hash_of(r), r.get("guid")} & blocked and is_bundle(r, want)), key=lambda r: r.get("seeders") or 0, reverse=True)
    read, budget = {}, [TORRENTS]

    def listing(r):  # the .torrent's hash and files, within the budget
        if not (budget[0] and r.get("downloadUrl")):
            return r
        budget[0] -= 1
        if (t := torrent(r)) and t["hash"]:
            read[t["hash"]] = t["files"]
            return {**r, "infoHash": t["hash"]}
        return r

    candidates = unique([r for r in map(lambda r: r if hash_of(r) else listing(r), candidates) if hash_of(r) and hash_of(r) not in blocked], hash_of)
    budget[0] = TORRENTS  # and as many for the file lists of those with hashes
    hashes = [hash_of(r) for r in candidates]
    listed = {h: files for i in range(0, len(hashes), 100) for h, files in cached_files(hashes[i:i + 100]).items()}  # 100 a call
    music = lambda files, kinds: any(f["name"].rsplit(".", 1)[-1].lower() in kinds for f in files)
    seeded = lambda r: uncached and (r.get("seeders") or 0) >= BUNDLE_SEEDERS
    picks = []
    for r in candidates:
        h, cached = hash_of(r), hash_of(r) in listed
        if cached and music(listed[h], AUDIO):
            got, holds, known = (t := targets(listed[h], want, groups, wanted, editions)), set(t), True
        elif cached and music(listed[h], ARCHIVES) or not cached and seeded(r):
            if h not in read:
                listing(r)
            got, known = None, bool(read.get(h))
            if not cached and not known and not alone(r["title"], want):
                continue
            holds = set(targets(read[h], want, groups, wanted, editions)) if known else expected(r, want, groups, wanted)
        else:
            continue
        if holds:
            picks.append(((cached or known, h not in slow, cached, len(holds), quality(r["title"]), r.get("seeders") or 0), r, got, holds))
    log.info("bundles of %s: %d results, %d bundles, %d cached, %d hold wanted albums (%d cached)", want["artist"], len(found), len(candidates),
             len(listed), len(picks), sum(p[0][2] for p in picks))
    return [(r, got, holds) for _, r, got, holds in sorted(picks, key=lambda p: p[0], reverse=True)]
