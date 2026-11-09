"""The Subsonic API, with the OpenSubsonic extensions oasis offers: each method of Api answers the request of its name,
for the oasis the running server holds (server.py)."""
import hashlib
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl
from xml.etree.ElementTree import Element, tostring

import anyio
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from . import accounts, acquire, artists, cover, forward, lastfm, latin, lyrics, mixes, musicbrainz, playlists, romanize, tracks, transcode, translate, upload, youtube, ytm
from .credits import credits_of, people
from .db import as_user, set_stat
from .playlists import RADIO, is_radio, set_entries, touch
from .recommend import PERIOD, PERIODS, SOURCES, STATIONS
from .text import fold
from .tracks import BY_ARTIST_NAME, IDS, MB_IDS, id_column, resolve

log = logging.getLogger(__name__)

VERSION = "1.16.1"
NOT_FOUND, MISSING, AUTH, MECHANISM, DENIED = 70, 10, 40, 42, 50


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
SIMILAR_WAIT = 6  # seconds getSimilarSongs waits for its slowest source: YouTube Music answers in about 2, Last.fm in up to 6
WARM = 3  # songs at the head of a radio or similar songs: the first from YouTube is readied before a client asks (youtube.warm)


class Params:
    def __init__(self, request: Request, body: bytes):
        self.multi = [*request.query_params.multi_items(), *parse_qsl(body.decode())]
        self.range = request.headers.get("range")
        self.oasis = self.get("oasis") == "1"  # the oasis extension (docs/oasis-extension.md)
        self.address = accounts.client_address(request)

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
        """Ids a client still holds for tracks that moved (streams that became local files, copies dedupe gave another
        format) and for MusicBrainz-only artists and albums that arrived point at the library's (tracks.current_id),
        and artist ids of another name of an artist at the artist."""
        self.multi = [(k, tracks.current_id(db, v) if k in MOVED_PARAMS else v) for k, v in self.multi]
        self.multi = [(k, resolve(db, v) if k in ARTIST_ID_PARAMS and v.startswith(IDS["artist"]) else v) for k, v in self.multi]


SONG_COLUMNS = """t.*, s.starred, s.rating, s.play_count, s.played"""
# Stars, ratings and plays are the requesting user's: me() names them for each request (db.as_user).
SONGS = f"select {SONG_COLUMNS} from track t left join stat s on s.id = t.id and s.user = me()"
ALBUMS = """
select t.album_id as id, t.album as name, coalesce(max(t.album_by), t.album_artist) as artist, t.artist_id,
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


def required(row):
    """row, or Fail NOT_FOUND when there is none."""
    if row is None:
        raise Fail(NOT_FOUND, "Data not found")
    return row


def artist_row(db, id):
    return db.execute(ARTISTS + " where t.artist_id = ? group by t.artist_id", (id,)).fetchone()


def album_row(db, id):
    return db.execute(ALBUMS + " where t.album_id = ? group by t.album_id", (id,)).fetchone()


def song_row(db, id):
    return db.execute(SONGS + " where t.id = ?", (id,)).fetchone()


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
        "shareUrl": f"https://music.youtube.com/watch?v={r['ytm_id']}" if r["ytm_id"] else None,  # the shareUrl extension
    })


def wanted(state: str | None, error: str | None) -> dict:
    """Acquisition progress of a starred album or artist, for clients that show it (acquire.BADGES), and why it
    failed."""
    badge = state if state in acquire.BADGES else None
    return {"oasisWanted": badge, "oasisWantedError": error if badge else None}


def album_base(id: str, name: str, artist: str | None, artist_id: str | None) -> dict:
    """What every album has, the library's or not: Subsonic also gives an album as a directory under its artist."""
    return {"id": id, "name": name, "title": name, "album": name, "artist": artist, "artistId": artist_id, "parent": artist_id, "isDir": True}


def album(r) -> dict:
    return clean({
        **album_base(r["id"], r["name"], r["artist"], r["artist_id"]),
        "coverArt": r["id"] if r["has_cover"] else None, "songCount": r["song_count"], "duration": r["duration"],
        "created": iso(r["created"]), "year": r["year"], "genre": r["genre"], "starred": iso(r["album_starred"]),
        "userRating": r["album_rating"], "playCount": r["plays"], "played": iso(r["last_played"]),
        **wanted(r["want_state"], r["want_error"]),
    })


def artist(r) -> dict:
    return clean({
        "id": r["id"], "name": r["name"], "albumCount": r["album_count"],
        "coverArt": r["id"] if r["has_cover"] else None, "starred": iso(r["starred"]), "userRating": r["rating"],
        **wanted(r["want_state"], r["want_error"]),
    })


# The oasis extension's fields (docs/oasis-extension.md): sent only to clients asking with oasis=1.
OASIS_FIELDS = {"oasisWanted", "oasisWantedError", "oasisMix", "oasisLink", "youtubeId", "oasisSource", "oasisStation", "oasisPeriod",
                "oasisMachineTranslation"}


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
            elif value.get("isDir") and str(value.get("id", "")).startswith(IDS["album"]):
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
    respell(db, payload)
    return payload


ARTIST_TEXT = ("artist", "albumArtist", "displayArtist", "displayAlbumArtist")


def respell(db, payload):
    """Artist names in a response under the spelling their artist is shown with (artists.unify), also within
    'A, B' and 'A feat. B'."""
    names = artists.spelled_as(db)
    if not names:
        return
    within = re.compile(r"(?<!\w)(?:" + "|".join(map(re.escape, sorted(names, key=len, reverse=True))) + r")(?!\w)")
    done = {}  # a response names the same artists over and over

    def spelled(v):
        if v not in done:
            done[v] = within.sub(lambda m: names[m[0]], names.get(v, v))
        return done[v]

    def walk(value):
        if isinstance(value, dict):
            for k, v in value.items():
                if isinstance(v, str) and (k in ARTIST_TEXT or k == "name" and str(value.get("id", "")).startswith(IDS["artist"])):
                    value[k] = spelled(v)
                else:
                    walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)
    walk(payload)


def genre_list(r) -> list[dict] | None:
    names = json.loads(r["genres"]) if r["genres"] else ([r["genre"]] if r["genre"] else [])
    return [{"name": g} for g in names] or None


def album_artist_of(track) -> tuple[list[dict], str | None]:
    """A song's album artist without credits: the album page's (streaming songs), else its album artist column, for a
    streaming song its first artist."""
    name = track and (track["album_by"] or track["album_artist"])
    if not name:
        return [], None
    return [{"id": track["artist_id"] if name == track["album_artist"] else tracks.name_id(name), "name": name}], name


def describe_songs(db, songs: list[dict]):
    """Adds to each song its artists and album artists with their own ids, the other contributors by role, composers,
    ISRCs and genres."""
    ids = list({s["id"] for s in songs})
    credits = credits_of(db, ids)
    tracks = {r["id"]: r for r in db.execute(f"select id, isrc, genres, genre, mb_recording, album_artist, album_by, artist_id from track "
                                              f"where id in ({','.join('?' * len(ids))})", ids)}
    for s in songs:
        mine, track = credits.get(s["id"], []), tracks.get(s["id"])
        artists, display = people(mine, "artist")
        album_artists, display_album = people(mine, "albumartist") if any(c["role"] == "albumartist" for c in mine) else album_artist_of(track)
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
    """Adds to each album its album artists with their own ids, labels, release types, genres and original release
    date."""
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
            a["releaseTypes"] = [musicbrainz.type_name(t) for t in types]


ARTIST_KEYS = ("artist", "artists", "albumArtists", "similarArtist", "directory")


def romanized(payload: dict, mode: str | None, curated=None) -> dict:
    """The OpenSubsonic extension romanization (docs/romanization-extension.md): mode both adds a …Latin field next to
    each name whose romanized form differs, latin puts that form in the name fields themselves. Siblings are as roma
    gives them: a title is read with its artist and album, so a Chinese singer's song isn't read as Japanese."""
    if mode not in ("both", "latin"):
        return payload

    def put(item, field, *siblings, title_of=False):
        if (found := latin.of(item.get(field), siblings, title_of, curated)) is not None:
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


def disc_release(release: dict, medium: dict, cover: str | None) -> dict:
    """A release lookupDisc found, with the tracks of medium, the disc."""
    labels = [l for l in release.get("label-info") or [] if l.get("label")]
    date = release.get("date") or ""
    return clean({
        "id": release["id"], "title": release["title"], "artist": musicbrainz.credited(release.get("artist-credit") or []),
        "year": int(date[:4]) if date[:4].isdigit() else None, "date": date or None, "country": release.get("country"),
        "label": labels[0]["label"]["name"] if labels else None,
        "catalogNumber": next((l["catalog-number"] for l in labels if l.get("catalog-number")), None),
        "barcode": release.get("barcode") or None, "discCount": len(release.get("media") or []), "discNumber": medium.get("position"),
        "coverArt": cover, "track": [disc_track(release, t) for t in medium.get("tracks") or []]})


def disc_track(release: dict, track: dict) -> dict:
    recording = track.get("recording") or {}
    length = track.get("length") or recording.get("length")
    return clean({"number": track["position"], "title": track["title"],
                  "artist": musicbrainz.credited(track.get("artist-credit") or recording.get("artist-credit") or release.get("artist-credit") or []),
                  "duration": round(length / 1000) if length else None, "musicBrainzId": recording.get("id")})


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


IGNORED_ARTICLES = ("The", "A")  # left out of an artist's name for its index letter


def indexed(artists: list[dict]) -> list[dict]:
    groups: dict[str, list] = {}
    for a in artists:
        groups.setdefault(index_letter(a["name"]), []).append(a)
    return [{"name": k, "artist": v} for k, v in sorted(groups.items())]


def index_letter(name: str) -> str:
    for article in IGNORED_ARTICLES:
        if name.startswith(article + " "):
            name = name[len(article) + 1:]
    letter = name[:1].upper()
    return letter if letter.isalpha() else "#"


class Api:
    """The Subsonic API's methods, for one oasis (server.py)."""

    def __init__(self, oasis):
        self.oasis = oasis
        self.finder = ThreadPoolExecutor(4)  # MusicBrainz searches, made while the rest of a search3 is looked up

    def _authenticate(self, db, p: Params) -> str:
        if p.get("apiKey") is not None:  # OpenSubsonic's apiKeyAuthentication, which oasis doesn't advertise
            raise Fail(MECHANISM, "Provided authentication mechanism not supported")
        user = p.need("u")
        if (token := p.get("t")) is not None:
            matches = lambda password: accounts.same(hashlib.md5((password + p.need("s")).encode()).hexdigest(), token.lower())
        else:
            matches = lambda password: accounts.same(decode_password(p.need("p")), password)
        try:
            accounts.sign_in(db, self.oasis.throttle, p.address, user, matches)
        except accounts.Refused as e:
            if e.throttled:
                raise Fail(0, "Too many failed sign-ins from this address. Try again in a minute.")
            raise Fail(AUTH, "Wrong username or password")
        return user

    def _one(self, db, sql: str, *args):
        return required(db.execute(sql, args).fetchone())

    # Subsonic methods are dispatched by name; helpers start with '_'. Each returns a payload dict or a Response.

    def ping(self, db, p, user):
        return {}

    def getLicense(self, db, p, user):
        return {"license": {"valid": True}}

    def getOpenSubsonicExtensions(self, db, p, user):
        return {"openSubsonicExtensions": [{"name": name, "versions": versions} for name, versions in (
            ("formPost", [1]), ("songLyrics", [1, 2]), ("transcodeOffset", [1]), ("romanization", [1]), ("upload", [1]), ("shareUrl", [1]), ("oasis", [1, 2, 3]))]}

    def _user(self, row) -> dict:
        roles = ("scrobbling", "settings", "download", "playlist", "coverArt", "comment", "stream", "share")
        scrobbling = lastfm.signs_in(self.oasis.lastfm) and bool(row["lastfm_session"])
        return {"username": row["name"], "adminRole": bool(row["admin"]), "scrobblingEnabled": scrobbling, **{f"{r}Role": True for r in roles},
                "uploadRole": bool(row["admin"]), **{f"{r}Role": False for r in ("podcast", "jukebox")}, "folder": [1]}

    def _admin(self, db, user):
        if not accounts.get(db, user)["admin"]:
            raise Fail(DENIED, "Only admins can do that")

    def _editable(self, name, change):
        try:
            accounts.check_editable(self.oasis.config.users, name, change)
        except ValueError as e:
            raise Fail(DENIED, str(e))

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
        password, admin = p.get("password"), p.get("adminRole")
        if password is not None:
            self._editable(name, "password")
        if admin is not None and admin != "true":  # oasis.toml's users are admins already
            self._editable(name, "admin")
        if password is not None:
            self._set_password(db, name, decode_password(password))
        if admin is not None:
            accounts.set_field(db, name, admin=int(admin == "true"))
        return {}

    def deleteUser(self, db, p, user):
        self._admin(db, user)
        name = p.need("username")
        self._editable(name, "remove")
        accounts.delete(db, name)
        return {}

    def changePassword(self, db, p, user):
        name = p.need("username")
        if name != user:
            self._admin(db, user)
        self._editable(name, "password")
        self._one(db, "select 1 from user where name = ?", name)
        self._set_password(db, name, decode_password(p.need("password")))
        return {}

    def _set_password(self, db, name, password):
        try:
            accounts.set_password(db, name, password)
        except ValueError as e:
            raise Fail(0, str(e))

    def oasisLinkYouTubeMusic(self, db, p, user):
        """For clients that already have a YouTube Music sign-in (roma): cookie is the music.youtube.com Cookie header
        or the request headers; authUser the X-Goog-AuthUser index, 0 by default."""
        try:
            headers = ytm.youtube_headers(p.need("cookie"))
            headers["x-goog-authuser"] = p.get("authUser", headers["x-goog-authuser"])
            return {"youtubeMusic": {"account": self.oasis.link_youtube(db, user, headers)}}
        except ValueError as e:
            raise Fail(0, str(e))

    def oasisUnlinkYouTubeMusic(self, db, p, user):
        self.oasis.unlink_youtube(db, user)
        return {}

    def getMusicFolders(self, db, p, user):
        return {"musicFolders": {"musicFolder": [{"id": 1, "name": "Music"}]}}

    def getScanStatus(self, db, p, user):
        return {"scanStatus": {"scanning": self.oasis.scanner.scanning, "count": db.execute("select count(*) from track").fetchone()[0]}}

    def startScan(self, db, p, user):
        self.oasis.scanner.start()
        return self.getScanStatus(db, p, user)

    def _artists_index(self, db):
        rows = db.execute(ARTISTS + " where t.library = 1 group by t.artist_id order by t.album_artist collate nocase")
        return {"ignoredArticles": " ".join(IGNORED_ARTICLES), "index": indexed([artist(r) for r in rows])}

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
            "where t.id in (select id from track where album_id = ? union select track_id from album_track where album_id = ?) "
            "order by disc, no is null, no, title", (album_id, album_id, album_id))]

    def getArtist(self, db, p, user):
        if tracks.mbid_of(id := p.need("id")):
            info = self._musicbrainz(musicbrainz.artist, tracks.mbid_of(id), db)
            groups = self._musicbrainz(musicbrainz.release_groups, tracks.mbid_of(id), db)
            albums = [self._mb_album(db, g) for g in musicbrainz.by_date(groups)]
            return {"artist": {**self._mb_artist(db, info), "albumCount": len(albums), "album": albums}}
        r = artist_row(db, p.need("id"))
        if r is None:
            return self._credited_artist(db, p.need("id"))
        albums = self._artist_albums(db, r["id"])
        known = {a["id"] for a in albums}
        self.oasis.youtube.artist_page(db, r["id"], r["name"])
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
        return clean({**album_base(x["album_id"], x["title"], artist_name, x["artist_id"]),
                      "coverArt": x["album_id"] if x["cover"] else None, "year": x["year"],
                      "releaseTypes": [musicbrainz.type_name(x["type"])] if x["type"] else None,
                      "songCount": song_count or 0, "duration": duration or 0})

    def _credited_artist(self, db, artist_id):
        """An artist only credited on others' songs (a guest singer, a band member, a composer): the albums they're on."""
        name = self._one(db, "select name from credit where artist_id = ? order by source = 'tags', role != 'artist' limit 1", artist_id)["name"]
        albums = [album(r) for r in db.execute(ALBUMS + " where t.id in (select track_id from credit where artist_id = ?) "
                                               "group by t.album_id order by year, name", (artist_id,))]
        return {"artist": {"id": artist_id, "name": name, "albumCount": len(albums), "album": albums}}

    def getAlbum(self, db, p, user):
        if tracks.mbid_of(id := p.need("id")):
            return {"album": {**self._mb_album(db, self._musicbrainz(musicbrainz.release_group, tracks.mbid_of(id), db)),
                              "song": []}}  # nothing to play until it arrives
        self.oasis.youtube.complete_album(db, id)
        songs = self._album_songs(db, id)
        if found := self._library_album(db, id, songs):
            return found
        release = db.execute("select * from ytm_release where album_id = ?", (id,)).fetchone()
        if not (release and songs):
            raise Fail(NOT_FOUND, "Data not found")
        # Every song of this one (a single, usually) is on another album too.
        return {"album": {**self._release_album(release, songs[0]["artist"], len(songs), sum(x.get("duration", 0) for x in songs)), "song": songs}}

    def _library_album(self, db, id, songs):
        r = album_row(db, id)
        return r and {"album": {**album(r), "song": songs, "songCount": len(songs), "duration": sum(x.get("duration", 0) for x in songs)}}

    def getSong(self, db, p, user):
        r = required(song_row(db, p.need("id")))
        if r["ytm_id"] is None and self.oasis.ytm:  # for share links
            try:
                youtube.video_of(db, r, self.oasis.ytm)
                r = required(song_row(db, r["id"]))
            except Exception as e:
                log.info("YouTube Music match for %s failed: %s", r["id"], e)
        return {"song": song(r)}

    def getMusicDirectory(self, db, p, user):
        id = p.need("id")
        if id.startswith(IDS["artist"]):
            r = required(artist_row(db, id))
            return {"directory": {"id": id, "name": r["name"], "child": self._artist_albums(db, id)}}
        r = required(album_row(db, id))
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
        text = " ".join(terms)
        # MusicBrainz, last in the answer, for what neither the library nor YouTube Music has: these can be starred like any
        # other. Only for the oasis extension: they have no songs until they arrive, which other clients don't expect.
        # Asked first, as it answers slowest (a second between requests), while the rest is looked up.
        finding = {kind: self.finder.submit(self._find, kind, text) for kind in ("artist", "album")
                   if self.oasis.acquire and p.oasis and len(text) >= FIND_FROM and p.int(kind + "Offset", 0) == 0 and p.int(kind + "Count", 20) > 0}
        latin.index(db)

        def match(columns: str, latin: str):
            """Each term in the names or in their romanized forms (track_latin, folded: "tokyo" finds 東京)."""
            return " and ".join(["t.library = 1", *[f"(({columns}) like ? or ({latin}) like ?)"] * len(terms)]), \
                [x for t in terms for x in (f"%{t}%", f"%{fold(t)}%" if fold(t) else None)]
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
        if self.oasis.ytm and terms and p.int("songOffset", 0) == 0:
            known = {s["id"] for s in songs}
            # Capped: a client asking for 500 songs would otherwise wait on YouTube Music's paging for many seconds.
            # At least 20, for the artists and albums of a client asking for those alone (songCount=0).
            found = self.oasis.youtube.search(db, " ".join(terms), max(min(p.int("songCount", 20), YTM_SEARCH), 20))
            found = [song(required(song_row(db, id))) for id in dict.fromkeys(found) if id not in known]
            songs += found[:p.int("songCount", 20)]
            # The artists and albums of YouTube Music's songs that the query names, as the library's are matched, after
            # the library's: opening one fetches the rest of it.
            folded, curated = [fold(t) for t in terms], latin.curated(db)

            def named(*texts):
                latins = [latin.of(x, curated=curated) for x in texts]
                return all(t in fold(" ".join(x or "" for x in [*texts, *latins])) for t in folded)
            if p.int("artistOffset", 0) == 0 and (room := p.int("artistCount", 20) - len(artists)) > 0:
                ids = [i for i in dict.fromkeys(s["artistId"] for s in found if s.get("artistId")) if i not in {a["id"] for a in artists}]
                artists += [a for a in self._in_order(db, ARTISTS, "t.artist_id", ids, artist) if named(a["name"])][:room]
            if p.int("albumOffset", 0) == 0 and (room := p.int("albumCount", 20) - len(albums)) > 0:
                ids = [i for i in dict.fromkeys(s["albumId"] for s in found if s.get("albumId")) if i not in {a["id"] for a in albums}]
                albums += [a for a in self._in_order(db, ALBUMS, "t.album_id", ids, album) if named(a["name"], a.get("artist"))][:room]
        if finding:
            names = {fold(x) for x in [*(a["name"] for a in artists), *(s.get("artist") for s in songs)] if x}
            titles = {fold(x) for x in [*(a["name"] for a in albums), *(s.get("album") for s in songs)] if x}
            if "artist" in finding and (room := p.int("artistCount", 20) - len(artists)) > 0:
                artists += [self._mb_artist(db, a) for a in finding["artist"].result()
                            if not {fold(n) for n in [a["name"], *(x["name"] for x in a.get("aliases") or [])]} & names
                            and not tracks.library_id(db, MB_IDS["artist"] + a["id"])][:room]
            if "album" in finding and (room := p.int("albumCount", 20) - len(albums)) > 0:
                albums += [self._mb_album(db, g) for g in finding["album"].result()
                           if fold(g["title"]) not in titles and not tracks.library_id(db, MB_IDS["album"] + g["id"])][:room]
        return {"artist": artists, "album": albums, "song": songs}

    @staticmethod
    def _in_order(db, query, column, ids, shape) -> list[dict]:
        """query's rows grouped by column, for ids in their order."""
        if not ids:
            return []
        rows = {r["id"]: r for r in db.execute(query + f" where {column} in ({','.join('?' * len(ids))}) group by {column}", ids)}
        return [shape(rows[i]) for i in ids if i in rows]

    def _find(self, kind, text) -> list[dict]:
        """MusicBrainz's artists or albums named like text, on a connection of its own (for the finder's threads)."""
        db = self.oasis.connect()
        try:
            return musicbrainz.find(kind, text, db)
        except (OSError, LookupError) as e:
            log.warning("MusicBrainz search failed: %s", e)
            return []
        finally:
            db.close()

    def _musicbrainz(self, get, *args):
        """get(*args) for a MusicBrainz-only item: a lookup of musicbrainz.py."""
        try:
            return get(*args)
        except LookupError:
            raise Fail(NOT_FOUND, "Data not found") from None
        except OSError as e:
            raise Fail(0, f"MusicBrainz didn't answer: {e}") from None

    def _wanted(self, db, kind, id) -> dict:
        """Starred and oasisWanted of an artist or album, as album() and artist() give them."""
        starred = db.execute("select starred from stat where id = ? and user = me()", (id,)).fetchone()
        item = db.execute("select state, error from want_item where kind = ? and ref = ?", (kind, id)).fetchone()
        return {"starred": iso(starred and starred[0]), **wanted(item and item["state"], item and item["error"])}

    def _mb_artist(self, db, a: dict) -> dict:
        """A MusicBrainz artist (search result or lookup) the library doesn't have."""
        id = MB_IDS["artist"] + a["id"]
        return clean({"id": id, "name": a["name"], "albumCount": 0, "musicBrainzId": a["id"], **self._wanted(db, "artist", id)})

    def _mb_album(self, db, g: dict) -> dict:
        """A MusicBrainz release group: the library's album when it has arrived, else one without songs."""
        if local := tracks.library_id(db, MB_IDS["album"] + g["id"]):
            return album(required(album_row(db, local)))
        id, credit = MB_IDS["album"] + g["id"], g.get("artist-credit") or []
        artist_id = MB_IDS["artist"] + credit[0]["artist"]["id"] if credit else None
        artist_id = artist_id and tracks.current_id(db, artist_id)
        year = (g.get("first-release-date") or "")[:4]
        artist = musicbrainz.credited(credit) or None
        return clean({**album_base(id, g["title"], artist, artist_id), "coverArt": id, "songCount": 0, "duration": 0, "year": int(year) if year.isdigit() else None,
                      "releaseTypes": [t for t in [g.get("primary-type"), *(g.get("secondary-types") or [])] if t] or None,
                      **self._wanted(db, "album", id)})

    def search3(self, db, p, user):
        return {"searchResult3": self._search(db, p)}

    def search2(self, db, p, user):
        return {"searchResult2": self._search(db, p)}

    def _starred(self, db, p):
        """The user's stars; MusicBrainz-only artists and albums only for the oasis extension, as search finds them."""
        def ids(prefix):
            return db.execute("select id from stat where user = me() and starred is not null and id like ? order by starred desc", (prefix + "%",)).fetchall()
        return {
            "artist": [a for r in ids(IDS["artist"]) for a in ([artist(required(artist_row(db, r["id"])))]
                                                      if db.execute("select 1 from track where artist_id = ?", (r["id"],)).fetchone()
                                                      else self._starred_mb(db, "artist", r["id"]) if p.oasis else [])],
            "album": [a for r in ids(IDS["album"]) for a in ([album(required(album_row(db, r["id"])))]
                                                     if db.execute("select 1 from track where album_id = ?", (r["id"],)).fetchone()
                                                     else self._starred_mb(db, "album", r["id"]) if p.oasis else [])],
            "song": [song(r) for r in db.execute(SONGS + " where s.starred is not null order by s.starred desc")],
        }

    def _starred_mb(self, db, kind, id) -> list[dict]:
        """A starred MusicBrainz-only artist or album, as getArtist and getAlbum give it (from mb_cache, where searching
        and acquiring it put it); none while MusicBrainz doesn't answer, or for another id."""
        if not (mbid := tracks.mbid_of(id)):
            return []
        try:
            if kind == "artist":
                return [self._mb_artist(db, musicbrainz.artist(mbid, db))]
            return [self._mb_album(db, musicbrainz.release_group(mbid, db))]
        except (OSError, LookupError) as e:
            log.info("starred %s unavailable: %s", id, e)
            return []

    def getStarred2(self, db, p, user):
        return {"starred2": self._starred(db, p)}

    def getStarred(self, db, p, user):
        return {"starred": self._starred(db, p)}

    def _set_starred(self, db, p, value, user):
        for id in p.all("id"):
            set_stat(db, user, id, "starred", value)
        touch(db, p.all("id"), self.oasis.youtube.fetch_soon)
        self.oasis.want(db, user, "album", p.all("albumId"), value)
        self.oasis.want(db, user, "artist", p.all("artistId"), value)
        return {}

    def star(self, db, p, user):
        return self._set_starred(db, p, time.time(), user)

    def unstar(self, db, p, user):
        return self._set_starred(db, p, None, user)

    def setRating(self, db, p, user):
        rating = p.int("rating", 0) or None
        set_stat(db, user, p.need("id"), "rating", rating)
        return {}

    def scrobble(self, db, p, user):
        if p.get("submission", "true") == "false":
            for id in p.all("id")[:1]:
                self.oasis.forwarder.now_playing(db, user, forward.listen_for(self._one(db, "select * from track where id = ?", id), time.time()))
            return {}
        # Every id is checked before anything is written, so a retried batch can't count plays twice.
        times = p.all("time")
        plays = [(id, int(times[i]) / 1000 if i < len(times) else time.time()) for i, id in enumerate(p.all("id"))]
        known = [(id, played) for id, played in plays if db.execute("select 1 from track where id = ?", (id,)).fetchone()]
        if plays and not known:
            raise Fail(NOT_FOUND, "Data not found")
        for id, _ in set(plays) - set(known):
            log.info("scrobble for unknown song %s skipped", id)
        self.oasis.played(db, user, known)
        return {}

    def getLyricsBySongId(self, db, p, user):
        r = self._one(db, "select * from track where id = ?", p.need("id"))
        found = lyrics.for_track(db, r, self.oasis.ytm)
        if not found:
            return {"lyricsList": {}}
        main = {"displayArtist": r["artist"], "displayTitle": r["title"], "lang": "xxx", "offset": 0, "synced": found.synced,
                "line": [clean({"start": start, "value": text}) for start, text in found.lines]}
        if p.get("enhanced") != "true":
            return {"lyricsList": {"structuredLyrics": [main]}}
        # songLyrics v2, with the romanization extension's kinds: main is the song's own lines, pronunciation their
        # reading (none for lyrics in Latin script, where main stands in), with karaoke word timing as cueLines;
        # translation the providers' translations.
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
        if "translation" in kinds:  # one line per main line, "" where the provider has none
            entries += [{**main, "kind": "translation", "lang": lang, "line": [{**clean({"start": start}), "value": v} for (start, _), v in zip(found.lines, values)]}
                        for lang, values in (found.translations or {}).items()]
            want = self._translate_to(db, p, user)
            if want and not any(translate.primary(lang) == translate.primary(want) for lang in found.translations or {}):  # a provider's comes first
                if machine := self.oasis.translations.lines(db, r["id"], [text for _, text in found.lines], want):
                    entries.append({**main, "kind": "translation", "lang": want, "oasisMachineTranslation": True,
                                    "line": [{**clean({"start": start}), "value": v} for (start, _), v in zip(found.lines, machine)]})
        return {"lyricsList": clean({"structuredLyrics": entries or None})}

    def _translate_to(self, db, p, user) -> str | None:
        """The language a request asks lyrics translated to (oasis extension only): oasisTranslate=<lang>, or 1 for the
        user's (the dashboard's). None: no machine translation."""
        asked = p.get("oasisTranslate") if p.oasis else None
        if not asked:
            return None
        if asked == "1":
            return accounts.get(db, user)["translate_lang"]
        if not translate.LANG.fullmatch(asked):
            raise Fail(0, "oasisTranslate must be 1 or a language code such as en, pl or zh-CN")
        return asked

    def oasisTranslation(self, db, p, user):
        """Lyrics translation for this user (docs/oasis-extension.md): the dashboard's language and those it offers."""
        available = self.oasis.translations.translator is not None
        return {"oasisTranslation": clean({"available": available, "language": accounts.get(db, user)["translate_lang"],
                                           "languages": [{"id": k, "name": v} for k, v in translate.LANGUAGES.items()]})}

    def getLyrics(self, db, p, user):
        artist, title = p.get("artist", ""), p.get("title", "")
        r = db.execute(f"select * from track where title = ? collate nocase and (? = '' or {BY_ARTIST_NAME}) "
                       "order by library desc limit 1", (title, artist, artist, artist)).fetchone()
        found = lyrics.for_track(db, r, self.oasis.ytm) if r else None
        text = found.text() if found else None
        return {"lyrics": clean({"artist": r["artist"] if r else artist, "title": r["title"] if r else title, "value": text})}

    def getNowPlaying(self, db, p, user):
        return {"nowPlaying": {}}

    def _artist_info(self, db, p):
        r = artist_row(db, p.need("id"))
        if r is None:  # only credited on others' songs
            self._one(db, "select 1 from credit where artist_id = ? limit 1", p.need("id"))
            return clean({"similarArtist": self._linked_artists(db, p.need("id")) or None})
        self.oasis.youtube.artist_page(db, r["id"], r["name"])
        page = db.execute("select description, image from ytm_artist where artist_id = ?", (r["id"],)).fetchone()
        youtube = clean({"biography": page["description"], "smallImageUrl": page["image"], "mediumImageUrl": page["image"],
                         "largeImageUrl": page["image"]}) if page else {}
        linked = self._linked_artists(db, r["id"])
        if not self.oasis.lastfm:
            return {**youtube, **clean({"similarArtist": linked or None})}
        try:
            info = self.oasis.lastfm.artist_info(r["name"])
        except Exception as e:
            log.info("Last.fm artist info failed: %s", e)
            return {**youtube, **clean({"similarArtist": linked or None})}
        names = lastfm.similar_artists(info)[:p.int("count", 20)]
        similar = [artist(a) for name in names for a in db.execute(
            ARTISTS + " where t.library = 1 and t.album_artist = ? collate nocase group by t.artist_id", (name,))]
        similar = linked + [a for a in similar if a["id"] not in {x["id"] for x in linked}]
        return {**youtube, **clean({"biography": lastfm.bio(info), "lastFmUrl": info.get("url"),
                                    "musicBrainzId": info.get("mbid") or None, "similarArtist": similar or None})}

    def _linked_artists(self, db, artist_id):
        """Personas, the user's related artists and band members that have songs here, closest first; oasisLink says how."""
        found = []
        for other, kind in artists.linked(db, artist_id):
            if r := artist_row(db, other):
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

    def _source(self, p, account) -> str:
        """The source a request names (oasis extension only), else the user's default (the dashboard's)."""
        source = p.get("oasisSource") if p.oasis else None
        if source is not None and source not in SOURCES:
            raise Fail(0, f"oasisSource must be one of {', '.join(SOURCES)}")
        return source or account["radio_source"]

    def _similar(self, db, p, user) -> dict:
        id = p.need("id")
        seed = self._one(db, SONGS + f" where t.{id_column(id)} = ? order by s.play_count desc, t.library desc, t.disc_no, t.track_no limit 1", id)
        account = accounts.get(db, user)
        source = self._source(p, account)
        # A player waits for this answer with nothing to play next, so a slow source is left out rather than waited for.
        ids = self.oasis.recommender.similar(db, seed, min(p.int("count", 50), 200), source=source, token=account["listenbrainz_token"],
                                             wait=SIMILAR_WAIT)
        self.oasis.youtube.warm(db, ids[:WARM])
        return {"song": [song(required(song_row(db, i))) for i in ids], "oasisSource": source}

    def getSimilarSongs2(self, db, p, user):
        return {"similarSongs2": self._similar(db, p, user)}

    def getSimilarSongs(self, db, p, user):
        return {"similarSongs": self._similar(db, p, user)}

    def oasisRadio(self, db, p, user):
        """The radio sources and stations this user can choose from (docs/oasis-extension.md)."""
        account = accounts.get(db, user)
        recommender = self.oasis.recommender
        return {"oasisRadio": {
            "defaultSource": account["radio_source"],
            "source": [{"id": s, "name": SOURCES[s]} for s in recommender.sources(account)],
            "station": [clean({"id": s, "name": STATIONS[s], "period": list(PERIODS) if s == "lastfm-top" else None,
                               "defaultPeriod": PERIOD if s == "lastfm-top" else None}) for s in recommender.stations(account)]}}

    def getTopSongs(self, db, p, user):
        name, count = p.need("artist"), min(p.int("count", 50), 200)
        ids = [r[0] for r in db.execute("select t.id from track t left join stat s on s.id = t.id and s.user = me() where t.library = 1 and "
                                        "t.album_artist = ? collate nocase order by coalesce(s.play_count, 0) desc limit ?", (name, count))]
        if len(ids) < count:
            ids += self.oasis.youtube.top_songs(db, name, count)
        return {"topSongs": {"song": [song(required(song_row(db, i))) for i in dict.fromkeys(ids)][:count]}}

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
        if entries:
            songs = [song(s) for s in db.execute(
                SONGS + " join playlist_entry e on e.track_id = t.id where e.playlist_id = ? order by e.pos", (r["id"],))]
            count, duration = len(songs), sum(s.get("duration", 0) for s in songs)
        else:
            count, duration = db.execute("select count(*), coalesce(sum(t.duration), 0) from playlist_entry e join track t on t.id = e.track_id "
                                         "where e.playlist_id = ?", (r["id"],)).fetchone()
        result = clean({
            "id": str(r["id"]), "name": r["name"], "comment": r["comment"], "owner": mixes.OWNER if r["mix"] else r["owner"], "public": False,
            "coverArt": IDS["playlist"] + str(r["id"]) if r["cover"] else None,
            "readonly": True if r["mix"] or r["radio"] else None,  # OpenSubsonic: clients offer no edits; oasis keeps mixes and radios
            "oasisMix": True if ytm.is_mix(r["mix"]) else None, "youtubeId": r["mix"],
            "oasisSource": (r["radio_source"] or accounts.get(db, r["owner"])["radio_source"]) if r["radio"] else None,
            "oasisStation": r["station"], "oasisPeriod": r["period"],
            "songCount": count, "duration": duration,
            "created": iso(r["created"]), "changed": iso(r["changed"]),
        })
        return {**result, "entry": songs} if entries else result

    def getPlaylists(self, db, p, user):
        rows = sorted(db.execute("select * from playlist where owner = ? and hidden = 0", (user,)), key=mixes.playlist_order)
        return {"playlists": {"playlist": [self._playlist(db, r, False) for r in rows]}}

    def _own_playlist(self, db, id, user):
        r = self._one(db, "select * from playlist where id = ? and owner = ? and hidden = 0", id, user)
        if r["mix"]:
            raise Fail(DENIED, "YouTube Music's playlists are read-only")
        return r

    def getPlaylist(self, db, p, user):
        r = self._one(db, "select * from playlist where id = ? and owner = ? and hidden = 0", p.need("id"), user)
        if r["radio"]:
            self.oasis.youtube.warm(db, [e[0] for e in db.execute("select track_id from playlist_entry where playlist_id = ? and pos > ? "
                                                                  "order by pos limit ?", (r["id"], r["played_pos"], WARM))])
        return {"playlist": self._playlist(db, r, True)}

    def createPlaylist(self, db, p, user):
        """With the oasis extension's oasisSource or oasisStation, a radio whatever its name, filled before the answer."""
        account, recommender = accounts.get(db, user), self.oasis.recommender
        source, station = (p.get("oasisSource"), p.get("oasisStation")) if p.oasis else (None, None)
        if id := p.get("playlistId"):
            playlist_id, source, station = self._own_playlist(db, id, user)["id"], None, None
        else:
            if source is not None and source not in recommender.sources(account):
                raise Fail(0, f"oasisSource {source} isn't available: oasisRadio lists those that are")
            if station is not None and station not in recommender.stations(account):
                raise Fail(0, f"oasisStation {station} isn't available: oasisRadio lists those that are")
            period = (p.get("oasisPeriod") or PERIOD) if station == "lastfm-top" else None
            if period is not None and period not in PERIODS:
                raise Fail(0, f"oasisPeriod must be one of {', '.join(PERIODS)}")
            name = p.get("name") or STATIONS.get(station) or p.need("name")
            radio = RADIO if source or station else is_radio(name)
            playlist_id = playlists.create(db, user, name, radio, source=(source or account["radio_source"]) if radio else None,
                                           station=station, period=period)
        set_entries(db, playlist_id, p.all("songId"), self.oasis.youtube.fetch_soon)
        if source or station:
            try:
                recommender.top_up(db, self._one(db, "select * from playlist where id = ?", playlist_id))
            except Exception:
                log.exception("radio fill failed")
        recommender.wake()
        return {"playlist": self._playlist(db, self._own_playlist(db, playlist_id, user), True)}

    def updatePlaylist(self, db, p, user):
        r = self._own_playlist(db, p.need("playlistId"), user)
        if (name := p.get("name")) is not None:
            playlists.rename(db, r["id"], name)
        if (comment := p.get("comment")) is not None:
            db.execute("update playlist set comment = ?, changed = ? where id = ?", (comment, time.time(), r["id"]))
        entries = [e["track_id"] for e in db.execute("select track_id from playlist_entry where playlist_id = ? order by pos", (r["id"],))]
        removed = {int(i) for i in p.all("songIndexToRemove")}
        added = p.all("songIdToAdd")
        if removed or added:
            set_entries(db, r["id"], [t for i, t in enumerate(entries) if i not in removed] + added, self.oasis.youtube.fetch_soon)
        self.oasis.recommender.wake()
        return {}

    def deletePlaylist(self, db, p, user):
        playlist_id = self._own_playlist(db, p.need("id"), user)["id"]
        playlists.delete(db, playlist_id, self.oasis.youtube.fetch_soon)
        return {}

    # The upload extension (docs/upload-extension.md): admins only, the users with uploadRole.

    def lookupDisc(self, db, p, user):
        self._admin(db, user)
        found = self.oasis.uploads.lookup(db, p.need("discId"), p.get("toc"))
        return {"discLookup": {"release": [disc_release(*f) for f in found]}}

    def createUpload(self, db, p, user):
        self._admin(db, user)
        if not p.get("releaseId") and p.get("tags") is None:
            raise Fail(MISSING, "Required parameter 'releaseId' or 'tags' is missing")
        try:
            disc = p.int("disc", 1)
        except ValueError:
            raise Fail(0, "disc must be an integer") from None
        id = self.oasis.uploads.create(db, user, p.get("releaseId"), disc, p.get("tags"))
        return {"upload": self._upload(db, user, id, progress=False)}

    def uploadTrack(self, db, p, user):
        self._admin(db, user)
        try:
            numbers = [int(p.need(k)) for k in ("disc", "number", "offset", "size")]
        except ValueError:
            raise Fail(0, "disc, number, offset and size must be integers") from None
        return {"uploadTrack": {"received": self.oasis.uploads.receive(db, user, p.need("uploadId"), *numbers, p.need("suffix"), p.body)}}

    def getUpload(self, db, p, user):
        self._admin(db, user)
        return {"upload": self._upload(db, user, p.need("id"))}

    def _upload(self, db, user, id, progress=True) -> dict:
        found = []
        for t in self.oasis.uploads.tracks(db, user, id):
            track = {"disc": t["disc"], "number": t["number"]}
            if progress:
                track |= clean({"received": t["received"], "size": t["size"]})
            found.append(track)
        return {"id": id, "expires": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + upload.KEEP)), "track": found}

    def uploadCover(self, db, p, user):
        self._admin(db, user)
        self.oasis.uploads.cover(db, user, p.need("uploadId"), p.body)
        return {}

    def cancelUpload(self, db, p, user):
        self._admin(db, user)
        self.oasis.uploads.cancel(db, user, p.need("uploadId"))
        return {}

    def commitUpload(self, db, p, user):
        self._admin(db, user)
        placed = self.oasis.uploads.commit(db, user, p.need("uploadId"), p.get("replace") == "true")
        self.oasis.scanner.now(db)
        ids = list(dict.fromkeys(self._one(db, "select album_id from track where ref = ?", str(f))["album_id"] for f in placed))
        albums = [self._library_album(db, id, self._album_songs(db, id))["album"] for id in ids]
        return {"album": albums[0], **({"albums": albums} if len(albums) > 1 else {})}  # loose singles: each an album

    def getCoverArt(self, db, p, user):
        image = cover.find(db, p.need("id"))
        if image is None:
            return Response(status_code=404)  # a status clients see, not an error document they would draw as an image
        return Response(image[0], media_type=image[1], headers={"Cache-Control": "public, max-age=86400"})

    def stream(self, db, p, user):
        r = self._one(db, "select source, ref, content_type, suffix, bitrate from track where id = ?", p.need("id"))
        target = transcode.target(p.get("format"), p.int("maxBitRate", 0), r["suffix"], r["bitrate"])
        source = r["ref"]
        if r["source"] == "ytm":
            if not self.oasis.ytm:
                raise Fail(NOT_FOUND, "YouTube Music is not enabled")
            try:
                source = youtube.audio(self.oasis.ytm, r["ref"])
            except Exception as e:
                log.warning("download of %s failed: %s", r["ref"], e)
                raise Fail(NOT_FOUND, "Stream unavailable")
        if isinstance(source, ytm.Stream):
            if target:
                return transcode.stream(source.url, *target, p.int("timeOffset", 0), source.headers)
            return self._proxy(r["ref"], source, p.range)
        if target:
            return transcode.stream(str(source), *target, p.int("timeOffset", 0))
        return FileResponse(source, media_type=r["content_type"])

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
                yield from ytm.read_range(stream, start, end)
            except Exception as e:
                log.warning("stream of %s broke: %s", video_id, e)
                self.oasis.ytm.forget(video_id)

        headers = {"Accept-Ranges": "bytes", "Content-Length": str(end - start + 1)}
        if range_header:
            headers["Content-Range"] = f"bytes {start}-{end}/{stream.size}"
        return StreamingResponse(body(), status_code=206 if range_header else 200, media_type=stream.content_type, headers=headers)

    def download(self, db, p, user):
        return self.stream(db, p, user)


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


def routes(oasis) -> list[Route]:
    """The Subsonic API's route, for oasis (server.py)."""
    api = Api(oasis)

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
            db = oasis.connect()
            try:
                user = api._authenticate(db, p)
                as_user(db, user)
                p.follow_moves(db)
                result = handler(api, db, p, user)
                if isinstance(result, Response):
                    return result
                return respond(fmt, romanized(describe(db, result if p.oasis else plain(result)), p.get("romanization"),
                                                     latin.curated(db) if p.get("romanization") in ("both", "latin") else None))
            except Fail as e:
                return respond(fmt, {}, e)
            except upload.Refused as e:
                code = NOT_FOUND if e.kind == upload.MISSING else 0
                return respond(fmt, {}, Fail(code, str(e), {"duplicate": True} if e.kind == upload.DUPLICATE else None))
            finally:
                db.close()
        return await run_in_threadpool(run)

    return [Route("/rest/{method}", endpoint, methods=["GET", "POST"])]
