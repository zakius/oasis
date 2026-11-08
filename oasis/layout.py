"""Where a track goes in the library and what it is tagged: "Artist/Year - Album/NN - Title.ext", named as Lidarr
names files, and the tags Picard writes for a MusicBrainz edition. The importer and uploads place files by these
names, and relocate.py moves older files to them."""
import re
from pathlib import Path

from .musicbrainz import credited
from .tags import position


def tags_for(release: dict, medium: dict, track: dict) -> dict[str, list[str]]:
    """Picard's tags for one track of an edition."""
    group, recording = release.get("release-group") or {}, track.get("recording") or {}
    credit = track.get("artist-credit") or recording.get("artist-credit") or release["artist-credit"]
    labels = release.get("label-info") or []
    return {
        "title": [track["title"]], "artist": [credited(credit)], "artists": [c["name"] for c in credit],
        "albumartist": [credited(release["artist-credit"])], "album": [release["title"]],
        "date": [release.get("date")], "originaldate": [group.get("first-release-date")],
        **position(track["position"], medium.get("track-count") or len(medium["tracks"]), medium["position"], len(release["media"])),
        "media": [medium.get("format")], "releasecountry": [release.get("country")], "releasestatus": [(release.get("status") or "").lower()],
        "releasetype": [t.lower() for t in [group.get("primary-type"), *(group.get("secondary-types") or [])] if t],
        "label": list(dict.fromkeys(l["label"]["name"] for l in labels if l.get("label"))),
        "catalognumber": list(dict.fromkeys(l.get("catalog-number") for l in labels)), "barcode": [release.get("barcode")],
        "isrc": recording.get("isrcs") or [], "musicbrainz_albumid": [release["id"]], "musicbrainz_releasegroupid": [group.get("id")],
        "musicbrainz_releasetrackid": [track["id"]], "musicbrainz_trackid": [recording.get("id")],
        "musicbrainz_artistid": [c["artist"]["id"] for c in credit], "musicbrainz_albumartistid": [c["artist"]["id"] for c in release["artist-credit"]],
    }


def given_tags(given: dict, track: dict, album: str, album_artist: str, disc: int, number: int, discs: int, tracks: int) -> dict[str, list[str]]:
    """The tags for one track of an upload its client described without MusicBrainz (upload.py): given, the upload's
    tags (year, genre, albumArtist); track, the track's (title, artist, artists)."""
    artists = track.get("artists") or [track.get("artist") or given["albumArtist"]]
    return {"title": [track["title"]], "artist": [track.get("artist") or ", ".join(artists)], "artists": artists, "albumartist": [album_artist],
            "album": [album], "date": [str(given.get("year") or "")[:4]], "genre": [given.get("genre")], **position(number, tracks, disc, discs)}


def clean(name: str) -> str:
    """One folder or file name as Lidarr makes it with "replace illegal characters" and smart colon replacement: a run
    of one separator becomes one, and neither separators nor question marks can end it."""
    name = name.rstrip("?").replace(": ", " - ").replace(":", "-")
    for bad, good in zip('\\/<>?*|"', ("+", "+", "", "", "!", "-", "", "")):
        name = name.replace(bad, good)
    return re.sub(r"[- ._]+$", "", re.sub(r"([- ._])\1+", r"\1", name)).lstrip(" .")


def album_folder(artist: str, year: str, title: str) -> Path:
    """An album's folder in the library: "<artist>/<year> - <album>", or "<artist>/<album>" without a year. A name that
    cleans to nothing (such as "..") is "Unknown Artist" or "Unknown Album", so the album never lands above its folder."""
    return Path(clean(artist) or "Unknown Artist", clean(f"{year} - {title}" if year else title) or "Unknown Album")


def release_folder(release: dict) -> Path:
    """An edition's album folder: its first artist's MusicBrainz name, the release group's year (else its own)."""
    group = release.get("release-group") or {}
    return album_folder(release["artist-credit"][0]["artist"]["name"], (group.get("first-release-date") or release.get("date") or "")[:4], release["title"])


def track_file(disc: int, discs: int, track: int, tracks: int, title: str, suffix: str) -> str:
    """A track's file name, "07 - Title.flac"; on an album of several discs "1-07 - Title.flac", so one folder keeps
    the discs in order. Each number is as wide as the album's widest one, a track's at least two digits. suffix is
    without its dot, as formats.suffix gives it: 'flac'."""
    number = f"{track:0{max(2, len(str(tracks)))}}"
    return clean(f"{disc:0{len(str(discs))}}-{number} - {title}" if discs > 1 else f"{number} - {title}") + "." + suffix.lower()


def edition_file(release: dict, medium: dict, track: dict, suffix: str) -> str:
    """A track's file name in an edition, numbered as wide as the edition's discs and its longest disc."""
    tracks = max(t["position"] for m in release["media"] for t in m["tracks"])
    return track_file(medium["position"], len(release["media"]), track["position"], tracks, track["title"], suffix)
