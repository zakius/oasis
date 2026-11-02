"""Latin names MusicBrainz's editors chose, which the romanization extension and search (api.py) prefer over
romanize.py's own: a Person's or Group's ja-Latn alias, else its sort name in its own order ('Yonezu, Kenshi' ->
Yonezu Kenshi; not the English alias, which turns a Japanese name around), for those whose sort name is Latin; a
release's transliteration (a Latin-script pseudo-release it links by transl-tracklisting) for its album and track
titles, by position. A pseudo-release in English counts only when its titles read as the originals do: editors tag
some transliterations English, and a translation ('Sleep' for 眠れ) is no reading. Read one MusicBrainz request at
a time after credits (credits.py), kept in mb_cache, read again after REFRESH."""
import difflib
import time
import unicodedata

from . import musicbrainz, romanize

ARTIST = "https://musicbrainz.org/ws/2/artist/{}?fmt=json&inc=aliases"
RELEASE = "https://musicbrainz.org/ws/2/release/{}?fmt=json&inc=release-rels"
PSEUDO = "https://musicbrainz.org/ws/2/release/{}?fmt=json&inc=recordings"
REFRESH = 180 * 86400   # seconds an artist's or release's Latin names stay before MusicBrainz is asked again
LIKE = 0.6              # how close an English pseudo-release's titles must be to generated readings to be readings


def artist_name(data: dict) -> str | None:
    sort = data.get("sort-name") or ""
    if data.get("type") not in ("Person", "Group") or not sort or romanize.has_non_latin(sort):
        return None
    alias = next((a["name"] for a in sorted(data.get("aliases") or [], key=lambda a: not a.get("primary"))
                  if a.get("locale") == "ja-Latn" and not romanize.has_non_latin(a["name"])), None)
    return alias or " ".join(p.strip() for p in sort.split(",", 1))


def letters(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s.casefold()) if c.isascii() and c.isalnum())


def readings(pairs: list[tuple[str, str]]) -> bool:
    """Whether the Latin titles read as the originals, by their likeness to generated readings: not translations."""
    ratios = [difflib.SequenceMatcher(None, letters(romanize.text(o, [], True)), letters(l)).ratio() for o, l in pairs]
    return bool(ratios) and sum(ratios) / len(ratios) >= LIKE


def transliteration(db, release: str, data: dict, fetch) -> dict[str, str]:
    """The library's album and track titles of the release -> its transliteration's, for those not Latin already."""
    lang = (data.get("text-representation") or {}).get("language")
    rows = db.execute("select title, album, coalesce(disc_no, 1) disc, track_no from track where mb_release = ? and library = 1", (release,)).fetchall()
    for r in data.get("relations") or []:
        if r.get("type") != "transl-tracklisting" or r.get("direction") != "forward" or not r.get("release"):
            continue
        pseudo = fetch(PSEUDO.format(r["release"]["id"]))
        shown = pseudo.get("text-representation") or {}
        if pseudo.get("status") != "Pseudo-Release" or shown.get("script") != "Latn":
            continue
        titles = {(m.get("position") or 1, t.get("position")): t["title"] for m in pseudo.get("media") or [] for t in m.get("tracks") or []}
        pairs = list(dict.fromkeys([*((a, pseudo["title"]) for a in {row["album"] for row in rows if row["album"]}),
                                    *((row["title"], titles[row["disc"], row["track_no"]]) for row in rows if row["title"] and (row["disc"], row["track_no"]) in titles)]))
        pairs = [(o, l) for o, l in pairs if romanize.has_non_latin(o) and not romanize.has_non_latin(l)]
        if shown.get("language") == "eng" and lang != "eng" and not readings(pairs):
            continue  # a translation
        return dict(pairs)
    return {}


def due(db) -> tuple[str, str] | None:
    """An artist credited on a library track under a name not in Latin script, or a release of the library with such a
    title, whose Latin names weren't read within REFRESH."""
    seen = {r[0] for r in db.execute("select ref from mb_latin_seen where fetched > ?", (time.time() - REFRESH,))}
    for mbid, name in db.execute("select distinct c.mbid, c.name from credit c join track t on t.id = c.track_id where t.library = 1 and c.mbid is not null"):
        if mbid not in seen and romanize.has_non_latin(name):
            return "artist", mbid
    for release, title, album in db.execute("select mb_release, title, album from track where library = 1 and mb_release is not null"):
        if release not in seen and (romanize.has_non_latin(title or "") or romanize.has_non_latin(album or "")):
            return "release", release
    return None


def step(db, fetch=None) -> bool:
    """Reads one artist's or release's Latin names; whether there was one to read. OSError: MusicBrainz is away."""
    if (found := due(db)) is None:
        return False
    fetch = fetch or (lambda url: musicbrainz.get(url, db, REFRESH))
    kind, ref = found
    names = {}
    try:
        if kind == "artist":
            data = fetch(ARTIST.format(ref))
            if latin := artist_name(data):
                credited = {r[0] for r in db.execute("select distinct name from credit where mbid = ?", (ref,))} | {data["name"]}
                names = {n: latin for n in credited if romanize.has_non_latin(n)}
        else:
            names = transliteration(db, ref, fetch(RELEASE.format(ref)), fetch)
    except LookupError:
        pass
    old = [r[0] for r in db.execute("select original from mb_latin where ref = ?", (ref,))]
    db.execute("delete from mb_latin where ref = ?", (ref,))
    db.executemany("insert or replace into mb_latin values (?, ?, ?, ?)", [("artist" if kind == "artist" else "title", o, l, ref) for o, l in names.items()])
    for original in {*old, *names}:  # search's romanized forms (api.latin_index) of the songs they name are read again
        db.execute("delete from track_latin where instr(source, ?) > 0", (original,))
    db.execute("insert or replace into mb_latin_seen values (?, ?)", (ref, time.time()))
    return True


def curated(db) -> dict[str, dict[str, str]]:
    """{'artist': {name: Latin}, 'title': {title: Latin}}: MusicBrainz's, for romanization to prefer."""
    found = {"artist": {}, "title": {}}
    for kind, original, latin in db.execute("select kind, original, latin from mb_latin"):
        found[kind][original] = latin
    return found


def names_in(s: str, artists: dict[str, str]) -> str:
    """s with each artist name MusicBrainz has a Latin name for put in it, longest first: '米津玄師 + アイナ・ジ・エンド'
    -> 'Yonezu Kenshi + アイナ・ジ・エンド', the rest for romanize.py."""
    for name in sorted((n for n in artists if n in s), key=len, reverse=True):
        s = s.replace(name, artists[name])
    return s
