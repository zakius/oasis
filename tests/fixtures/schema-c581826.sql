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
create table if not exists lidarr_item(  -- stars and follows (see acquire.py); named from Lidarr, kept so a rollback finds them
    kind text not null, ref text not null,  -- 'album' or 'artist', and its oasis id
    state text not null,                    -- wanted, downloading, have, followed (artists), failed
    mbid text, lidarr_id int, error text,   -- error: why it isn't downloading, for clients to show
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
    error text, created real not null, updated real not null
);
create table if not exists download_target(  -- a bundle's albums, fetched and imported from one download (see download.py)
    hash text not null, rg text not null,   -- the download; the album's release group
    files text not null,                    -- its paths inside the torrent (JSON)
    state text not null,                    -- pending, done, failed
    primary key(hash, rg)
);
create table if not exists bundle_search(artist_mbid text primary key, searched real not null);  -- see acquire.py
create table if not exists mb_cache(url text primary key, body text not null, fetched real not null);  -- see musicbrainz.py
create table if not exists release_block(  -- releases that failed: never picked again. hash: info hash, or guid for those listed without one
    hash text primary key, reason text, at real not null
);
create table if not exists audio_pair(  -- dedupe verdicts, valid while both files are unchanged
    a text not null, b text not null, a_mtime real not null, b_mtime real not null, level int not null, primary key(a, b)
);
create table if not exists audio_shared(  -- files dedupe made reflinks of a kept copy
    path text primary key, kept text not null, mtime real not null, kept_mtime real not null
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
create index if not exists play_track on play(track_id);
create table if not exists session(token text primary key, user text not null, expires real not null);
create table if not exists lyrics(track_id text primary key, found text, fetched real not null);
create table if not exists playlist_entry(
    playlist_id int not null references playlist(id) on delete cascade,
    pos int not null, track_id text not null
);
