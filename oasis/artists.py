"""Who is who among artists. One MusicBrainz artist is one artist page, whatever spelling or script a file or YouTube
Music uses (米津玄師, Kenshi Yonezu), and two artists who share a name get two pages. Artists that are separate but close
stay separate pages, linked: personas of one person (MusicBrainz), groups the user declares related (oasis.toml), and
bands with their members. Links lead the similar artists clients show and feed radios. The user's merges, splits and
groups win over MusicBrainz."""
from itertools import combinations

from .db import artist_id, make_id, move_stat, resolve

PERSONA, RELATED, MEMBER = "persona", "related", "member"
CLOSENESS = (PERSONA, RELATED, MEMBER)
LINKS = {"is person": PERSONA, "member of band": MEMBER, "subgroup": MEMBER, "collaboration": MEMBER}
# Names the artist performs under; search hints and legal names aren't, and often belong to someone else too.
ALIAS_TYPES = {"Artist name", None}


def learn(db, data: dict):
    """Stores what MusicBrainz knows about an artist: its other names (for artists credited as performing ones) and
    its links."""
    own = artist_id(db, data["name"], data["id"])
    db.execute("update mb_artist set type = ?, fetched = strftime('%s') where mbid = ?", (data.get("type"), data["id"]))
    if db.execute("select 1 from credit where mbid = ? and role in ('artist', 'albumartist')", (data["id"],)).fetchone():
        for a in data.get("aliases") or []:
            id = make_id("ar-", a["name"])
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


def configure(db, settings: dict):
    """Applies the user's [artists] settings: merge (lists whose later names become the first), separate (names never
    merged into another artist) and related (groups of separate artists that belong together)."""
    before = dict(db.execute("select id, artist_id from artist_alias where id != artist_id").fetchall())
    db.execute("delete from artist_alias where source = 'config'")
    db.execute("delete from artist_link where source = 'config'")
    for name in settings.get("separate", []):
        db.execute("insert or replace into artist_alias values (?, ?, 'config')", (make_id("ar-", name), make_id("ar-", name)))
    for first, *others in settings.get("merge", []):
        db.executemany("insert or replace into artist_alias values (?, ?, 'config')",
                       [(make_id("ar-", name), artist_id(db, first)) for name in others])
    for group in settings.get("related", []):
        for a, b in combinations([artist_id(db, name) for name in group], 2):
            link(db, a, b, RELATED, "config")
    for id, target in before.items():  # a merge taken back or overridden: that name's songs and credits leave the artist
        if (now := resolve(db, id)) != target:
            for table, name, own in (("track", "album_artist", ""), ("credit", "artist_name", " and mbid is null")):
                rows = db.execute(f"select rowid, {name} from {table} where artist_id = ?{own}", (target,)).fetchall()
                db.executemany(f"update {table} set artist_id = ? where rowid = ?", [(now, r) for r, n in rows if make_id("ar-", n) == id])
    merge(db)


def merge(db):
    """Moves what is filed under another name of an artist to the artist: songs, credits without a MusicBrainz id,
    stars, follows and the YouTube Music page."""
    for old, new in db.execute("select id, artist_id from artist_alias where id != artist_id and (id in (select artist_id from track) "
                               "or id in (select artist_id from credit) or id in (select id from stat))").fetchall():
        db.execute("update track set artist_id = ? where artist_id = ?", (new, old))
        db.execute("update credit set artist_id = ? where artist_id = ? and mbid is null", (new, old))
        db.execute("update ytm_release set artist_id = ? where artist_id = ?", (new, old))
        db.execute("delete from ytm_artist where artist_id = ?", (old,))
        db.execute("update or ignore lidarr_item set ref = ? where kind = 'artist' and ref = ?", (new, old))
        move_stat(db, old, new)


def reidentify(db):
    """Gives MusicBrainz credits the id their artist has: credits from before artists were told apart by MusicBrainz id
    share one per name."""
    for mbid, name, old in db.execute("select mbid, artist_name, artist_id from credit where mbid is not null group by mbid").fetchall():
        if (new := artist_id(db, name, mbid)) != old:
            db.execute("update credit set artist_id = ? where mbid = ?", (new, mbid))


def linked(db, id: str) -> list[tuple[str, str]]:
    """The artists linked to one, (id, kind), closest first."""
    rows = db.execute("select other_id, kind from artist_link where artist_id = ? union select artist_id, kind from artist_link "
                      "where other_id = ?", (id, id)).fetchall()
    found = {}
    for other, kind in sorted(rows, key=lambda r: CLOSENESS.index(r[1])):
        found.setdefault(other, kind)
    return list(found.items())
