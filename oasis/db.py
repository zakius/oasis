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
    ytm_id text,                    -- YouTube Music videoId, also for local tracks matched to one
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
    created real not null, changed real not null
);
create table if not exists playlist_entry(
    playlist_id int not null references playlist(id) on delete cascade,
    pos int not null, track_id text not null
);
"""


def make_id(prefix: str, *parts: str) -> str:
    return prefix + hashlib.sha1("\0".join(p.casefold() for p in parts).encode()).hexdigest()[:16]


def connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=30, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("pragma journal_mode=wal")
    db.execute("pragma foreign_keys=on")
    db.executescript(SCHEMA)
    return db
