from conftest import call, make_client, make_track
from oasis import latin
from oasis.db import connect

YONEZU = {"name": "米津玄師", "sort-name": "Yonezu, Kenshi", "type": "Person",
          "aliases": [{"name": "Kenshi Yonezu", "locale": "en", "primary": True}, {"name": "米津玄師", "locale": "ja", "primary": True}]}


def test_an_artists_latin_name_is_its_ja_latn_alias_else_its_sort_name_in_its_own_order():
    assert latin.artist_name(YONEZU) == "Yonezu Kenshi"  # not the English alias, which turns the name around
    assert latin.artist_name({**YONEZU, "aliases": [*YONEZU["aliases"], {"name": "Yonedzu Kenshi", "locale": "ja-Latn"}]}) == "Yonedzu Kenshi"
    assert latin.artist_name({**YONEZU, "type": "Character"}) is None
    assert latin.artist_name({**YONEZU, "sort-name": "米津, 玄師"}) is None  # its sort name isn't Latin: generated


def release(title, language, *tracks, rel=None):
    return {"title": title, "status": "Pseudo-Release" if rel is None else "Official", "text-representation": {"language": language, "script": "Latn" if rel is None else "Jpan"},
            "media": [{"position": 1, "tracks": [{"position": i, "title": t} for i, t in enumerate(tracks, 1)]}],
            "relations": [{"type": "transl-tracklisting", "direction": "forward", "release": {"id": r}} for r in rel or []]}


def library(tmp_path):
    db = connect(tmp_path / "oasis.db")
    for i, (title, album) in enumerate([("眠れ", "眠れ"), ("夜", "眠れ")], 1):
        db.execute("insert into track(id, source, ref, title, artist, album, track_no, disc_no, library, mb_release) values (?, 'local', ?, ?, 'ずっと真夜中でいいのに。', ?, ?, 1, 1, 'rel')",
                   (f"t{i}", f"/m/{i}.flac", title, album, i))
    db.execute("insert into credit(track_id, role, name, mbid, source) values ('t1', 'artist', '米津玄師', 'mb-yonezu', 'musicbrainz')")
    return db


def test_a_releases_transliteration_names_its_album_and_tracks_but_a_translation_doesnt(tmp_path):
    db = library(tmp_path)
    answers = {latin.ARTIST.format("mb-yonezu"): YONEZU,
               latin.RELEASE.format("rel"): release("眠れ", "jpn", "眠れ", "夜", rel=["sleep", "nemure"]),
               latin.PSEUDO.format("sleep"): release("Sleep", "eng", "Sleep", "Night"),  # a translation, tagged English
               latin.PSEUDO.format("nemure"): release("Nemure", "eng", "Nemure", "Yoru")}  # a transliteration, tagged English too
    asked = []
    fetch = lambda url: asked.append(url) or answers[url]
    db.execute("insert into track_latin values ('t1', '眠れ', 'x', 'x', 'x', 'x')")
    while latin.step(db, fetch):
        pass
    assert latin.curated(db) == {"artist": {"米津玄師": "Yonezu Kenshi"}, "title": {"眠れ": "Nemure", "夜": "Yoru"}}
    assert not db.execute("select 1 from track_latin").fetchone()  # search reads the songs' romanized forms again
    assert not latin.step(db, fetch) and len(asked) == 4  # read once, till REFRESH


def test_musicbrainz_names_come_before_generated_ones_in_names_and_search(tmp_path):
    root = tmp_path / "m"
    make_track(root / "k/1.flac", title="感電", artist="米津玄師 + アイナ・ジ・エンド", album="STRAY SHEEP", tracknumber="1")
    make_track(root / "k/2.flac", title="馬と鹿", artist="米津玄師", album="STRAY SHEEP", tracknumber="2")
    with make_client(root, tmp_path / "oasis.db") as client:
        db = connect(tmp_path / "oasis.db")
        db.executemany("insert into mb_latin values (?, ?, ?, 'ref')", [("artist", "米津玄師", "Yonezu Kenshi MB"), ("title", "感電", "Kanden MB")])
        db.execute("delete from track_latin")
        db.commit()
        found = {s["title"]: s for s in call(client, "search3", query="", romanization="both")["searchResult3"]["song"]}
        assert found["感電"]["titleLatin"] == "Kanden MB" and found["馬と鹿"]["artistLatin"] == "Yonezu Kenshi MB"
        assert found["感電"]["artistLatin"].startswith("Yonezu Kenshi MB + ")  # the other name generated
        assert found["馬と鹿"]["titleLatin"] == "Uma to Shika"  # none from MusicBrainz: generated
        assert [s["title"] for s in call(client, "search3", query="kanden mb")["searchResult3"]["song"]] == ["感電"]
