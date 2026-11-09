import sqlite3
from pathlib import Path

from .tracks import artist_id

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
create index if not exists track_title on track(title collate nocase);  -- songs found by artist and title (recommend.resolve)
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
create table if not exists artist_key(  -- each folded artist name (text.fold) -> the artist all its spellings are
    key text primary key, artist_id text not null
);
create table if not exists spelling(  -- artist names shown as another spelling of the same name (artists.unify)
    variant text primary key, spelled text not null
);
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
    hidden int not null default 0,  -- 1: a YouTube Music library playlist its owner chose not to show (mixes.py): no client sees it
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
    radio_source text not null default 'all', -- the default source of similar songs and radios (recommend.SOURCES)
    translate_lang text             -- the language lyrics are translated to when an app asks with oasisTranslate=1
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
    kept int not null default 0,            -- 1: sent from a remembered bundle (bundle_kept)
    tracker text,                           -- the indexer it came from (Prowlarr's name), whose seeding rule and totals it counts toward; null before oasis noted it
    finished real,                          -- when TorBox had it whole: seeding's clock
    uploaded int, downloaded int,           -- TorBox's last totals for it, kept after it is deleted: the tracker's running ratio
    seeding int not null default 0,         -- 1: imported and still seeding on TorBox (see download.py)
    mode text,                              -- seeding as sent to TorBox: torbox (its account's setting), seed (oasis stops it), off; null: seed if tracker, else off
    indexer int                             -- the Prowlarr indexer it came from; null before oasis noted it (by_indexer goes by the guid's site then)
);
create table if not exists search_stat(  -- each indexer's answers to album searches, a day at a time: results, the album, usable
    day text not null, indexer int not null, results int not null, album int not null, usable int not null, primary key(day, indexer)
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
create table if not exists seed_rule(  -- an indexer's seeding rule (Prowlarr's name), as the indexers page sets it: over oasis.toml's and the default (download.py)
    tracker text primary key, mode text not null, ratio real not null, days real not null, min_hours real not null, min_ratio real
);
create table if not exists release_block(  -- releases that failed: never picked again, but slow and long-dead ones last (see download.py). hash: info hash, or guid for those listed without one
    hash text primary key, reason text, at real not null,
    artist_mbid text, wanted text   -- a bundle that held no wanted album: its artist, and the albums wanted then (JSON)
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
    state text not null,                    -- missing, downloading, have
    searched real, next_search real, tries int not null default 0,
    error text                              -- what its last search found
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
create table if not exists moved(old_id text primary key, new_id text not null);  -- old ids clients may still hold: streams now local, MusicBrainz-only artists and albums that arrived
create table if not exists play(user text not null, track_id text not null, played real not null);
create table if not exists album_track(  -- the songs each YouTube Music album lists: a song is on its album and its single too
    album_id text not null, track_id text not null, disc_no int, track_no int, primary key(album_id, track_id)
);
create index if not exists play_track on play(track_id);
create table if not exists session(token text primary key, user text not null, expires real not null);
create table if not exists lyrics(track_id text primary key, found text, fetched real not null);
create table if not exists lyrics_translation(  -- machine translations of lyrics (translate.py), made when an app asked
    track_id text not null, lang text not null,
    original text not null,         -- the lines translated, as JSON: other lines are translated again
    lines text,                     -- JSON, one per line; null: the lyrics are in that language already
    primary key(track_id, lang)
);
create table if not exists mb_latin(  -- Latin names MusicBrainz's editors chose, preferred over generated ones (see latin.py)
    kind text not null, original text not null,  -- artist: a credited name; title: an album's or track's title
    latin text not null, ref text not null,      -- ref: the artist's or release's MusicBrainz id
    primary key(kind, original)
);
create table if not exists mb_latin_seen(ref text primary key, fetched real not null);  -- artists and releases latin.py read
create table if not exists track_latin(  -- library tracks' names romanized and folded, for search to match romaji (see latin.index)
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


migrated: set[Path] = set()


def connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=30, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("pragma journal_mode=wal")
    db.execute("pragma foreign_keys=on")
    as_user(db, None)
    if path not in migrated:
        for step in MIGRATIONS:
            step(db)
        migrated.add(path)
    return db


def as_user(db: sqlite3.Connection, user: str | None):
    """Names the user that SQL's me() stands for on this connection. Stars, ratings and plays are each user's, and
    queries join them with stat.user = me() (api.py's SONGS, ALBUMS and ARTISTS): a Subsonic request sets its signed-in
    user before its queries run. A new connection is for no user, so it joins nobody's."""
    db.create_function("me", 0, lambda: user, deterministic=True)


def rename(db: sqlite3.Connection):
    """Gives tables of an older oasis their current names, rows and all, before SCHEMA creates them empty."""
    tables = {row[0] for row in db.execute("select name from sqlite_master where type = 'table'")}
    if "lidarr_item" in tables and "want_item" not in tables:  # named from when Lidarr kept the stars and follows
        db.execute("alter table lidarr_item rename to want_item")
        db.execute("alter table want_item drop column lidarr_id")
    if "seed_rule" in tables and "seed" in {row[1] for row in db.execute("pragma table_info(seed_rule)")}:  # from when a rule only seeded or not
        db.execute("alter table seed_rule rename column seed to mode")
        db.execute("update seed_rule set mode = case mode when 1 then 'seed' else 'off' end")
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


def lyrics_with_words(db: sqlite3.Connection):
    """Forgets karaoke lyrics cached before their words were kept, so they are fetched again with them."""
    db.execute("""delete from lyrics where found like '%"word_level": true%' and found not like '%"words"%'""")


def one_listening_radio(db: sqlite3.Connection):
    """Makes the database keep one listening radio (playlist.radio 2) per user with the unique index one_radio. A race
    once made two; the later ones stay as radios of their own tracks (1)."""
    db.execute("update playlist set radio = 1 where radio = 2 and id not in (select min(id) from playlist where radio = 2 group by owner)")
    db.execute("create unique index if not exists one_radio on playlist(owner) where radio = 2")


def reidentify(db: sqlite3.Connection):
    """Gives MusicBrainz credits the id their artist has: credits from before artists were told apart by MusicBrainz id
    share one per name."""
    for mbid, name, old in db.execute("select mbid, artist_name, artist_id from credit where mbid is not null group by mbid").fetchall():
        if (new := artist_id(db, name, mbid)) != old:
            db.execute("update credit set artist_id = ? where mbid = ?", (new, mbid))


def drop_left_out(db: sqlite3.Connection):
    """Drops the rows an older oasis kept for albums a catalogue left out or a user gave up on (state skipped): those
    aren't wanted, and have no row."""
    db.execute("delete from want_album where state = 'skipped'")


def set_stat(db: sqlite3.Connection, user: str, id: str, column: str, value):
    """Sets the user's starred or rating of a track, album or artist."""
    db.execute(f"insert into stat(user, id, {column}) values (?, ?, ?) on conflict(user, id) do update set {column} = excluded.{column}",
               (user, id, value))


def record_play(db: sqlite3.Connection, user: str, track_id: str, played: float):
    """Counts a play of a track by the user, and keeps when it was for the radios' played-together."""
    db.execute("insert into stat(user, id, play_count, played) values (?, ?, 1, ?) on conflict(user, id) do update "
               "set play_count = play_count + 1, played = excluded.played", (user, track_id, played))
    db.execute("insert into play values (?, ?, ?)", (user, track_id, played))


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


def create(db: sqlite3.Connection):
    """SCHEMA's tables, with the columns an older oasis's lack (migrate) and the indexes on those."""
    db.executescript(SCHEMA)
    migrate(db)
    db.execute("create index if not exists track_release on track(mb_release)")
    db.execute("create index if not exists track_group on track(mb_release_group)")
    # The library's albums and artists, listed without reading the streaming tracks beside them (api.py's ALBUMS and ARTISTS)
    db.execute("create index if not exists track_library_album on track(library, album_id)")
    db.execute("create index if not exists track_library_artist on track(library, artist_id)")


# What a database from any older oasis needs, in order, each time a process first connects to it; each step does
# nothing once done.
MIGRATIONS = [rename, create, own_stats, lyrics_with_words, one_listening_radio, reidentify, drop_left_out]
