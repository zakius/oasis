"""oasis's own reading of release names for its Prowlarr search: which torrents are the album, and which to try first.
Names come in any script and shape ('[마신] [2022.09.07] Creepy Nuts 3rdアルバム「アンサンブル・プレイ」[FLAC 48kHz/24bit]'),
so the name and the album are both folded to words and compared word by word; each kana, kanji or Hangul syllable
is a word of its own, as those scripts don't space words. A wrong album is worse than none: variants (live,
instrumentals, deluxe...), bundles and videos are left out, and so are short or self-titled names that don't pin the
album down. The importer's edition match comes after, as a second check."""
import logging
import re
import unicodedata

from . import musicbrainz
from .lidarr import BUNDLES

log = logging.getLogger(__name__)

CJK = re.compile("[぀-ヺー-ヿ㐀-䶿一-鿿가-힯豈-﫿]")  # kana, kanji, Hangul; not ・
LETTERS = str.maketrans({"æ": "ae", "œ": "oe", "ø": "o", "ł": "l", "đ": "d", "ð": "d", "þ": "th", "ı": "i"})  # no accent to strip
BRACKETS = re.compile(r"[\[\]「」『』\"“”„«»()〈〉《》【】]")
BRACKETED = re.compile(r"\s*[\[(（【][^\])）】]*[\])）】]")
QUOTED = re.compile(r"[「『\"“]([^」』\"”]+)[」』\"”]")
# Words that make a release something else than the album, unless its title says so. Remasters are the album.
VARIANTS = re.compile(r"\b(best of|greatest hits|very best|the essential|anthology|singles|anniversary|deluxe|expanded|tour edition|bonus tracks?|"
                      r"re ?recorded|reissue|remix(es|ed)?|instrumentals?|inst|off vocal|karaoke|acapellas?|a ?cappellas?|live|unplugged|"
                      r"acoustic|demos?|tribute|orchestral|symphonic|piano|lullaby|8 ?bit|sped up|slowed|nightcore|tv size)\b")
VARIANTS_JA = re.compile(r"インスト|カラオケ|ライ[ブヴ]|リミックス|オフボーカル")
VIDEO = re.compile(r"\b(dvd\d*|dvdrip|bdmv|blu ?ray|bd ?rip|bd|\d{3,4}p|x26[45]|h ?26[45]|hevc|mkv|avi|video)\b")
IMAGE = re.compile(r"\b(image|img) cue\b")  # one file for the whole disc: the importer takes a file per track
LOSSLESS = re.compile(r"\b(flac|alac|wav|lossless|24 ?bits?|hi ?res|ape)\b")
MP3_320 = re.compile(r"\b320( ?k| ?kbps| ?kbit)?\b")
V0 = re.compile(r"\bv0\b")
SIZES = 20e6, 5e9      # bytes a release may have, the most per disc
ALIASES = "https://musicbrainz.org/ws/2/artist/{}?inc=aliases&fmt=json"
KEEP_ALIASES = 30 * 86400
NATIVE_QUERIES = 2     # searches by the artist's names in other scripts


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


def target(db, lidarr, album: dict) -> dict:
    """What the search looks for: the artist under all its names (MusicBrainz aliases, Lidarr's name), the title,
    year, discs, the artist's other albums and EPs (a name holding one is a bundle), and whether the album is here
    lossy already (then only lossless will do)."""
    artist = album["artist"]
    albums = [a for a in lidarr.albums_of(album["artistId"]) if a.get("albumType") in ("Album", "EP")]
    names = aliases(db, artist["artistName"], artist.get("foreignArtistId"), [album["title"], *(a["title"] for a in albums)])
    return {"artist": artist["artistName"], "names": names, "title": album["title"], "year": (album.get("releaseDate") or "")[:4],
            "native": native(names), "discs": next((r.get("mediumCount") or 1 for r in album.get("releases") or [] if r.get("monitored")), 1),
            "others": [a["title"] for a in albums if a["id"] != album["id"]],
            "upgrade": bool((album.get("statistics") or {}).get("trackFileCount"))}


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


def reject(release: dict, want: dict) -> str | None:
    """Why the release isn't the album, or None when it is."""
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
    allowed = hits(VARIANTS, " ".join([title_text, *(" ".join(words(n)) for n in want["names"])]))
    if extra := hits(VARIANTS, text) - allowed or hits(VARIANTS_JA, plain(name)) - hits(VARIANTS_JA, plain(title)):
        return f"variant: {sorted(extra)[0]}"
    if missing := hits(VARIANTS, title_text) - hits(VARIANTS, text):
        return f"not the variant: {sorted(missing)[0]}"
    if VIDEO.search(text) and not VIDEO.search(title_text):
        return "video"
    if IMAGE.search(text):
        return "disc image"
    if BUNDLES.search(name) and not BUNDLES.search(title):
        return "bundle"
    compact, names = "".join(words(title)), {"".join(words(n)) for n in want["names"]}
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


def rank(releases: list[dict], have: set[str], upgrade: bool = False) -> list[dict]:
    """Best first: lossless, then cached on TorBox (it has those at once, seeders or not), then quality, then seeders.
    A release TorBox hasn't cached needs a seeder. The same torrent from several indexers is tried once."""
    usable = [r for r in releases if hash_of(r) in have or (r.get("seeders") or 0) >= 1]
    if upgrade:
        usable = [r for r in usable if quality(r["title"]) == 3]
    usable.sort(key=lambda r: (quality(r["title"]) == 3, hash_of(r) in have, quality(r["title"]), r.get("seeders") or 0), reverse=True)
    return unique(usable, lambda r: hash_of(r) or r["guid"])


def queries(want: dict) -> list[str]:
    """Artist and album; the album alone when distinctive; the album with the artist's names in other scripts.
    Brackets and quotes go: indexers find nothing for '[Rammstein]'."""
    clean = lambda text: " ".join(BRACKETS.sub(" ", unicodedata.normalize("NFKC", text)).split())
    title = clean(want["title"])
    return list(dict.fromkeys([f"{clean(want['artist'])} {title}", *([title] if len(title.split()) >= 2 or len(title) >= 8 else []),
                               *(f"{clean(n)} {title}" for n in want["native"])]))


def find(prowlarr, want: dict, blocked: set, cached) -> list[dict]:
    """The releases to try, best first, from the first query that finds a usable one."""
    for query in queries(want):
        found = prowlarr.search(query)
        fits = [r for r in found if not {hash_of(r), r.get("guid")} & blocked and not reject(r, want)]
        hashes = list(dict.fromkeys(h for r in fits if (h := hash_of(r))))[:100]
        picks = rank(fits, cached(hashes) if hashes else set(), want["upgrade"])
        log.info("Prowlarr %r: %d results, %d the album, %d usable", query, len(found), len(fits), len(picks))
        if picks:
            return picks
    return []
