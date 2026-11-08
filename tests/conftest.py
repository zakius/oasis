import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import mutagen
import pytest
from mutagen.flac import FLAC, Picture
from starlette.testclient import TestClient

from oasis import download
from oasis.server import create_app
from oasis.config import Config, TorBox as TorBoxSettings
from oasis.db import connect
from oasis.download import Downloads
from oasis.scan import scan

PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                    "1f15c4890000000d49444154789c6360000000000200015e2a77e10000000049454e44ae426082")
NS = "{http://subsonic.org/restapi}"


def make_track(path: Path, seconds=2, **tags) -> Path:
    """A sine tone of that length, in the format path's suffix names, with tags."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=duration={seconds}", str(path)], check=True)
    if tags:
        audio = mutagen.File(path, easy=True)
        if audio.tags is None:
            audio.add_tags()
        for key, value in tags.items():
            audio[key] = value
        audio.save()
    return path


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-nostdin", *map(str, args)], check=True)


def music(path, rate=44100, seconds=10, fmt="s16", *more):
    """Pink noise swelling and fading, as music's level does: content up to the file's Nyquist frequency."""
    path.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg("-f", "lavfi", "-i", f"anoisesrc=d={seconds}:c=pink:r={rate}:a=0.3,volume='0.6+0.4*sin(t)':eval=frame", "-ac", "2", *more,
           "-sample_fmt", fmt, "-c:a", "flac", path)
    return path


def lossy(path, work, bitrate="128k"):
    """A FLAC made from an MP3: work is a folder for the files it is made from."""
    music(work / "source.flac")
    ffmpeg("-i", work / "source.flac", "-b:a", bitrate, work / "source.mp3")
    path.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg("-i", work / "source.mp3", "-sample_fmt", "s16", "-c:a", "flac", path)
    return path


def edition(id, *media, title="First", status="Official", fmt="CD"):
    """A MusicBrainz release of Beta's release group rg-1. media: per disc, (title, seconds) per track."""
    return {"id": id, "title": title, "status": status, "country": "XW", "date": "2001-02-03", "barcode": "123",
            "artist-credit": [{"name": "Beta", "joinphrase": "", "artist": {"id": "mb-beta", "name": "Beta"}}],
            "label-info": [{"catalog-number": "CAT-1", "label": {"name": "Label"}}],
            "release-group": {"id": "rg-1", "title": "First", "primary-type": "Album", "secondary-types": ["Live"], "first-release-date": "2001"},
            "media": [{"position": d + 1, "format": fmt, "track-count": len(tracks), "tracks": [
                {"id": f"{id}-t{d + 1}{n + 1}", "position": n + 1, "title": title, "length": seconds * 1000,
                 "recording": {"id": f"rec-{title}", "length": seconds * 1000, "isrcs": [f"ISRC{n}"]}}
                for n, (title, seconds) in enumerate(tracks)]} for d, tracks in enumerate(media)]}


@pytest.fixture(scope="session")
def library(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("music")
    make_track(root / "Beta/First/01.flac", title="One", artist="Beta", album="First", tracknumber="1", date="2001", genre="Rock")
    make_track(root / "Beta/First/02.flac", title="Two", artist="Beta", album="First", tracknumber="2", date="2001", genre="Rock")
    make_track(root / "The Alpha/Second/b.mp3", title="Song B", artist="The Alpha", album="Second", tracknumber="2/9", date="1999-05-01")
    make_track(root / "The Alpha/Second/a.mp3", title="Song A", artist="The Alpha", album="Second", tracknumber="1/9", date="1999")
    make_track(root / "Loose/untagged.ogg")
    flac = FLAC(root / "Beta/First/01.flac")
    picture = Picture()
    picture.data, picture.mime, picture.type = PNG, "image/png", 3
    flac.add_picture(picture)
    flac.save()
    (root / "The Alpha/Second/cover.png").write_bytes(PNG)
    return root


@pytest.fixture(autouse=True)
def offline_lyrics(monkeypatch):
    """Tests never ask the real lyrics providers."""
    from oasis import lyrics
    monkeypatch.setattr(lyrics, "PROVIDERS", {})


def make_client(library, database, ytm=None, services=None, **settings):
    """settings: more of the Config."""
    config = Config(library=[library], users={"admin": "secret"}, database=database, **settings)
    scan(connect(config.database), config.library)
    return TestClient(create_app(config, background=False, ytm=ytm, services=services or {}))


@pytest.fixture
def client(library, tmp_path):
    with make_client(library, tmp_path / "oasis.db") as c:
        yield c


def call(client, method, fmt="json", **params):
    response = client.get(f"/rest/{method}.view", params={"u": "admin", "p": "secret", "f": fmt, **params})
    return response.json()["subsonic-response"] if fmt == "json" else response


def sign_in(client, name="admin", password="secret"):
    response = client.post("/login", data={"name": name, "password": password}, follow_redirects=False)
    assert response.status_code == 303
    return response


TORBOX_NOW = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)  # the fake TorBox's present, for the doctor's clock


def ago(**delta) -> str:
    return (TORBOX_NOW - timedelta(**delta)).isoformat()


class FakeTorBox:
    def __init__(self):
        self.waiting, self.list, self.have, self.deleted, self.dropped = [], [], set(), [], []

    def queued(self):
        return self.waiting

    def delete_queued(self, queued_id):
        self.dropped.append(queued_id)

    def torrents(self):
        return self.list

    def cached(self, hashes):
        return self.have & set(hashes)

    def delete_torrent(self, torrent_id):
        self.deleted.append(torrent_id)


def torrent(id, hash, progress=0.5, seeds=3, created=None, active=True):
    return {"id": id, "hash": hash, "name": f"torrent {id}", "progress": progress, "seeds": seeds, "active": active,
            "created_at": created or ago(hours=3)}


class TorBox(FakeTorBox):
    """Takes adds, or queues them when full, and serves each file's link from tmp_path/cdn."""
    def __init__(self, tmp_path):
        super().__init__()
        self.tmp, self.added, self.full, self.links = tmp_path, [], False, []
        self.seeded = []  # each add's mode

    def add(self, name, magnet=None, torrent=None, mode="off"):
        self.added.append((name, magnet, torrent))
        self.seeded.append(mode)
        hash = magnet.split("btih:")[1][:4] if magnet else "file"
        if self.full:
            self.waiting.append({"id": 90, "hash": hash})
            return {"queued_id": 90, "hash": hash}
        return {"torrent_id": 70 + len(self.added), "hash": hash.upper()}

    def link(self, torrent_id, file_id):
        self.links.append(file_id)
        return (self.tmp / "cdn" / str(file_id)).as_uri()


def tb(id, hash, peers=1, availability=1.0, speed=0, size=0, **extra):
    return {"download_state": "downloading", "download_finished": False, "download_present": False, "peers": peers,
            "availability": availability, "download_speed": speed, "size": size, **torrent(id, hash, **extra)}


def result(title, hash="", seeders=5, size=400e6, **extra):
    return {"title": title, "infoHash": hash, "seeders": seeders, "size": size, "guid": f"guid-{title}", "indexerId": 1, **extra}


class Prowlarr:
    names = {}   # indexer id -> name
    logins = {}  # the same, of the login trackers

    def __init__(self, results):
        self.results, self.asked = results, []

    def tracker(self, release):
        return self.names.get(release.get("indexerId"))

    def search(self, query):
        self.asked.append(query)
        return self.results.get(query, [])


class Importer:
    """importer.py's place: records what it got, and fails for the release groups in failing."""
    def __init__(self):
        self.placed, self.failing = [], set()

    def place(self, folder, group):
        self.placed.append((folder, group))
        if group in self.failing:
            raise LookupError("no edition matches")


WANT = {"artist": "Artist", "names": ["Artist"], "title": "Album", "year": "", "others": [], "discs": 1, "upgrade": False, "native": [], "other_titles": []}


def files(tmp_path, *names):
    (tmp_path / "cdn").mkdir(exist_ok=True)
    for id, name in enumerate(names):
        (tmp_path / "cdn" / str(id)).write_bytes(name.encode() * (id + 1))
    return [{"id": id, "name": name, "size": len(name.encode()) * (id + 1)} for id, name in enumerate(names)]


def finished(id, hash, names, tmp_path):
    return {**tb(id, hash, progress=1, active=False), "download_state": "completed", "download_finished": True, "download_present": True,
            "files": files(tmp_path, *names)}


@pytest.fixture
def db(tmp_path):
    return connect(tmp_path / "oasis.db")


@pytest.fixture
def torbox(tmp_path):
    return TorBox(tmp_path)


@pytest.fixture
def downloads(tmp_path, torbox, monkeypatch):
    monkeypatch.setattr(download, "PACE", 0)
    made = Downloads(lambda: connect(tmp_path / "oasis.db"), lambda: torbox, Importer(), Prowlarr({}), TorBoxSettings("", stall_hours=1, downloads=str(tmp_path / "dl")),
                     imported=lambda: made.scans.append(1))
    made.scans = []
    return made
