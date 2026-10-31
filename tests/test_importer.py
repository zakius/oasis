import io
import json
import subprocess
import threading
import time
import urllib.error

import mutagen
import pytest

from oasis import credits, importer, musicbrainz
from oasis.db import artist_id, connect
from oasis.importer import Importer
from oasis.scan import read


def audio(path, seconds, **tags):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=duration={seconds}", str(path)], check=True)
    if tags:
        file = mutagen.File(path, easy=True)
        if file.tags is None:
            file.add_tags()
        for key, value in tags.items():
            file[key] = value
        file.save()
    return path


def edition(id, *media, title="First", status="Official", fmt="CD"):
    """media: per disc, (title, seconds) per track."""
    return {"id": id, "title": title, "status": status, "country": "XW", "date": "2001-02-03", "barcode": "123",
            "artist-credit": [{"name": "Beta", "joinphrase": "", "artist": {"id": "mb-beta", "name": "Beta"}}],
            "label-info": [{"catalog-number": "CAT-1", "label": {"name": "Label"}}],
            "release-group": {"id": "rg-1", "title": "First", "primary-type": "Album", "secondary-types": ["Live"], "first-release-date": "2001"},
            "media": [{"position": d + 1, "format": fmt, "track-count": len(tracks), "tracks": [
                {"id": f"{id}-t{d + 1}{n + 1}", "position": n + 1, "title": title, "length": seconds * 1000,
                 "recording": {"id": f"rec-{title}", "length": seconds * 1000, "isrcs": [f"ISRC{n}"]}}
                for n, (title, seconds) in enumerate(tracks)]} for d, tracks in enumerate(media)]}


@pytest.fixture(autouse=True)
def no_cover_art(monkeypatch):
    monkeypatch.setattr(importer, "COVER_ART", "http://127.0.0.1:9/{}")


@pytest.fixture
def root(tmp_path):
    return tmp_path / "music"


@pytest.fixture
def make(tmp_path, root, monkeypatch):
    def make(*releases):
        monkeypatch.setattr(importer, "editions", lambda db, group: list(releases))
        return Importer(lambda: connect(tmp_path / "oasis.db"), root)
    return make


def test_the_edition_the_files_cover_is_picked_and_placed_with_its_tags(make, tmp_path, root):
    staging = tmp_path / "dl"
    audio(staging / "Beta - First/01. Beta - One.flac", 2, title="One", tracknumber="1")
    audio(staging / "Beta - First/02 Two.flac", 3, title="Two")
    audio(staging / "Beta - First/03 Three [bonus].flac", 4)
    audio(staging / "Beta - First/extra.flac", 9, title="Not on any edition")
    (staging / "Beta - First/folder.jpg").write_bytes(b"jpg")
    single = edition("single", [("One", 2)])
    standard = edition("standard", [("One", 2), ("Two", 3), ("Three", 4)])
    deluxe = edition("deluxe", [("One", 2), ("Two", 3), ("Three", 4), ("Four", 5), ("Five", 6)])
    translated = edition("pseudo", [("One", 2), ("Two", 3), ("Three", 4), ("Not on any edition", 9)], status="Pseudo-Release")
    unknown = edition("unknown", [("One", 0), ("Two", 0), ("Three", 0)])  # MusicBrainz has no lengths
    album = make(unknown, single, deluxe, standard, translated).place(staging, "rg-1")
    assert album == root / "Beta/First (2001)"
    assert sorted(p.name for p in album.iterdir()) == ["01 - One.flac", "02 - Two.flac", "03 - Three.flac", "cover.jpg"]

    row = read(album / "03 - Three.flac", root, {})
    assert (row["title"], row["artist"], row["album_artist"], row["album"], row["track_no"], row["disc_no"], row["year"]) == \
           ("Three", "Beta", "Beta", "First", 3, 1, 2001)
    assert (row["mb_release"], row["mb_track"], row["mb_recording"], row["mb_release_group"]) == ("standard", "standard-t13", "rec-Three", "rg-1")
    assert (row["isrc"], row["label"], row["release_types"], row["original_date"], row["_albumartist_mbids"]) == \
           ("ISRC2", "Label", "album; live", "2001", ["mb-beta"])
    tags = mutagen.File(album / "01 - One.flac")
    assert tags["totaltracks"] == ["3"] and tags["catalognumber"] == ["CAT-1"] and tags["releasestatus"] == ["official"]


def test_an_edition_whose_lengths_differ_is_rejected(make, tmp_path):
    audio(tmp_path / "dl/01 One.flac", 2, title="One")
    with pytest.raises(LookupError, match="no edition matches"):
        make(edition("long", [("One", 60)])).place(tmp_path / "dl", "rg-1")


def test_untitled_files_are_matched_by_their_numbers_into_an_existing_artist_folder(make, tmp_path, root):
    audio(tmp_path / "dl/01.flac", 2)
    audio(tmp_path / "dl/02.flac", 3)
    audio(root / "beta (jp)/Old (1999)/01 - Old.flac", 2)
    db = connect(tmp_path / "oasis.db")
    db.execute("insert into track(id, source, ref, artist_id) values ('t', 'local', ?, ?)",
               (str(root / "beta (jp)/Old (1999)/01 - Old.flac"), artist_id(db, "Beta", "mb-beta")))
    album = make(edition("jp", [("いち", 2), ("に: 二", 3)])).place(tmp_path / "dl", "rg-1")
    assert album == root / "beta (jp)/First (2001)"
    assert sorted(p.name for p in album.glob("*.flac")) == ["01 - いち.flac", "02 - に - 二.flac"]


def test_a_multi_disc_edition_gets_a_folder_per_disc(make, tmp_path, root):
    audio(tmp_path / "dl/CD1/01 One.flac", 2, title="One", discnumber="1", tracknumber="1")
    audio(tmp_path / "dl/CD2/01 One.flac", 3, title="One", discnumber="2", tracknumber="1")
    album = make(edition("two", [("One", 2)], [("One", 3)])).place(tmp_path / "dl", "rg-1")
    assert sorted(str(p.relative_to(album)) for p in album.rglob("*.flac")) == ["CD 01/01 - One.flac", "CD 02/01 - One.flac"]
    assert mutagen.File(album / "CD 02/01 - One.flac")["discnumber"] == ["2"]


def test_library_files_are_never_overwritten_and_lossy_ones_move_aside_for_lossless(make, tmp_path, root):
    album = root / "Beta/First (2001)"
    kept = audio(album / "01 - One.flac", 2)
    lossy = audio(album / "02 - Two.mp3", 3)
    before, lossy_bytes = kept.read_bytes(), lossy.read_bytes()
    audio(tmp_path / "dl/01 One.flac", 2, title="One")
    audio(tmp_path / "dl/02 Two.flac", 3, title="Two")
    make(edition("standard", [("One", 2), ("Two", 3)])).place(tmp_path / "dl", "rg-1")
    assert kept.read_bytes() == before
    assert not lossy.exists() and (root / ".replaced/Beta/First (2001)/02 - Two.mp3").read_bytes() == lossy_bytes
    assert mutagen.File(album / "02 - Two.flac")["musicbrainz_albumid"] == ["standard"]
    # A lossy download doesn't replace anything either.
    audio(tmp_path / "dl2/01 One.mp3", 2, title="One")
    make(edition("single", [("One", 2)])).place(tmp_path / "dl2", "rg-1")
    assert sorted(p.name for p in album.glob("01 *")) == ["01 - One.flac"]


@pytest.mark.parametrize("suffix", ["mp3", "m4a", "ogg"])
def test_other_formats_carry_the_same_tags_for_the_scan(make, tmp_path, root, suffix):
    audio(tmp_path / f"dl/01 One.{suffix}", 2, title="One")
    album = make(edition("standard", [("One", 2)])).place(tmp_path / "dl", "rg-1")
    row = read(album / f"01 - One.{suffix}", root, {})
    assert (row["mb_release"], row["mb_track"], row["mb_recording"], row["mb_release_group"], row["track_no"]) == \
           ("standard", "standard-t11", "rec-One", "rg-1", 1)
    assert (row["album_artist"], row["isrc"], row["label"], row["release_types"], row["original_date"], row["_albumartist_mbids"]) == \
           ("Beta", "ISRC0", "Label", "album; live", "2001", ["mb-beta"])


def test_credits_and_imports_share_one_musicbrainz_pace(tmp_path, monkeypatch):
    sent = []

    def urlopen(request, timeout):
        sent.append(time.monotonic())
        assert request.get_header("User-agent") == musicbrainz.USER_AGENT
        if len(sent) == 2:  # busy once: everyone backs off
            raise urllib.error.HTTPError(request.full_url, 503, "busy", {}, None)
        return io.BytesIO(json.dumps({"id": "x", "releases": [], "release-count": 0}).encode())

    monkeypatch.setattr(musicbrainz.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(musicbrainz, "BACKOFF", (2,))
    db = connect(tmp_path / "oasis.db")
    workers = [threading.Thread(target=lambda: [credits.fetch_release(f"r{i}") for i in range(3)]),
               threading.Thread(target=lambda: [importer.editions(connect(tmp_path / "oasis.db"), f"g{i}") for i in range(2)])]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    gaps = [b - a for a, b in zip(sent, sent[1:])]
    assert len(sent) == 6 and min(gaps) >= 1.0 and max(gaps) >= 2
    importer.editions(db, "g0")  # cached
    assert len(sent) == 6
