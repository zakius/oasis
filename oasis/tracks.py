"""A track's identity in the library: the ids tracks, albums and artists get, the same song under another source (a
stream and a file, or two YouTube uploads, are one song), and moving one id onto another when two turn out to be one."""
import hashlib

from .text import fold

SAME_SONG_SECONDS = 5  # two copies of one song differ in length by at most this
IDS = {"artist": "ar-", "album": "al-", "track": "tr-", "stream": "yt-", "playlist": "pl-"}  # each kind's id prefix
MB_IDS = {"artist": "ar-mb-", "album": "al-mb-"}  # ids of artists and release groups only MusicBrainz has (see acquire.adopt)

# The tables that refer to each kind of id, as (table, column), besides stat, which holds all three kinds.
REFERENCES = {
    "track": [("playlist_entry", "track_id"), ("play", "track_id"), ("outbox", "track_id"), ("album_track", "track_id")],
    "album": [("track", "album_id"), ("album_track", "album_id"), ("wanted", "ref"), ("want_item", "ref")],
    "artist": [("track", "artist_id"), ("ytm_release", "artist_id"), ("wanted", "ref"), ("want_item", "ref"),
               ("artist_link", "artist_id"), ("artist_link", "other_id")],
}
# Rows about one id alone, dropped when it moves (the new id has its own) or its track is gone.
OWN_ROWS = {
    "track": [("track", "id"), ("lyrics", "track_id"), ("track_latin", "id"), ("credit", "track_id")],
    "album": [],
    "artist": [("ytm_artist", "artist_id")],
}


def make_id(prefix: str, *parts: str) -> str:
    return prefix + hashlib.sha1("\0".join(p.casefold() for p in parts).encode()).hexdigest()[:16]


def album_id(artist: str, album: str) -> str:
    """An album's oasis id, by its album artist and title: a local album and a YouTube Music one of these share it."""
    return make_id(IDS["album"], artist, album)


def track_id(path: str) -> str:
    """The id a library file gets when it is first scanned."""
    return make_id(IDS["track"], path)


def id_column(id: str) -> str:
    """The track column an artist, album or track id is in."""
    if id.startswith(IDS["artist"]):
        return "artist_id"
    if id.startswith(IDS["album"]):
        return "album_id"
    return "id"


def name_id(name: str) -> str:
    """The id an artist name gives, before merges (artist_alias) and MusicBrainz ids (artist_id) have their say."""
    return make_id(IDS["artist"], name)


def artist_id(db, name: str, mbid: str | None = None) -> str:
    """The artist's oasis id. A MusicBrainz artist keeps the id it first got, so its spellings share one and two
    artists of the same name get two; a name alone follows the merges in artist_alias, else is the artist another
    spelling of it is (artist_key)."""
    id = name_id(name)
    if not mbid:
        if (row := db.execute("select artist_id from artist_alias where id = ?", (id,)).fetchone()) or not (key := fold(name)):
            return row[0] if row else id
        db.execute("insert or ignore into artist_key values (?, ?)", (key, id))
        return db.execute("select artist_id from artist_key where key = ?", (key,)).fetchone()[0]
    if row := db.execute("select artist_id from mb_artist where mbid = ?", (mbid,)).fetchone():
        return resolve(db, row[0], config_only=True)
    if db.execute("select 1 from mb_artist where artist_id = ?", (id,)).fetchone():
        id = make_id(IDS["artist"], name, mbid)  # another artist of this name came first
    db.execute("insert into mb_artist(mbid, artist_id, name) values (?, ?, ?)", (mbid, id, name))
    db.execute("delete from artist_alias where id = ? and source = 'musicbrainz'", (id,))  # the name is its own now
    return resolve(db, id, config_only=True)


def resolve(db, id: str, config_only=False) -> str:
    row = db.execute("select artist_id from artist_alias where id = ? and (? or source = 'config')", (id, not config_only)).fetchone()
    return row[0] if row else id


# SQL: the track's artist or album artist is the name given, but for case. It takes the name twice.
BY_ARTIST_NAME = "(artist = ? collate nocase or album_artist = ? collate nocase)"


def outside_names(r) -> tuple[str, str | None]:
    """The artist and album a track goes by for services outside oasis (Last.fm, ListenBrainz, lyrics providers).
    YouTube Music joins a song's artists into one string, and the services match better on the main one; an album
    named as the song (a single) is left out."""
    artist = r["album_artist"] if r["source"] == "ytm" else r["artist"]
    album = r["album"] if r["album"] != r["title"] else None
    return artist, album


def same_song(a: str, b: str) -> str:
    """SQL saying tracks a and b are one song: same artist, same title but for case, lengths within SAME_SONG_SECONDS.
    A song of unknown length matches none."""
    return (f"{a}.artist_id = {b}.artist_id and {a}.title = {b}.title collate nocase "
            f"and abs({a}.duration - {b}.duration) <= {SAME_SONG_SECONDS}")


def remember(db, row: dict) -> str:
    """Caches a streaming track's metadata and returns its id. The same song already known, local or under another
    YouTube upload, keeps its id; a local copy wins."""
    row["artist_id"] = artist_id(db, row["album_artist"]) if row["artist_id"] == name_id(row["album_artist"]) else resolve(db, row["artist_id"])
    known = db.execute(f"select t.id, t.source from track t, (select ? artist_id, ? title, ? duration) new "
                       f"where {same_song('t', 'new')} and t.id != ? order by t.source != 'local' limit 1",
                       (row["artist_id"], row["title"], row["duration"], row["id"])).fetchone()
    if known and not db.execute("select 1 from track where id = ?", (row["id"],)).fetchone():
        if known["source"] == "local":
            db.execute("update track set ytm_id = ? where id = ?", (row["ytm_id"], known["id"]))
        return known["id"]
    # The album a song was first seen on stays its own: other albums that list it hold it in album_track.
    kept = ("album", "album_id", "ytm_album", "track_no", "disc_no", "year")  # a playlist's sighting carries no year
    updates = ", ".join(f"{k} = coalesce(track.{k}, excluded.{k})" if k in kept else f"{k} = excluded.{k}"
                        for k in row if k not in ("id", "library", "created"))
    db.execute(f"insert into track({', '.join(row)}) values ({', '.join('?' * len(row))}) "
               f"on conflict(id) do update set {updates}", list(row.values()))
    return row["id"]


def adopt_local_copies(db):
    """Points streams that now have a local copy at the local file: playlist entries, stars and plays move over and the
    stream entry goes, so nothing keeps streaming what's on disk."""
    pairs = db.execute(f"select y.id stream, y.ref video, l.id local from track y join track l on l.source = 'local' "
                       f"and {same_song('l', 'y')} where y.source = 'ytm'").fetchall()
    for stream, video, local in pairs:
        db.execute("update track set ytm_id = ? where id = ? and coalesce(ytm_id, '') = ''", (video, local))
        move_track(db, stream, local)
    return len(pairs)


def move_track(db, old: str, new: str):
    """Moves playlist entries, album listings, plays, stars and ratings from one track to another and drops the old
    one; its id keeps working."""
    move(db, "track", old, new)
    keep_old_id(db, old, new)


def move(db, kind: str, old: str, new: str):
    """Points everything that refers to an id of this kind (track, album or artist) at another. Where the new id
    already has a row of its own, the old one's goes; stars, ratings and plays add up."""
    for table, column in REFERENCES[kind]:
        db.execute(f"update or ignore {table} set {column} = ? where {column} = ?", (new, old))
        db.execute(f"delete from {table} where {column} = ?", (old,))
    move_stat(db, old, new)
    for table, column in OWN_ROWS[kind]:
        db.execute(f"delete from {table} where {column} = ?", (old,))


def forget(db, ids: list[str]):
    """Drops tracks no longer in the library, with the rows about them alone."""
    for table, column in OWN_ROWS["track"]:
        db.executemany(f"delete from {table} where {column} = ?", [(id,) for id in ids])


def keep_old_id(db, old: str, new: str):
    """Lets a moved id keep working as the one it moved to, since clients keep ids in queues and caches."""
    db.execute("update moved set new_id = ? where new_id = ?", (new, old))
    db.execute("insert or replace into moved values (?, ?)", (old, new))


def move_stat(db, old: str, new: str):
    """Each user's stars, ratings and plays of one id added to their own of another."""
    db.execute("insert into stat(user, id, starred, rating, play_count, played) select user, ?, starred, rating, play_count, played "
               "from stat where id = ? on conflict(user, id) do update set starred = coalesce(stat.starred, excluded.starred), "
               "rating = coalesce(stat.rating, excluded.rating), play_count = stat.play_count + excluded.play_count, "
               "played = max(coalesce(stat.played, 0), coalesce(excluded.played, 0))", (new, old))
    db.execute("delete from stat where id = ?", (old,))


def artist_name_of(db, artist_id: str) -> str | None:
    """An artist's name by its oasis id, as the library's files give it, else YouTube Music; None when neither has it."""
    row = db.execute("select album_artist from track where artist_id = ? limit 1", (artist_id,)).fetchone() or \
        db.execute("select name from ytm_artist where artist_id = ?", (artist_id,)).fetchone()
    return row and row[0]


def album_name_of(db, album_id: str):
    """An album's artist (name, artist_id) and title by its oasis id, as the library's files give them, else YouTube
    Music; None when neither has it."""
    return db.execute("select album_artist name, album title, artist_id from track where album_id = ? limit 1", (album_id,)).fetchone() or \
        db.execute("select a.name, r.title, r.artist_id from ytm_release r join ytm_artist a on a.artist_id = r.artist_id "
                   "where r.album_id = ?", (album_id,)).fetchone()


def mbid_of(id: str) -> str | None:
    return next((id.removeprefix(p) for p in MB_IDS.values() if id.startswith(p)), None)


def library_id(db, id: str) -> str | None:
    """The library's id of a MusicBrainz-only artist or album that has arrived."""
    if id.startswith(MB_IDS["album"]):
        row = db.execute("select album_id from track where mb_release_group = ? and source = 'local' limit 1", (mbid_of(id),)).fetchone()
        return row and row[0]
    if id.startswith(MB_IDS["artist"]) and (row := db.execute("select artist_id from mb_artist where mbid = ?", (mbid_of(id),)).fetchone()):
        own = resolve(db, row[0])
        return own if db.execute("select 1 from track where artist_id = ? and source = 'local'", (own,)).fetchone() else None
    return None


def current_id(db, id: str) -> str:
    """The id that id, which a client may have kept, names now: the one it moved to (keep_old_id), else the library's
    once the MusicBrainz-only artist or album it names has arrived, else id itself. A track that is back keeps its id."""
    if db.execute("select 1 from track where id = ?", (id,)).fetchone():
        return id
    if row :=db.execute("select new_id from moved where old_id = ?", (id,)).fetchone():
        return row[0]
    return library_id(db, id) or id


def mb_only(column: str) -> str:
    """SQL: the column holds an id of an artist or release group only MusicBrainz has."""
    return " or ".join(f"{column} like '{prefix}%'" for prefix in MB_IDS.values())
