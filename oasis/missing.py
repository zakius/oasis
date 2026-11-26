"""Songs nothing can play: recommended by Last.fm or ListenBrainz, or on an album only MusicBrainz has, but neither in
the library nor on any streaming service oasis uses. They are tracks of source 'missing', never in the library, so they
can be shown, starred and kept in playlists: starring one wants its album. A worker finds each one's album on
MusicBrainz. When the song turns up (a stream of it, or a file), its stars and playlist entries move there
(tracks.remember, tracks.adopt_local_copies)."""
import logging
import time

from . import musicbrainz
from .cover import GROUP_COVER
from .db import set_stat
from .text import fold
from .tracks import IDS, MB_IDS, make_id, mbid_of, name_id

log = logging.getLogger(__name__)

RETRY = 7 * 86400  # seconds before MusicBrainz is asked again for a song it had no album for
# Release groups a song's album is picked from, best first: a studio album, an EP, a single, then anything official.
GROUP_ORDER = ("Album", "EP", "Single")
NOT_PLAYABLE = "Nothing can play this song: not in the library or on any streaming service. Star it to want its album."


def song_id(artist: str, title: str) -> str:
    return make_id(IDS["missing"], artist, title)


def library_artist(db, name: str) -> str | None:
    """The library's artist of that name, if the library has one."""
    id = name_id(name)
    row = db.execute("select artist_id from track where library = 1 and (artist_id = ? or album_artist = ? collate nocase) limit 1",
                     (id, name)).fetchone()
    return row and row[0]


def remember(db, artist: str, title: str, mbid: str | None = None, duration: int | None = None, preview: str | None = None) -> str:
    """Keeps a song nothing can play and returns its id. Its artist is the library's when it has one; its album and
    MusicBrainz artist come later (fill). preview: its clip, '<preview service>:<its id>' (sources.PREVIEWS)."""
    id = song_id(artist, title)
    db.execute("insert into track(id, source, ref, title, artist, album_artist, artist_id, library, mb_recording, duration, preview, created) "
               "values (?, 'missing', ?, ?, ?, ?, ?, 0, ?, ?, ?, ?) on conflict(id) do update set "
               "mb_recording = coalesce(track.mb_recording, excluded.mb_recording), duration = coalesce(track.duration, excluded.duration), "
               "preview = coalesce(excluded.preview, track.preview)",
               (id, id, title, artist, artist, library_artist(db, artist), mbid, duration, preview, time.time()))
    return id


def group_rank(release: dict) -> tuple:
    """How fit a release's group is to want for a song: official studio albums first, then EPs, singles, anything
    official; earliest first."""
    group = release.get("release-group") or {}
    kind = group.get("primary-type")
    studio = kind in GROUP_ORDER and not group.get("secondary-types")
    order = GROUP_ORDER.index(kind) if studio else len(GROUP_ORDER)
    return order, release.get("date") or "9999"


def credited(recording: dict) -> set[str]:
    """The names a recording is credited to, folded: each artist's, and the credit as written."""
    credit = recording.get("artist-credit") or []
    names = {fold(c.get("name") or "") for c in credit} | {fold((c.get("artist") or {}).get("name") or "") for c in credit}
    return names | {fold(musicbrainz.credited(credit))} if credit else names


def pick(recordings: list[dict], artist: str, title: str, mbid: str | None) -> tuple[dict, dict] | None:
    """The recording (the one with mbid, else the artist's one titled so) and the official release of it whose group is
    best to want."""
    if mbid:
        found = [r for r in recordings if r["id"] == mbid]
    else:
        found = [r for r in recordings if fold(r.get("title") or "") == fold(title) and fold(artist) in credited(r)]
    options = [(group_rank(release), recording, release) for recording in found for release in recording.get("releases") or []
               if release.get("status") == "Official" and release.get("release-group")]
    if not options:
        return None
    _, recording, release = min(options, key=lambda o: o[0])
    return recording, release


def look_up(db, r) -> tuple[dict, dict] | None:
    """MusicBrainz's recording of a missing song, and the release to want: by its recording id when known, else by
    artist and title."""
    if r["mb_recording"]:
        query = "rid:" + r["mb_recording"]
    else:
        query = f'recording:{musicbrainz.lucene(r["title"])} AND artist:{musicbrainz.lucene(r["album_artist"])}'
    return pick(musicbrainz.search("recording", query, db), r["album_artist"], r["title"], r["mb_recording"])


def place(db, id: str, recording: dict, release: dict):
    """Gives a missing song its MusicBrainz recording, album and artist: the library's artist when it has them, else the
    MusicBrainz one (ar-mb-), which can be followed."""
    group = release["release-group"]
    credit = recording.get("artist-credit") or release.get("artist-credit") or []
    artist = credit[0]["artist"] if credit else None
    r = db.execute("select album_artist from track where id = ?", (id,)).fetchone()
    artist_id = library_artist(db, r["album_artist"]) or (artist and library_artist(db, artist["name"])) or \
        (artist and MB_IDS["artist"] + artist["id"])
    year = (release.get("date") or "")[:4]
    db.execute("update track set mb_recording = ?, mb_release_group = ?, album = ?, artist_id = ?, year = ?, cover = ?, "
               "duration = coalesce(duration, ?), looked_up = ? where id = ?",
               (recording["id"], group["id"], group.get("title") or release.get("title"), artist_id, int(year) if year.isdigit() else None,
                GROUP_COVER.format(group["id"]), round(recording["length"] / 1000) if recording.get("length") else None, time.time(), id))


def album_of(r) -> str | None:
    """The album id a missing song goes by: its MusicBrainz release group's (al-mb-), which can be starred to want it."""
    return MB_IDS["album"] + r["mb_release_group"] if r["mb_release_group"] else None


def fill(db, id: str) -> bool:
    """Looks a missing song's album up on MusicBrainz now; whether it has one. OSError when MusicBrainz is away."""
    r = db.execute("select * from track where id = ? and source = 'missing'", (id,)).fetchone()
    if r is None:
        return False
    if r["mb_release_group"]:
        return True
    found = look_up(db, r)
    if not found:
        db.execute("update track set looked_up = ? where id = ?", (time.time(), id))
        return False
    place(db, id, *found)
    return True


def fill_next(db, want) -> bool:
    """Finds the album of one missing song that has none yet, and has its starred users want it (want as in star);
    whether there was one to look up."""
    r = db.execute("select id from track where source = 'missing' and mb_release_group is null and coalesce(looked_up, 0) < ? "
                   "order by exists (select 1 from stat s where s.id = track.id and s.starred is not null) desc, created limit 1",
                   (time.time() - RETRY,)).fetchone()
    if r is None:
        return False
    try:
        found = fill(db, r["id"])
    except OSError:
        raise  # MusicBrainz is away: the worker tries again later
    except Exception:
        log.exception("looking up the album of %s failed", r["id"])
        db.execute("update track set looked_up = ? where id = ?", (time.time(), r["id"]))  # not again till RETRY
        return True
    if found:
        song = db.execute("select * from track where id = ?", (r["id"],)).fetchone()
        for user, starred in db.execute("select user, starred from stat where id = ? and starred is not null", (r["id"],)).fetchall():
            want_for_song(db, user, album_of(song), starred, want)
    return True


def star(db, user: str, id: str, value: float | None, want) -> bool:
    """Stars a missing song, which wants its album (looked up now when not known yet; the worker does it when
    MusicBrainz is away), or takes the star back, which lets the album go unless another of its starred songs holds it
    or the user starred the album themselves. want(db, user, album ids, value) stars albums. False when id is no
    missing song."""
    r = db.execute("select * from track where id = ? and source = 'missing'", (id,)).fetchone()
    if r is None:
        return False
    before = starred_at(db, user, id)
    if value and before:
        return True  # starred already: its album's star keeps its time
    set_stat(db, user, id, "starred", value)
    if value and not r["mb_release_group"]:
        try:
            fill(db, id)
        except OSError as e:
            log.info("MusicBrainz away for %s: %s; its album is wanted once found", id, e)
        r = db.execute("select * from track where id = ?", (id,)).fetchone()
    album = album_of(r)
    if album is None:
        return True
    if value:
        want_for_song(db, user, album, value, want)
        return True
    # The album's star came from a song when it has that song's star time (want_for_song); one the user gave it stays.
    album_star = starred_at(db, user, album)
    if album_star is None or album_star != before:
        return True
    holder = db.execute("select s.starred from track t join stat s on s.id = t.id and s.user = ? and s.starred is not null "
                        "where t.source = 'missing' and t.mb_release_group = ? order by s.starred limit 1", (user, r["mb_release_group"])).fetchone()
    if holder:
        set_stat(db, user, album, "starred", holder[0])  # another starred song of the album holds it now
    else:
        want(db, user, [album], None)
    return True


def starred_at(db, user: str, id: str) -> float | None:
    row = db.execute("select starred from stat where user = ? and id = ?", (user, id)).fetchone()
    return row[0] if row else None


def want_for_song(db, user: str, album: str, starred: float, want):
    """Wants a starred song's album, starring it with the song's star time so that unstarring the song can tell the
    star was the song's (star). An album the user starred already keeps its star."""
    if starred_at(db, user, album) is None:
        want(db, user, [album], starred)


def album_songs(db, rg: str) -> list[str]:
    """The songs of a release group only MusicBrainz has, as missing songs, from its earliest official edition with the
    fewest discs. OSError when MusicBrainz is away, LookupError when it has no such group."""
    editions = [e for e in musicbrainz.editions(rg, db) if e.get("status") == "Official"
                and not any(musicbrainz.VIDEO_MEDIA.search(m.get("format") or "") for m in e.get("media") or [])]
    if not editions:
        return []
    edition = min(editions, key=lambda e: (len(e.get("media") or []), e.get("date") or "9999"))
    group = edition.get("release-group") or {"id": rg}
    ids = []
    for medium in edition.get("media") or []:
        for t in medium.get("tracks") or []:
            recording = t.get("recording") or {}
            credit = t.get("artist-credit") or recording.get("artist-credit") or edition.get("artist-credit") or []
            artist = musicbrainz.credited(credit[:1]) if credit else "Unknown Artist"
            title = t.get("title") or recording.get("title")
            existing = db.execute("select id from track where mb_recording = ? and source != 'missing' limit 1", (recording.get("id"),)).fetchone()
            if existing:
                ids.append(existing[0])
                continue
            id = remember(db, artist, title, recording.get("id"), round(t["length"] / 1000) if t.get("length") else None)
            placed = db.execute("select mb_release_group from track where id = ?", (id,)).fetchone()[0]
            if placed in (None, group["id"]):  # a song placed on another album (starring wants that one) stays there
                place(db, id, {**recording, "artist-credit": credit}, {**edition, "release-group": group})
                db.execute("update track set track_no = ?, disc_no = ? where id = ?", (t.get("position"), medium.get("position"), id))
            ids.append(id)
    return ids


def rg_of(album_id: str) -> str | None:
    """The release group of an album id only MusicBrainz has (al-mb-)."""
    return mbid_of(album_id) if album_id.startswith(MB_IDS["album"]) else None
