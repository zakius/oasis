import dataclasses
import hashlib
import json
import logging
import re
import shutil
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl
from xml.etree.ElementTree import Element, tostring

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from . import accounts, acquire, artists, cover, dedupe, forward, lyrics, musicbrainz, releases, scan, web, ytm
from .acquire import MB_IDS, SECONDARY, Acquire
from .config import Config
from .credits import PERFORMANCE_ROLES, Credits
from .db import connect, make_id, move_track, remember, resolve
from .doctor import Doctor
from .download import Downloads
from .importer import Importer
from .forward import Forwarder, LastFm
from .lidarr import Library, Lidarr
from .prowlarr import Prowlarr
from .recommend import Recommender, set_entries
from .ytm import YouTubeMusic

log = logging.getLogger(__name__)

VERSION = "1.16.1"
NOT_FOUND, MISSING, AUTH, DENIED = 70, 10, 40, 50
MIX_OWNER = "YouTube Music"
# YouTube Music's fixed personal mix ids in its own order (the names are localized), as in roma's Mixes.kt.
MIX_ORDER = [
    "RDTMAK5uy_kset8DisdE7LSD4TNjEVvrKRTmG7a56sY",  # My Supermix
    "RDTMAK5uy_nGQKSMIkpr4o9VI_2i56pkGliD6FQRo50",  # My Mix 1
    "RDTMAK5uy_lz2owBgwWf1mjzyn_NbxzMViQzIg8IAIg",  # My Mix 2
    "RDTMAK5uy_k5UUl0lmrrfrjMpsT0CoMpdcBz1ruAO1k",  # My Mix 3
    "RDTMAK5uy_nTsa0Irmcu2li2-qHBoZxtrpG9HuC3k_Q",  # My Mix 4
    "RDTMAK5uy_lfZhS7zmIcmUhsKtkWylKzc0EN0LW90-s",  # My Mix 5
    "RDTMAK5uy_k78ni6Y4fyyl0r2eiKkBEICh9Q5wJdfXk",  # My Mix 6
    "RDTMAK5uy_lfhhWWw9v71CPrR7MRMHgZzbH6Vku9iJc",  # My Mix 7
    "RDTMAK5uy_n_5IN6hzAOwdCnM8D8rzrs3vDl12UcZpA",  # Discover Mix
    "RDTMAK5uy_lr0LWzGrq6FU9GIxWvFHTRPQD2LHMqlFA",  # New Release Mix
    "RDTMAK5uy_nilrsVWxrKskY0ZUpVZ3zpB0u4LwWTVJ4",  # Replay Mix
]


def playlist_order(r) -> tuple:
    """Mixes first in YouTube Music's order, then Liked Music, then everything else by name."""
    mix = r["mix"] or ""
    if mix.startswith("RDTMAK"):
        return 0, MIX_ORDER.index(mix) if mix in MIX_ORDER else len(MIX_ORDER), r["name"].casefold()
    return (1 if mix == "LM" else 2), 0, r["name"].casefold()
MIX_REFRESH = 6 * 3600


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


SONG_ID_PARAMS = ("id", "songId", "songIdToAdd")
ARTIST_ID_PARAMS = ("id", "artistId")
MOVED_PARAMS = (*SONG_ID_PARAMS, "albumId", "artistId")
FIND_FROM = 3  # characters a search needs before MusicBrainz is asked too


class Params:
    def __init__(self, request: Request, body: bytes):
        self.multi = [*request.query_params.multi_items(), *parse_qsl(body.decode())]
        self.range = request.headers.get("range")

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

    def follow_moves(self, db):
        """Ids a client still holds for streams that became local files, and for MusicBrainz-only artists and albums
        that arrived, point at the library's, and artist ids of another name of an artist at the artist."""
        ids = {v for k, v in self.multi if k in MOVED_PARAMS and v.startswith(("yt-", *MB_IDS.values()))}
        if ids:
            moved = dict(db.execute(f"select old_id, new_id from moved where old_id in ({','.join('?' * len(ids))})", list(ids)))
            moved |= {id: here for id in ids - moved.keys() if (here := acquire.library_id(db, id))}  # arrived since acquire's last pass
            self.multi = [(k, moved.get(v, v) if k in MOVED_PARAMS else v) for k, v in self.multi]
        self.multi = [(k, resolve(db, v) if k in ARTIST_ID_PARAMS and v.startswith("ar-") else v) for k, v in self.multi]


SONG_COLUMNS = """t.*, s.starred, s.rating, s.play_count, s.played"""
SONGS = f"select {SONG_COLUMNS} from track t left join stat s on s.id = t.id"
ALBUMS = """
select t.album_id as id, t.album as name, t.album_artist as artist, t.artist_id,
       count(*) as song_count, sum(t.duration) as duration, min(t.created) as created,
       max(t.year) as year, max(t.genre) as genre, max(t.cover is not null) as has_cover,
       s.starred as album_starred, s.rating as album_rating,
       coalesce(sum(ps.play_count), 0) as plays, max(ps.played) as last_played, li.state as lidarr_state, li.error as lidarr_error
from track t left join stat s on s.id = t.album_id left join stat ps on ps.id = t.id
left join lidarr_item li on li.kind = 'album' and li.ref = t.album_id
"""
ARTISTS = """
select t.artist_id as id, t.album_artist as name, count(distinct t.album_id) as album_count,
       max(t.cover is not null) as has_cover, s.starred, s.rating, li.state as lidarr_state, li.error as lidarr_error
from track t left join stat s on s.id = t.artist_id left join lidarr_item li on li.kind = 'artist' and li.ref = t.artist_id
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
        "playCount": r["play_count"] or 0, "played": iso(r["played"]), "youtubeId": r["ytm_id"] or None,
    })


LIDARR_KEYS = ("url", "api_key", "root_folder", "quality_profile", "metadata_profile")
DOCTOR_KEYS = ("rdt_db", "stall_hours", "dry_run")
# Lidarr progress of starred albums and artists, for clients that show it; complete albums and followed artists need no badge.
WANTED_STATES = {"wanted": "wanted", "sent": "wanted", "downloading": "downloading", "failed": "failed"}


def album(r) -> dict:
    return clean({
        "id": r["id"], "name": r["name"], "title": r["name"], "album": r["name"], "artist": r["artist"],
        "artistId": r["artist_id"], "parent": r["artist_id"], "isDir": True,
        "coverArt": r["id"] if r["has_cover"] else None, "songCount": r["song_count"], "duration": r["duration"],
        "created": iso(r["created"]), "year": r["year"], "genre": r["genre"], "starred": iso(r["album_starred"]),
        "userRating": r["album_rating"], "playCount": r["plays"], "played": iso(r["last_played"]),
        "oasisWanted": WANTED_STATES.get(r["lidarr_state"]),
        "oasisWantedError": r["lidarr_error"] if WANTED_STATES.get(r["lidarr_state"]) else None,
    })


def artist(r) -> dict:
    return clean({
        "id": r["id"], "name": r["name"], "albumCount": r["album_count"],
        "coverArt": r["id"] if r["has_cover"] else None, "starred": iso(r["starred"]), "userRating": r["rating"],
        "oasisWanted": WANTED_STATES.get(r["lidarr_state"]),
        "oasisWantedError": r["lidarr_error"] if WANTED_STATES.get(r["lidarr_state"]) else None,
    })


def describe(db, payload: dict) -> dict:
    """Adds to every song and album in a response what OpenSubsonic clients show beyond the basics: each artist and
    album artist with their own id, the other contributors by role, ISRCs, labels, release types and genres."""
    songs, albums = [], []

    def walk(value):
        if isinstance(value, dict):
            if value.get("mediaType") == "song":
                songs.append(value)
            elif value.get("isDir") and str(value.get("id", "")).startswith("al-"):
                albums.append(value)
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)
    walk(payload)
    if songs:
        describe_songs(db, songs)
    if albums:
        describe_albums(db, albums)
    return payload


def credits_of(db, track_ids: list[str]) -> dict[str, list]:
    """Each track's credits: MusicBrainz's when it has them, else the file's tags; plus the performance credits
    MusicBrainz has for other releases of the same audio (see credits.performance_credits)."""
    marks = ",".join("?" * len(track_ids))
    rows = db.execute(f"select * from credit where track_id in ({marks}) order by position", track_ids).fetchall()
    found = {}
    for r in rows:
        found.setdefault(r["track_id"], {}).setdefault(r["source"], []).append(r)
    mine = {t: sources.get("musicbrainz") or sources.get("tags", []) for t, sources in found.items()}
    siblings = db.execute(f"select t.id, o.id other from track t join audio_pair p on p.level > 0 and t.ref in (p.a, p.b) "
                          f"join track o on o.ref = case when p.a = t.ref then p.b else p.a end where t.id in ({marks})", track_ids).fetchall()
    for r in siblings:
        extra = [c for c in db.execute("select * from credit where track_id = ? and source = 'musicbrainz' order by position", (r["other"],))
                 if c["role"] in PERFORMANCE_ROLES]
        have = {(c["role"], c["sub_role"], c["artist_id"]) for c in mine.get(r["id"], [])}
        mine[r["id"]] = [*mine.get(r["id"], []), *(c for c in extra if (c["role"], c["sub_role"], c["artist_id"]) not in have)]
    return mine


def people(credits, role: str) -> tuple[list[dict], str | None]:
    """The artists of a role, and how they read together ('A feat. B')."""
    rows = [c for c in credits if c["role"] == role]
    if not rows:
        return [], None
    joined = "".join(c["name"] + (c["joinphrase"] or "") for c in rows) if any(c["joinphrase"] for c in rows) else ", ".join(c["name"] for c in rows)
    return [{"id": c["artist_id"], "name": c["name"]} for c in rows], joined


def genre_list(r) -> list[dict] | None:
    names = json.loads(r["genres"]) if r["genres"] else ([r["genre"]] if r["genre"] else [])
    return [{"name": g} for g in names] or None


def describe_songs(db, songs: list[dict]):
    ids = list({s["id"] for s in songs})
    credits = credits_of(db, ids)
    tracks = {r["id"]: r for r in db.execute(f"select id, isrc, genres, genre, mb_recording from track where id in ({','.join('?' * len(ids))})", ids)}
    for s in songs:
        mine, track = credits.get(s["id"], []), tracks.get(s["id"])
        artists, display = people(mine, "artist")
        album_artists, display_album = people(mine, "albumartist")
        contributors = [clean({"role": c["role"], "subRole": c["sub_role"], "artist": {"id": c["artist_id"], "name": c["name"]}})
                        for c in mine if c["role"] not in ("artist", "albumartist")]
        composers = [c["name"] for c in mine if c["role"] == "composer"] or [c["name"] for c in mine if c["role"] == "writer"]
        s.update(clean({"artists": artists or None, "displayArtist": display, "albumArtists": album_artists or None,
                        "displayAlbumArtist": display_album, "contributors": contributors or None,
                        "displayComposer": ", ".join(dict.fromkeys(composers)) or None}))
        if track is not None:
            s.update(clean({"isrc": track["isrc"].split("; ") if track["isrc"] else None, "musicBrainzId": track["mb_recording"],
                            "genres": genre_list(track)}))


def describe_albums(db, albums: list[dict]):
    ids = list({a["id"] for a in albums})
    tracks = {r["album_id"]: r for r in db.execute(
        f"select album_id, min(id) as id, mb_release, label, release_types, original_date, genres, genre from track "
        f"where album_id in ({','.join('?' * len(ids))}) group by album_id", ids)}
    credits = credits_of(db, [t["id"] for t in tracks.values()]) if tracks else {}
    for a in albums:
        track = tracks.get(a["id"])
        if track is None:
            continue
        artists, display = people(credits.get(track["id"], []), "albumartist")
        types = [t.strip() for t in (track["release_types"] or "").split(";") if t.strip()]
        date = [int(x) for x in (track["original_date"] or "").split("-") if x.isdigit()]
        a.update(clean({"artists": artists or None, "displayArtist": display, "musicBrainzId": track["mb_release"],
                        "isCompilation": "compilation" in types or None, "recordLabels": [{"name": l} for l in track["label"].split("; ")] if track["label"] else None,
                        "genres": genre_list(track),
                        "originalReleaseDate": dict(zip(("year", "month", "day"), date)) if date else None}))
        if types and not a.get("releaseTypes"):
            a["releaseTypes"] = [t.title() for t in types]


def decode_password(value: str) -> str:
    return bytes.fromhex(value[4:]).decode() if value.startswith("enc:") else value


def is_radio(playlist_name: str) -> int:
    """Playlists named "Radio ..." grow by themselves from the tracks played in them."""
    return int(playlist_name.casefold().startswith("radio"))


def index_letter(name: str) -> str:
    for article in ("The ", "A "):
        if name.startswith(article):
            name = name[len(article):]
    letter = name[:1].upper()
    return letter if letter.isalpha() else "#"


class Api:
    def __init__(self, config: Config, ytm=None, services=None, background=True):
        self.config, self.background = config, background
        self.ytm = ytm or (YouTubeMusic(config.ytm_cache) if config.ytm_cache else None)
        db = self._db()
        accounts.sync_config_users(db, config.users)
        if not config.lastfm_key:  # oasis.toml wins over the dashboard
            settings = dict(db.execute("select key, value from setting where key in ('lastfm_key', 'lastfm_secret')").fetchall())
            config.lastfm_key, config.lastfm_secret = settings.get("lastfm_key"), settings.get("lastfm_secret")
        artists.reidentify(db)
        artists.configure(db, self._artist_settings(db))
        db.close()
        services_for = (lambda db, user: services) if services is not None else \
            (lambda db, user: forward.user_services(config, self.ytm, accounts.get(db, user)))
        self.forwarder = Forwarder(self._db, services_for, self.ytm.match if self.ytm else None)
        lastfm = LastFm(config.lastfm_key, config.lastfm_secret or "", None) if config.lastfm_key else None
        self.recommender = Recommender(self._db, self.ytm, lastfm)
        self.completed: set[str] = set()
        lidarr = Lidarr(**{k: v for k, v in config.lidarr.items() if k in LIDARR_KEYS}) if config.lidarr else None
        torbox = config.torbox if lidarr else None
        self.doctor = Doctor(self._db, lidarr, **{k: v for k, v in torbox.items() if k in DOCTOR_KEYS}) if torbox else None
        # Its own importer: the doctor's dry run mustn't stop downloads.
        self.downloads = Downloads(self._db, lidarr, Doctor(self._db, lidarr, torbox["rdt_db"]), torbox["downloads"], torbox.get("max_active", 8),
                                   torbox.get("stall_hours", 1), self._start_scan,
                                   Importer(self._db, config.library[0]) if torbox.get("import", "oasis") == "oasis" else None,
                                   Prowlarr(**config.prowlarr) if config.prowlarr else None) \
            if torbox and torbox.get("downloads") else None
        # oasis's own catalogue needs oasis's search, download and import; else, or with catalogue = "lidarr", Lidarr's.
        own = self.downloads and self.downloads.prowlarr and self.downloads.own and config.acquire.get("catalogue", "oasis") == "oasis"
        self.library = Acquire(self._db, self.downloads, lidarr, config.acquire.get("secondary_types", SECONDARY),
                               config.acquire.get("bundles", True)) if own else \
            Library(self._db, lidarr, self.downloads) if lidarr else None
        self.scanning = self.scan_again = False
        self.scan_lock = threading.Lock()
        self.credits = Credits(self._db)

    def _db(self):
        return connect(self.config.database)

    def _artist_settings(self, db) -> dict:
        """The dashboard's artist settings; oasis.toml's [artists] until they are first saved there."""
        row = db.execute("select value from setting where key = 'artists'").fetchone()
        return json.loads(row[0]) if row else self.config.artists

    def _set_artist_settings(self, db, settings: dict):
        db.execute("insert or replace into setting values ('artists', ?)", (json.dumps(settings, ensure_ascii=False),))
        artists.configure(db, settings)

    def _set_lastfm_app(self, db, key: str, secret: str):
        """Checks a Last.fm API key and secret with a signed call, then keeps and uses them."""
        LastFm(key, secret, None).call({"method": "auth.getToken"})
        db.executemany("insert or replace into setting values (?, ?)", [("lastfm_key", key), ("lastfm_secret", secret)])
        self.config.lastfm_key, self.config.lastfm_secret = key, secret
        self.recommender.lastfm = LastFm(key, secret, None)

    def _start_scan(self):
        """A scan asked for while one runs follows it: files an import placed meanwhile may be past it."""
        if not self.scan_lock.acquire(blocking=False):
            self.scan_again = True
            return
        self.scanning = True

        def run():
            db = self._db()
            try:
                while True:
                    self.scan_again = False
                    scan.scan(db, self.config.library)
                    if self.config.dedupe:
                        self._dedupe(db)
                    self.credits.wake.set()
                    if not self.scan_again:
                        break
            finally:
                db.close()
                self.scanning = False
                self.scan_lock.release()
        threading.Thread(target=run, daemon=True).start()

    def _dedupe(self, db):
        replaced = dedupe.run(db)
        if not replaced:
            return
        scan.scan(db, self.config.library)
        renamed = [(old, new) for old, new in replaced if old != new]  # took the kept copy's format, so a new path and id
        for old, new in renamed:
            move_track(db, make_id("tr-", old), make_id("tr-", new))
        db.commit()
        if renamed and self.library and self.library.lidarr:
            try:
                self.library.lidarr.rescan(sorted({str(Path(new).parent) for _, new in renamed}))
            except OSError as e:
                log.warning("dedupe: Lidarr rescan failed: %s", e)

    def _authenticate(self, db, p: Params) -> str:
        user = p.need("u")
        row = accounts.get(db, user)
        if row is None:
            raise Fail(AUTH, "Wrong username or password")
        password = row["password"]
        if (token := p.get("t")) is not None:
            ok = hashlib.md5((password + p.need("s")).encode()).hexdigest() == token.lower()
        else:
            ok = decode_password(p.need("p")) == password
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
        return {"openSubsonicExtensions": [{"name": name, "versions": [1]} for name in ("formPost", "songLyrics", "transcodeOffset")]}

    def _user(self, row) -> dict:
        roles = ("scrobbling", "settings", "download", "playlist", "coverArt", "comment", "stream", "share")
        return {"username": row["name"], "adminRole": bool(row["admin"]), **{f"{r}Role": True for r in roles},
                **{f"{r}Role": False for r in ("upload", "podcast", "jukebox")}, "folder": [1]}

    def _admin(self, db, user):
        if not accounts.get(db, user)["admin"]:
            raise Fail(DENIED, "Only admins can do that")

    def getUser(self, db, p, user):
        name = p.get("username", user)
        if name != user:
            self._admin(db, user)
        return {"user": self._user(self._one(db, "select * from user where name = ?", name))}

    def getUsers(self, db, p, user):
        self._admin(db, user)
        return {"users": {"user": [self._user(r) for r in db.execute("select * from user order by name")]}}

    def createUser(self, db, p, user):
        self._admin(db, user)
        try:
            accounts.create(db, p.need("username"), decode_password(p.need("password")), p.get("adminRole") == "true")
        except ValueError as e:
            raise Fail(0, str(e))
        return {}

    def updateUser(self, db, p, user):
        self._admin(db, user)
        name = self._one(db, "select name from user where name = ?", p.need("username"))["name"]
        if (password := p.get("password")) is not None:
            accounts.set_field(db, name, password=decode_password(password))
        if (admin := p.get("adminRole")) is not None and name not in self.config.users:
            accounts.set_field(db, name, admin=int(admin == "true"))
        return {}

    def deleteUser(self, db, p, user):
        self._admin(db, user)
        name = p.need("username")
        if name in self.config.users:
            raise Fail(DENIED, "Users from oasis.toml can only be removed there")
        accounts.delete(db, name)
        return {}

    def changePassword(self, db, p, user):
        name = p.need("username")
        if name != user:
            self._admin(db, user)
        if name in self.config.users:
            raise Fail(DENIED, "This password is set in oasis.toml")
        self._one(db, "select 1 from user where name = ?", name)
        accounts.set_field(db, name, password=decode_password(p.need("password")))
        return {}

    def oasisLinkYouTubeMusic(self, db, p, user):
        """For clients that already have a YouTube Music sign-in (roma): cookie is the music.youtube.com Cookie header
        or the request headers; authUser the X-Goog-AuthUser index, 0 by default."""
        try:
            headers = accounts.youtube_headers(p.need("cookie"))
        except ValueError as e:
            raise Fail(0, str(e))
        headers["x-goog-authuser"] = p.get("authUser", headers["x-goog-authuser"])
        return {"youtubeMusic": {"account": self._link_youtube(db, user, headers)}}

    def oasisUnlinkYouTubeMusic(self, db, p, user):
        accounts.set_field(db, user, ytm_auth=None, ytm_name=None)
        self._drop_mixes(db, user)
        return {}

    def _link_youtube(self, db, user, headers: dict) -> str:
        """Checks the sign-in by reading the account name, saves it, and loads the user's mixes."""
        if not self.ytm:
            raise Fail(0, "YouTube Music is not enabled on this server")
        auth = json.dumps(headers)
        try:
            name = self.ytm.account_name(self.ytm.client(auth))
        except Exception as e:
            self.ytm.clients.pop(auth, None)
            log.warning("YouTube Music sign-in for %s rejected: %s", user, e)
            raise Fail(0, "YouTube Music didn't accept that sign-in. Sign in again and retry.")
        accounts.set_field(db, user, ytm_auth=auth, ytm_name=name)
        if self.background:
            threading.Thread(target=self._refresh_mixes, args=(user,), daemon=True).start()
        return name

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
        return [song(r) for r in db.execute(SONGS + " where t.album_id = ? order by disc_no, track_no is null, track_no, title", (album_id,))]

    def getArtist(self, db, p, user):
        if acquire.mbid_of(id := p.need("id")):
            info = self._musicbrainz(releases.ALIASES.format(acquire.mbid_of(id)), db, releases.KEEP_ALIASES)
            groups = self._musicbrainz(acquire.release_groups, db, acquire.mbid_of(id))
            albums = [self._mb_album(db, g) for g in sorted(groups, key=lambda g: g.get("first-release-date") or "9999")
                      if g.get("primary-type") in acquire.TYPES]
            return {"artist": {**self._mb_artist(db, info), "albumCount": len(albums), "album": albums}}
        r = db.execute(ARTISTS + " where t.artist_id = ? group by t.artist_id", (p.need("id"),)).fetchone()
        if r is None:
            return self._credited_artist(db, p.need("id"))
        albums = self._artist_albums(db, r["id"])
        known = {a["id"] for a in albums}
        self._artist_page(db, r["id"], r["name"])
        # The rest of the artist's discography on YouTube Music, newest first; opening one fetches its tracks.
        albums += [clean({"id": x["album_id"], "name": x["title"], "title": x["title"], "album": x["title"], "artist": r["name"],
                          "artistId": r["id"], "parent": r["id"], "isDir": True, "coverArt": x["album_id"] if x["cover"] else None,
                          "year": x["year"], "releaseTypes": [x["type"].title()] if x["type"] else None})
                   for x in db.execute("select * from ytm_release where artist_id = ? order by coalesce(year, 0) desc, title", (r["id"],))
                   if x["album_id"] not in known]
        return {"artist": {**artist(r), "albumCount": len(albums), "album": albums}}

    def _credited_artist(self, db, artist_id):
        """An artist only credited on others' songs (a guest singer, a band member, a composer): the albums they're on."""
        name = self._one(db, "select name from credit where artist_id = ? order by source = 'tags', role != 'artist' limit 1", artist_id)["name"]
        albums = [album(r) for r in db.execute(ALBUMS + " where t.id in (select track_id from credit where artist_id = ?) "
                                               "group by t.album_id order by year, name", (artist_id,))]
        return {"artist": {"id": artist_id, "name": name, "albumCount": len(albums), "album": albums}}

    def _artist_page(self, db, artist_id, name):
        """Fetches the artist's YouTube Music page into ytm_artist and ytm_release, at most once a day."""
        page = db.execute("select fetched from ytm_artist where artist_id = ?", (artist_id,)).fetchone()
        if not self.ytm or (page and page["fetched"] > time.time() - 86400):
            return
        try:
            found = self.ytm.artist(name)
        except Exception as e:
            log.info("YouTube Music artist %s unavailable: %s", name, e)
            return
        db.execute("insert or replace into ytm_artist values (?, ?, ?, ?, ?, ?)", (artist_id, *(
            (found["name"], found["description"], found["image"], found["songs"]) if found else (name, None, None, None)), time.time()))
        for x in (found or {}).get("releases", []):
            # Ids follow the oasis artist, so these albums sit with the local ones and merge with an album of the same title.
            db.execute("insert or replace into ytm_release values (?, ?, ?, ?, ?, ?, ?)",
                       (make_id("al-", name, x["title"]), artist_id, x["browse_id"], x["title"], x["year"], x["type"], x["cover"]))

    def getAlbum(self, db, p, user):
        if acquire.mbid_of(id := p.need("id")):
            return {"album": {**self._mb_album(db, self._musicbrainz(acquire.GROUP.format(acquire.mbid_of(id)), db, acquire.KEEP_GROUP)),
                              "song": []}}  # nothing to play until it arrives
        self._complete_album(db, p.need("id"))
        r = self._one(db, ALBUMS + " where t.album_id = ? group by t.album_id", p.need("id"))
        return {"album": {**album(r), "song": self._album_songs(db, r["id"])}}

    def _complete_album(self, db, album_id):
        """Fetches the rest of an album that is only known from streaming tracks seen in search or radio, or only
        from an artist's YouTube Music page."""
        if not self.ytm or album_id in self.completed:
            return
        sources = db.execute("select source, ytm_album from track where album_id = ?", (album_id,)).fetchall()
        release = db.execute("select * from ytm_release where album_id = ?", (album_id,)).fetchone()
        browse_id = next((r["ytm_album"] for r in sources if r["ytm_album"]), None) or (release and release["browse_id"])
        if not browse_id or any(r["source"] == "local" for r in sources):
            return
        try:
            rows = self.ytm.album(browse_id)
        except Exception as e:
            log.info("album %s unavailable: %s", browse_id, e)
            return
        ids = [remember(db, row) for row in rows]
        if release:  # file them under the artist page's album, whatever spelling YouTube credits the tracks with
            db.executemany("update track set album_id = ?, artist_id = ? where id = ? and source = 'ytm'",
                           [(album_id, release["artist_id"], id) for id in ids])
        self.completed.add(album_id)

    def getSong(self, db, p, user):
        r = self._one(db, SONGS + " where t.id = ?", p.need("id"))
        if r["ytm_id"] is None and self.ytm:
            # Finds the song on YouTube Music once, for share links; "" remembers that it isn't there.
            try:
                db.execute("update track set ytm_id = ? where id = ?", (self.ytm.match(r) or "", r["id"]))
                r = self._one(db, SONGS + " where t.id = ?", r["id"])
            except Exception as e:
                log.info("YouTube Music match for %s failed: %s", r["id"], e)
        return {"song": song(r)}

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
        artists = [artist(r) for r in artists]
        albums = db.execute(ALBUMS + f" where {cond} group by t.album_id order by name collate nocase limit ? offset ?",
                            (*args, p.int("albumCount", 20), p.int("albumOffset", 0)))
        cond, args = match("t.title || ' ' || t.artist || ' ' || t.album")
        songs = db.execute(SONGS + f" where {cond} order by t.album_artist, t.album, t.disc_no, t.track_no limit ? offset ?",
                           (*args, p.int("songCount", 20), p.int("songOffset", 0)))
        songs = [song(r) for r in songs]
        if self.ytm and terms and p.int("songOffset", 0) == 0:
            known = {s["id"] for s in songs}
            try:
                found = [remember(db, row) for row in self.ytm.search(" ".join(terms), p.int("songCount", 20))]
            except Exception as e:
                log.warning("YouTube Music search failed: %s", e)
                found = []
            songs += [song(self._one(db, SONGS + " where t.id = ?", id)) for id in dict.fromkeys(found) if id not in known]
        albums = [album(r) for r in albums]
        text = " ".join(terms)
        if isinstance(self.library, Acquire) and len(text) >= FIND_FROM:
            # MusicBrainz last, for what neither the library nor YouTube Music has: these can be starred like any other.
            names = {ytm.fold(x) for x in [*(a["name"] for a in artists), *(s.get("artist") for s in songs)] if x}
            titles = {ytm.fold(x) for x in [*(a["name"] for a in albums), *(s.get("album") for s in songs)] if x}
            if p.int("artistOffset", 0) == 0 and (room := p.int("artistCount", 20) - len(artists)) > 0:
                artists += [self._mb_artist(db, a) for a in self._find(db, "artist", text)
                            if ytm.fold(a["name"]) not in names and not acquire.library_id(db, MB_IDS["artist"] + a["id"])][:room]
            if p.int("albumOffset", 0) == 0 and (room := p.int("albumCount", 20) - len(albums)) > 0:
                albums += [self._mb_album(db, g) for g in self._find(db, "album", text)
                           if ytm.fold(g["title"]) not in titles and not acquire.library_id(db, MB_IDS["album"] + g["id"])][:room]
        return {"artist": artists, "album": albums, "song": songs}

    def _find(self, db, kind, text) -> list[dict]:
        try:
            return acquire.find(db, kind, text)
        except (OSError, LookupError) as e:
            log.warning("MusicBrainz search failed: %s", e)
            return []

    def _musicbrainz(self, get, *args):
        """get(*args) for a MusicBrainz-only item: an URL, or a function asking MusicBrainz."""
        try:
            return musicbrainz.get(get, *args) if isinstance(get, str) else get(*args)
        except LookupError:
            raise Fail(NOT_FOUND, "Data not found") from None
        except OSError as e:
            raise Fail(0, f"MusicBrainz didn't answer: {e}") from None

    def _wanted(self, db, kind, id) -> dict:
        """Starred and oasisWanted of an artist or album, as album() and artist() give them."""
        starred = db.execute("select starred from stat where id = ?", (id,)).fetchone()
        item = db.execute("select state, error from lidarr_item where kind = ? and ref = ?", (kind, id)).fetchone()
        state = WANTED_STATES.get(item and item["state"])
        return {"starred": iso(starred and starred[0]), "oasisWanted": state, "oasisWantedError": item["error"] if state else None}

    def _mb_artist(self, db, a: dict) -> dict:
        """A MusicBrainz artist (search result or lookup) the library doesn't have."""
        id = MB_IDS["artist"] + a["id"]
        return clean({"id": id, "name": a["name"], "albumCount": 0, "musicBrainzId": a["id"], **self._wanted(db, "artist", id)})

    def _mb_album(self, db, g: dict) -> dict:
        """A MusicBrainz release group: the library's album when it has arrived, else one without songs."""
        if local := acquire.library_id(db, MB_IDS["album"] + g["id"]):
            return album(self._one(db, ALBUMS + " where t.album_id = ? group by t.album_id", local))
        id, credit = MB_IDS["album"] + g["id"], g.get("artist-credit") or []
        artist_id = MB_IDS["artist"] + credit[0]["artist"]["id"] if credit else None
        artist_id = artist_id and (acquire.library_id(db, artist_id) or artist_id)
        year = (g.get("first-release-date") or "")[:4]
        return clean({"id": id, "name": g["title"], "title": g["title"], "album": g["title"],
                      "artist": "".join(c["name"] + (c.get("joinphrase") or "") for c in credit) or None, "artistId": artist_id,
                      "parent": artist_id, "isDir": True, "songCount": 0, "duration": 0, "year": int(year) if year.isdigit() else None,
                      "releaseTypes": [t for t in [g.get("primary-type"), *(g.get("secondary-types") or [])] if t] or None,
                      **self._wanted(db, "album", id)})

    def _touch(self, db, ids):
        """Streaming tracks are in the library while starred or in a playlist (radios aside)."""
        for id in ids:
            r = db.execute("select ref, library from track where id = ? and source = 'ytm'", (id,)).fetchone()
            if r is None:
                continue
            starred = db.execute("select 1 from stat where id = ? and starred is not null", (id,)).fetchone()
            playlists = db.execute("select p.mix from playlist_entry e join playlist p on p.id = e.playlist_id "
                                   "where e.track_id = ? and p.radio = 0", (id,)).fetchall()
            db.execute("update track set library = ? where id = ?", (int(bool(starred or playlists)), id))
            # Mixes put their tracks in the library but change daily, so only the user's own choices download.
            wanted = starred or any(row["mix"] is None for row in playlists)
            if wanted and self.ytm and not self.ytm.cached(r["ref"]):
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

    def _set_starred(self, db, p, value, user):
        for id in p.all("id"):
            db.execute("insert into stat(id, starred) values (?, ?) on conflict(id) do update set starred = excluded.starred", (id, value))
        self._touch(db, p.all("id"))
        self._want(db, user, "album", p.all("albumId"), value)
        self._want(db, user, "artist", p.all("artistId"), value)
        return {}

    def _want(self, db, user, kind, ids, value):
        """Stars albums or artists, or takes the star back (value None): a starred album or artist is one the user
        wants in the library (see acquire.py, or lidarr.py). The dashboard's want and follow are stars too."""
        for id in ids:
            db.execute("insert into stat(id, starred) values (?, ?) on conflict(id) do update set starred = excluded.starred", (id, value))
            if value:
                db.execute("insert or ignore into wanted values (?, ?, ?)", (user, kind, id))
            else:
                db.execute("delete from wanted where user = ? and kind = ? and ref = ?", (user, kind, id))
        if self.library and ids:
            self.library.wake.set()

    def star(self, db, p, user):
        return self._set_starred(db, p, time.time(), user)

    def unstar(self, db, p, user):
        return self._set_starred(db, p, None, user)

    def setRating(self, db, p, user):
        rating = p.int("rating", 0) or None
        db.execute("insert into stat(id, rating) values (?, ?) on conflict(id) do update set rating = excluded.rating", (p.need("id"), rating))
        return {}

    def scrobble(self, db, p, user):
        if p.get("submission", "true") == "false":
            for id in p.all("id")[:1]:
                self.forwarder.now_playing(db, user, forward.listen_for(self._one(db, "select * from track where id = ?", id), time.time()))
            return {}
        # Every id is checked before anything is written, so a retried batch can't count plays twice.
        times = p.all("time")
        plays = [(id, int(times[i]) / 1000 if i < len(times) else time.time()) for i, id in enumerate(p.all("id"))]
        known = [(id, played) for id, played in plays if db.execute("select 1 from track where id = ?", (id,)).fetchone()]
        if plays and not known:
            raise Fail(NOT_FOUND, "Data not found")
        for id, _ in set(plays) - set(known):
            log.info("scrobble for unknown song %s skipped", id)
        for id, played in known:
            db.execute("insert into stat(id, play_count, played) values (?, 1, ?) on conflict(id) do update "
                       "set play_count = play_count + 1, played = excluded.played", (id, played))
            db.execute("insert into play values (?, ?, ?)", (user, id, played))
            self.forwarder.enqueue(db, user, id, played)
        self.recommender.played(db, user, [id for id, _ in known])
        return {}

    def _lyrics(self, db, r) -> lyrics.Lyrics | None:
        """The track's own lyrics first; otherwise the providers' pick, cached (misses for a day)."""
        if r["source"] == "local" and (found := lyrics.local(r["ref"])):
            return found
        cached = db.execute("select found, fetched from lyrics where track_id = ?", (r["id"],)).fetchone()
        if cached and (cached["found"] or cached["fetched"] > time.time() - 86400):
            return lyrics.Lyrics(**json.loads(cached["found"])) if cached["found"] else None
        youtube = None
        if self.ytm:
            def youtube():
                video_id = r["ytm_id"] or self.ytm.match(r)
                return self.ytm.lyrics(video_id) if video_id else None
        artist = r["album_artist"] if r["source"] == "ytm" else r["artist"]
        album = r["album"] if r["album"] != r["title"] else None
        found = lyrics.find(lyrics.Query(r["title"], artist, album, r["duration"] or 0), youtube)
        db.execute("insert or replace into lyrics values (?, ?, ?)",
                   (r["id"], json.dumps(dataclasses.asdict(found)) if found else None, time.time()))
        return found

    def getLyricsBySongId(self, db, p, user):
        r = self._one(db, "select * from track where id = ?", p.need("id"))
        found = self._lyrics(db, r)
        if not found:
            return {"lyricsList": {}}
        return {"lyricsList": {"structuredLyrics": [{
            "displayArtist": r["artist"], "displayTitle": r["title"], "lang": "xxx", "offset": 0, "synced": found.synced,
            "line": [clean({"start": start, "value": text}) for start, text in found.lines]}]}}

    def getLyrics(self, db, p, user):
        artist, title = p.get("artist", ""), p.get("title", "")
        r = db.execute("select * from track where title = ? collate nocase and (? = '' or artist = ? collate nocase "
                       "or album_artist = ? collate nocase) order by library desc limit 1", (title, artist, artist, artist)).fetchone()
        found = self._lyrics(db, r) if r else None
        text = found.text() if found else None
        return {"lyrics": clean({"artist": r["artist"] if r else artist, "title": r["title"] if r else title, "value": text})}

    def getNowPlaying(self, db, p, user):
        return {"nowPlaying": {}}

    def _artist_info(self, db, p):
        r = db.execute(ARTISTS + " where t.artist_id = ? group by t.artist_id", (p.need("id"),)).fetchone()
        if r is None:  # only credited on others' songs
            self._one(db, "select 1 from credit where artist_id = ? limit 1", p.need("id"))
            return clean({"similarArtist": self._linked_artists(db, p.need("id")) or None})
        self._artist_page(db, r["id"], r["name"])
        page = db.execute("select description, image from ytm_artist where artist_id = ?", (r["id"],)).fetchone()
        youtube = clean({"biography": page["description"], "smallImageUrl": page["image"], "mediumImageUrl": page["image"],
                         "largeImageUrl": page["image"]}) if page else {}
        linked = self._linked_artists(db, r["id"])
        if not self.recommender.lastfm:
            return {**youtube, **clean({"similarArtist": linked or None})}
        try:
            info = self.recommender.lastfm.artist_info(r["name"])
        except Exception as e:
            log.info("Last.fm artist info failed: %s", e)
            return {**youtube, **clean({"similarArtist": linked or None})}
        names = [a["name"] for a in info.get("similar", {}).get("artist", [])][:p.int("count", 20)]
        similar = [artist(a) for name in names for a in db.execute(
            ARTISTS + " where t.library = 1 and t.album_artist = ? collate nocase group by t.artist_id", (name,))]
        similar = linked + [a for a in similar if a["id"] not in {x["id"] for x in linked}]
        bio = info.get("bio", {}).get("summary", "")
        return {**youtube, **clean({"biography": bio.split("<a href")[0].strip() or None, "lastFmUrl": info.get("url"),
                                    "musicBrainzId": info.get("mbid") or None, "similarArtist": similar or None})}

    def _linked_artists(self, db, artist_id):
        """Personas, the user's related artists and band members that have songs here, closest first; oasisLink says how."""
        found = []
        for other, kind in artists.linked(db, artist_id):
            if r := db.execute(ARTISTS + " where t.artist_id = ? group by t.artist_id", (other,)).fetchone():
                found.append({**artist(r), "oasisLink": kind})
            elif c := db.execute("select name from credit where artist_id = ? order by source = 'tags' limit 1", (other,)).fetchone():
                found.append({"id": other, "name": c["name"], "oasisLink": kind})
        return found

    def getArtistInfo2(self, db, p, user):
        return {"artistInfo2": self._artist_info(db, p)}

    def getArtistInfo(self, db, p, user):
        return {"artistInfo": self._artist_info(db, p)}

    def getAlbumInfo2(self, db, p, user):
        return {"albumInfo": {}}

    def getAlbumInfo(self, db, p, user):
        return {"albumInfo": {}}

    def _similar(self, db, p):
        id = p.need("id")
        column = {"ar-": "artist_id", "al-": "album_id"}.get(id[:3], "id")
        seed = self._one(db, SONGS + f" where t.{column} = ? order by s.play_count desc, t.library desc, t.disc_no, t.track_no limit 1", id)
        try:
            ids = self.recommender.similar(db, seed, min(p.int("count", 50), 200))
        except Exception as e:
            log.warning("recommendations failed: %s", e)
            ids = []
        return [song(self._one(db, SONGS + " where t.id = ?", i)) for i in ids]

    def getSimilarSongs2(self, db, p, user):
        return {"similarSongs2": {"song": self._similar(db, p)}}

    def getSimilarSongs(self, db, p, user):
        return {"similarSongs": {"song": self._similar(db, p)}}

    def getTopSongs(self, db, p, user):
        name, count = p.need("artist"), min(p.int("count", 50), 200)
        ids = [r[0] for r in db.execute("select t.id from track t left join stat s on s.id = t.id where t.library = 1 and "
                                        "t.album_artist = ? collate nocase order by coalesce(s.play_count, 0) desc limit ?", (name, count))]
        if self.ytm and len(ids) < count:
            try:
                # The artist's own top songs list on YouTube Music; a search when the page has none.
                owner = db.execute("select artist_id from track where album_artist = ? collate nocase limit 1", (name,)).fetchone()
                if owner:
                    self._artist_page(db, owner[0], name)
                page = owner and db.execute("select songs from ytm_artist where artist_id = ?", (owner[0],)).fetchone()
                rows = self.ytm.playlist_rows(page["songs"], count) if page and page["songs"] else \
                    [r for r in self.ytm.search(name, count) if name.casefold() in (r["album_artist"].casefold(), r["artist"].casefold())]
                ids += [remember(db, r) for r in rows]
            except Exception as e:
                log.warning("YouTube Music top songs failed: %s", e)
        return {"topSongs": {"song": [song(self._one(db, SONGS + " where t.id = ?", i)) for i in dict.fromkeys(ids)][:count]}}

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
            "id": str(r["id"]), "name": r["name"], "comment": r["comment"], "owner": MIX_OWNER if r["mix"] else r["owner"], "public": False,
            "coverArt": f"pl-{r['id']}" if r["cover"] else None,
            "oasisMix": True if (r["mix"] or "").startswith("RDTMAK") else None, "youtubeId": r["mix"],
            "songCount": len(songs), "duration": sum(s.get("duration", 0) for s in songs),
            "created": iso(r["created"]), "changed": iso(r["changed"]),
        })
        return {**result, "entry": songs} if entries else result

    def getPlaylists(self, db, p, user):
        rows = sorted(db.execute("select * from playlist where owner = ?", (user,)), key=playlist_order)
        return {"playlists": {"playlist": [self._playlist(db, r, False) for r in rows]}}

    def _own_playlist(self, db, id, user):
        return self._one(db, "select * from playlist where id = ? and owner = ? and mix is null", id, user)

    def getPlaylist(self, db, p, user):
        r = self._one(db, "select * from playlist where id = ? and owner = ?", p.need("id"), user)
        return {"playlist": self._playlist(db, r, True)}

    def _refresh_mixes(self, user: str):
        """Mirrors the user's YouTube Music mixes and library playlists as read-only playlists shown as owned by
        "YouTube Music"."""
        db = self._db()
        try:
            row = accounts.get(db, user)
            if not (self.ytm and row and row["ytm_auth"]):
                return
            client = self.ytm.client(row["ytm_auth"])
            mixes = self.ytm.playlists(client)
            for mix in mixes:
                tracks = list(dict.fromkeys(remember(db, row) for row in self.ytm.playlist_tracks(client, mix["id"])))
                found = db.execute("select id from playlist where owner = ? and mix = ?", (user, mix["id"])).fetchone()
                if found:
                    playlist_id = found["id"]
                    db.execute("update playlist set name = ?, comment = ?, cover = ? where id = ?",
                               (mix["title"], mix["description"], mix["cover"], playlist_id))
                else:
                    now = time.time()
                    playlist_id = db.execute("insert into playlist(name, owner, comment, created, changed, mix, cover) "
                                             "values (?, ?, ?, ?, ?, ?, ?)", (mix["title"], user, mix["description"],
                                             now, now, mix["id"], mix["cover"])).lastrowid
                self._set_entries(db, playlist_id, tracks)
            self._drop_mixes(db, user, keep=[m["id"] for m in mixes])
            log.info("mixes for %s: %d refreshed", user, len(mixes))
        except Exception as e:
            log.warning("mix refresh for %s failed: %s", user, e)
        finally:
            db.close()

    def _drop_mixes(self, db, user, keep=()):
        for gone in db.execute(f"select id from playlist where owner = ? and mix is not null and mix not in ({','.join('?' * len(keep))})",
                               (user, *keep)).fetchall():
            self._set_entries(db, gone["id"], [])
            db.execute("delete from playlist where id = ?", (gone["id"],))

    def _mix_loop(self):
        while True:
            db = self._db()
            users = [r["name"] for r in db.execute("select name from user where ytm_auth is not null")]
            db.close()
            for user in users:
                self._refresh_mixes(user)
            time.sleep(MIX_REFRESH)

    def _set_entries(self, db, playlist_id, track_ids):
        old = [r[0] for r in db.execute("select track_id from playlist_entry where playlist_id = ?", (playlist_id,))]
        set_entries(db, playlist_id, track_ids)
        self._touch(db, set(old) | set(track_ids))

    def createPlaylist(self, db, p, user):
        if id := p.get("playlistId"):
            playlist_id = self._own_playlist(db, id, user)["id"]
        else:
            now = time.time()
            name = p.need("name")
            playlist_id = db.execute("insert into playlist(name, owner, created, changed, radio) values (?, ?, ?, ?, ?)",
                                     (name, user, now, now, is_radio(name))).lastrowid
        self._set_entries(db, playlist_id, p.all("songId"))
        self.recommender.wake.set()
        return {"playlist": self._playlist(db, self._own_playlist(db, playlist_id, user), True)}

    def updatePlaylist(self, db, p, user):
        r = self._own_playlist(db, p.need("playlistId"), user)
        for column in ("name", "comment"):
            if (value := p.get(column)) is not None:
                db.execute(f"update playlist set {column} = ?, changed = ? where id = ?", (value, time.time(), r["id"]))
        if (name := p.get("name")) is not None and r["radio"] != 2:
            db.execute("update playlist set radio = ? where id = ?", (is_radio(name), r["id"]))
        entries = [e["track_id"] for e in db.execute("select track_id from playlist_entry where playlist_id = ? order by pos", (r["id"],))]
        removed = {int(i) for i in p.all("songIndexToRemove")}
        added = p.all("songIdToAdd")
        if removed or added:
            self._set_entries(db, r["id"], [t for i, t in enumerate(entries) if i not in removed] + added)
        self.recommender.wake.set()
        return {}

    def deletePlaylist(self, db, p, user):
        playlist_id = self._own_playlist(db, p.need("id"), user)["id"]
        self._set_entries(db, playlist_id, [])
        db.execute("delete from playlist where id = ?", (playlist_id,))
        return {}

    def getCoverArt(self, db, p, user):
        id = p.need("id")
        if id.startswith("pl-"):
            r = self._one(db, "select null as ref, cover from playlist where id = ? and cover is not null", id[3:])
        else:
            column = {"ar-": "artist_id", "al-": "album_id"}.get(id[:3], "id")
            r = db.execute(f"select ref, cover from track where {column} = ? and cover is not null "
                           "order by source != 'local', cover = 'embedded', disc_no, track_no limit 1", (id,)).fetchone() or \
                self._one(db, "select null as ref, cover from ytm_release where album_id = ? and cover is not null", id)
        try:
            image = cover.load(r["ref"], r["cover"])
        except OSError as e:
            log.warning("cover %s unavailable: %s", r["cover"], e)
            image = None
        if image is None:
            raise Fail(NOT_FOUND, "Cover art not found")
        return Response(image[0], media_type=image[1], headers={"Cache-Control": "public, max-age=86400"})

    def stream(self, db, p, user):
        r = self._one(db, "select source, ref, content_type, suffix, bitrate from track where id = ?", p.need("id"))
        target = transcode_target(p.get("format"), p.int("maxBitRate", 0), r["suffix"], r["bitrate"])
        if r["source"] == "local":
            if target:
                return transcode(r["ref"], *target, p.int("timeOffset", 0))
            return FileResponse(r["ref"], media_type=r["content_type"])
        if not self.ytm:
            raise Fail(NOT_FOUND, "YouTube Music is not enabled")
        if path := self.ytm.cached(r["ref"]):
            if target:
                return transcode(str(path), *target, p.int("timeOffset", 0))
            return FileResponse(path, media_type=r["content_type"])
        try:
            stream = self.ytm.resolve(r["ref"])
        except Exception as e:
            log.info("no direct stream for %s (%s), downloading", r["ref"], e)
            try:
                return FileResponse(self.ytm.fetch(r["ref"]), media_type=r["content_type"])
            except Exception as e:
                log.warning("download of %s failed: %s", r["ref"], e)
                raise Fail(NOT_FOUND, "Stream unavailable")
        # Play straight from YouTube while the cache fills, so playback starts in well under a second.
        threading.Thread(target=self._prefetch, args=(r["ref"],), daemon=True).start()
        if target:
            return transcode(stream.url, *target, p.int("timeOffset", 0))
        return self._proxy(r["ref"], stream, p.range)

    def _proxy(self, video_id, stream, range_header):
        start, end = 0, stream.size - 1
        if m := re.fullmatch(r"bytes=(\d*)-(\d*)", range_header or ""):
            if m[1]:
                start, end = int(m[1]), min(int(m[2]), end) if m[2] else end
            elif m[2]:
                start = max(0, stream.size - int(m[2]))
            if start > end:
                return Response(status_code=416, headers={"Content-Range": f"bytes */{stream.size}"})

        def body():
            try:
                yield from ytm.read_range(stream.url, start, end)
            except Exception as e:
                log.warning("stream of %s broke: %s", video_id, e)
                self.ytm.forget(video_id)

        headers = {"Accept-Ranges": "bytes", "Content-Length": str(end - start + 1)}
        if range_header:
            headers["Content-Range"] = f"bytes {start}-{end}/{stream.size}"
        return StreamingResponse(body(), status_code=206 if range_header else 200, media_type=stream.content_type, headers=headers)

    def download(self, db, p, user):
        return self.stream(db, p, user)


TRANSCODE = {  # format: ffmpeg codec, container, content type
    "opus": ("libopus", "webm", "audio/webm"), "mp3": ("libmp3lame", "mp3", "audio/mpeg"), "aac": ("aac", "adts", "audio/aac"),
}


def transcode_target(fmt: str | None, max_bitrate: int, suffix: str, bitrate: int | None) -> tuple[str, int] | None:
    """The (format, kbit/s) to transcode to, or None to send the file as it is. Needs ffmpeg."""
    if not shutil.which("ffmpeg") or fmt == "raw":
        return None
    if fmt in TRANSCODE and fmt != suffix:
        return fmt, min(max_bitrate or 192, 320)
    if max_bitrate and bitrate and max_bitrate < bitrate:
        return "opus", max_bitrate
    return None


def transcode(source: str, fmt: str, kbps: int, offset: int) -> StreamingResponse:
    codec, container, content_type = TRANSCODE[fmt]
    web = ["-user_agent", ytm.VISIONOS_UA] if source.startswith("https://") else []
    command = ["ffmpeg", "-v", "error", "-ss", str(offset), *web, "-i", source,
               "-map", "0:a:0", "-c:a", codec, "-b:a", f"{kbps}k", "-f", container, "pipe:1"]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL)

    def body():
        try:
            while data := process.stdout.read(1 << 16):
                yield data
        finally:
            process.kill()
            process.wait()
    return StreamingResponse(body(), media_type=content_type)


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


def create_app(config: Config, background: bool = True, ytm=None, services=None) -> Starlette:
    """background starts the scan and the forwarding, radio and mix workers; tests drive those themselves."""
    api = Api(config, ytm, services, background)

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
                user = api._authenticate(db, p)
                p.follow_moves(db)
                result = handler(api, db, p, user)
                return result if isinstance(result, Response) else respond(fmt, describe(db, result))
            except Fail as e:
                return respond(fmt, {}, e)
            finally:
                db.close()
        return await run_in_threadpool(run)

    @asynccontextmanager
    async def lifespan(app):
        if not background:
            yield
            return
        api._start_scan()
        api.forwarder.start()
        if api.library:
            api.library.start()
        if api.doctor:
            api.doctor.start()
        if api.downloads:
            api.downloads.start()
        if api.ytm:
            threading.Thread(target=api._mix_loop, daemon=True).start()
        api.recommender.start()
        api.recommender.wake.set()
        api.credits.start()
        yield

    app = Starlette(routes=[Route("/rest/{method}", endpoint, methods=["GET", "POST"]), *web.routes(api)], lifespan=lifespan)
    app.state.api = api
    return app
