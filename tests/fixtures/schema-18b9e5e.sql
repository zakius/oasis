create table if not exists track(
    id text primary key,
    source text not null,           -- 'local' now; streaming backends later
    ref text not null unique,       -- file path for local tracks
    root text,
    title text, artist text, album_artist text, album text,
    artist_id text, album_id text,
    track_no int, disc_no int, year int, genre text,
    duration int, bitrate int, size int, suffix text, content_type text,
    cover text,                     -- 'embedded', an image path, or null
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
