import subprocess

import mutagen
import pytest
from mutagen.flac import FLAC
from mutagen.id3 import TIT2

from oasis import pcm
from oasis.db import connect
from oasis.scan import scan
import test_importer
from conftest import edition

make, no_cover_art, root = test_importer.make, test_importer.no_cover_art, test_importer.root  # its fixtures


def wav(path, codec="pcm_s16le", seconds=2, *more):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"anoisesrc=d={seconds}:c=pink:r=44100:a=0.3", "-ac", "2",
                    *more, "-c:a", codec, *(["-f", "wav"] if path.suffix == ".wav" else []), str(path)], check=True)
    return path


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(pcm, "KEPT", set())


@pytest.mark.parametrize("codec, suffix, bits", [("pcm_s16le", ".wav", 16), ("pcm_s24le", ".wav", 24), ("pcm_u8", ".wav", 16),
                                                 ("pcm_s16be", ".aiff", 16), ("pcm_s24be", ".aif", 24)])
def test_integer_pcm_becomes_flac_of_the_same_samples_rate_and_depth_with_its_tags(tmp_path, codec, suffix, bits):
    source = wav(tmp_path / f"01 One{suffix}", codec, 2, "-ar", "96000", "-metadata", "title=Jedynka", "-metadata", "artist=Stachursky",
                 *(["-write_id3v2", "1"] if "aif" in suffix else []))
    want = pcm.samples(source)
    new = pcm.convert(source)
    assert new == tmp_path / "01 One.flac" and not source.exists() and pcm.samples(new) == want
    info = FLAC(new)
    assert (info.info.sample_rate, info.info.bits_per_sample) == (96000, bits)
    assert info["title"] == ["Jedynka"] and info["artist"] == ["Stachursky"]


def test_id3_tags_in_a_wav_carry_over(tmp_path):
    source = wav(tmp_path / "a.wav")
    tagged = mutagen.File(source)
    tagged.add_tags()
    tagged.tags.add(TIT2(encoding=3, text="Ósemka"))
    tagged.save()
    assert FLAC(pcm.convert(source))["title"] == ["Ósemka"]


@pytest.mark.parametrize("codec", ["pcm_f32le", "ac3"])
def test_float_pcm_and_surround_formats_stay_as_they_are(tmp_path, codec):
    source = wav(tmp_path / "a.wav", codec)
    assert pcm.convert(source) is None and source.exists() and not (tmp_path / "a.flac").exists()
    assert str(source) in pcm.KEPT


def test_mp3_in_a_wav_is_remuxed_to_mp3_without_reencoding(tmp_path):
    source = wav(tmp_path / "a.wav", "libmp3lame")
    frames = subprocess.run(["ffmpeg", "-v", "error", "-i", str(source), "-map", "0:a", "-c", "copy", "-f", "hash", "-"], capture_output=True, text=True).stdout
    new = pcm.convert(source)
    assert new == tmp_path / "a.mp3" and not source.exists() and pcm.codec(new) == "mp3"
    assert subprocess.run(["ffmpeg", "-v", "error", "-i", str(new), "-map", "0:a", "-c", "copy", "-f", "hash", "-"], capture_output=True, text=True).stdout == frames


def test_a_flac_that_decodes_otherwise_keeps_the_wav(tmp_path, monkeypatch, caplog):
    source = wav(tmp_path / "a.wav")
    real = pcm.samples
    monkeypatch.setattr(pcm, "samples", lambda p: "other" if p.name.endswith("oasis-part") else real(p))
    assert pcm.convert(source) is None and source.exists() and not list(tmp_path.glob("*.flac")) and not list(tmp_path.glob(".*"))
    assert "decodes to other samples" in caplog.text


def test_an_existing_flac_of_that_name_is_never_overwritten(tmp_path):
    source = wav(tmp_path / "a.wav")
    (tmp_path / "a.flac").write_bytes(b"mine")
    assert pcm.convert(source) is None and source.exists() and (tmp_path / "a.flac").read_bytes() == b"mine"


def test_a_wav_download_imports_as_tagged_flac(make, tmp_path, root):
    wav(tmp_path / "dl/01 One.wav", "pcm_s24le", 2)
    album = make(edition("standard", [("One", 2)])).place(tmp_path / "dl", "rg-1")
    assert sorted(p.name for p in album.iterdir()) == ["01 - One.flac"]
    tags = FLAC(album / "01 - One.flac")
    assert tags.info.bits_per_sample == 24 and tags["musicbrainz_albumid"] == ["standard"] and tags["album"] == ["First"]


def test_the_scan_makes_a_library_wav_flac_and_the_track_keeps_its_id(tmp_path):
    library = tmp_path / "music"
    source = wav(library / "Beta/2001 - First/01 - One.wav", "pcm_s16le", 2, "-metadata", "title=One")
    db = connect(tmp_path / "oasis.db")
    pcm.KEPT.add(str(source))  # the first scan sees it as it was
    scan(db, [library])
    (id, suffix), = [tuple(r) for r in db.execute("select id, suffix from track")]
    assert suffix == "wav"
    pcm.KEPT.clear()
    scan(db, [library])
    assert [tuple(r) for r in db.execute("select id, suffix, ref from track")] == [(id, "flac", str(library / "Beta/2001 - First/01 - One.flac"))]
    assert not source.exists()
