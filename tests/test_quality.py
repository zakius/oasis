import subprocess
import time

import pytest
from mutagen.flac import FLAC, Picture

from oasis import quality
from oasis.db import connect
from oasis.scan import scan
from test_accounts import sign_in
from conftest import make_client


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-nostdin", *map(str, args)], check=True)


def music(path, rate=44100, seconds=10, fmt="s16", *more):
    """Pink noise swelling and fading, as music's level does: content up to the file's Nyquist frequency."""
    path.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg("-f", "lavfi", "-i", f"anoisesrc=d={seconds}:c=pink:r={rate}:a=0.3,volume='0.6+0.4*sin(t)':eval=frame", "-ac", "2", *more,
           "-sample_fmt", fmt, "-c:a", "flac", path)
    return path


def padded(path):
    """24-bit FLAC of 16-bit samples."""
    source = music(path.with_name("16-bit.flac"))
    ffmpeg("-i", source, "-sample_fmt", "s32", "-c:a", "flac", path)
    source.unlink()
    return path


def tagged(path):
    audio = FLAC(path)
    audio["title"], audio["artist"] = "Zażółć", "Beta"
    picture = Picture()
    picture.data, picture.mime, picture.type = b"\x89PNG" + bytes(3000), "image/png", 3
    audio.add_picture(picture)
    audio.save()
    return path


def metadata(path):
    return quality.kept(quality.blocks(path)[0])


def lossy(path, tmp_path, bitrate="128k"):
    music(tmp_path / "source.flac")
    ffmpeg("-i", tmp_path / "source.flac", "-b:a", bitrate, tmp_path / "source.mp3")
    path.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg("-i", tmp_path / "source.mp3", "-sample_fmt", "s16", "-c:a", "flac", path)
    return path


def test_a_padded_24_bit_file_becomes_the_16_bit_file_it_is_sample_for_sample(tmp_path):
    path = tagged(padded(tmp_path / "music" / "padded.flac"))
    assert FLAC(path).info.bits_per_sample == 24
    samples, tags, mode = quality.pcm_hash(path), metadata(path), path.stat().st_mode
    found = quality.act(path, quality.examine(path), shared=False)
    assert (found["verdict"], found["action"], found["was"], found["bits"]) == ("padded", "converted", "24-bit 44.1 kHz", 16)
    assert FLAC(path).info.bits_per_sample == 16 and quality.pcm_hash(path) == samples
    assert metadata(path) == tags and path.stat().st_mode == mode
    assert [p.name for p in path.parent.iterdir()] == ["padded.flac"]  # no temporary files left


@pytest.mark.parametrize("source, rate, bits", [(44100, 44100, 16), (48000, 48000, 24)])
def test_an_upsampled_file_is_resampled_to_the_rate_its_spectrum_shows_keeping_its_tags(tmp_path, source, rate, bits):
    path = tagged(music(tmp_path / "up.flac", source, 10, "s32", "-af", "aresample=96000:resampler=soxr"))
    tags, seconds = metadata(path), FLAC(path).info.length
    found = quality.act(path, quality.examine(path), shared=False)
    assert (found["verdict"], found["source_rate"], found["action"], found["was"]) == ("upsampled", source, "resampled", "24-bit 96 kHz")
    info = FLAC(path).info
    assert (info.sample_rate, info.bits_per_sample) == (rate, bits) and abs(info.length - seconds) < 0.05 and metadata(path) == tags
    assert found["saved"] > 0


def test_a_flac_made_from_an_mp3_is_lossy_and_real_lossless_quiet_or_silent_files_are_not(tmp_path):
    made = quality.examine(lossy(tmp_path / "from mp3.flac", tmp_path))
    assert made["verdict"] == "lossy" and 15000 < made["cutoff"] < 17500 and made["sustained"]
    assert quality.act(tmp_path / "from mp3.flac", made, shared=False)["action"] is None  # purge deals with it
    real = quality.examine(music(tmp_path / "real.flac"))
    assert real["verdict"] is None and real["cutoff"] is None
    ffmpeg("-f", "lavfi", "-i", "anoisesrc=d=10:c=pink:r=44100:a=0.001,lowpass=f=16000", "-sample_fmt", "s16", "-c:a", "flac", tmp_path / "quiet.flac")
    ffmpeg("-f", "lavfi", "-i", "anullsrc=r=96000:cl=stereo", "-t", "5", "-sample_fmt", "s32", "-c:a", "flac", tmp_path / "silent.flac")
    for name in ("quiet.flac", "silent.flac"):
        assert quality.examine(tmp_path / name)["verdict"] is None


def test_an_outlier_is_recompressed_to_the_same_samples_even_without_an_md5_and_others_stay_as_they_are(tmp_path):
    path = tagged(music(tmp_path / "fast.flac", 44100, 10, "s16", "-compression_level", "0"))
    data = bytearray(path.read_bytes())
    data[8 + 18:8 + 34] = bytes(16)  # STREAMINFO's MD5, which some encoders leave out
    path.write_bytes(data)
    samples, tags, size = quality.pcm_hash(path), metadata(path), path.stat().st_size
    found = quality.act(path, quality.examine(path), shared=False)
    assert found["action"] == "recompressed" and found["saved"] == size - path.stat().st_size > size * quality.SMALLER
    assert quality.pcm_hash(path) == samples and metadata(path) == tags and any(FLAC(path).info.md5_signature.to_bytes(16, "big"))
    before = path.read_bytes()
    assert quality.act(path, quality.examine(path), shared=False)["action"] == "kept" and path.read_bytes() == before


def test_files_dedupe_shares_and_rewrites_failing_their_checks_stay_byte_for_byte(tmp_path, monkeypatch):
    path = padded(tmp_path / "padded.flac")
    before = path.read_bytes()
    assert quality.act(path, quality.examine(path), shared=True)["action"] == "shared" and path.read_bytes() == before

    def wrong(*args):
        raise quality.Unverified("the decoded samples differ")
    monkeypatch.setattr(quality, "check", wrong)
    assert quality.act(path, quality.examine(path), shared=False)["action"] == "failed"
    assert path.read_bytes() == before and [p.name for p in tmp_path.iterdir()] == ["padded.flac"]


def library_db(tmp_path):
    root = tmp_path / "music"
    lossy(root / "Beta/2001 - First/01 - One.flac", tmp_path)
    lossy(root / "Beta/2001 - First/02 - Two.flac", tmp_path)
    music(root / "Beta/2001 - First/03 - Three.flac")
    music(root / "Beta/2002 - Second/01 - Four.flac")
    lossy(root / "Beta/2002 - Second/02 - Five.flac", tmp_path)
    music(root / "Beta/2002 - Second/03 - Six.flac")
    lossy(root / "Gamma/Loose/01 - Seven.flac", tmp_path)
    padded(root / "Beta/2003 - Third/01 - Eight.flac")
    for path in root.rglob("*.flac"):
        audio = FLAC(path)
        audio["title"], audio["album"] = path.stem[5:], path.parent.name
        audio["albumartist"] = path.parts[-3]
        if path.parts[-3] == "Beta":
            audio["musicbrainz_releasegroupid"] = {"2001 - First": "rg-first", "2002 - Second": "rg-second", "2003 - Third": "rg-third"}[path.parent.name]
        audio.save()
    db = connect(tmp_path / "oasis.db")
    scan(db, [root])
    db.execute("insert into user(name, password, admin, created) values ('admin', 'x', 1, 0)")
    db.execute("insert into want_album(rg, artist_mbid, title, reason, state, next_search) values ('rg-second', 'mb-beta', 'Second', 'followed', 'have', ?)",
               (time.time() + 1e6,))
    return root, db


def test_the_backlog_acts_on_each_file_once_and_lists_lossy_sourced_ones_until_told_to_delete_them(tmp_path):
    root, db = library_db(tmp_path)
    first = quality.backlog(db, jobs=4)
    assert first["checked"] == 8 and all(p.exists() for p in root.rglob("*.flac"))
    assert {(e["album"], e["lossy"], e["tracks"], e["deleted"]) for e in first["purged"]} == {("2001 - First", 2, 3, 0), ("2002 - Second", 1, 3, 0),
                                                                                              ("Loose", 1, 1, 0)}
    assert db.execute("select action from audio_quality where path like '%Eight.flac'").fetchone()[0] == "converted"
    assert quality.backlog(db)["checked"] == 0  # resumes: nothing changed since

    purged = quality.backlog(db, delete_lossy=True)["purged"]
    assert {(e["album"], e["deleted"]) for e in purged} == {("2001 - First", 3), ("2002 - Second", 1), ("Loose", 0)}
    assert not (root / "Beta/2001 - First/03 - Three.flac").exists()  # mostly lossy-sourced: the whole album goes
    assert sorted(p.name for p in (root / "Beta/2002 - Second").iterdir()) == ["01 - Four.flac", "03 - Six.flac"]
    assert (root / "Gamma/Loose/01 - Seven.flac").exists()  # no release group: nothing to get instead
    assert db.execute("select state, next_search from want_album where rg = 'rg-second'").fetchone()[:] == ("missing", None)
    assert db.execute("select * from wanted").fetchall()[0][:] == ("admin", "album", "al-mb-rg-first")
    assert db.execute("select mbid, state from want_item").fetchone()[:] == ("rg-first", "wanted")


def test_album_and_song_pages_say_what_the_files_really_hold(tmp_path):
    root = tmp_path / "music"
    one, two = music(root / "Beta/First/01.flac"), music(root / "Beta/First/02.flac")
    for path, title in ((one, "One"), (two, "Two")):
        audio = FLAC(path)
        audio["title"], audio["album"], audio["artist"] = title, "First", "Beta"
        audio.save()
    with make_client(root, tmp_path / "oasis.db") as client:
        db = client.app.state.api._db()
        base = {"cutoff": 21700, "drop": 90.0, "sustained": True, "rate": 96000, "bits": 24, "bits_used": 24, "low_share": 0.9, "borderline": None}
        quality.store(db, one, base | {"verdict": "upsampled", "source_rate": 44100, "action": "shared"})
        quality.store(db, two, base | {"verdict": "upsampled", "source_rate": 44100, "action": "resampled", "was": "24-bit 96 kHz"})
        client.app.state.api.library = True  # the library pages are acquisition's
        sign_in(client)
        album_id, song_id = db.execute("select album_id, id from track where title = 'One'").fetchone()
        page = client.get(f"/music/library/album/{album_id}").text
        assert "upsampled from 44.1 kHz · 1 of 2" in page and "was fake 24-bit 96 kHz · 1 of 2" in page
        assert "upsampled from 44.1 kHz" in client.get(f"/music/library/song/{song_id}").text
