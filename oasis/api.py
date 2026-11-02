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

import anyio
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from . import accounts, acquire, artists, cover, dedupe, forward, latin, lyrics, musicbrainz, releases, romanize, scan, upload, web, ytm
from .acquire import MB_IDS, SECONDARY, Acquire
from .config import Config
from .credits import PERFORMANCE_ROLES, Credits
from .db import connect, make_id, move_track, remember, resolve
from .doctor import Doctor
from .download import Downloads
from .importer import Importer
from .forward import Forwarder, LastFm
from .prowlarr import Prowlarr
from .recommend import Recommender, set_entries
from .upload import Uploads
from .ytm import YouTubeMusic

log = logging.getLogger(__name__)

VERSION = "1.16.1"
NOT_FOUND, MISSING, AUTH, MECHANISM, DENIED = 70, 10, 40, 42, 50
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
    def __init__(self, code: int, message: str, extra: dict | None = None):
        self.code, self.message, self.extra = code, message, extra or {}


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
YTM_SEARCH = 50  # YouTube Music songs a search asks for at most


class Params:
    def __init__(self, request: Request, body: bytes):
        self.multi = [*request.query_params.multi_items(), *parse_qsl(body.decode())]
        self.range = request.headers.get("range")
        self.oasis = self.get("oasis") == "1"  # the oasis extension (docs/oasis-extension.md)
        self.address = request.client.host if request.client else ""

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
# Stars, ratings and plays are the requesting user's: me() (db.connect) names them for each request.
SONGS = f"select {SONG_COLUMNS} from track t left join stat s on s.id = t.id and s.user = me()"
ALBUMS = """
select t.album_id as id, t.album as name, t.album_artist as artist, t.artist_id,
       count(*) as song_count, sum(t.duration) as duration, min(t.created) as created,
       coalesce(max(t.year), (select x.year from ytm_release x where x.album_id = t.album_id)) as year,
       max(t.genre) as genre, max(t.cover is not null) as has_cover,
       s.starred as album_starred, s.rating as album_rating,
       coalesce(sum(ps.play_count), 0) as plays, max(ps.played) as last_played, wi.state as want_state, wi.error as want_error
from track t left join stat s on s.id = t.album_id and s.user = me() left join stat ps on ps.id = t.id and ps.user = me()
left join want_item wi on wi.kind = 'album' and wi.ref = t.album_id
"""
ARTISTS = """
select t.artist_id as id, t.album_artist as name, count(distinct t.album_id) as album_count,
       max(t.cover is not null) as has_cover, s.starred, s.rating, wi.state as want_state, wi.error as want_error
from track t left join stat s on s.id = t.artist_id and s.user = me() left join want_item wi on wi.kind = 'artist' and wi.ref = t.artist_id
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


DOCTOR_KEYS = ("rdt_db", "stall_hours", "dry_run")
# Acquisition progress of starred albums and artists, for clients that show it; complete albums and followed artists need no badge.
WANTED_STATES = {"wanted": "wanted", "downloading": "downloading", "failed": "failed"}


def album(r) -> dict:
    return clean({
        "id": r["id"], "name": r["name"], "title": r["name"], "album": r["name"], "artist": r["artist"],
        "artistId": r["artist_id"], "parent": r["artist_id"], "isDir": True,
        "coverArt": r["id"] if r["has_cover"] else None, "songCount": r["song_count"], "duration": r["duration"],
        "created": iso(r["created"]), "year": r["year"], "genre": r["genre"], "starred": iso(r["album_starred"]),
        "userRating": r["album_rating"], "playCount": r["plays"], "played": iso(r["last_played"]),
        "oasisWanted": WANTED_STATES.get(r["want_state"]),
        "oasisWantedError": r["want_error"] if WANTED_STATES.get(r["want_state"]) else None,
    })


def artist(r) -> dict:
    return clean({
        "id": r["id"], "name": r["name"], "albumCount": r["album_count"],
        "coverArt": r["id"] if r["has_cover"] else None, "starred": iso(r["starred"]), "userRating": r["rating"],
        "oasisWanted": WANTED_STATES.get(r["want_state"]),
        "oasisWantedError": r["want_error"] if WANTED_STATES.get(r["want_state"]) else None,
    })


# The oasis extension's fields (docs/oasis-extension.md): sent only to clients asking with oasis=1.
OASIS_FIELDS = {"oasisWanted", "oasisWantedError", "oasisMix", "oasisLink", "youtubeId"}


def plain(value):
    """value without the oasis extension's fields, for clients that didn't ask for them."""
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items() if k not in OASIS_FIELDS}
    return [plain(v) for v in value] if isinstance(value, list) else value


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


def latin_of(s, siblings=(), title_of=False, curated=None) -> str | None:
    """s romanized, or None when it needs no romanizing: MusicBrainz's Latin name first (latin.py), an artist's in a
    credit naming several too, else generated."""
    if not isinstance(s, str) or not romanize.has_non_latin(s):
        return None
    known = (curated or {}).get("title" if title_of else "artist") or {}
    if s in known:
        return known[s]
    named = s if title_of else latin.names_in(s, known)
    generated = romanize.text(named, [x for x in siblings if x], title_of) if romanize.has_non_latin(named) else named
    return None if romanize.is_latin(s, generated) else generated


ARTIST_KEYS = ("artist", "artists", "albumArtists", "similarArtist", "directory")


def romanized(payload: dict, mode: str | None, curated=None) -> dict:
    """The OpenSubsonic extension romanization (docs/romanization-extension.md): mode both adds a …Latin field next to
    each name whose romanized form differs, latin puts that form in the name fields themselves. Siblings are as roma
    gives them: a title is read with its artist and album, so a Chinese singer's song isn't read as Japanese."""
    if mode not in ("both", "latin"):
        return payload

    def put(item, field, *siblings, title_of=False):
        if (found := latin_of(item.get(field), siblings, title_of, curated)) is not None:
            item[field if mode == "latin" else field + "Latin"] = found

    def walk(value, key=None):
        if isinstance(value, list):
            for v in value:
                walk(v, key)
            return
        if not isinstance(value, dict):
            return
        if value.get("mediaType") == "song":
            title, artist, album = value.get("title"), value.get("artist"), value.get("album")
            put(value, "title", artist, album, title_of=True)
            put(value, "album", title, artist, title_of=True)
            for field in ("artist", "displayArtist", "displayAlbumArtist", "displayComposer"):
                put(value, field, title, album)
            for a in [*value.get("artists", []), *value.get("albumArtists", []), *(c["artist"] for c in value.get("contributors", []))]:
                put(a, "name", title, album)
            return
        if key == "release" and "track" in value:  # lookupDisc's
            put(value, "title", value.get("artist"), title_of=True)
            put(value, "artist", value.get("title"))
            for t in value["track"]:
                put(t, "title", t.get("artist"), value.get("title"), title_of=True)
                put(t, "artist", t.get("title"), value.get("title"))
            return
        if value.get("isDir"):  # an album
            name = value.get("name")
            for field in ("name", "title", "album") if mode == "latin" else ("name",):
                put(value, field, value.get("artist"), title_of=True)
            for field in ("artist", "displayArtist"):
                put(value, field, name)
            for a in value.get("artists", []):
                put(a, "name", name)
            walk(value.get("song"))
            return
        if "owner" in value or key in ARTIST_KEYS:  # a playlist, an artist, or a directory named as its artist or album
            put(value, "name")
        for k, v in value.items():
            walk(v, k)
        if mode == "latin" and "ignoredArticles" in value:
            value["index"] = indexed(sorted((a for i in value["index"] for a in i["artist"]), key=lambda a: a["name"].casefold()))
    walk(payload)
    return payload


LATIN_SOURCE = "coalesce(t.title, '') || char(31) || coalesce(t.artist, '') || char(31) || coalesce(t.album, '') || char(31) || coalesce(t.album_artist, '')"


def latin_index(db):
    """Fills track_latin for library tracks new or renamed since, as the songs show their names romanized."""
    rows = db.execute(f"select t.*, {LATIN_SOURCE} as source from track t left join track_latin l on l.id = t.id "
                      f"where t.library = 1 and l.source is not {LATIN_SOURCE}").fetchall()

    known = latin.curated(db) if rows else None

    def folded(s, *siblings, title_of=False):
        return " ".join(filter(None, map(ytm.fold, (latin_of(s, siblings, title_of, known) or "").split())))  # words kept apart
    db.executemany("insert or replace into track_latin values (?, ?, ?, ?, ?, ?)", [(
        r["id"], r["source"], folded(r["title"], r["artist"], r["album"], title_of=True), folded(r["artist"], r["title"], r["album"]),
        folded(r["album"], r["title"], r["artist"], title_of=True), folded(r["album_artist"], r["title"], r["album"])) for r in rows])


def cue_lines(values: list[str], words: list[list]) -> dict:
    """songLyrics v2 cueLine for each line with karaoke words: each word found in the line's text in order, with
    inclusive UTF-8 byte offsets; a word the text doesn't hold (romaji that went to the word before) is left out."""
    found = []
    for index, (value, line_words) in enumerate(zip(values, words)):
        cues, at = [], 0
        for start, end, text in line_words:
            i = value.find(text, at) if text else -1
            if i < 0 and (text := text.strip()):
                i = value.find(text, at)
            if i < 0 or not text:
                continue
            byte = len(value[:i].encode())
            cues.append({"start": start, "end": end, "value": text, "byteStart": byte, "byteEnd": byte + len(text.encode()) - 1})
            at = i + len(text)
        for cue, after in zip(cues, cues[1:]):
            cue["end"] = max(cue["start"], min(cue["end"], after["start"]))
        if cues:
            found.append({"index": index, "start": cues[0]["start"], "end": cues[-1]["end"], "value": value, "cue": cues})
    return {"cueLine": found} if found else {}


def decode_password(value: str) -> str:
    return bytes.fromhex(value[4:]).decode() if value.startswith("enc:") else value


def is_radio(playlist_name: str) -> int:
    """Playlists named "Radio ..." grow by themselves from the tracks played in them."""
    return int(playlist_name.casefold().startswith("radio"))


def indexed(artists: list[dict]) -> list[dict]:
    groups: dict[str, list] = {}
    for a in artists:
        groups.setdefault(index_letter(a["name"]), []).append(a)
    return [{"name": k, "artist": v} for k, v in sorted(groups.items())]


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
        self.uploads = Uploads(self._db, config.library[0], config.database.parent / "uploads")
        self.recommender.chores.append(self.uploads.clean)
        self.completed: set[str] = set()
        torbox = config.torbox
        self.doctor = Doctor(self._db, **{k: v for k, v in torbox.items() if k in DOCTOR_KEYS}) if torbox else None
        # oasis's search, download and import: all three, or no acquisition.
        self.downloads = Downloads(self._db, self.doctor.torbox, Importer(self._db, config.library[0]), Prowlarr(**config.prowlarr),
                                   torbox["downloads"], torbox.get("max_active", 8), torbox.get("stall_hours", 1), self._start_scan) \
            if torbox and torbox.get("downloads") and config.prowlarr else None
        self.library = Acquire(self._db, self.downloads, config.acquire.get("secondary_types", SECONDARY),
                               config.acquire.get("bundles", True)) if self.downloads else None
        self.scanning = self.scan_again = False
        self.scan_lock = threading.Lock()
        self.credits = Credits(self._db)
        self.throttle = accounts.Throttle()  # the API's and the dashboard's sign-ins

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
                    self._scan(db)
                    if not self.scan_again:
                        break
            finally:
                db.close()
                self.scanning = False
                self.scan_lock.release()
        threading.Thread(target=run, daemon=True).start()

    def _scan(self, db):
        scan.scan(db, self.config.library)
        if self.config.dedupe:
            self._dedupe(db)
        latin_index(db)
        self.credits.wake.set()

    def _scan_now(self, db):
        """A scan that is done on return, after the one running if any; one asked for meanwhile follows it."""
        with self.scan_lock:
            self.scan_again = False
            self._scan(db)
        if self.scan_again:
            self._start_scan()

    def _dedupe(self, db):
        replaced = dedupe.run(db)
        if not replaced:
            return
        ids = dict(db.execute("select ref, id from track where source = 'local'").fetchall())  # the scan drops the old paths' rows
        scan.scan(db, self.config.library)
        renamed = [(old, new) for old, new in replaced if old != new]  # took the kept copy's format, so a new path and id
        for old, new in renamed:
            move_track(db, ids.get(old, make_id("tr-", old)), make_id("tr-", new))
        db.commit()

    def _authenticate(self, db, p: Params) -> str:
        if p.get("apiKey") is not None:  # OpenSubsonic's apiKeyAuthentication, which oasis doesn't advertise
            raise Fail(MECHANISM, "Provided authentication mechanism not supported")
        user = p.need("u")
        if self.throttle.blocked(p.address):
            raise Fail(0, "Too many failed sign-ins from this address. Try again in a minute.")
        row = accounts.get(db, user)
        password = row["password"] if row else ""
        if (token := p.get("t")) is not None:
            ok = accounts.same(hashlib.md5((password + p.need("s")).encode()).hexdigest(), token.lower())
        else:
            ok = accounts.same(decode_password(p.need("p")), password)
        if row is None or not ok:
            self.throttle.fail(p.address)
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
        return {"openSubsonicExtensions": [{"name": name, "versions": versions} for name, versions in (
            ("formPost", [1]), ("songLyrics", [1, 2]), ("transcodeOffset", [1]), ("romanization", [1]), ("upload", [1]), ("oasis", [1]))]}

    def _user(self, row) -> dict:
        roles = ("scrobbling", "settings", "download", "playlist", "coverArt", "comment", "stream", "share")
        lastfm = bool(self.config.lastfm_key and self.config.lastfm_secret and row["lastfm_session"])  # plays forward to Last.fm
        return {"username": row["name"], "adminRole": bool(row["admin"]), "scrobblingEnabled": lastfm, **{f"{r}Role": True for r in roles},
                "uploadRole": bool(row["admin"]), **{f"{r}Role": False for r in ("podcast", "jukebox")}, "folder": [1]}

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
        rows = db.execute(ARTISTS + " where t.library = 1 group by t.artist_id order by t.album_artist collate nocase")
        return {"ignoredArticles": "The A", "index": indexed([artist(r) for r in rows])}

    def getArtists(self, db, p, user):
        return {"artists": self._artists_index(db)}

    def getIndexes(self, db, p, user):
        return {"indexes": {"lastModified": 0, **self._artists_index(db)}}

    def _artist_albums(self, db, artist_id):
        # Albums seen only in streaming search results stay hidden unless the artist has nothing in the library.
        sql = ALBUMS + " where t.artist_id = ? and t.library >= ? group by t.album_id order by year, name"
        return [album(r) for r in db.execute(sql, (artist_id, 1))] or [album(r) for r in db.execute(sql, (artist_id, 0))]

    def _album_songs(self, db, album_id):
        """The album's own songs, and the songs its YouTube Music listing shares with another album, in its order."""
        return [clean(song(r) | {"track": r["no"], "discNumber": r["disc"]}) for r in db.execute(
            f"select {SONG_COLUMNS}, coalesce(at.disc_no, t.disc_no) as disc, coalesce(at.track_no, t.track_no) as no "
            "from track t left join stat s on s.id = t.id and s.user = me() left join album_track at on at.track_id = t.id and at.album_id = ? "
            "where t.album_id = ? or at.album_id is not null order by disc, no is null, no, title", (album_id, album_id))]

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
        listed = {a: (n, d) for a, n, d in db.execute("select at.album_id, count(*), sum(t.duration) from album_track at join track t "
                                                       "on t.id = at.track_id join ytm_release x on x.album_id = at.album_id "
                                                       "where x.artist_id = ? group by at.album_id", (r["id"],))}
        albums += [self._release_album(x, r["name"], *listed.get(x["album_id"], (None, None)))
                   for x in db.execute("select * from ytm_release where artist_id = ? order by coalesce(year, 0) desc, title", (r["id"],))
                   if x["album_id"] not in known]
        return {"artist": {**artist(r), "albumCount": len(albums), "album": albums}}

    @staticmethod
    def _release_album(x, artist_name, song_count=None, duration=None) -> dict:
        """An album from an artist's YouTube Music page. Its song count and length are known once it was opened."""
        return clean({"id": x["album_id"], "name": x["title"], "title": x["title"], "album": x["title"], "artist": artist_name,
                      "artistId": x["artist_id"], "parent": x["artist_id"], "isDir": True,
                      "coverArt": x["album_id"] if x["cover"] else None, "year": x["year"],
                      "releaseTypes": [x["type"].title()] if x["type"] else None,
                      "songCount": song_count or 0, "duration": duration or 0})

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
        self._complete_album(db, id)
        songs = self._album_songs(db, id)
        if found := self._library_album(db, id, songs):
            return found
        release = db.execute("select * from ytm_release where album_id = ?", (id,)).fetchone()
        if not (release and songs):
            raise Fail(NOT_FOUND, "Data not found")
        # Every song of this one (a single, usually) is on another album too.
        return {"album": {**self._release_album(release, songs[0]["artist"], len(songs), sum(x.get("duration", 0) for x in songs)), "song": songs}}

    def _library_album(self, db, id, songs):
        r = db.execute(ALBUMS + " where t.album_id = ? group by t.album_id", (id,)).fetchone()
        return r and {"album": {**album(r), "song": songs, "songCount": len(songs), "duration": sum(x.get("duration", 0) for x in songs)}}

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
        db.executemany("insert or replace into album_track select ?, id, ?, ? from track where id = ? and source = 'ytm'",
                       [(album_id, row.get("disc_no"), row.get("track_no"), id) for id, row in zip(ids, rows)])
        if release:  # file this album's own songs under the artist page's album, whatever spelling YouTube credits them with
            db.executemany("update track set album_id = ?, artist_id = ? where id = ? and source = 'ytm' and ytm_album = ?",
                           [(album_id, release["artist_id"], id, browse_id) for id in ids])
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
        latin_index(db)

        def match(columns: str, latin: str):
            """Each term in the names or in their romanized forms (track_latin, folded: "tokyo" finds 東京)."""
            return " and ".join(["t.library = 1", *[f"(({columns}) like ? or ({latin}) like ?)"] * len(terms)]), \
                [x for t in terms for x in (f"%{t}%", f"%{ytm.fold(t)}%" if ytm.fold(t) else None)]
        joined = " left join track_latin l on l.id = t.id"

        # Every row is read before YouTube Music is asked: a cursor left open keeps its read snapshot, and remember()
        # can't write from a snapshot that another connection's write (the mix refresh's) made stale: "database is locked".
        cond, args = match("t.album_artist", "l.album_artist")
        artists = [artist(r) for r in db.execute(ARTISTS + joined + f" where {cond} group by t.artist_id order by t.album_artist collate nocase "
                                                 "limit ? offset ?", (*args, p.int("artistCount", 20), p.int("artistOffset", 0)))]
        cond, args = match("t.album || ' ' || t.album_artist", "l.album || ' ' || l.album_artist")
        albums = [album(r) for r in db.execute(ALBUMS + joined + f" where {cond} group by t.album_id order by name collate nocase limit ? offset ?",
                                               (*args, p.int("albumCount", 20), p.int("albumOffset", 0)))]
        cond, args = match("t.title || ' ' || t.artist || ' ' || t.album", "l.title || ' ' || l.artist || ' ' || l.album")
        songs = [song(r) for r in db.execute(SONGS + joined + f" where {cond} order by t.album_artist, t.album, t.disc_no, t.track_no limit ? offset ?",
                                             (*args, p.int("songCount", 20), p.int("songOffset", 0)))]
        if self.ytm and terms and p.int("songOffset", 0) == 0:
            known = {s["id"] for s in songs}
            try:
                # Capped: a client asking for 500 songs would otherwise wait on YouTube Music's paging for many seconds.
                # At least 20, for the artists and albums of a client asking for those alone (songCount=0).
                found = [remember(db, row) for row in self.ytm.search(" ".join(terms), max(min(p.int("songCount", 20), YTM_SEARCH), 20))]
            except Exception as e:
                log.warning("YouTube Music search failed: %s", e)
                found = []
            found = [song(self._one(db, SONGS + " where t.id = ?", id)) for id in dict.fromkeys(found) if id not in known]
            songs += found[:p.int("songCount", 20)]
            # The artists and albums of YouTube Music's songs that the query names, as the library's are matched, after
            # the library's: opening one fetches the rest of it.
            folded = [ytm.fold(t) for t in terms]
            def named(*texts): return all(t in ytm.fold(" ".join(x or "" for x in [*texts, *map(latin_of, texts)])) for t in folded)
            if p.int("artistOffset", 0) == 0 and (room := p.int("artistCount", 20) - len(artists)) > 0:
                ids = [i for i in dict.fromkeys(s["artistId"] for s in found if s.get("artistId")) if i not in {a["id"] for a in artists}]
                artists += [a for a in self._in_order(db, ARTISTS, "t.artist_id", ids, artist) if named(a["name"])][:room]
            if p.int("albumOffset", 0) == 0 and (room := p.int("albumCount", 20) - len(albums)) > 0:
                ids = [i for i in dict.fromkeys(s["albumId"] for s in found if s.get("albumId")) if i not in {a["id"] for a in albums}]
                albums += [a for a in self._in_order(db, ALBUMS, "t.album_id", ids, album) if named(a["name"], a.get("artist"))][:room]
        text = " ".join(terms)
        if self.library and p.oasis and len(text) >= FIND_FROM:
            # MusicBrainz last, for what neither the library nor YouTube Music has: these can be starred like any other.
            # Only for the oasis extension: they have no songs until they arrive, which other clients don't expect.
            names = {ytm.fold(x) for x in [*(a["name"] for a in artists), *(s.get("artist") for s in songs)] if x}
            titles = {ytm.fold(x) for x in [*(a["name"] for a in albums), *(s.get("album") for s in songs)] if x}
            if p.int("artistOffset", 0) == 0 and (room := p.int("artistCount", 20) - len(artists)) > 0:
                artists += [self._mb_artist(db, a) for a in self._find(db, "artist", text)
                            if not {ytm.fold(n) for n in [a["name"], *(x["name"] for x in a.get("aliases") or [])]} & names
                            and not acquire.library_id(db, MB_IDS["artist"] + a["id"])][:room]
            if p.int("albumOffset", 0) == 0 and (room := p.int("albumCount", 20) - len(albums)) > 0:
                albums += [self._mb_album(db, g) for g in self._find(db, "album", text)
                           if ytm.fold(g["title"]) not in titles and not acquire.library_id(db, MB_IDS["album"] + g["id"])][:room]
        return {"artist": artists, "album": albums, "song": songs}

    @staticmethod
    def _in_order(db, query, column, ids, shape) -> list[dict]:
        """query's rows grouped by column, for ids in their order."""
        if not ids:
            return []
        rows = {r["id"]: r for r in db.execute(query + f" where {column} in ({','.join('?' * len(ids))}) group by {column}", ids)}
        return [shape(rows[i]) for i in ids if i in rows]

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
        starred = db.execute("select starred from stat where id = ? and user = me()", (id,)).fetchone()
        item = db.execute("select state, error from want_item where kind = ? and ref = ?", (kind, id)).fetchone()
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
        return clean({"id": id, "name": g["title"], "title": g["title"], "album": g["title"], "coverArt": id,
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
            starred = db.execute("select 1 from stat where id = ? and starred is not null", (id,)).fetchone()  # by anyone
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

    def _starred(self, db, p):
        """The user's stars; MusicBrainz-only artists and albums only for the oasis extension, as search finds them."""
        def ids(prefix):
            return db.execute("select id from stat where user = me() and starred is not null and id like ? order by starred desc", (prefix + "%",)).fetchall()
        return {
            "artist": [a for r in ids("ar-") for a in ([artist(self._one(db, ARTISTS + " where t.artist_id = ? group by t.artist_id", r["id"]))]
                                                      if db.execute("select 1 from track where artist_id = ?", (r["id"],)).fetchone()
                                                      else self._starred_mb(db, "artist", r["id"]) if p.oasis else [])],
            "album": [a for r in ids("al-") for a in ([album(self._one(db, ALBUMS + " where t.album_id = ? group by t.album_id", r["id"]))]
                                                     if db.execute("select 1 from track where album_id = ?", (r["id"],)).fetchone()
                                                     else self._starred_mb(db, "album", r["id"]) if p.oasis else [])],
            "song": [song(r) for r in db.execute(SONGS + " where s.starred is not null order by s.starred desc")],
        }

    def _starred_mb(self, db, kind, id) -> list[dict]:
        """A starred MusicBrainz-only artist or album, as getArtist and getAlbum give it (from mb_cache, where searching
        and acquiring it put it); none while MusicBrainz doesn't answer, or for another id."""
        if not (mbid := acquire.mbid_of(id)):
            return []
        try:
            if kind == "artist":
                return [self._mb_artist(db, musicbrainz.get(releases.ALIASES.format(mbid), db, releases.KEEP_ALIASES))]
            return [self._mb_album(db, musicbrainz.get(acquire.GROUP.format(mbid), db, acquire.KEEP_GROUP))]
        except (OSError, LookupError) as e:
            log.info("starred %s unavailable: %s", id, e)
            return []

    def getStarred2(self, db, p, user):
        return {"starred2": self._starred(db, p)}

    def getStarred(self, db, p, user):
        return {"starred": self._starred(db, p)}

    def _set_starred(self, db, p, value, user):
        for id in p.all("id"):
            db.execute("insert into stat(user, id, starred) values (?, ?, ?) on conflict(user, id) do update set starred = excluded.starred", (user, id, value))
        self._touch(db, p.all("id"))
        self._want(db, user, "album", p.all("albumId"), value)
        self._want(db, user, "artist", p.all("artistId"), value)
        return {}

    def _want(self, db, user, kind, ids, value):
        """Stars albums or artists, or takes the star back (value None): a starred album or artist is one the user
        wants in the library (see acquire.py). The dashboard's want and follow are stars too."""
        for id in ids:
            db.execute("insert into stat(user, id, starred) values (?, ?, ?) on conflict(user, id) do update set starred = excluded.starred", (user, id, value))
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
        db.execute("insert into stat(user, id, rating) values (?, ?, ?) on conflict(user, id) do update set rating = excluded.rating", (user, p.need("id"), rating))
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
            db.execute("insert into stat(user, id, play_count, played) values (?, ?, 1, ?) on conflict(user, id) do update "
                       "set play_count = play_count + 1, played = excluded.played", (user, id, played))
            db.execute("insert into play values (?, ?, ?)", (user, id, played))
            self.forwarder.enqueue(db, user, id, played)
        self.recommender.played(db, user, [id for id, _ in known])
        return {}

    def _lyrics(self, db, r) -> lyrics.Lyrics | None:
        """The track's own lyrics first; otherwise the providers' pick, cached (misses for a day)."""
        if r["source"] == "local" and (found := lyrics.local(r["ref"])):
            return found
        cached = db.execute("select found, fetched from lyrics where track_id = ?", (r["id"],)).fetchone()
        old = cached and cached["found"] and '"word_level": true' in cached["found"] and '"words"' not in cached["found"]  # cached without its words
        if cached and not old and (cached["found"] or cached["fetched"] > time.time() - 86400):
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
        main = {"displayArtist": r["artist"], "displayTitle": r["title"], "lang": "xxx", "offset": 0, "synced": found.synced,
                "line": [clean({"start": start, "value": text}) for start, text in found.lines]}
        if p.get("enhanced") != "true":
            return {"lyricsList": {"structuredLyrics": [main]}}
        # songLyrics v2, with the romanization extension's kinds. oasis keeps no word timing (so no cueLine) and no
        # translations; lyrics already in Latin script have no pronunciation, and main stands in for one.
        kinds = (p.get("kinds") or "main,pronunciation,translation").split(",")
        texts, siblings = [text for _, text in found.lines], [x for x in (r["artist"], r["album"]) if x]
        words = found.words if found.synced and found.words else [[] for _ in texts]
        readings = romanize.lyrics(texts, siblings, found.lang) if any(map(romanize.has_non_latin, texts)) else None
        entries = [{**main, "kind": "main", **cue_lines(texts, words)}] if "main" in kinds or "pronunciation" in kinds and readings is None else []
        if readings and "pronunciation" in kinds:
            # A line with karaoke words reads as its words' romaji, each word keeping its timing (roma's romanizeLyrics).
            word_latin = romanize.lyric_words(texts, [[w[2] for w in ws] for ws in words], siblings, found.lang)
            readings = ["".join(wl).strip() if wl else l for l, wl in zip(readings, word_latin)]
            words = [[[a, b, t] for (a, b, _), t in zip(ws, wl)] for ws, wl in zip(words, word_latin)]
            entries.append({**main, "kind": "pronunciation", "lang": romanize.language(texts, siblings, found.lang) + "-Latn",
                            "line": [clean({"start": start, "value": l}) for (start, _), l in zip(found.lines, readings)], **cue_lines(readings, words)})
        return {"lyricsList": clean({"structuredLyrics": entries or None})}

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
        ids = [r[0] for r in db.execute("select t.id from track t left join stat s on s.id = t.id and s.user = me() where t.library = 1 and "
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
            "readonly": True if r["mix"] or r["radio"] else None,  # OpenSubsonic: clients offer no edits; oasis keeps mixes and radios
            "oasisMix": True if (r["mix"] or "").startswith("RDTMAK") else None, "youtubeId": r["mix"],
            "songCount": len(songs), "duration": sum(s.get("duration", 0) for s in songs),
            "created": iso(r["created"]), "changed": iso(r["changed"]),
        })
        return {**result, "entry": songs} if entries else result

    def getPlaylists(self, db, p, user):
        rows = sorted(db.execute("select * from playlist where owner = ?", (user,)), key=playlist_order)
        return {"playlists": {"playlist": [self._playlist(db, r, False) for r in rows]}}

    def _own_playlist(self, db, id, user):
        r = self._one(db, "select * from playlist where id = ? and owner = ?", id, user)
        if r["mix"]:
            raise Fail(DENIED, "YouTube Music's playlists are read-only")
        return r

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

    # The upload extension (docs/upload-extension.md): admins only, the users with uploadRole.

    def lookupDisc(self, db, p, user):
        self._admin(db, user)
        return {"discLookup": {"release": self.uploads.lookup(db, p.need("discId"), p.get("toc"))}}

    def createUpload(self, db, p, user):
        self._admin(db, user)
        if not p.get("releaseId") and p.get("tags") is None:
            raise Fail(MISSING, "Required parameter 'releaseId' or 'tags' is missing")
        try:
            disc = p.int("disc", 1)
        except ValueError:
            raise Fail(0, "disc must be an integer") from None
        return {"upload": self.uploads.create(db, user, p.get("releaseId"), disc, p.get("tags"))}

    def uploadTrack(self, db, p, user):
        self._admin(db, user)
        try:
            numbers = [int(p.need(k)) for k in ("disc", "number", "offset", "size")]
        except ValueError:
            raise Fail(0, "disc, number, offset and size must be integers") from None
        return {"uploadTrack": {"received": self.uploads.receive(db, user, p.need("uploadId"), *numbers, p.need("suffix"), p.body)}}

    def getUpload(self, db, p, user):
        self._admin(db, user)
        return {"upload": self.uploads.describe(db, user, p.need("id"))}

    def uploadCover(self, db, p, user):
        self._admin(db, user)
        self.uploads.cover(db, user, p.need("uploadId"), p.body)
        return {}

    def cancelUpload(self, db, p, user):
        self._admin(db, user)
        self.uploads.cancel(db, user, p.need("uploadId"))
        return {}

    def commitUpload(self, db, p, user):
        self._admin(db, user)
        placed = self.uploads.commit(db, user, p.need("uploadId"), p.get("replace") == "true")
        self._scan_now(db)
        ids = list(dict.fromkeys(self._one(db, "select album_id from track where ref = ?", str(f))["album_id"] for f in placed))
        albums = [self._library_album(db, id, self._album_songs(db, id))["album"] for id in ids]
        return {"album": albums[0], **({"albums": albums} if len(albums) > 1 else {})}  # loose singles: each an album

    def getCoverArt(self, db, p, user):
        try:
            return self._cover_art(db, p)
        except Fail as e:
            if e.code != NOT_FOUND:
                raise
            return Response(status_code=404)  # a status clients see, not an error document they would draw as an image

    def _cover_art(self, db, p):
        id = p.need("id")
        id = acquire.mbid_of(id) and acquire.library_id(db, id) or id
        if id.startswith(MB_IDS["album"]):  # a MusicBrainz-only album until it arrives
            r = {"ref": None, "cover": acquire.GROUP_COVER.format(acquire.mbid_of(id))}
        elif id.startswith("pl-"):
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
        body["error"] = {"code": error.code, "message": error.message, **error.extra}
    if fmt == "json":
        return JSONResponse({"subsonic-response": body})
    root = to_xml("subsonic-response", body)
    root.set("xmlns", "http://subsonic.org/restapi")
    return Response(b'<?xml version="1.0" encoding="UTF-8"?>' + tostring(root), media_type="application/xml")


RAW_BODY = ("uploadTrack", "uploadCover")  # the body is a file, read as it arrives; parameters go in the query


def body_chunks(request: Request):
    """The request body's chunks, for a handler in a worker thread."""
    stream = request.stream()
    while (chunk := anyio.from_thread.run(anext, stream, None)) is not None:
        yield chunk


def create_app(config: Config, background: bool = True, ytm=None, services=None) -> Starlette:
    """background starts the scan and the forwarding, radio and mix workers; tests drive those themselves."""
    api = Api(config, ytm, services, background)

    async def endpoint(request: Request):
        method = request.path_params["method"].removesuffix(".view")
        raw = method in RAW_BODY
        p = Params(request, b"" if raw else await request.body())
        p.body = body_chunks(request) if raw else iter(())
        fmt = p.get("f", "xml")
        handler = getattr(Api, method, None)
        if handler is None or method.startswith("_"):
            return respond(fmt, {}, Fail(0, f"Unknown method '{method}'"))

        def run():
            db = api._db()
            try:
                user = api._authenticate(db, p)
                db.create_function("me", 0, lambda: user, deterministic=True)
                p.follow_moves(db)
                result = handler(api, db, p, user)
                if isinstance(result, Response):
                    return result
                return respond(fmt, romanized(describe(db, result if p.oasis else plain(result)), p.get("romanization"),
                                                     latin.curated(db) if p.get("romanization") in ("both", "latin") else None))
            except Fail as e:
                return respond(fmt, {}, e)
            except upload.Refused as e:
                return respond(fmt, {}, Fail(e.code, str(e), {"duplicate": True} if e.duplicate else None))
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
