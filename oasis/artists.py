"""Who is who among artists. One MusicBrainz artist is one artist page, whatever spelling or script a file or YouTube
Music uses (米津玄師, Kenshi Yonezu), and two artists who share a name get two pages. Artists that are separate but close
stay separate pages, linked: personas of one person (MusicBrainz), groups the user declares related (oasis.toml), and
bands with their members. Links lead the similar artists clients show and feed radios. The user's merges, splits and
groups win over MusicBrainz."""
import json
import unicodedata
from collections import Counter, defaultdict
from itertools import combinations

from . import musicbrainz
from .text import fold
from .tracks import artist_id, move, name_id, resolve

PERSONA, RELATED, MEMBER = "persona", "related", "member"
CLOSENESS = (PERSONA, RELATED, MEMBER)
LINKS = {"is person": PERSONA, "member of band": MEMBER, "subgroup": MEMBER, "collaboration": MEMBER}
# Names the artist performs under; search hints and legal names aren't, and often belong to someone else too.
ALIAS_TYPES = {"Artist name", None}
NOT_FOUND = -1  # mb_artist.fetched for artists MusicBrainz doesn't have


def step(db) -> bool:
    """Learns one credited artist MusicBrainz wasn't asked about yet; whether there was one. OSError: MusicBrainz is
    away."""
    row = db.execute("select mbid from mb_artist where fetched is null and mbid in (select mbid from credit) limit 1").fetchone()
    if row is None:
        return False
    mbid = row["mbid"]
    try:
        learn(db, musicbrainz.artist(mbid, db))
    except LookupError:
        db.execute("update mb_artist set fetched = ? where mbid = ?", (NOT_FOUND, mbid))
    merge(db)
    return True


def learn(db, data: dict):
    """Stores what MusicBrainz knows about an artist: its other names (for artists credited as performing ones) and
    its links."""
    own = artist_id(db, data["name"], data["id"])
    db.execute("update mb_artist set type = ?, fetched = strftime('%s') where mbid = ?", (data.get("type"), data["id"]))
    if db.execute("select 1 from credit where mbid = ? and role in ('artist', 'albumartist')", (data["id"],)).fetchone():
        for a in data.get("aliases") or []:
            id = name_id(a["name"])
            # A name another known artist goes by stays theirs; the first artist to claim one keeps it.
            if a.get("type") in ALIAS_TYPES and id != own and not db.execute("select 1 from mb_artist where artist_id = ?", (id,)).fetchone():
                db.execute("insert or ignore into artist_alias values (?, ?, 'musicbrainz')", (id, own))
    db.execute("delete from artist_link where artist_id = ? and source = 'musicbrainz'", (own,))
    for r in data.get("relations") or []:
        if (kind := LINKS.get(r.get("type"))) and r.get("artist"):
            link(db, own, artist_id(db, r["artist"]["name"], r["artist"]["id"]), kind, "musicbrainz")


def link(db, a: str, b: str, kind: str, source: str):
    if a != b:
        db.execute("insert or ignore into artist_link values (?, ?, ?, ?)", (a, b, kind, source))


def load_settings(db, fallback: dict) -> dict:
    """The dashboard's artist settings; fallback (oasis.toml's [artists]) until they are first saved there."""
    row = db.execute("select value from setting where key = 'artists'").fetchone()
    return json.loads(row[0]) if row else fallback


def save_settings(db, settings: dict):
    """Keeps the dashboard's artist settings and applies them."""
    db.execute("insert or replace into setting values ('artists', ?)", (json.dumps(settings, ensure_ascii=False),))
    configure(db, settings)


def configure(db, settings: dict):
    """Applies the user's [artists] settings: merge (lists whose later names become the first), separate (names never
    merged into another artist) and related (groups of separate artists that belong together)."""
    before = dict(db.execute("select id, artist_id from artist_alias where id != artist_id").fetchall())
    db.execute("delete from artist_alias where source = 'config'")
    db.execute("delete from artist_link where source = 'config'")
    for name in settings.get("separate", []):
        db.execute("insert or replace into artist_alias values (?, ?, 'config')", (name_id(name), name_id(name)))
    for first, *others in settings.get("merge", []):
        db.executemany("insert or replace into artist_alias values (?, ?, 'config')",
                       [(name_id(name), artist_id(db, first)) for name in others])
    for group in settings.get("related", []):
        for a, b in combinations([artist_id(db, name) for name in group], 2):
            link(db, a, b, RELATED, "config")
    for id, target in before.items():  # a merge taken back or overridden: that name's songs and credits leave the artist
        if (now := resolve(db, id)) != target:
            for table, name, own in (("track", "album_artist", ""), ("credit", "artist_name", " and mbid is null")):
                rows = db.execute(f"select rowid, {name} from {table} where artist_id = ?{own}", (target,)).fetchall()
                db.executemany(f"update {table} set artist_id = ? where rowid = ?", [(now, r) for r, n in rows if name_id(n) == id])
    merge(db)


def merge(db):
    """Moves what is filed under another name of an artist to the artist: songs, credits without a MusicBrainz id,
    stars, follows, links to other artists and the YouTube Music page."""
    for old, new in db.execute("select id, artist_id from artist_alias where id != artist_id and (id in (select artist_id from track) "
                               "or id in (select artist_id from credit) or id in (select id from stat))").fetchall():
        db.execute("update credit set artist_id = ? where artist_id = ? and mbid is null", (new, old))
        move(db, "artist", old, new)
    db.execute("delete from artist_link where artist_id = other_id")  # a link between two names of one artist


def accents(name: str) -> int:
    return sum(1 for c in unicodedata.normalize("NFKD", name) if unicodedata.combining(c)) + name.casefold().count("ł")


def unify(db):
    """One artist per name however it's spelled: names alike but for case, accents and punctuation (fold) are one
    artist, shown under MusicBrainz's name for it, else the spelling most of its tracks have, more accents breaking a
    tie ('Chylińska' over 'Chylinska', but 'Jay‐Z' on most tracks over YouTube's 'Jaÿ‐Z'). The artist keeps the id it
    has (artist_key) unless MusicBrainz's takes over; the others' ids resolve to it. Two MusicBrainz artists of one
    name stay two, and names the user's merges or separations mention stay as the user has them."""
    spellings, ids, mb = defaultdict(Counter), defaultdict(set), defaultdict(dict)
    for sql in ("select album_artist, artist_id, count(*) from track where album_artist is not null group by 1, 2",
                "select artist_name, artist_id, count(distinct track_id) from credit where mbid is null and artist_name is not null group by 1, 2"):
        for name, id, n in db.execute(sql):
            spellings[key := fold(name)][name] += n
            ids[key] |= {id, name_id(name)}
    for (name,) in db.execute("select distinct artist from track where artist is not null"):  # respelled in songs, not counted
        if (key := fold(name)) in spellings:
            spellings[key][name] += 0
    for name, id in db.execute("select name, artist_id from mb_artist"):
        mb[fold(name)][id] = name
    mbs = {id for found in mb.values() for id in found}
    config = {id for row in db.execute("select id, artist_id from artist_alias where source = 'config'") for id in row}
    keys = dict(db.execute("select key, artist_id from artist_key").fetchall())
    spelled = {}
    for key, names in spellings.items():
        own = {id for id in ids[key] if id in mbs} | set(mb[key])
        if not key or len(own) > 1 or ids[key] & config:
            continue
        display = next(iter(mb[key].values()), None) or max(names, key=lambda n: (names[n], accents(n), n))
        target = next(iter(own), None) or (keys[key] if keys.get(key) in ids[key] else resolve(db, name_id(display)))
        if keys.get(key) != target:
            db.execute("insert or replace into artist_key values (?, ?)", (key, target))
        spelled |= {n: display for n in names if n != display}
        for id in ids[key] - {target}:
            if resolve(db, id) != target:  # a name MusicBrainz or the user already gave an artist stays theirs
                db.execute("insert into artist_alias values (?, ?, 'spelling') on conflict(id) do update set artist_id = excluded.artist_id "
                           "where source = 'spelling'", (id, target))
                db.execute("update artist_alias set artist_id = ? where artist_id = ? and source != 'config'", (target, id))
        if resolve(db, target) != target:
            db.execute("delete from artist_alias where id = ? and source = 'spelling'", (target,))
    if spelled != spelled_as(db):
        db.execute("delete from spelling")
        db.executemany("insert into spelling values (?, ?)", spelled.items())
    merge(db)


def spelled_as(db) -> dict[str, str]:
    """Artist names shown as another spelling (unify): each variant's spelling."""
    return dict(db.execute("select variant, spelled from spelling").fetchall())


def linked(db, id: str) -> list[tuple[str, str]]:
    """The artists linked to one, (id, kind), closest first."""
    rows = db.execute("select other_id, kind from artist_link where artist_id = ? union select artist_id, kind from artist_link "
                      "where other_id = ?", (id, id)).fetchall()
    found = {}
    for other, kind in sorted(rows, key=lambda r: CLOSENESS.index(r[1])):
        found.setdefault(other, kind)
    return list(found.items())
