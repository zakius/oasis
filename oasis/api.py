import hashlib
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl
from xml.etree.ElementTree import Element, tostring

from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from . import cover, scan
from .config import Config
from .db import connect
from .ytm import YouTubeMusic

log = logging.getLogger(__name__)

VERSION = "1.16.1"
NOT_FOUND, MISSING, AUTH = 70, 10, 40


class Fail(Exception):
    def __init__(self, code: int, message: str):
        self.code, self.message = code, message


def iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z") if ts else None


def clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


def to_xml(tag: str, value: dict) -> Element:
    elem = Element(tag)
    for key, v in value.items():
        for item in v if isinstance(v, list) else [v]:
            if isinstance(item, dict):
                elem.append(to_xml(key, item))
            elif key == "value":
                elem.text = str(item)
            elif isinstance(v, list):
                child = Element(key)
                child.text = str(item)
                elem.append(child)
            else:
                elem.set(key, str(item).lower() if isinstance(item, bool) else str(item))
    return elem


class Params:
    def __init__(self, request: Request, body: bytes):
        self.multi = [*request.query_params.multi_items(), *parse_qsl(body.decode())]

    def get(self, key: str, default=None):
        return next((v for k, v in self.multi if k == key), default)

    def all(self, key: str) -> list[str]:
        return [v for k, v in self.multi if k == key]

    def int(self, key: str, default: int | None = None) -> int | None:
        value = self.get(key)
        return int(value) if value not in (None, "") else default

    def need(self, key: str) -> str:
        value = self.get(key)
        if value is None:
            raise Fail(MISSING, f"Required parameter '{key}' is missing")
        return value


SONG_COLUMNS = """t.*, s.starred, s.rating, s.play_count, s.played"""
SONGS = f"select {SONG_COLUMNS} from track t left join stat s on s.id = t.id"
ALBUMS = """
select t.album_id as id, t.album as name, t.album_artist as artist, t.artist_id,
       count(*) as song_count, sum(t.duration) as duration, min(t.created) as created,
       max(t.year) as year, max(t.genre) as genre, max(t.cover is not null) as has_cover,
       s.starred as album_starred, s.rating as album_rating,
       coalesce(sum(ps.play_count), 0) as plays, max(ps.played) as last_played
from track t left join stat s on s.id = t.album_id left join stat ps on ps.id = t.id
"""
ARTISTS = """
select t.artist_id as id, t.album_artist as name, count(distinct t.album_id) as album_count,
       max(t.cover is not null) as has_cover, s.starred, s.rating
from track t left join stat s on s.id = t.artist_id
"""


def song(r) -> dict:
    root = Path(r["root"] or "/")
    return clean({
        "id": r["id"], "parent": r["album_id"], "isDir": False, "title": r["title"], "album": r["album"],
        "artist": r["artist"], "track": r["track_no"], "discNumber": r["disc_no"], "year": r["year"],
        "genre": r["genre"], "coverArt": r["album_id"] if r["cover"] else None, "size": r["size"],
        "contentType": r["content_type"], "suffix": r["suffix"], "duration": r["duration"],
        "bitRate": r["bitrate"], "path": str(Path(r["ref"]).relative_to(root)) if r["source"] == "local" else None,
        "albumId": r["album_id"], "artistId": r["artist_id"], "type": "music", "mediaType": "song",
        "created": iso(r["created"]), "starred": iso(r["starred"]), "userRating": r["rating"],
        "playCount": r["play_count"] or 0, "played": iso(r["played"]),
    })


def album(r) -> dict:
    return clean({
        "id": r["id"], "name": r["name"], "title": r["name"], "album": r["name"], "artist": r["artist"],
        "artistId": r["artist_id"], "parent": r["artist_id"], "isDir": True,
        "coverArt": r["id"] if r["has_cover"] else None, "songCount": r["song_count"], "duration": r["duration"],
        "created": iso(r["created"]), "year": r["year"], "genre": r["genre"], "starred": iso(r["album_starred"]),
        "userRating": r["album_rating"], "playCount": r["plays"], "played": iso(r["last_played"]),
    })


def artist(r) -> dict:
    return clean({
        "id": r["id"], "name": r["name"], "albumCount": r["album_count"],
        "coverArt": r["id"] if r["has_cover"] else None, "starred": iso(r["starred"]), "userRating": r["rating"],
    })


def index_letter(name: str) -> str:
    for article in ("The ", "A "):
        if name.startswith(article):
            name = name[len(article):]
    letter = name[:1].upper()
    return letter if letter.isalpha() else "#"


class Api:
    def __init__(self, config: Config, ytm=None):
        self.config = config
        self.ytm = ytm or (YouTubeMusic(config.ytm_cache) if config.ytm_cache else None)
        self.scanning = False
        self.scan_lock = threading.Lock()

    def _db(self):
        return connect(self.config.database)

    def _start_scan(self):
        if not self.scan_lock.acquire(blocking=False):
            return
        self.scanning = True

        def run():
            db = self._db()
            try:
                scan.scan(db, self.config.library)
            finally:
                db.close()
                self.scanning = False
                self.scan_lock.release()
        threading.Thread(target=run, daemon=True).start()

    def _authenticate(self, p: Params) -> str:
        user = p.need("u")
        password = self.config.users.get(user)
        if password is None:
            raise Fail(AUTH, "Wrong username or password")
        if (token := p.get("t")) is not None:
            ok = hashlib.md5((password + p.need("s")).encode()).hexdigest() == token.lower()
        else:
            given = p.need("p")
            ok = (bytes.fromhex(given[4:]).decode() if given.startswith("enc:") else given) == password
        if not ok:
            raise Fail(AUTH, "Wrong username or password")
        return user

    def _one(self, db, sql: str, *args):
        row = db.execute(sql, args).fetchone()
        if row is None:
            raise Fail(NOT_FOUND, "Data not found")
        return row

    # Subsonic methods are dispatched by name; helpers start with '_'. Each returns a payload dict or a Response.

    def ping(self, db, p, user):
        return {}

    def getLicense(self, db, p, user):
        return {"license": {"valid": True}}

    def getOpenSubsonicExtensions(self, db, p, user):
        return {"openSubsonicExtensions": [{"name": "formPost", "versions": [1]}]}

    def getUser(self, db, p, user):
        roles = ("scrobbling", "admin", "settings", "download", "upload", "playlist", "coverArt", "comment",
                 "podcast", "stream", "jukebox", "share")
        return {"user": {"username": user, **{f"{r}Role": True for r in roles}, "folder": [1]}}

    def getMusicFolders(self, db, p, user):
        return {"musicFolders": {"musicFolder": [{"id": 1, "name": "Music"}]}}

    def getScanStatus(self, db, p, user):
        return {"scanStatus": {"scanning": self.scanning, "count": db.execute("select count(*) from track").fetchone()[0]}}

    def startScan(self, db, p, user):
        self._start_scan()
        return self.getScanStatus(db, p, user)

    def _artists_index(self, db):
        groups: dict[str, list] = {}
        for r in db.execute(ARTISTS + " where t.library = 1 group by t.artist_id order by t.album_artist collate nocase"):
            groups.setdefault(index_letter(r["name"]), []).append(artist(r))
        return {"ignoredArticles": "The A", "index": [{"name": k, "artist": v} for k, v in sorted(groups.items())]}

    def getArtists(self, db, p, user):
        return {"artists": self._artists_index(db)}

    def getIndexes(self, db, p, user):
        return {"indexes": {"lastModified": 0, **self._artists_index(db)}}

    def _artist_albums(self, db, artist_id):
        # Albums seen only in streaming search results stay hidden unless the artist has nothing in the library.
        sql = ALBUMS + " where t.artist_id = ? and t.library >= ? group by t.album_id order by year, name"
        return [album(r) for r in db.execute(sql, (artist_id, 1))] or [album(r) for r in db.execute(sql, (artist_id, 0))]

    def _album_songs(self, db, album_id):
        return [song(r) for r in db.execute(SONGS + " where t.album_id = ? order by disc_no, track_no, title", (album_id,))]

    def getArtist(self, db, p, user):
        r = self._one(db, ARTISTS + " where t.artist_id = ? group by t.artist_id", p.need("id"))
        return {"artist": {**artist(r), "album": self._artist_albums(db, r["id"])}}

    def getAlbum(self, db, p, user):
        r = self._one(db, ALBUMS + " where t.album_id = ? group by t.album_id", p.need("id"))
        return {"album": {**album(r), "song": self._album_songs(db, r["id"])}}

    def getSong(self, db, p, user):
        return {"song": song(self._one(db, SONGS + " where t.id = ?", p.need("id")))}

    def getMusicDirectory(self, db, p, user):
        id = p.need("id")
        if id.startswith("ar-"):
            r = self._one(db, ARTISTS + " where t.artist_id = ? group by t.artist_id", id)
            return {"directory": {"id": id, "name": r["name"], "child": self._artist_albums(db, id)}}
        r = self._one(db, ALBUMS + " where t.album_id = ? group by t.album_id", id)
        return {"directory": {"id": id, "name": r["name"], "parent": r["artist_id"], "child": self._album_songs(db, id)}}

    def _album_list(self, db, p):
        kind = p.need("type")
        where, args = ["t.library = 1"], []
        order = {
            "random": "random()", "newest": "created desc", "highest": "album_rating desc",
            "frequent": "plays desc", "recent": "last_played desc", "alphabeticalByName": "name collate nocase",
            "alphabeticalByArtist": "artist collate nocase, year", "starred": "album_starred desc",
            "byYear": "year", "byGenre": "name collate nocase",
        }.get(kind)
        if order is None:
            raise Fail(MISSING, f"Unknown list type '{kind}'")
        having = {"highest": "album_rating > 0", "frequent": "plays > 0", "recent": "last_played is not null",
                  "starred": "album_starred is not null"}.get(kind)
        if kind == "byYear":
            lo, hi = p.int("fromYear", 0), p.int("toYear", 9999)
            where.append("t.year between ? and ?")
            args += [min(lo, hi), max(lo, hi)]
            order = "year" if lo <= hi else "year desc"
        if kind == "byGenre":
            where.append("t.genre = ?")
            args.append(p.need("genre"))
        sql = ALBUMS + " where " + " and ".join(where) + " group by t.album_id"
        sql += (f" having {having}" if having else "") + f" order by {order} limit ? offset ?"
        return [album(r) for r in db.execute(sql, (*args, min(p.int("size", 10), 500), p.int("offset", 0)))]

    def getAlbumList2(self, db, p, user):
        return {"albumList2": {"album": self._album_list(db, p)}}

    def getAlbumList(self, db, p, user):
        return {"albumList": {"album": self._album_list(db, p)}}

    def getRandomSongs(self, db, p, user):
        where, args = ["t.library = 1"], []
        if genre := p.get("genre"):
            where.append("t.genre = ?")
            args.append(genre)
        if (lo := p.int("fromYear")) is not None:
            where.append("t.year >= ?")
            args.append(lo)
        if (hi := p.int("toYear")) is not None:
            where.append("t.year <= ?")
            args.append(hi)
        sql = SONGS + f" where {' and '.join(where)} order by random() limit ?"
        return {"randomSongs": {"song": [song(r) for r in db.execute(sql, (*args, min(p.int("size", 10), 500)))]}}

    def getSongsByGenre(self, db, p, user):
        sql = SONGS + " where t.library = 1 and t.genre = ? order by t.album_artist, t.album, t.disc_no, t.track_no limit ? offset ?"
        rows = db.execute(sql, (p.need("genre"), min(p.int("count", 10), 500), p.int("offset", 0)))
        return {"songsByGenre": {"song": [song(r) for r in rows]}}

    def getGenres(self, db, p, user):
        rows = db.execute("select genre, count(*) n, count(distinct album_id) a from track where library = 1 and genre is not null group by genre order by genre")
        return {"genres": {"genre": [{"value": r["genre"], "songCount": r["n"], "albumCount": r["a"]} for r in rows]}}

    def _search(self, db, p):
        terms = [t for t in p.get("query", "").replace('"', "").split() if t]

        def match(columns: str):
            return " and ".join(["t.library = 1", *[f"({columns}) like ?"] * len(terms)]), [f"%{t}%" for t in terms]

        cond, args = match("t.album_artist")
        artists = db.execute(ARTISTS + f" where {cond} group by t.artist_id order by t.album_artist collate nocase limit ? offset ?",
                             (*args, p.int("artistCount", 20), p.int("artistOffset", 0)))
        cond, args = match("t.album || ' ' || t.album_artist")
        albums = db.execute(ALBUMS + f" where {cond} group by t.album_id order by name collate nocase limit ? offset ?",
                            (*args, p.int("albumCount", 20), p.int("albumOffset", 0)))
        cond, args = match("t.title || ' ' || t.artist || ' ' || t.album")
        songs = db.execute(SONGS + f" where {cond} order by t.album_artist, t.album, t.disc_no, t.track_no limit ? offset ?",
                           (*args, p.int("songCount", 20), p.int("songOffset", 0)))
        songs = [song(r) for r in songs]
        if self.ytm and terms and p.int("songOffset", 0) == 0:
            known = {s["id"] for s in songs}
            try:
                found = [self._remember(db, row) for row in self.ytm.search(" ".join(terms), p.int("songCount", 20))]
            except Exception as e:
                log.warning("YouTube Music search failed: %s", e)
                found = []
            songs += [song(self._one(db, SONGS + " where t.id = ?", id)) for id in dict.fromkeys(found) if id not in known]
        return {"artist": [artist(r) for r in artists], "album": [album(r) for r in albums], "song": songs}

    def _remember(self, db, row: dict) -> str:
        """Caches a streaming track's metadata and returns its id, or the id of the local copy of the same song."""
        local = db.execute("select id from track where source = 'local' and artist_id = ? and title = ? collate nocase "
                           "and abs(duration - ?) <= 5", (row["artist_id"], row["title"], row["duration"] or 0)).fetchone()
        if local:
            db.execute("update track set ytm_id = ? where id = ?", (row["ytm_id"], local["id"]))
            return local["id"]
        updates = ", ".join(f"{k} = excluded.{k}" for k in row if k not in ("id", "library", "created"))
        db.execute(f"insert into track({', '.join(row)}) values ({', '.join('?' * len(row))}) "
                   f"on conflict(id) do update set {updates}", list(row.values()))
        return row["id"]

    def _touch(self, db, ids):
        """Streaming tracks join the library while starred or in a playlist; joining starts their download."""
        for id in ids:
            r = db.execute("select ref, library from track where id = ? and source = 'ytm'", (id,)).fetchone()
            if r is None:
                continue
            kept = db.execute("select exists(select 1 from stat where id = ? and starred is not null) "
                              "or exists(select 1 from playlist_entry where track_id = ?)", (id, id)).fetchone()[0]
            db.execute("update track set library = ? where id = ?", (kept, id))
            if kept and not r["library"] and self.ytm:
                threading.Thread(target=self._prefetch, args=(r["ref"],), daemon=True).start()

    def _prefetch(self, video_id):
        try:
            self.ytm.fetch(video_id)
        except Exception as e:
            log.warning("download of %s failed: %s", video_id, e)

    def search3(self, db, p, user):
        return {"searchResult3": self._search(db, p)}

    def search2(self, db, p, user):
        return {"searchResult2": self._search(db, p)}

    def _starred(self, db):
        def ids(prefix):
            return db.execute("select id from stat where starred is not null and id like ? order by starred desc", (prefix + "%",)).fetchall()
        return {
            "artist": [artist(self._one(db, ARTISTS + " where t.artist_id = ? group by t.artist_id", r["id"])) for r in ids("ar-")
                       if db.execute("select 1 from track where artist_id = ?", (r["id"],)).fetchone()],
            "album": [album(self._one(db, ALBUMS + " where t.album_id = ? group by t.album_id", r["id"])) for r in ids("al-")
                      if db.execute("select 1 from track where album_id = ?", (r["id"],)).fetchone()],
            "song": [song(r) for r in db.execute(SONGS + " where s.starred is not null order by s.starred desc")],
        }

    def getStarred2(self, db, p, user):
        return {"starred2": self._starred(db)}

    def getStarred(self, db, p, user):
        return {"starred": self._starred(db)}

    def _set_starred(self, db, p, value):
        for id in p.all("id") + p.all("albumId") + p.all("artistId"):
            db.execute("insert into stat(id, starred) values (?, ?) on conflict(id) do update set starred = excluded.starred", (id, value))
        self._touch(db, p.all("id"))
        return {}

    def star(self, db, p, user):
        return self._set_starred(db, p, time.time())

    def unstar(self, db, p, user):
        return self._set_starred(db, p, None)

    def setRating(self, db, p, user):
        rating = p.int("rating", 0) or None
        db.execute("insert into stat(id, rating) values (?, ?) on conflict(id) do update set rating = excluded.rating", (p.need("id"), rating))
        return {}

    def scrobble(self, db, p, user):
        if p.get("submission", "true") == "false":
            return {}
        times = p.all("time")
        for i, id in enumerate(p.all("id")):
            played = int(times[i]) / 1000 if i < len(times) else time.time()
            db.execute("insert into stat(id, play_count, played) values (?, 1, ?) on conflict(id) do update "
                       "set play_count = play_count + 1, played = excluded.played", (id, played))
        return {}

    def getNowPlaying(self, db, p, user):
        return {"nowPlaying": {}}

    def getArtistInfo2(self, db, p, user):
        return {"artistInfo2": {}}

    def getArtistInfo(self, db, p, user):
        return {"artistInfo": {}}

    def getAlbumInfo2(self, db, p, user):
        return {"albumInfo": {}}

    def getAlbumInfo(self, db, p, user):
        return {"albumInfo": {}}

    def getSimilarSongs2(self, db, p, user):
        return {"similarSongs2": {}}

    def getSimilarSongs(self, db, p, user):
        return {"similarSongs": {}}

    def getTopSongs(self, db, p, user):
        return {"topSongs": {}}

    def getInternetRadioStations(self, db, p, user):
        return {"internetRadioStations": {}}

    def getPodcasts(self, db, p, user):
        return {"podcasts": {}}

    def getShares(self, db, p, user):
        return {"shares": {}}

    def getBookmarks(self, db, p, user):
        return {"bookmarks": {}}

    def getPlayQueue(self, db, p, user):
        return {}

    def _playlist(self, db, r, entries: bool) -> dict:
        songs = [song(s) for s in db.execute(
            SONGS + " join playlist_entry e on e.track_id = t.id where e.playlist_id = ? order by e.pos", (r["id"],))]
        result = clean({
            "id": str(r["id"]), "name": r["name"], "comment": r["comment"], "owner": r["owner"], "public": False,
            "songCount": len(songs), "duration": sum(s.get("duration", 0) for s in songs),
            "created": iso(r["created"]), "changed": iso(r["changed"]),
        })
        return {**result, "entry": songs} if entries else result

    def getPlaylists(self, db, p, user):
        rows = db.execute("select * from playlist where owner = ? order by name collate nocase", (user,))
        return {"playlists": {"playlist": [self._playlist(db, r, False) for r in rows]}}

    def _own_playlist(self, db, id, user):
        return self._one(db, "select * from playlist where id = ? and owner = ?", id, user)

    def getPlaylist(self, db, p, user):
        return {"playlist": self._playlist(db, self._own_playlist(db, p.need("id"), user), True)}

    def _set_entries(self, db, playlist_id, track_ids):
        old = [r[0] for r in db.execute("select track_id from playlist_entry where playlist_id = ?", (playlist_id,))]
        db.execute("delete from playlist_entry where playlist_id = ?", (playlist_id,))
        db.executemany("insert into playlist_entry values (?, ?, ?)", [(playlist_id, i, t) for i, t in enumerate(track_ids)])
        db.execute("update playlist set changed = ? where id = ?", (time.time(), playlist_id))
        self._touch(db, set(old) | set(track_ids))

    def createPlaylist(self, db, p, user):
        if id := p.get("playlistId"):
            playlist_id = self._own_playlist(db, id, user)["id"]
        else:
            now = time.time()
            playlist_id = db.execute("insert into playlist(name, owner, created, changed) values (?, ?, ?, ?)",
                                     (p.need("name"), user, now, now)).lastrowid
        self._set_entries(db, playlist_id, p.all("songId"))
        return {"playlist": self._playlist(db, self._own_playlist(db, playlist_id, user), True)}

    def updatePlaylist(self, db, p, user):
        r = self._own_playlist(db, p.need("playlistId"), user)
        for column in ("name", "comment"):
            if (value := p.get(column)) is not None:
                db.execute(f"update playlist set {column} = ?, changed = ? where id = ?", (value, time.time(), r["id"]))
        entries = [e["track_id"] for e in db.execute("select track_id from playlist_entry where playlist_id = ? order by pos", (r["id"],))]
        removed = {int(i) for i in p.all("songIndexToRemove")}
        added = p.all("songIdToAdd")
        if removed or added:
            self._set_entries(db, r["id"], [t for i, t in enumerate(entries) if i not in removed] + added)
        return {}

    def deletePlaylist(self, db, p, user):
        playlist_id = self._own_playlist(db, p.need("id"), user)["id"]
        self._set_entries(db, playlist_id, [])
        db.execute("delete from playlist where id = ?", (playlist_id,))
        return {}

    def getCoverArt(self, db, p, user):
        id = p.need("id")
        column = {"ar-": "artist_id", "al-": "album_id"}.get(id[:3], "id")
        r = self._one(db, f"select ref, cover from track where {column} = ? and cover is not null "
                         "order by source != 'local', cover = 'embedded', disc_no, track_no limit 1", id)
        try:
            image = cover.load(r["ref"], r["cover"])
        except OSError as e:
            log.warning("cover %s unavailable: %s", r["cover"], e)
            image = None
        if image is None:
            raise Fail(NOT_FOUND, "Cover art not found")
        return Response(image[0], media_type=image[1], headers={"Cache-Control": "public, max-age=86400"})

    def stream(self, db, p, user):
        r = self._one(db, "select source, ref, content_type from track where id = ?", p.need("id"))
        if r["source"] == "local":
            return FileResponse(r["ref"], media_type=r["content_type"])
        if not self.ytm:
            raise Fail(NOT_FOUND, "YouTube Music is not enabled")
        try:
            path = self.ytm.cached(r["ref"]) or self.ytm.fetch(r["ref"])
        except Exception as e:
            log.warning("download of %s failed: %s", r["ref"], e)
            raise Fail(NOT_FOUND, "Stream unavailable")
        return FileResponse(path, media_type=r["content_type"])

    def download(self, db, p, user):
        return self.stream(db, p, user)


def respond(fmt: str, payload: dict, error: Fail | None = None) -> Response:
    body = {"status": "failed" if error else "ok", "version": VERSION, "type": "oasis", "serverVersion": "0.1.0",
            "openSubsonic": True, **payload}
    if error:
        body["error"] = {"code": error.code, "message": error.message}
    if fmt == "json":
        return JSONResponse({"subsonic-response": body})
    root = to_xml("subsonic-response", body)
    root.set("xmlns", "http://subsonic.org/restapi")
    return Response(b'<?xml version="1.0" encoding="UTF-8"?>' + tostring(root), media_type="application/xml")


def create_app(config: Config, scan_on_start: bool = True, ytm=None) -> Starlette:
    api = Api(config, ytm)

    async def endpoint(request: Request):
        p = Params(request, await request.body())
        fmt = p.get("f", "xml")
        method = request.path_params["method"].removesuffix(".view")
        handler = getattr(Api, method, None)
        if handler is None or method.startswith("_"):
            return respond(fmt, {}, Fail(0, f"Unknown method '{method}'"))

        def run():
            db = api._db()
            try:
                user = api._authenticate(p)
                result = handler(api, db, p, user)
                return result if isinstance(result, Response) else respond(fmt, result)
            except Fail as e:
                return respond(fmt, {}, e)
            finally:
                db.close()
        return await run_in_threadpool(run)

    @asynccontextmanager
    async def lifespan(app):
        if scan_on_start:
            api._start_scan()
        yield

    return Starlette(routes=[Route("/rest/{method}", endpoint, methods=["GET", "POST"])], lifespan=lifespan)
