"""Who made each song: every performer with their instrument or voice, the writers, composers, lyricists, arrangers,
producers and engineers, as MusicBrainz credits them, plus ISRCs, labels and genres. Lidarr tags each file with its
MusicBrainz release and recording ids; one request per release fetches everything (MusicBrainz allows one a second).
Names are kept as credited: an in-universe persona or anime character stays the credit, never swapped for whoever is
behind it. Missing tags are written into the files, so other players see the same; values already there are kept.
Files without MusicBrainz ids get their credits from their own tags."""
import json
import logging
import time

from . import musicbrainz
from .tracks import artist_id
from .audiorecords import retagged, same_audio
from .tags import write_tags

log = logging.getLogger(__name__)

NOT_FOUND = -1  # credits_at for releases MusicBrainz doesn't have

# MusicBrainz relationship -> role, for recordings, works and whole releases.
ROLES = {"vocal": "performer", "instrument": "performer", "performer": "performer", "performing orchestra": "performer",
         "conductor": "conductor", "producer": "producer", "mix": "mixer", "recording": "engineer", "engineer": "engineer",
         "sound": "engineer", "programming": "performer", "remixer": "remixer", "arranger": "arranger",
         "instrument arranger": "arranger", "vocal arranger": "arranger", "orchestrator": "arranger",
         "composer": "composer", "lyricist": "lyricist", "writer": "writer", "librettist": "lyricist"}
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


def same_credit(c: dict) -> tuple:
    """What makes two credits one, as files are tagged: the role, sub-role and artist, by MusicBrainz id, else by name."""
    return c["role"], c["sub_role"], c["mbid"] or c["artist_name"].casefold()


def same_listed_credit(c: dict) -> tuple:
    """What makes two stored credits one in a listing: the role, sub-role and oasis artist, which a name without a
    MusicBrainz id shares with the MusicBrainz artist of that name."""
    return c["role"], c["sub_role"], c["artist_id"]


def dedupe_credits(credits: list[dict], key=same_credit) -> list[dict]:
    seen, out = set(), []
    for c in credits:
        key_of = key(c)
        if key_of not in seen:
            seen.add(key_of)
            out.append(c)
    return out


def step(db) -> bool:
    """Fills one release that arrived in the library; whether there was one. OSError: MusicBrainz is away."""
    row = db.execute("select mb_release from track where source = 'local' and mb_release is not null "
                     "and credits_at is null limit 1").fetchone()
    if row is None:
        return False
    release = row["mb_release"]
    try:
        data = musicbrainz.release_credits(release)
    except LookupError:
        db.execute("update track set credits_at = ? where mb_release = ?", (NOT_FOUND, release))
        return True
    apply(db, data)
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


def performance_credits(db, path: str, credits: list[dict], key=same_credit) -> list[dict]:
    """`credits` plus what MusicBrainz credits on other releases of the same audio (dedupe's matches): one
    performance entered twice and never merged gets the union of both entries' credits. key: see dedupe_credits."""
    others = same_audio(db, path)
    if not others:
        return credits
    rows = db.execute(f"select c.* from credit c join track t on t.id = c.track_id where c.source = 'musicbrainz' and t.ref in "
                      f"({','.join('?' * len(others))}) order by c.position", others).fetchall()
    shared = [dict(r) for r in rows if r["role"] in PERFORMANCE_ROLES]
    return dedupe_credits(credits + shared, key)


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


def credits_of(db, track_ids: list[str]) -> dict[str, list[dict]]:
    """Each track's credits: MusicBrainz's when it has them, else the file's tags; plus the performance credits
    MusicBrainz has for other releases of the same audio (performance_credits)."""
    marks = ",".join("?" * len(track_ids))
    found = {}
    for r in db.execute(f"select * from credit where track_id in ({marks}) order by position", track_ids):
        found.setdefault(r["track_id"], {}).setdefault(r["source"], []).append(dict(r))
    mine = {}
    for id, ref in db.execute(f"select id, ref from track where id in ({marks})", track_ids):
        sources = found.get(id, {})
        credits = performance_credits(db, ref, sources.get("musicbrainz") or sources.get("tags", []), same_listed_credit)
        if credits:
            mine[id] = credits
    return mine


def people(credits, role: str) -> tuple[list[dict], str | None]:
    """The artists of a role, and how they read together ('A feat. B')."""
    rows = [c for c in credits if c["role"] == role]
    if not rows:
        return [], None
    joined = "".join(c["name"] + (c["joinphrase"] or "") for c in rows) if any(c["joinphrase"] for c in rows) else ", ".join(c["name"] for c in rows)
    return [{"id": c["artist_id"], "name": c["name"]} for c in rows], joined
