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
    album_by text,                  -- streaming tracks: the album's artist as its YouTube Music album page names it
    artist_id text, album_id text,
    track_no int, disc_no int, year int, genre text,
    duration int, bitrate int, size int, suffix text, content_type text,
    cover text,                     -- 'embedded', an image path or URL, or null
    ytm_id text,                    -- YouTube Music videoId, also for local tracks matched to one; '' when none exists
    ytm_album text,                 -- YouTube Music album browse id
    library int not null default 1, -- 0 for streaming tracks only seen in search or radio
    mtime real, created real,
    mb_release text, mb_track text, mb_recording text,  -- MusicBrainz release, release track and recording ids
    mb_release_group text,
    isrc text, label text, release_types text, original_date text, -- several values joined with '; '
    genres text,                    -- JSON list
    credits_at real                 -- when MusicBrainz credits were filled; -1 when it has none, null while pending
);
create index if not exists track_album on track(album_id);
create index if not exists track_artist on track(artist_id);
create table if not exists credit(  -- who made each track, as credited
    track_id text not null, role text not null,  -- artist, albumartist, performer, composer, lyricist, writer, arranger...
    sub_role text,                  -- performers: instrument or voice
    name text not null,             -- as credited: a persona or character stays one
    artist_name text, artist_id text, mbid text, artist_type text, position int, joinphrase text,
    source text not null            -- musicbrainz or tags
);
create index if not exists credit_track on credit(track_id);
create index if not exists credit_artist on credit(artist_id);
create table if not exists mb_artist(  -- MusicBrainz artists and the oasis artist each one is
    mbid text primary key, artist_id text not null, name text not null, type text,
    fetched real                    -- when its other names and links were read; -1 when MusicBrainz lacks it
);
create table if not exists artist_alias(  -- another name's id -> the artist it belongs to
    id text primary key, artist_id text not null,
    source text not null            -- musicbrainz, or config (the user's merges; a name mapped to itself stays separate)
);
create table if not exists artist_link(  -- separate artists close to each other, either way round
    artist_id text not null, other_id text not null,
    kind text not null,             -- persona (one person's projects), related (the user's groups), member (bands)
    source text not null, primary key(artist_id, other_id, kind)
);
create index if not exists artist_link_other on artist_link(other_id);
create table if not exists setting(key text primary key, value text);  -- settings made on the dashboard
create table if not exists stat(  -- each user's stars, ratings and plays, as Subsonic keeps them
    user text not null, id text not null,  -- id: any track, album or artist id
    starred real, rating int, play_count int not null default 0, played real, primary key(user, id)
);
create index if not exists stat_id on stat(id);
create table if not exists playlist(
    id integer primary key, name text not null, owner text not null, comment text,
    created real not null, changed real not null,
    radio int not null default 0,   -- 1: grows from its own tracks, 2: grows from everything played
    played_pos int not null default -1,
    mix text,                       -- YouTube Music playlist id (mix or library playlist); such playlists are read-only
    cover text,                     -- image URL
    hidden int not null default 0,  -- 1: a YouTube Music library playlist its owner chose not to show (web.py): no client sees it
    radio_source text,              -- a radio's source (recommend.SOURCES); null: its owner's default
    station text, period text       -- a station's kind (recommend.STATIONS) and Last.fm period: seeds from the owner's Last.fm
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
    listenbrainz_token text,
    ytm_new_shown int not null default 0, -- YouTube Music library playlists that appear later: 1 shown till unticked, 0 hidden till ticked
    radio_source text not null default 'all'  -- the default source of similar songs and radios (recommend.SOURCES)
);
create table if not exists wanted(user text not null, kind text not null, ref text not null, primary key(user, kind, ref));
create table if not exists want_item(  -- stars and follows (see acquire.py)
    kind text not null, ref text not null,  -- 'album' or 'artist', and its oasis id
    state text not null,                    -- wanted, downloading, have, followed (artists), failed
    mbid text, error text,                  -- error: why it isn't downloading, for clients to show
    searched real,                          -- albums: last search; artists: when the back catalogue was monitored
    retry_at real, updated real not null,
    catalogued real,                        -- artists: when oasis last read their catalogue from MusicBrainz (see acquire.py)
    primary key(kind, ref)
);
create table if not exists lidarr_rescue(  -- unused since Lidarr left; kept for a rollback
    hash text primary key, album_id int not null, grabbed real not null
);
create table if not exists download(  -- releases oasis downloads through TorBox itself (see download.py)
    hash text primary key, guid text,       -- info hash; the indexer's id of the release
    album_id int not null, album_mbid text, -- Lidarr's album (0 since Lidarr left), and the release group
    title text, torbox_id int,
    state text not null,                    -- added, downloading, fetching, importing, done, failed
    error text, created real not null, updated real not null,
    bundle text,                            -- an uncached bundle's search (JSON: want, groups, wanted) until its targets are known
    picks text,                             -- an album's search's next picks (JSON), for a slow download to make way for (see download.py)
    catalogue text,                         -- a bundle's artist search (JSON: want, groups), to remember all it holds (bundle_kept)
    kept int not null default 0             -- 1: sent from a remembered bundle (bundle_kept)
);
create table if not exists download_target(  -- a bundle's albums, fetched and imported from one download (see download.py)
    hash text not null, rg text not null,   -- the download; the album's release group
    files text not null,                    -- its paths inside the torrent (JSON)
    state text not null,                    -- pending, done, failed; expected: one it likely holds, till its files are known
    primary key(hash, rg)
);
create table if not exists bundle_kept(  -- bundles oasis has had, to fetch albums from again while TorBox has them cached (see download.py)
    hash text primary key, title text not null, artist_mbid text,
    files text not null,                    -- the torrent's files (JSON: [{name, size}])
    groups text not null,                   -- every release group its folders hold, wanted or not (JSON: {group: [paths]})
    seen real not null,                     -- when TorBox last had it cached
    tried text not null default '[]'        -- the groups fetched from it, imported or failed: never again from it (JSON)
);
create table if not exists bundle_search(artist_mbid text primary key, searched real not null);  -- see acquire.py
create table if not exists mb_cache(url text primary key, body text not null, fetched real not null);  -- see musicbrainz.py
create table if not exists release_block(  -- releases that failed: never picked again, but slow ones last. hash: info hash, or guid for those listed without one
    hash text primary key, reason text, at real not null
);
create table if not exists audio_pair(  -- dedupe verdicts, valid while both files are unchanged
    a text not null, b text not null, a_mtime real not null, b_mtime real not null, level int not null, primary key(a, b)
);
create table if not exists audio_shared(  -- files dedupe made reflinks of a kept copy
    path text primary key, kept text not null, mtime real not null, kept_mtime real not null
);
create table if not exists audio_quality(  -- what each library FLAC really holds, as of mtime (see quality.py)
    path text primary key, mtime real not null, checked real not null,
    rate int, bits int,                     -- the file as it is now
    bits_used int, low_share real,          -- bits its samples use; the share of samples using the low 8 of 24
    cutoff int, drop_db real, sustained int, -- the spectrum's hard cutoff (Hz), its drop (dB), whether every loud stretch shows it
    verdict text,                           -- padded, upsampled, lossy, or null
    borderline text,                        -- the same kinds near the thresholds: recorded, never acted on
    source_rate int,                        -- upsampled: the rate the cutoff implies
    action text,                            -- resampled, converted, recompressed, kept, shared (left: dedupe shares it), failed
    was text,                               -- before a conversion: '24-bit 96 kHz'
    saved int not null default 0            -- bytes the action saved
);
create table if not exists catalogue_search(  -- the albums Lidarr searched and looked for, by Lidarr id: carried over, read only
    album_id int primary key, searched real not null,
    mbid text                               -- the release group, for oasis's own catalogue to carry the search over
);
create table if not exists want_album(  -- albums oasis looks for: starred ones and followed artists' catalogues (see acquire.py)
    rg text primary key,                    -- MusicBrainz release group
    artist_mbid text not null, artist text, title text not null,
    primary_type text, secondary_types text, first_date text,  -- secondary types joined with '; '
    reason text not null,                   -- starred, followed
    state text not null,                    -- missing, downloading, have, skipped
    searched real, next_search real, tries int not null default 0,
    error text                              -- why it is skipped, or what its last search found
);
create index if not exists want_album_artist on want_album(artist_mbid);
create table if not exists given_up(rg text primary key, at real not null);  -- albums users gave up on: followed artists' catalogues leave them out
create table if not exists mb_group(  -- each release group's smallest official edition, read from MusicBrainz (see acquire.py)
    rg text primary key, tracks int not null, discs int not null,  -- tracks: 0 when it has no official edition; discs: the most of any
    songs text not null, fetched real not null  -- the smallest edition's song keys (JSON)
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
create table if not exists album_track(  -- the songs each YouTube Music album lists: a song is on its album and its single too
    album_id text not null, track_id text not null, disc_no int, track_no int, primary key(album_id, track_id)
);
create index if not exists play_track on play(track_id);
create table if not exists session(token text primary key, user text not null, expires real not null);
create table if not exists lyrics(track_id text primary key, found text, fetched real not null);
create table if not exists mb_latin(  -- Latin names MusicBrainz's editors chose, preferred over generated ones (see latin.py)
    kind text not null, original text not null,  -- artist: a credited name; title: an album's or track's title
    latin text not null, ref text not null,      -- ref: the artist's or release's MusicBrainz id
    primary key(kind, original)
);
create table if not exists mb_latin_seen(ref text primary key, fetched real not null);  -- artists and releases latin.py read
create table if not exists track_latin(  -- library tracks' names romanized and folded, for search to match romaji (see api.py)
    id text primary key, source text not null,  -- source: the names they were made from
    title text, artist text, album text, album_artist text
);
create table if not exists upload(  -- uploads in progress (see upload.py); release: MusicBrainz's JSON, else tags: the client's
    id text primary key, user text not null, release text, disc int, tags text, touched real not null
);
create table if not exists upload_track(  -- the tracks an upload expects; the file is <uploads>/<upload>/<disc>-<number>.<suffix>
    upload text not null references upload(id) on delete cascade, disc int not null, number int not null,
    suffix text, size int, received int not null default 0, primary key(upload, disc, number)
);
create table if not exists playlist_entry(
    playlist_id int not null references playlist(id) on delete cascade,
    pos int not null, track_id text not null
);
"""


def make_id(prefix: str, *parts: str) -> str:
    return prefix + hashlib.sha1("\0".join(p.casefold() for p in parts).encode()).hexdigest()[:16]


def artist_id(db, name: str, mbid: str | None = None) -> str:
    """The artist's oasis id. A MusicBrainz artist keeps the id it first got, so its spellings share one and two
    artists of the same name get two; a name alone follows the merges in artist_alias."""
    id = make_id("ar-", name)
    if not mbid:
        return resolve(db, id)
    if row := db.execute("select artist_id from mb_artist where mbid = ?", (mbid,)).fetchone():
        return resolve(db, row[0], config_only=True)
    if db.execute("select 1 from mb_artist where artist_id = ?", (id,)).fetchone():
        id = make_id("ar-", name, mbid)  # another artist of this name came first
    db.execute("insert into mb_artist(mbid, artist_id, name) values (?, ?, ?)", (mbid, id, name))
    db.execute("delete from artist_alias where id = ? and source = 'musicbrainz'", (id,))  # the name is its own now
    return resolve(db, id, config_only=True)


def resolve(db, id: str, config_only=False) -> str:
    row = db.execute("select artist_id from artist_alias where id = ? and (? or source = 'config')", (id, not config_only)).fetchone()
    return row[0] if row else id


migrated: set[Path] = set()


def connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=30, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("pragma journal_mode=wal")
    db.execute("pragma foreign_keys=on")
    db.create_function("me", 0, lambda: None, deterministic=True)  # the user a request is for: api.py sets it, nobody elsewhere
    if path not in migrated:
        rename(db)
        db.executescript(SCHEMA)
        migrate(db)
        own_stats(db)
        db.execute("create index if not exists track_release on track(mb_release)")  # on columns migrate may add
        db.execute("create index if not exists track_group on track(mb_release_group)")
        # One radio that follows all listening per user (recommend.played): a race once made two; the extra stays as a plain radio.
        db.execute("update playlist set radio = 1 where radio = 2 and id not in (select min(id) from playlist where radio = 2 group by owner)")
        db.execute("create unique index if not exists one_radio on playlist(owner) where radio = 2")
        migrated.add(path)
    return db


def rename(db: sqlite3.Connection):
    """Gives tables of an older oasis their current names, rows and all, before SCHEMA creates them empty."""
    tables = {row[0] for row in db.execute("select name from sqlite_master where type = 'table'")}
    if "lidarr_item" in tables and "want_item" not in tables:  # named from when Lidarr kept the stars and follows
        db.execute("alter table lidarr_item rename to want_item")
        db.execute("alter table want_item drop column lidarr_id")
    if "stat" in tables and "user" not in {row[1] for row in db.execute("pragma table_info(stat)")}:  # from when stats were global
        db.execute("alter table stat rename to stat_global")


def own_stats(db: sqlite3.Connection):
    """Stars, ratings and plays from when they were global go to every admin, or every user when there is no admin,
    so whoever had them keeps them; with no users yet they wait in stat_global."""
    if not db.execute("select 1 from sqlite_master where name = 'stat_global'").fetchone():
        return
    users = [r[0] for r in db.execute("select name from user where admin = 1")] or [r[0] for r in db.execute("select name from user")]
    if users:
        db.executemany("insert or ignore into stat select ?, id, starred, rating, play_count, played from stat_global", [(u,) for u in users])
        db.execute("drop table stat_global")


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
                if table == "track":
                    db.execute("update track set mtime = null where source = 'local'")  # the next scan reads files for it
    current.close()


def remember(db, row: dict) -> str:
    """Caches a streaming track's metadata and returns its id. The same song already known (same artist and title,
    length within 5 s), local or under another YouTube upload, keeps its id; a local copy wins."""
    row["artist_id"] = resolve(db, row["artist_id"])
    known = db.execute("select id, source from track where artist_id = ? and title = ? collate nocase and abs(duration - ?) <= 5 "
                       "and id != ? order by source != 'local' limit 1",
                       (row["artist_id"], row["title"], row["duration"] or 0, row["id"])).fetchone()
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


def move_track(db, old: str, new: str):
    """Moves playlist entries, plays, stars and ratings from one track to another and drops the old one; its id
    keeps working as an alias, since clients keep ids in queues and caches."""
    for table in ("playlist_entry", "play", "outbox"):
        db.execute(f"update {table} set track_id = ? where track_id = ?", (new, old))
    move_stat(db, old, new)
    for table, column in (("lyrics", "track_id"), ("track", "id")):
        db.execute(f"delete from {table} where {column} = ?", (old,))
    db.execute("update moved set new_id = ? where new_id = ?", (new, old))
    db.execute("insert or replace into moved values (?, ?)", (old, new))


def move_stat(db, old: str, new: str):
    """Each user's stars, ratings and plays of one id added to their own of another."""
    db.execute("insert into stat(user, id, starred, rating, play_count, played) select user, ?, starred, rating, play_count, played "
               "from stat where id = ? on conflict(user, id) do update set starred = coalesce(stat.starred, excluded.starred), "
               "rating = coalesce(stat.rating, excluded.rating), play_count = stat.play_count + excluded.play_count, "
               "played = max(coalesce(stat.played, 0), coalesce(excluded.played, 0))", (new, old))
    db.execute("delete from stat where id = ?", (old,))


def adopt_local_copies(db):
    """Points streams that now have a local copy (same artist and title, length within 5 s) at the local file: playlist
    entries, stars and plays move over and the stream entry goes, so nothing keeps streaming what's on disk."""
    pairs = db.execute("select y.id stream, y.ref video, l.id local from track y join track l on l.source = 'local' "
                       "and l.artist_id = y.artist_id and l.title = y.title collate nocase "
                       "and abs(coalesce(l.duration, 0) - coalesce(y.duration, 0)) <= 5 where y.source = 'ytm'").fetchall()
    for stream, video, local in pairs:
        db.execute("update track set ytm_id = ? where id = ? and coalesce(ytm_id, '') = ''", (video, local))
        move_track(db, stream, local)
    return len(pairs)
