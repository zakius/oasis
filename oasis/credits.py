"""Who made each song: every performer with their instrument or voice, the writers, composers, lyricists, arrangers,
producers and engineers, as MusicBrainz credits them, plus ISRCs, labels and genres. Lidarr tags each file with its
MusicBrainz release and recording ids; one request per release fetches everything (MusicBrainz allows one a second).
Names are kept as credited: an in-universe persona or anime character stays the credit, never swapped for whoever is
behind it. Missing tags are written into the files, so other players see the same; values already there are kept.
Files without MusicBrainz ids get their credits from their own tags."""
import json
import logging
import threading
import time

import mutagen
from mutagen._vorbis import VCommentDict

from . import artists, musicbrainz
from .db import artist_id
from .dedupe import retagged, same_audio

log = logging.getLogger(__name__)

MUSICBRAINZ = "https://musicbrainz.org/ws/2/release/{}?fmt=json&inc=artist-credits+recordings+isrcs+labels+genres+" \
              "release-groups+artist-rels+recording-level-rels+work-rels+work-level-rels"
MUSICBRAINZ_ARTIST = "https://musicbrainz.org/ws/2/artist/{}?fmt=json&inc=aliases+artist-rels"
RETRY = 30, 600       # seconds before retrying after MusicBrainz errors (503 when busy): first, doubling up to the last
IDLE = 300            # seconds between looks for new releases when nothing woke the worker
NOT_FOUND = -1        # credits_at for releases MusicBrainz doesn't have

# MusicBrainz relationship -> role, for recordings, works and whole releases.
ROLES = {"vocal": "performer", "instrument": "performer", "performer": "performer", "performing orchestra": "performer",
         "conductor": "conductor", "producer": "producer", "mix": "mixer", "recording": "engineer", "engineer": "engineer",
         "sound": "engineer", "programming": "performer", "remixer": "remixer", "arranger": "arranger",
         "instrument arranger": "arranger", "vocal arranger": "arranger", "orchestrator": "arranger",
         "composer": "composer", "lyricist": "lyricist", "writer": "writer", "librettist": "lyricist"}
# Role -> tag written into files (Picard's names); performers go to PERFORMER as "Name (instrument)".
TAGS = {"composer": "COMPOSER", "lyricist": "LYRICIST", "writer": "WRITER", "arranger": "ARRANGER", "conductor": "CONDUCTOR",
        "producer": "PRODUCER", "mixer": "MIXER", "engineer": "ENGINEER", "remixer": "REMIXER", "performer": "PERFORMER"}


def credit(role, name, artist, position=0, sub_role=None, joinphrase=None) -> dict:
    """`name` as credited, `artist` the MusicBrainz artist (id, canonical name, type) when known."""
    artist = artist or {}
    canonical = artist.get("name") or name
    return {"role": role, "sub_role": sub_role, "name": name or canonical, "artist_name": canonical, "mbid": artist.get("id"),
            "artist_type": artist.get("type"), "position": position, "joinphrase": joinphrase}


def artist_credits(role: str, credits: list) -> list[dict]:
    return [credit(role, c.get("name"), c.get("artist"), i, joinphrase=c.get("joinphrase")) for i, c in enumerate(credits or [])]


def relation_credits(relations: list) -> list[dict]:
    found = []
    for r in relations or []:
        role = ROLES.get(r.get("type"))
        if not role or not r.get("artist"):
            continue
        attributes = [a for a in r.get("attributes") or [] if a not in ("additional", "guest", "solo", "co")]
        sub = ", ".join(attributes) or ("vocals" if r["type"] == "vocal" else None)
        found.append(credit(role, r.get("target-credit") or r["artist"]["name"], r["artist"], len(found), sub))
    return found


def release_metadata(data: dict) -> tuple[dict, dict]:
    """Release-wide fields, and per track (by release track id and by recording id): fields and credits."""
    group = data.get("release-group") or {}
    release = {
        "mb_release": data["id"],
        "label": "; ".join(dict.fromkeys(l["label"]["name"] for l in data.get("label-info") or [] if l.get("label"))) or None,
        "release_types": "; ".join(t.lower() for t in [group.get("primary-type"), *(group.get("secondary-types") or [])] if t) or None,
        "genres": json.dumps([g["name"] for g in sorted(group.get("genres") or data.get("genres") or [], key=lambda g: -g.get("count", 0))][:5]),
        "original_date": group.get("first-release-date") or None,
    }
    album_artists = artist_credits("albumartist", data.get("artist-credit"))
    release_wide = relation_credits(data.get("relations"))  # producers and engineers credited for the whole release
    tracks = {}
    for medium in data.get("media") or []:
        for track in medium.get("tracks") or []:
            recording = track.get("recording") or {}
            credits = artist_credits("artist", track.get("artist-credit") or recording.get("artist-credit"))
            credits += album_artists + release_wide + relation_credits(recording.get("relations"))
            for r in recording.get("relations") or []:
                if r.get("type") == "performance" and r.get("work"):
                    credits += relation_credits(r["work"].get("relations"))
            genres = [g["name"] for g in sorted(recording.get("genres") or [], key=lambda g: -g.get("count", 0))][:5]
            entry = {"fields": {"mb_recording": recording.get("id"), "isrc": "; ".join(recording.get("isrcs") or []) or None,
                                **({"genres": json.dumps(genres)} if genres else {})},
                     "credits": dedupe_credits(credits)}
            tracks[track["id"]] = entry
            tracks.setdefault(recording.get("id"), entry)
    return release, tracks


def dedupe_credits(credits: list[dict]) -> list[dict]:
    seen, out = set(), []
    for c in credits:
        key = (c["role"], c["sub_role"], c["mbid"] or c["artist_name"].casefold())
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def tag_credits(tags) -> list[dict]:
    """Credits from a file's own tags, for files MusicBrainz knows nothing about."""
    if tags is None:
        return []
    get = lambda key: [str(v) for v in (tags.get(key) or [])] if key in tags else []
    found = []
    artists = get("artists") or get("artist")
    found += [credit("artist", name, None, i) for i, name in enumerate(artists)]
    found += [credit("albumartist", name, None, i) for i, name in enumerate(get("albumartist"))]
    for role, tag in TAGS.items():
        if role == "performer":
            continue
        found += [credit(role, name, None, i) for i, name in enumerate(get(tag.lower()))]
    return dedupe_credits(found)


def write_tags(path: str, fields: dict, credits: list[dict]) -> bool:
    """Adds what the file lacks (Vorbis comments: FLAC, Ogg), never removing a value; returns whether it changed."""
    audio = mutagen.File(path)
    if audio is None or not isinstance(audio.tags, VCommentDict):
        return False
    tags = audio.tags
    by_role = {}
    for c in credits:
        by_role.setdefault(c["role"], []).append(c)
    wanted = {"ARTISTS": [c["name"] for c in by_role.get("artist", [])],
              "ISRC": (fields.get("isrc") or "").split("; ") if fields.get("isrc") else [],
              "LABEL": fields["label"].split("; ") if fields.get("label") else [],
              "GENRE": json.loads(fields.get("genres") or "[]"),
              "ORIGINALDATE": [fields["original_date"]] if fields.get("original_date") else []}
    for role, tag in TAGS.items():
        people = by_role.get(role, [])
        wanted[tag] = [f"{c['name']} ({c['sub_role']})" if role == "performer" and c["sub_role"] else c["name"] for c in people]
    changed = False
    for tag, values in wanted.items():
        values = list(dict.fromkeys(v for v in values if v))
        have = tags.get(tag) or []
        # Only adds: an empty tag is filled, and one holding nothing but values from here gains the rest; a tag with
        # anything else in it (someone's own edit) stays as it is.
        if set(have) < set(values):
            tags[tag] = [*have, *(v for v in values if v not in have)]
            changed = True
    if not changed:
        return False
    audio.save()
    return True


class Credits:
    """Fills credits for releases that arrived in the library, one MusicBrainz request at a time."""

    def __init__(self, connect, fetch=None, fetch_artist=None):
        self.connect, self.fetch, self.fetch_artist = connect, fetch or fetch_release, fetch_artist or fetch_artist_data
        self.wake = threading.Event()
        self.retry = RETRY[0]

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        while True:
            wait = IDLE
            try:
                while self.step():
                    pass
            except OSError as e:
                log.warning("MusicBrainz: %s; again in %d s", e, self.retry)
                wait, self.retry = self.retry, min(self.retry * 2, RETRY[1])
            except Exception:
                log.exception("credits failed")
            self.wake.wait(wait)
            self.wake.clear()

    def step(self) -> bool:
        """Fills one release, or when all are, one credited artist's names and links; whether there may be more to do."""
        db = self.connect()
        try:
            row = db.execute("select mb_release from track where source = 'local' and mb_release is not null "
                             "and credits_at is null limit 1").fetchone()
            if row is None:
                return self.artist_step(db)
            release = row["mb_release"]
            try:
                data = self.fetch(release)
            except LookupError:
                db.execute("update track set credits_at = ? where mb_release = ?", (NOT_FOUND, release))
                db.commit()
                return True
            apply(db, data)
            self.retry = RETRY[0]
            db.commit()
            return True
        finally:
            db.close()

    def artist_step(self, db) -> bool:
        row = db.execute("select mbid from mb_artist where fetched is null and mbid in (select mbid from credit) limit 1").fetchone()
        if row is None:
            return False
        try:
            artists.learn(db, self.fetch_artist(row["mbid"]))
        except LookupError:
            db.execute("update mb_artist set fetched = ? where mbid = ?", (NOT_FOUND, row["mbid"]))
        artists.merge(db)
        self.retry = RETRY[0]
        db.commit()
        return True


def apply(db, data: dict):
    release, tracks = release_metadata(data)
    for row in db.execute("select id, ref, mb_track, mb_recording from track where source = 'local' and mb_release = ?",
                          (release["mb_release"],)).fetchall():
        entry = tracks.get(row["mb_track"]) or tracks.get(row["mb_recording"])
        if entry is None:
            db.execute("update track set credits_at = ? where id = ?", (NOT_FOUND, row["id"]))
            continue
        fields = {**release, **entry["fields"]}
        store(db, row["id"], entry["credits"], "musicbrainz")
        retag(db, row["ref"], fields, performance_credits(db, row["ref"], entry["credits"]))
        for other in same_audio(db, row["ref"]):  # releases filled earlier gain what this one adds
            own = [dict(c) for c in db.execute("select c.* from credit c join track t on t.id = c.track_id where t.ref = ? "
                                               "and c.source = 'musicbrainz' order by c.position", (other,))]
            if own:
                retag(db, other, {}, performance_credits(db, other, own))
        columns = [k for k in ("label", "release_types", "isrc", "original_date", "mb_recording", "genres") if fields.get(k)]
        genres = json.loads(fields.get("genres") or "[]")
        db.execute(f"update track set {', '.join(f'{c} = ?' for c in columns)}{', ' if columns else ''}"
                   "genre = coalesce(genre, ?), credits_at = ? where id = ?",
                   [*(fields[c] for c in columns), genres[0] if genres else None, time.time(), row["id"]])


# Credits that describe the performance itself, the same for every release of one stream; the artist credit, album
# artist, label and ISRC are what each release prints and stay its own.
PERFORMANCE_ROLES = {"performer", "conductor", "producer", "mixer", "engineer", "remixer", "arranger", "composer", "lyricist", "writer"}


def performance_credits(db, path: str, credits: list[dict]) -> list[dict]:
    """`credits` plus what MusicBrainz credits on other releases of the same audio (dedupe's matches): one
    performance entered twice and never merged gets the union of both entries' credits."""
    others = same_audio(db, path)
    if not others:
        return credits
    rows = db.execute(f"select c.* from credit c join track t on t.id = c.track_id where c.source = 'musicbrainz' and t.ref in "
                      f"({','.join('?' * len(others))}) order by c.position", others).fetchall()
    shared = [dict(r) for r in rows if r["role"] in PERFORMANCE_ROLES]
    return dedupe_credits(credits + shared)


def retag(db, path: str, fields: dict, credits: list[dict]):
    try:
        if write_tags(path, fields, credits):
            retagged(db, path)
    except OSError as e:  # read-only library: the database still has it all
        log.info("can't write tags to %s: %s", path, e)


def store(db, track_id: str, credits: list[dict], source: str):
    db.execute("delete from credit where track_id = ? and source = ?", (track_id, source))
    db.executemany("insert into credit(track_id, role, sub_role, name, artist_name, artist_id, mbid, artist_type, position, "
                   "joinphrase, source) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   [(track_id, c["role"], c["sub_role"], c["name"], c["artist_name"], artist_id(db, c["artist_name"], c["mbid"]), c["mbid"], c["artist_type"],
                     c["position"], c["joinphrase"], source) for c in credits])


def fetch_release(mbid: str) -> dict:
    return musicbrainz.get(MUSICBRAINZ.format(mbid))


def fetch_artist_data(mbid: str) -> dict:
    return musicbrainz.get(MUSICBRAINZ_ARTIST.format(mbid))
