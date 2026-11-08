import errno
import fcntl
import shutil
import subprocess
import tempfile
from pathlib import Path

import mutagen
import numpy as np
import pytest

from oasis import audio, audiorecords, dedupe, files
from oasis.db import connect
from oasis.scan import scan


def reflinks_work(directory: Path) -> bool:
    a, b = directory / "a", directory / "b"
    a.write_bytes(b"x" * 4096)
    try:
        with open(a, "rb") as source, open(b, "wb") as copy:
            fcntl.ioctl(copy.fileno(), files.FICLONE, source.fileno())
        return True
    except OSError as e:
        if e.errno in (errno.EOPNOTSUPP, errno.EXDEV, errno.ENOTTY, errno.EINVAL):
            return False
        raise
    finally:
        a.unlink(missing_ok=True)
        b.unlink(missing_ok=True)


@pytest.fixture
def root():
    """A library on a filesystem with reflinks: the home directory, since /tmp is often tmpfs."""
    base = Path.home() / ".cache"
    base.mkdir(exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="oasis-dedupe-", dir=base))
    if not reflinks_work(directory):
        shutil.rmtree(directory)
        pytest.skip("no reflinks on the home filesystem")
    yield directory
    shutil.rmtree(directory)


def master(path: Path, seed=1, effect=None):
    """20 s of 'music': pink noise under two tones, the same samples for the same seed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    source = f"anoisesrc=d=20:c=pink:seed={seed}:a=0.3,volume=enable='lt(mod(t,2),1)':volume=0.4"
    chain = ["-f", "lavfi", "-i", source, "-f", "lavfi", "-i", "sine=f=220:d=20", "-filter_complex",
             "[0][1]amix=inputs=2" + (f",{effect}" if effect else "") + ",aformat=sample_fmts=s16:sample_rates=44100:channel_layouts=stereo"]
    subprocess.run(["ffmpeg", "-v", "error", "-y", *chain, str(path)], check=True)


def encode(source: Path, path: Path, *args):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(source), *args, str(path)], check=True)


def tag(path: Path, **tags):
    audio = mutagen.File(path, easy=True)
    for key, value in tags.items():
        audio[key] = value
    audio.save()


def pcm(path: Path) -> np.ndarray:
    return audio.decode(str(path))


def library(root: Path):
    db = connect(root / "oasis.db")
    scan(db, [root / "music"])
    return db


def test_identical_audio_keeps_the_smallest_flac_and_every_editions_tags(root):
    wav = root / "src.wav"
    master(wav)
    big, small = root / "music/Type A/01 Song.flac", root / "music/Type B/01 Song.flac"
    encode(wav, big, "-compression_level", "0")
    encode(wav, small, "-compression_level", "12")
    tag(big, title="Song", artist="AKB", albumartist="AKB", album="Single (Type A)", tracknumber="1")
    tag(small, title="Song", artist="AKB", albumartist="AKB", album="Single (Type B)", tracknumber="1")
    assert big.stat().st_size > small.stat().st_size
    before = pcm(big)
    db = library(root)

    assert dedupe.run(db) == [(str(big), str(big))]
    assert np.array_equal(pcm(big), before) and np.array_equal(pcm(small), before)
    assert mutagen.File(big, easy=True)["album"] == ["Single (Type A)"]
    assert mutagen.File(small, easy=True)["album"] == ["Single (Type B)"]
    assert abs(big.stat().st_size - small.stat().st_size) < 1 << 17  # a clone of the small one, plus its own tags
    scan(db, [root / "music"])
    assert dedupe.run(db) == []  # nothing left to do, and nothing compared again


def test_a_lossy_copy_of_the_same_master_becomes_the_lossless_one_with_its_tags(root):
    wav = root / "src.wav"
    master(wav)
    flac, mp3 = root / "music/Album/01 Song.flac", root / "music/Single/01 Song.mp3"
    encode(wav, flac)
    encode(wav, mp3, "-b:a", "320k")
    tag(flac, title="Song", artist="AKB", album="Album")
    tag(mp3, title="Song (Remastered)", artist="AKB", album="Single", tracknumber="1/3")
    db = library(root)

    new = mp3.with_suffix(".flac")
    assert dedupe.run(db) == [(str(mp3), str(new))]
    assert not mp3.exists() and np.array_equal(pcm(new), pcm(flac))
    tags = mutagen.File(new, easy=True)
    assert (tags["album"], tags["title"], tags["tracknumber"]) == (["Single"], ["Song (Remastered)"], ["1/3"])


def test_offset_rips_merge_into_the_most_complete_one(root):
    wav = root / "src.wav"
    master(wav)
    full, trimmed = root / "music/A/01 Song.flac", root / "music/B/01 Song.flac"
    encode(wav, full)
    encode(wav, trimmed, "-af", "atrim=start_sample=588")
    for path, album in ((full, "A"), (trimmed, "B")):
        tag(path, title="Song", artist="X", album=album)
    db = library(root)

    assert dedupe.run(db) == [(str(trimmed), str(trimmed))]
    assert np.array_equal(pcm(trimmed), pcm(full)) and mutagen.File(trimmed, easy=True)["album"] == ["B"]


def test_remasters_rerecordings_and_tagged_versions_stay_apart(root):
    wav, remaster, other, = root / "src.wav", root / "remaster.wav", root / "other.wav"
    master(wav)
    master(remaster, effect="equalizer=f=1000:t=q:w=2:g=1.5")
    master(other, seed=2)
    files = {"Album/01 Song.flac": (wav, "Album", "Song"), "Remaster/01 Song.flac": (remaster, "Remaster", "Song"),
             "Aurora/01 Song.flac": (other, "Aurora", "Song"), "Live in Tokyo/01 Song.flac": (wav, "Live in Tokyo", "Song"),
             "Single/02 Song.flac": (wav, "Single", "Song (Demo)")}
    for name, (source, album, title) in files.items():
        encode(source, root / "music" / name, "-compression_level", "0" if "Live" in name or "Single" in name else "8")
        tag(root / "music" / name, title=title, artist="X", album=album)
    db = library(root)

    assert dedupe.run(db) == []


def test_a_file_missing_for_a_scan_comes_back_with_its_records(tmp_path):
    one, two = tmp_path / "music/A/01 Song.flac", tmp_path / "music/B/01 Song.flac"
    for path in (one, two):
        path.parent.mkdir(parents=True)
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=1", str(path)], check=True)
    db = library(tmp_path)
    db.execute("insert into audio_pair values (?, ?, 1, 1, 1)", (str(one), str(two)))
    db.execute("insert into audio_shared values (?, ?, 1, 1)", (str(two), str(one)))
    db.execute("insert into audio_quality(path, mtime, checked) values (?, 1, 1)", (str(one),))
    records = lambda: (audiorecords.same_audio(db, str(two)), audiorecords.shared_paths(db), len(audiorecords.findings(db, [str(one)])))
    assert records() == ([str(one)], {str(one), str(two)}, 1)
    kept = tmp_path / "kept.flac"
    one.rename(kept)  # its folder unmounted, say
    scan(db, [tmp_path / "music"])
    assert not db.execute("select 1 from track where ref = ?", (str(one),)).fetchone()
    kept.rename(one)
    scan(db, [tmp_path / "music"])
    assert db.execute("select 1 from track where ref = ?", (str(one),)).fetchone()
    assert records() == ([str(one)], {str(one), str(two)}, 1)
