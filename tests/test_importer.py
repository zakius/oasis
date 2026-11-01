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
from oasis.quality import LossySource
from oasis.scan import read
from test_quality import lossy, music


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
    assert album == root / "Beta/2001 - First"
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


def test_untitled_files_are_matched_by_their_numbers_and_named_as_musicbrainz_names_the_edition(make, tmp_path, root):
    audio(tmp_path / "dl/01.flac", 2)
    audio(tmp_path / "dl/02.flac", 3)
    audio(root / "beta (jp)/Old (1999)/01 - Old.flac", 2)  # another spelling of the artist's folder doesn't count
    db = connect(tmp_path / "oasis.db")
    db.execute("insert into track(id, source, ref, artist_id) values ('t', 'local', ?, ?)",
               (str(root / "beta (jp)/Old (1999)/01 - Old.flac"), artist_id(db, "Beta", "mb-beta")))
    jp = edition("jp", [("いち", 2), ("に: 二", 3)], title="Fürst?")  # the edition's title, the release group's year
    jp["artist-credit"] = [{"name": "ベータ", "joinphrase": " & ", "artist": {"id": "mb-beta", "name": "Beta"}},
                           {"name": "Gamma", "joinphrase": "", "artist": {"id": "mb-gamma", "name": "Gamma"}}]
    album = make(jp).place(tmp_path / "dl", "rg-1")
    assert album == root / "Beta/2001 - Fürst"
    assert sorted(p.name for p in album.glob("*.flac")) == ["01 - いち.flac", "02 - に - 二.flac"]


def test_a_multi_disc_edition_keeps_one_folder_with_the_disc_before_each_number(make, tmp_path, root):
    audio(tmp_path / "dl/CD1/01 One.flac", 2, title="One", discnumber="1", tracknumber="1")
    audio(tmp_path / "dl/CD2/01 One.flac", 3, title="One", discnumber="2", tracknumber="1")
    album = make(edition("two", [("One", 2)], [("One", 3)])).place(tmp_path / "dl", "rg-1")
    assert sorted(str(p.relative_to(album)) for p in album.rglob("*.flac")) == ["1-01 - One.flac", "2-01 - One.flac"]
    assert mutagen.File(album / "2-01 - One.flac")["discnumber"] == ["2"]


def test_numbers_are_as_wide_as_the_albums_widest():
    assert importer.track_file(1, 1, 7, 9, "A/B: C?", ".FLAC") == "07 - A+B - C.flac"
    assert importer.track_file(2, 12, 7, 120, "T", ".mp3") == "02-007 - T.mp3"
    assert importer.album_folder("O.N.A.", "1995", "Modlishka") == importer.Path("O.N.A/1995 - Modlishka")


def test_library_files_are_never_overwritten_and_lossy_ones_give_way_to_lossless(make, tmp_path, root):
    album = root / "Beta/2001 - First"
    kept = audio(album / "01 - One.flac", 2)
    lossy = audio(album / "02 - Two.mp3", 3)
    before = kept.read_bytes()
    audio(tmp_path / "dl/01 One.flac", 2, title="One")
    audio(tmp_path / "dl/02 Two.flac", 3, title="Two")
    make(edition("standard", [("One", 2), ("Two", 3)])).place(tmp_path / "dl", "rg-1")
    assert kept.read_bytes() == before
    assert not lossy.exists() and sorted(p.name for p in album.iterdir()) == ["01 - One.flac", "02 - Two.flac"]
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


def sheet(path, image, *titles, encoding="utf-8", starts=(0, 2, 5)):
    lines = ['REM GENRE Pop', 'PERFORMER "Beta"', 'TITLE "First"', f'FILE "{image}" WAVE']
    for n, (title, start) in enumerate(zip(titles, starts), 1):
        lines += [f"  TRACK {n:02} AUDIO", f'    TITLE "{title}"', '    PERFORMER "Beta"', f"    INDEX 01 00:{start:02}:00"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes("\r\n".join(lines).encode(encoding))


def test_a_disc_image_is_split_by_its_cue_sheet_into_lossless_tracks(make, tmp_path, root):
    image = tmp_path / "dl/Beta - First/Beta - First.flac"
    image.parent.mkdir(parents=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=duration=9", "-ar", "96000", "-c:a", "flac", "-sample_fmt", "s32",
                    "-metadata", "title=First", str(image)], check=True)
    # Shift-JIS, naming the .wav it was ripped to
    sheet(image.with_suffix(".cue"), "Beta - First.wav", "いち", "に", "さん", encoding="cp932")
    album = make(edition("standard", [("いち", 2), ("に", 3), ("さん", 4)])).place(tmp_path / "dl", "rg-1")
    assert sorted(p.name for p in album.iterdir()) == ["01 - いち.flac", "02 - に.flac", "03 - さん.flac"]
    three = mutagen.File(album / "03 - さん.flac")
    assert (three.info.bits_per_sample, three.info.sample_rate, round(three.info.length, 2)) == (24, 96000, 4.0)
    assert three["title"] == ["さん"] and three["musicbrainz_albumid"] == ["standard"]
    assert image.exists()  # the download's files stay as they came


def test_disc_images_of_a_multi_disc_lossy_album_are_cut_as_they_are(make, tmp_path, root):
    for disc in (1, 2):
        audio(tmp_path / f"dl/CD{disc}/image.mp3", 5)
    sheet(tmp_path / "dl/CD1/image.cue", "image.mp3", "Один", "Два", encoding="cp1251", starts=(0, 2))
    sheet(tmp_path / "dl/CD2/image.cue", "image.mp3", "Три", "Четыре", encoding="utf-8-sig", starts=(0, 3))
    album = make(edition("two", [("Один", 2), ("Два", 3)], [("Три", 3), ("Четыре", 2)])).place(tmp_path / "dl", "rg-1")
    assert sorted(str(p.relative_to(album)) for p in album.rglob("*.mp3")) == \
           ["1-01 - Один.mp3", "1-02 - Два.mp3", "2-01 - Три.mp3", "2-02 - Четыре.mp3"]


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16", "cp932", "cp1251"])
def test_cue_sheets_are_read_in_their_encoding(tmp_path, encoding):
    titles = {"cp932": ["アンサンブル・プレイ", "日本語"], "cp1251": ["Кино", "Группа крови"]}.get(encoding, ["Ünïcode", "日本語"])
    sheet(tmp_path / "a.cue", "a.flac", *titles, encoding=encoding)
    assert [(t["number"], t["title"], t["start"]) for _, tracks in importer.cue(tmp_path / "a.cue") for t in tracks] == \
           [(1, titles[0], 0), (2, titles[1], 2)]


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


def test_identical_editions_fall_to_the_official_original():
    tracks = [("One", 100), ("Two", 200)]
    files = [{"key": importer.key(t), "number": (1, n + 1), "length": s, "path": f"{n}.flac"} for n, (t, s) in enumerate(tracks)]
    later, original, bootleg = edition("af", tracks), edition("jp", tracks), edition("bt", tracks, status="Bootleg")
    later["date"], original["date"], bootleg["date"] = "2023-05-01", "2023-03-15", "2022"
    assert importer.pick([later, bootleg, original], files)[0]["id"] == "jp"


def test_a_file_named_and_numbered_as_its_track_may_be_longer_off():
    track = lambda n, title, ms: ({"position": 1}, {"id": n, "position": n, "title": title, "recording": {"length": ms}})
    file = lambda n, title, s: {"path": f"{n}.flac", "key": importer.key(title), "number": (1, n), "length": s}
    tracks = [track(1, "Casus Belli", 322_000), track(2, "O Worze", 267_000)]
    assert importer.match([file(1, "Casus Belli", 318), file(2, "O Worze", 249)], tracks)
    assert importer.match([file(1, "Casus Belli", 318), file(4, "O Worze", 249)], tracks) is None  # numbered otherwise
    assert importer.match([file(1, "Casus Belli", 318), file(2, "O Worze", 230)], tracks) is None  # 14 % off


def test_a_tape_in_another_order_matches_by_titles_a_track_apart():
    """Lou Reed's Rock and Roll Heart on cassette: Rock and Roll Heart moved from 6 to 8, and a tape runs long."""
    lp = [("I Believe in Love", 167), ("Banging on My Drum", 132), ("Follow the Leader", 133), ("You Wear It So Well", 292),
          ("Ladies Pay", 263), ("Rock and Roll Heart", 186), ("Chooser and the Chosen One", 167), ("Senselessly Cruel", 128),
          ("Claim to Fame", 172), ("Vicious Circle", 174), ("A Sheltered Life", 140), ("Temporary Thing", 313)]
    tape = [*lp[:5], (lp[6][0], 174), (lp[7][0], 136), (lp[5][0], 190), (lp[8][0], 175), (lp[9][0], 180), lp[10], (lp[11][0], 321)]
    files = lambda tracks: [{"path": f"{n}.flac", "key": importer.key(t), "number": (1, n + 1), "length": s} for n, (t, s) in enumerate(tracks)]
    assert importer.pick([edition("lp", lp)], files(tape))[0]["id"] == "lp"
    with pytest.raises(LookupError):  # Chooser, 4 % long, two tracks off
        importer.pick([edition("lp", lp)], files([*tape[:5], tape[8], tape[6], tape[7], tape[5], *tape[9:]]))


def test_vinyl_sides_number_a_folder_of_untagged_files(make, tmp_path, root):
    """Rammstein's UK 7" singles in one torrent: 'A Amerika', 'B Wilder Wine' (sic) is the Amerika 7"."""
    audio(tmp_path / "dl/UK 7/Amerika UK 7/A Amerika.flac", 2)
    audio(tmp_path / "dl/UK 7/Amerika UK 7/B Wilder Wine (Exclusive Track).flac", 4)
    audio(tmp_path / "dl/UK 7/Keine Lust UK 7/A Keine Lust.flac", 3)
    audio(tmp_path / "dl/UK 7/Keine Lust UK 7/B Du hast - Live.flac", 9)
    seven = edition("7in", [("Amerika", 2), ("Wilder Wein", 4)], title="Amerika", fmt='7" Vinyl')
    cd = edition("cd", [("Amerika", 2), ("Amerika (Digital Hardcore mix)", 6)], title="Amerika")
    album = make(cd, seven).place(tmp_path / "dl", "rg-1")
    assert sorted(p.name for p in album.glob("*.flac")) == ["01 - Amerika.flac", "02 - Wilder Wein.flac"]


def test_side_letters_need_a_whole_folder_of_them():
    files = lambda *names: [{"path": importer.Path("dl", n), "key": importer.key(importer.title_of(n, "Linkin Park")), "number": None, "length": 1}
                            for n in names]
    assert [f["number"] for f in importer.sides(files("A Place for My Head.flac", "Papercut.flac"), "Linkin Park")] == [None, None]
    assert [f["key"] for f in importer.sides(files("B Side.flac", "C Side.flac"), "")] == ["bside", "cside"]  # no side A
    assert [(f["number"], f["key"]) for f in importer.sides(files("B1. Two.flac", "A1 One.flac", "A2 - Three.flac"), "")] == \
           [((1, 3), "two"), ((1, 1), "one"), ((1, 2), "three")]


def test_another_album_of_the_same_titles_still_fails():
    """Live takes of the album's songs, in its order: none within the album's lengths."""
    studio = [("Papercut", 185), ("One Step Closer", 156), ("With You", 203)]
    live = [{"path": f"{n}.flac", "key": importer.key(t), "number": (1, n + 1), "length": s * 1.15} for n, (t, s) in enumerate(studio)]
    with pytest.raises(LookupError):
        importer.pick([edition("studio", studio)], live)


def test_a_polish_cue_sheet_reads_as_polish():
    assert importer.cue_text('TITLE "Żółta łódź"\nTITLE "Gollum"'.encode("cp1250")) == 'TITLE "Żółta łódź"\nTITLE "Gollum"'
    assert importer.cue_text('TITLE "Кино"'.encode("cp1251")) == 'TITLE "Кино"'


def test_an_album_packed_in_a_zip_is_unpacked_and_imported(make, tmp_path, root):
    import zipfile
    one, two = audio(tmp_path / "src/01 One.flac", 2, title="One"), audio(tmp_path / "src/02 Two.flac", 3, title="Two")
    (tmp_path / "dl").mkdir()
    with zipfile.ZipFile(tmp_path / "dl/Beta - First.zip", "w") as z:
        z.write(one, "First/01 One.flac")
        z.write(two, "First/02 Two.flac")
    album = make(edition("e", [("One", 2), ("Two", 3)])).place(tmp_path / "dl", "rg-1")
    assert sorted(p.name for p in album.iterdir() if p.suffix == ".flac") == ["01 - One.flac", "02 - Two.flac"]
    assert importer.LATER_PART.search("a.part2.rar") and not importer.LATER_PART.search("a.part01.rar")


def test_a_flac_made_from_a_lossy_file_fails_the_import_before_anything_is_placed(make, tmp_path, root):
    audio(tmp_path / "dl/01 One.flac", 10, title="One")
    lossy(tmp_path / "dl/02 Two.flac", tmp_path / "work")
    FLAC_tags(tmp_path / "dl/02 Two.flac", title="Two")
    with pytest.raises(LossySource, match="lossy source in a FLAC: 02 Two.flac is cut off at 1[5-7]"):
        make(edition("standard", [("One", 10), ("Two", 10)])).place(tmp_path / "dl", "rg-1")
    assert not root.exists()


def test_fake_hi_res_arrives_as_what_it_is_with_its_verdict(make, tmp_path, root):
    music(tmp_path / "dl/01 One.flac", 44100, 10, "s32", "-af", "aresample=96000:resampler=soxr")
    FLAC_tags(tmp_path / "dl/01 One.flac", title="One")
    album = make(edition("standard", [("One", 10)])).place(tmp_path / "dl", "rg-1")
    info = mutagen.File(album / "01 - One.flac").info
    assert (info.sample_rate, info.bits_per_sample) == (44100, 16)
    row = connect(tmp_path / "oasis.db").execute("select * from audio_quality").fetchone()
    assert (row["path"], row["verdict"], row["action"], row["was"], row["rate"]) == (str(album / "01 - One.flac"), "upsampled", "resampled",
                                                                                    "24-bit 96 kHz", 44100)


def FLAC_tags(path, **tags):
    file = mutagen.File(path)
    for key, value in tags.items():
        file[key] = value
    file.save()
