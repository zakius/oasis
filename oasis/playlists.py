"""Playlists' entries and kinds. A plain playlist holds what its owner or a YouTube Music mix put in it; a radio grows
by itself as it is played (see recommend.py), from its own tracks or from everything its owner plays. Streaming tracks
are in the library while starred or in a plain playlist that is not one of YouTube Music's own mixes, so writing
entries keeps that up."""
import time

from . import accounts
from .ytm import LIKED, MIX_PREFIX, is_mix

PLAIN, RADIO, LISTENING_RADIO = 0, 1, 2  # playlist.radio: a radio grows from its own tracks, a listening radio from everything played


def is_radio(playlist_name: str) -> int:
    """Playlists named "Radio ..." grow by themselves from the tracks played in them."""
    return RADIO if playlist_name.casefold().startswith("radio") else PLAIN


def create(db, owner: str, name: str, radio: int = PLAIN, comment: str | None = None, mix: str | None = None,
           cover: str | None = None, hidden: int = 0, source: str | None = None, station: str | None = None,
           period: str | None = None) -> int | None:
    """A new playlist's id, without entries; None when it would be the owner's second listening radio, which the index
    one_radio refuses (two plays at once can both find the owner has none). A radio's source, station and period: see
    recommend.py."""
    now = time.time()
    cursor = db.execute("insert or ignore into playlist(name, owner, comment, created, changed, radio, mix, cover, hidden, radio_source, station, period) "
                        "values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (name, owner, comment, now, now, radio, mix, cover, hidden, source, station, period))
    return cursor.lastrowid if cursor.rowcount else None


def rename(db, playlist_id, name: str):
    """Renames a playlist. A plain one named "Radio ..." becomes a radio with its owner's default source, and a radio
    renamed otherwise plain (is_radio); a listening radio stays one, as does a radio started with a source or station
    whatever its name, and a YouTube Music mix is never a radio."""
    r = db.execute("select * from playlist where id = ?", (playlist_id,)).fetchone()
    db.execute("update playlist set name = ?, changed = ? where id = ?", (name, time.time(), playlist_id))
    if r["mix"] is not None or r["radio"] == LISTENING_RADIO or (r["radio"] and not is_radio(r["name"])):
        return
    radio = is_radio(name)
    source = (r["radio_source"] or accounts.get(db, r["owner"])["radio_source"]) if radio else None
    db.execute("update playlist set radio = ?, radio_source = ? where id = ?", (radio, source, playlist_id))


def delete(db, playlist_id, fetch=None):
    """Deletes a playlist, its entries first so the streaming tracks only it held leave the library (touch)."""
    set_entries(db, playlist_id, [], fetch)
    db.execute("delete from playlist where id = ?", (playlist_id,))


def set_entries(db, playlist_id, track_ids, fetch=None):
    """Replaces a playlist's entries. fetch(video id) is given the streaming tracks this puts in the library to
    download."""
    old = [r[0] for r in db.execute("select track_id from playlist_entry where playlist_id = ?", (playlist_id,))]
    db.execute("delete from playlist_entry where playlist_id = ?", (playlist_id,))
    db.executemany("insert into playlist_entry values (?, ?, ?)", [(playlist_id, i, t) for i, t in enumerate(track_ids)])
    db.execute("update playlist set changed = ? where id = ?", (time.time(), playlist_id))
    # A radio's entries don't count for the library, so writing them leaves it as it was.
    if db.execute("select radio from playlist where id = ?", (playlist_id,)).fetchone()[0] == PLAIN:
        touch(db, set(old) | set(track_ids), fetch)


def counts(row) -> bool:
    """Whether a playlist's songs are library songs: not a radio or a mix, and shown (Liked Music's whatever)."""
    return row["radio"] == PLAIN and not is_mix(row["mix"]) and (not row["hidden"] or row["mix"] == LIKED)


def touch(db, ids, fetch=None):
    """Puts streaming tracks in the library while starred or in a plain playlist that counts, and takes them out
    otherwise: YouTube Music's own mixes change daily and stay playlists; library playlists count while shown, Liked
    Music always. fetch(video id) is given the ones the user chose, to download."""
    for id in ids:
        r = db.execute("select ref from track where id = ? and source = 'ytm'", (id,)).fetchone()
        if r is None:
            continue
        starred = db.execute("select 1 from stat where id = ? and starred is not null", (id,)).fetchone()  # by anyone
        playlists = db.execute("select p.mix, p.radio, p.hidden from playlist_entry e join playlist p on p.id = e.playlist_id "
                               "where e.track_id = ? and p.radio = ?", (id, PLAIN)).fetchall()
        db.execute("update track set library = ? where id = ?", (int(bool(starred or any(map(counts, playlists)))), id))
        # Mixes put their tracks in the library but change daily, so only the user's own choices download.
        wanted = starred or any(row["mix"] is None for row in playlists)
        if wanted and fetch:
            fetch(r["ref"])


def demix(db):
    """Streaming tracks in the library only through mixes or hidden playlists leave it (counts)."""
    db.execute("update track set library = 0 where source = 'ytm' and library = 1 and not exists (select 1 from stat s where s.id = track.id "
               "and s.starred is not null) and not exists (select 1 from playlist_entry e join playlist p on p.id = e.playlist_id "
               f"where e.track_id = track.id and p.radio = {PLAIN} and coalesce(p.mix, '') not like '{MIX_PREFIX}%' "
               f"and (p.hidden = 0 or p.mix = '{LIKED}'))")
