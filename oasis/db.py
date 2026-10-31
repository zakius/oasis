import hashlib
import sqlite3
from pathlib import Path

SCHEMA = """
create table if not exists track(
    id text primary key,
    source text not null,           -- 'local' or 'ytm'
    ref text not null unique,       -- file path, or the videoId for 'ytm'
    root text,
    title text, artist text, album_artist text, album text,
    artist_id text, album_id text,
    track_no int, disc_no int, year int, genre text,
    duration int, bitrate int, size int, suffix text, content_type text,
    cover text,                     -- 'embedded', an image path or URL, or null
    ytm_id text,                    -- YouTube Music videoId, also for local tracks matched to one; '' when none exists
    ytm_album text,                 -- YouTube Music album browse id
    library int not null default 1, -- 0 for streaming tracks only seen in search or radio
    mtime real, created real
);
create index if not exists track_album on track(album_id);
create index if not exists track_artist on track(artist_id);
create table if not exists stat(
    id text primary key,            -- any track, album or artist id
    starred real, rating int, play_count int not null default 0, played real
);
create table if not exists playlist(
    id integer primary key, name text not null, owner text not null, comment text,
    created real not null, changed real not null,
    radio int not null default 0,   -- 1: grows from its own tracks, 2: grows from everything played
    played_pos int not null default -1,
    mix text,                       -- YouTube Music playlist id (mix or library playlist); such playlists are read-only
    cover text                      -- image URL
);
create table if not exists outbox(
    id integer primary key, user text not null, service text not null, track_id text not null, played real not null,
    attempts int not null default 0, next_try real not null
);
create table if not exists user(
    name text primary key, password text not null, admin int not null default 0, created real not null,
    ytm_auth text,                  -- browser headers (JSON) for ytmusicapi
    ytm_name text,
    lastfm_session text, lastfm_name text,
    listenbrainz_token text
);
create table if not exists wanted(user text not null, kind text not null, ref text not null, primary key(user, kind, ref));
create table if not exists lidarr_item(
    kind text not null, ref text not null,  -- 'album' or 'artist', and its oasis id
    state text not null,                    -- wanted, sent, downloading, have, followed (artists), failed
    mbid text, lidarr_id int, error text,   -- error: why it isn't downloading, for clients to show
    searched real, retry_at real, updated real not null,
    primary key(kind, ref)
);
create table if not exists ytm_artist(  -- an artist's YouTube Music page, kept for a day
    artist_id text primary key, name text, description text, image text, songs text, fetched real not null
);
create table if not exists ytm_release(  -- albums and singles from YouTube Music artist pages
    album_id text primary key, artist_id text not null, browse_id text not null,
    title text, year int, type text, cover text
);
create table if not exists moved(old_id text primary key, new_id text not null);  -- stream ids now local
create table if not exists play(user text not null, track_id text not null, played real not null);
create index if not exists play_track on play(track_id);
create table if not exists session(token text primary key, user text not null, expires real not null);
create table if not exists lyrics(track_id text primary key, found text, fetched real not null);
create table if not exists playlist_entry(
    playlist_id int not null references playlist(id) on delete cascade,
    pos int not null, track_id text not null
);
"""


def make_id(prefix: str, *parts: str) -> str:
    return prefix + hashlib.sha1("\0".join(p.casefold() for p in parts).encode()).hexdigest()[:16]


migrated: set[Path] = set()


def connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=30, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("pragma journal_mode=wal")
    db.execute("pragma foreign_keys=on")
    if path not in migrated:
        db.executescript(SCHEMA)
        migrate(db)
        migrated.add(path)
    return db


def migrate(db: sqlite3.Connection):
    """Adds the columns a database from an older oasis lacks, as SCHEMA defines them."""
    current = sqlite3.connect(":memory:")
    current.executescript(SCHEMA)
    for (table,) in current.execute("select name from sqlite_master where type = 'table'"):
        have = {row[1] for row in db.execute(f"pragma table_info({table})")}
        for _, name, kind, not_null, default, _ in current.execute(f"pragma table_info({table})"):
            if name not in have:
                # SQLite adds a NOT NULL column only with a default.
                constraint = (f" not null default {default}" if not_null else f" default {default}") if default is not None else ""
                db.execute(f"alter table {table} add column {name} {kind}{constraint}")
    current.close()


def remember(db, row: dict) -> str:
    """Caches a streaming track's metadata and returns its id. The same song already known (same artist and title,
    length within 5 s), local or under another YouTube upload, keeps its id; a local copy wins."""
    known = db.execute("select id, source from track where artist_id = ? and title = ? collate nocase and abs(duration - ?) <= 5 "
                       "and id != ? order by source != 'local' limit 1",
                       (row["artist_id"], row["title"], row["duration"] or 0, row["id"])).fetchone()
    if known and not db.execute("select 1 from track where id = ?", (row["id"],)).fetchone():
        if known["source"] == "local":
            db.execute("update track set ytm_id = ? where id = ?", (row["ytm_id"], known["id"]))
        elif row.get("track_no"):  # an album listing places the song on its album
            db.execute("update track set album = ?, album_id = ?, ytm_album = ?, track_no = ?, year = coalesce(?, year), "
                       "cover = coalesce(?, cover) where id = ?", (row["album"], row["album_id"], row["ytm_album"],
                       row["track_no"], row["year"], row["cover"], known["id"]))
        return known["id"]
    updates = ", ".join(f"{k} = excluded.{k}" for k in row if k not in ("id", "library", "created"))
    db.execute(f"insert into track({', '.join(row)}) values ({', '.join('?' * len(row))}) "
               f"on conflict(id) do update set {updates}", list(row.values()))
    return row["id"]


def adopt_local_copies(db):
    """Points streams that now have a local copy (same artist and title, length within 5 s) at the local file: playlist
    entries, stars and plays move over and the stream entry goes, so nothing keeps streaming what's on disk."""
    pairs = db.execute("select y.id stream, y.ref video, l.id local from track y join track l on l.source = 'local' "
                       "and l.artist_id = y.artist_id and l.title = y.title collate nocase "
                       "and abs(coalesce(l.duration, 0) - coalesce(y.duration, 0)) <= 5 where y.source = 'ytm'").fetchall()
    for stream, video, local in pairs:
        db.execute("update track set ytm_id = ? where id = ? and coalesce(ytm_id, '') = ''", (video, local))
        for table in ("playlist_entry", "play", "outbox"):
            db.execute(f"update {table} set track_id = ? where track_id = ?", (local, stream))
        db.execute("insert into stat(id, starred, rating, play_count, played) select ?, starred, rating, play_count, played "
                   "from stat where id = ? on conflict(id) do update set starred = coalesce(stat.starred, excluded.starred), "
                   "rating = coalesce(stat.rating, excluded.rating), play_count = stat.play_count + excluded.play_count, "
                   "played = max(coalesce(stat.played, 0), coalesce(excluded.played, 0))", (local, stream))
        for table, column in (("stat", "id"), ("lyrics", "track_id"), ("track", "id")):
            db.execute(f"delete from {table} where {column} = ?", (stream,))
        # Clients keep the old id in queues and caches; it keeps working as an alias.
        db.execute("update moved set new_id = ? where new_id = ?", (local, stream))
        db.execute("insert or replace into moved values (?, ?)", (stream, local))
    return len(pairs)
