import subprocess
from pathlib import Path

import mutagen
import pytest
from mutagen.flac import FLAC, Picture
from starlette.testclient import TestClient

from oasis.api import create_app
from oasis.config import Config
from oasis.db import connect
from oasis.scan import scan

PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                    "1f15c4890000000d49444154789c6360000000000200015e2a77e10000000049454e44ae426082")
NS = "{http://subsonic.org/restapi}"


def make_track(path: Path, **tags):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=duration=2", str(path)], check=True)
    audio = mutagen.File(path, easy=True)
    for key, value in tags.items():
        audio[key] = value
    audio.save()


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


def make_client(library, database, ytm=None, services=None):
    config = Config(library=[library], users={"admin": "secret"}, database=database)
    scan(connect(config.database), config.library)
    return TestClient(create_app(config, scan_on_start=False, ytm=ytm, services=services or {}))


@pytest.fixture
def client(library, tmp_path):
    with make_client(library, tmp_path / "oasis.db") as c:
        yield c


def call(client, method, fmt="json", **params):
    response = client.get(f"/rest/{method}.view", params={"u": "admin", "p": "secret", "f": fmt, **params})
    return response.json()["subsonic-response"] if fmt == "json" else response
