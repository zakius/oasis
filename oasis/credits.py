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
import urllib.error
import urllib.request

import mutagen
from mutagen._vorbis import VCommentDict

from .db import make_id

log = logging.getLogger(__name__)

MUSICBRAINZ = "https://musicbrainz.org/ws/2/release/{}?fmt=json&inc=artist-credits+recordings+isrcs+labels+genres+" \
              "release-groups+artist-rels+recording-level-rels+work-rels+work-level-rels"
USER_AGENT = "oasis/0.1 ( https://github.com/zakius/oasis )"
GAP = 1.1             # seconds between MusicBrainz requests
RETRY = 300           # seconds before retrying after MusicBrainz errors
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
            "artist_type": artist.get("type"), "artist_id": make_id("ar-", canonical), "position": position, "joinphrase": joinphrase}


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
        key = (c["role"], c["sub_role"], c["artist_id"])
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
    """Adds what the file lacks (Vorbis comments: FLAC, Ogg); returns whether it changed."""
    audio = mutagen.File(path)
    if audio is None or not isinstance(audio.tags, VCommentDict):
        return False
    tags, before = audio.tags, len(audio.tags)
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
    for tag, values in wanted.items():
        values = list(dict.fromkeys(v for v in values if v))
        if values and not tags.get(tag):
            tags[tag] = values
    if len(tags) == before:
        return False
    audio.save()
    return True


class Credits:
    """Fills credits for releases that arrived in the library, one MusicBrainz request at a time."""

    def __init__(self, connect, fetch=None):
        self.connect, self.fetch = connect, fetch or fetch_release
        self.wake = threading.Event()

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        while True:
            try:
                while self.step():
                    time.sleep(GAP)
            except Exception:
                log.exception("credits failed")
            self.wake.wait(RETRY)
            self.wake.clear()

    def step(self) -> bool:
        """Fills one release; whether there may be more to do."""
        db = self.connect()
        try:
            row = db.execute("select mb_release from track where source = 'local' and mb_release is not null "
                             "and credits_at is null limit 1").fetchone()
            if row is None:
                return False
            release = row["mb_release"]
            try:
                data = self.fetch(release)
            except LookupError:
                db.execute("update track set credits_at = ? where mb_release = ?", (NOT_FOUND, release))
                db.commit()
                return True
            except OSError as e:
                log.warning("MusicBrainz release %s: %s", release, e)
                return False
            apply(db, data)
            db.commit()
            return True
        finally:
            db.close()


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
        try:
            write_tags(row["ref"], fields, entry["credits"])
        except OSError as e:  # read-only library: the database still has it all
            log.info("can't write tags to %s: %s", row["ref"], e)
        columns = [k for k in ("label", "release_types", "isrc", "original_date", "mb_recording", "genres") if fields.get(k)]
        genres = json.loads(fields.get("genres") or "[]")
        db.execute(f"update track set {', '.join(f'{c} = ?' for c in columns)}{', ' if columns else ''}"
                   "genre = coalesce(genre, ?), credits_at = ? where id = ?",
                   [*(fields[c] for c in columns), genres[0] if genres else None, time.time(), row["id"]])


def store(db, track_id: str, credits: list[dict], source: str):
    db.execute("delete from credit where track_id = ? and source = ?", (track_id, source))
    db.executemany("insert into credit(track_id, role, sub_role, name, artist_name, artist_id, mbid, artist_type, position, "
                   "joinphrase, source) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   [(track_id, c["role"], c["sub_role"], c["name"], c["artist_name"], c["artist_id"], c["mbid"], c["artist_type"],
                     c["position"], c["joinphrase"], source) for c in credits])


def fetch_release(mbid: str) -> dict:
    request = urllib.request.Request(MUSICBRAINZ.format(mbid), headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise LookupError(mbid) from e
        raise OSError(f"MusicBrainz: {e.code}") from e
