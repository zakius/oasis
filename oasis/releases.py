"""oasis's own reading of release names for its Prowlarr search: which torrents are the album, and which to try first.
Names come in any script and shape ('[마신] [2022.09.07] Creepy Nuts 3rdアルバム「アンサンブル・プレイ」[FLAC 48kHz/24bit]'),
so the name and the album are both folded to words and compared word by word; each kana, kanji or Hangul syllable
is a word of its own, as those scripts don't space words. Uploads may name the album by another of its titles in
MusicBrainz ('Ensemble Play'), which count as its own. A wrong album is worse than none: variants (live,
instrumentals, deluxe...), bundles and videos are left out, and so are short or self-titled names that don't pin the
album down. Disc images are tried after a file per track. The importer's edition match comes after, as a second check.
Discographies and other bundles TorBox has cached are read by their file lists: each folder that is one wanted album
becomes a target, and only its files are fetched. A folder of disc images is taken by its name and size alone."""
import logging
import re
import unicodedata
from pathlib import PurePosixPath

from . import musicbrainz
from .importer import AUDIO, LOSSLESS as LOSSLESS_FILES, key, match, number_of, title_of

log = logging.getLogger(__name__)

CJK = re.compile("[぀-ヺー-ヿ㐀-䶿一-鿿가-힯豈-﫿]")  # kana, kanji, Hangul; not ・
LETTERS = str.maketrans({"æ": "ae", "œ": "oe", "ø": "o", "ł": "l", "đ": "d", "ð": "d", "þ": "th", "ı": "i"})  # no accent to strip
BRACKETS = re.compile(r"[\[\]「」『』\"“”„«»()〈〉《》【】/／]")  # and slashes, which break some indexers' search URLs
BRACKETED = re.compile(r"\s*[\[(（【][^\])）】]*[\])）】]")
QUOTED = re.compile(r"[「『\"“]([^」』\"”]+)[」』\"”]")
# Words that make a release something else than the album, unless its title says so. Remasters are the album.
VARIANTS = re.compile(r"\b(best of|greatest hits|very best|the essential|anthology|singles|anniversary|deluxe|expanded|tour edition|bonus tracks?|"
                      r"re ?recorded|reissue|remix(es|ed)?|instrumentals?|inst|off vocal|karaoke|acapellas?|a ?cappellas?|live|unplugged|"
                      r"acoustic|demos?|tribute|orchestral|symphonic|piano|lullaby|8 ?bit|sped up|slowed|nightcore|tv size)\b")
VARIANTS_JA = re.compile(r"インスト|カラオケ|ライ[ブヴ]|リミックス|オフボーカル")
BUNDLES = re.compile(r"discograph|collection|complete|box ?set|anthology|全集|ディスコグラフィ", re.I)
VIDEO = re.compile(r"\b(dvd\d*|dvdrip|bdmv|blu ?ray|bd ?rip|bd|hdtv|\d{3,4}[pi]|x26[45]|h ?26[45]|hevc|mkv|avi|video)\b")
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
YEAR = re.compile(r"^(19|20)\d\d$")
IMAGES = ("jpg", "jpeg", "png", "webp", "gif")


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


def rank(releases: list[dict], have: set[str], upgrade: bool = False) -> list[dict]:
    """Best first: lossless, then a file per track over a disc image, then cached on TorBox (it has those at once,
    seeders or not), then quality, then seeders.
    A release TorBox hasn't cached needs a seeder. The same torrent from several indexers is tried once."""
    usable = [r for r in releases if hash_of(r) in have or (r.get("seeders") or 0) >= 1]
    if upgrade:
        usable = [r for r in usable if quality(r["title"]) == 3]
    usable.sort(key=lambda r: (quality(r["title"]) == 3, not IMAGE.search(" ".join(words(r["title"]))), hash_of(r) in have, quality(r["title"]),
                               r.get("seeders") or 0), reverse=True)
    return unique(usable, lambda r: hash_of(r) or r["guid"])


def clean(text: str) -> str:
    return " ".join(BRACKETS.sub(" ", unicodedata.normalize("NFKC", text)).split())


def queries(want: dict) -> list[str]:
    """Artist and album; the album alone when distinctive; the album with the artist's names in other scripts; the
    artist and the album's other titles. Brackets and quotes go: indexers find nothing for '[Rammstein]'."""
    title = clean(want["title"])
    return list(dict.fromkeys([f"{clean(want['artist'])} {title}", *([title] if len(title.split()) >= 2 or len(title) >= 8 else []),
                               *(f"{clean(n)} {title}" for n in want["native"]),
                               *(f"{clean(want['artist'])} {clean(t)}" for t in want["other_titles"][:OTHER_TITLE_QUERIES])]))


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


def is_bundle(release: dict, want: dict) -> bool:
    """A discography or bundle of the artist: its name says so, or holds two of the artist's album titles.
    want: artist, names, native, albums (the artist's album and EP titles)."""
    tokens = words(release["title"])
    artists = [s for n in want["names"] for s in spans(tokens, words(n))]
    if not artists or VIDEO.search(" ".join(tokens)):
        return False
    names = {"".join(words(n)) for n in want["names"]}
    held = {"".join(words(t)) for t in want["albums"] if len(j := "".join(words(t))) >= 4 and j not in names
            and any(all(e <= a or b <= s for a, b in artists) for s, e in spans(tokens, words(t)))}
    return bool(BUNDLES.search(release["title"])) or len(held) >= 2


def bundle_queries(want: dict) -> list[str]:
    return list(dict.fromkeys([f"{clean(want['artist'])} discography", clean(want["artist"]), *map(clean, want["native"])]))


def folder_album(name: str, want: dict, groups: list[dict]) -> str | None:
    """The release group an album folder inside a bundle is: its name holds the title (or one of the group's
    other_titles) apart from the artist's name, and the year when it has one; variants and videos aren't the album. Of
    titles inside one another the longest wins ('One More Light' over 'One'); two groups of one title that the year
    doesn't tell apart are neither."""
    tokens = words(name)
    text, years = " ".join(tokens), {t for t in tokens if YEAR.match(t)}
    artists = [s for n in want["names"] for s in spans(tokens, words(n))]
    names = {"".join(words(n)) for n in want["names"]}
    fits = []
    for g, named in ((g, t) for g in groups for t in [g["title"], *g.get("other_titles", ())]):
        title, year = words(named), (g.get("first-release-date") or "")[:4]
        compact, title_text = "".join(title), " ".join(title)
        if not compact or not any(all(e <= a or b <= s for a, b in artists) for s, e in spans(tokens, title)):
            continue
        if years and year not in years or (len(compact) < 4 or compact in names) and year not in years:
            continue
        allowed = hits(VARIANTS, " ".join([title_text, *(" ".join(words(n)) for n in want["names"])]))
        if hits(VARIANTS, text) - allowed or hits(VARIANTS_JA, plain(name)) - hits(VARIANTS_JA, plain(named)) or \
                hits(VARIANTS, title_text) - hits(VARIANTS, text) or VIDEO.search(text) and not VIDEO.search(title_text):
            continue
        fits.append((len(compact), g["id"]))
    best = {rg for n, rg in fits if n == max(fits)[0]}
    return best.pop() if len(best) == 1 else None


def covers(audio: list[dict], editions: list[dict], artist: str) -> bool:
    """Whether the files' names cover every track of an edition: by title, or by disc and track number. Their
    lengths are checked when they are imported."""
    def number(name):
        found, disc = number_of(name), DISC.match(PurePosixPath(name).parent.name)
        return (int(disc[1]), found[1]) if found and disc else found
    files = [{"path": f["name"], "key": key(title_of(f["name"], artist)), "number": number(f["name"]), "length": None} for f in audio]
    return any(r.get("status") != "Pseudo-Release" and (tracks := [(m, t) for m in r.get("media") or [] for t in m.get("tracks") or []])
               and len(tracks) <= len(files) and match(files, tracks) is not None for r in editions)


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
    """The wanted albums a bundle's file list ({name, size}) holds, by release group, each with its files to fetch:
    one album folder's music (its discs' folders too), cue sheets and images. A folder must cover a whole edition by
    its file names, or be disc images of an edition's size; lossless folders are tried first, a file per track before
    images. groups: the artist's release groups; wanted: those missing. editions(rg): the release group's editions
    with their tracks."""
    folders, sheets = {}, {PurePosixPath(f["name"]).parent for f in files if f["name"].lower().endswith(".cue")}
    for f in files:
        if f["name"].rsplit(".", 1)[-1].lower() in AUDIO:
            parent = PurePosixPath(f["name"]).parent
            folders.setdefault(parent.parent if DISC.match(parent.name) else parent, []).append(f)
    lossless = lambda audio: sum(f["name"].rsplit(".", 1)[-1].lower() in LOSSLESS_FILES for f in audio)
    found = {}
    for folder, audio in sorted(folders.items(), key=lambda item: (lossless(item[1]) > 0, not disc_images(item[1], sheets), lossless(item[1]),
                                                                    len(item[1])), reverse=True):
        rg = folder_album(folder.name, want, groups)
        if rg not in wanted or rg in found:
            continue
        try:
            fits = sized(audio, editions(rg)) if disc_images(audio, sheets) else covers(audio, editions(rg), want["artist"])
        except (OSError, LookupError) as e:
            log.info("editions of %s: %s", rg, e)
            continue
        if fits:
            extras = [f["name"] for f in files if f["name"].rsplit(".", 1)[-1].lower() in (*IMAGES, "cue") and PurePosixPath(f["name"]).is_relative_to(folder)]
            found[rg] = [f["name"] for f in audio] + extras
        else:
            log.info("%s holds only part of %s", folder, rg)
    return found


def bundles(prowlarr, want: dict, groups: list[dict], wanted: set[str], blocked: set, cached_files, editions) -> list[tuple[dict, dict]]:
    """The artist's bundles TorBox has cached that hold wanted albums, with their targets: most albums first, then
    quality and seeders. Uncached ones are left out, as they would download everything and hold a slot for long."""
    found = unique([r for q in bundle_queries(want) for r in prowlarr.search(q)], hash_of)
    candidates = sorted((r for r in found if hash_of(r) and not {hash_of(r), r.get("guid")} & blocked and is_bundle(r, want)),
                        key=lambda r: r.get("seeders") or 0, reverse=True)[:100]
    listed = cached_files([hash_of(r) for r in candidates]) if candidates else {}
    picks = [(r, t) for r in candidates if hash_of(r) in listed and (t := targets(listed[hash_of(r)], want, groups, wanted, editions))]
    log.info("bundles of %s: %d results, %d bundles, %d cached, %d with wanted albums", want["artist"], len(found), len(candidates), len(listed), len(picks))
    return sorted(picks, key=lambda p: (len(p[1]), quality(p[0]["title"]), p[0].get("seeders") or 0), reverse=True)
