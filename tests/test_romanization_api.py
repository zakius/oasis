import json

import pytest

from conftest import call, make_client, make_track
from oasis.db import connect


@pytest.fixture(scope="module")
def names(tmp_path_factory):
    root = tmp_path_factory.mktemp("names")
    make_track(root / "y/1.flac", title="夜に駆ける", artist="YOASOBI", album="THE BOOK", tracknumber="1")
    make_track(root / "j/1.flac", title="晴天", artist="周杰倫", album="葉惠美", tracknumber="1")
    make_track(root / "k/1.flac", title="Lemon", artist="米津玄師", album="Lemon", tracknumber="1")
    make_track(root / "b/1.flac", title="One", artist="Beta", album="First", tracknumber="1")
    (root / "y/1.lrc").write_text("[00:01.00]沈むように溶けてゆくように\n[00:05.00]二人だけの空が広がる夜に\n")
    (root / "b/1.lrc").write_text("[00:01.00]Hello there\n")
    return root


@pytest.fixture
def client(names, tmp_path):
    with make_client(names, tmp_path / "oasis.db") as c:
        yield c


def songs(client, **params):
    return {s["title"]: s for s in call(client, "search3", query="", **params)["searchResult3"]["song"]}


def latin_keys(value) -> set:
    if isinstance(value, dict):
        return {k for k in value if k.endswith("Latin")} | {k for v in value.values() for k in latin_keys(v)}
    return {k for v in value for k in latin_keys(v)} if isinstance(value, list) else set()


def test_advertised(client):
    extensions = {e["name"]: e["versions"] for e in call(client, "getOpenSubsonicExtensions")["openSubsonicExtensions"]}
    assert extensions["romanization"] == [1] and extensions["songLyrics"] == [1, 2]


def test_absent_or_original_changes_nothing(client):
    album = songs(client)["夜に駆ける"]["albumId"]
    call(client, "createPlaylist", name="夜のうた")
    for method, params in [("getArtists", {}), ("getIndexes", {}), ("search3", {"query": ""}), ("getAlbum", {"id": album}),
                           ("getAlbumList2", {"type": "alphabeticalByName"}), ("getPlaylists", {}), ("getStarred2", {})]:
        plain = call(client, method, **params)
        assert not latin_keys(plain), method
        assert call(client, method, romanization="original", **params) == plain, method
        assert client.get(f"/rest/{method}.view", params={"u": "admin", "p": "secret", **params}).content == \
            client.get(f"/rest/{method}.view", params={"u": "admin", "p": "secret", "romanization": "original", **params}).content


def test_both_adds_only_differing_fields(client):
    found = songs(client, romanization="both")
    yoru = found["夜に駆ける"]
    assert yoru["titleLatin"] == "Yoru ni Kakeru" and yoru["title"] == "夜に駆ける"
    assert not {"artistLatin", "albumLatin", "displayArtistLatin"} & yoru.keys()  # YOASOBI, THE BOOK
    assert found["晴天"]["titleLatin"] == "Qíng Tiān"  # a Chinese singer's song, not Japanese "Seiten"
    assert found["晴天"]["artistLatin"] == "Zhōu Jié Lún" and found["晴天"]["artists"][0]["nameLatin"] == "Zhōu Jié Lún"
    assert not latin_keys(found["One"]) and not latin_keys(found["Lemon"]) - {"artistLatin", "displayArtistLatin", "nameLatin"}
    index = call(client, "getArtists", romanization="both")["artists"]["index"]
    artists = {a["name"]: a for i in index for a in i["artist"]}
    assert artists["米津玄師"]["nameLatin"] == "Yonetsu Genshi" and "nameLatin" not in artists["YOASOBI"]
    album = call(client, "getAlbum", id=yoru["albumId"], romanization="both")["album"]
    assert "nameLatin" not in album and album["song"][0]["titleLatin"] == "Yoru ni Kakeru"
    album = call(client, "getAlbum", id=found["晴天"]["albumId"], romanization="both")["album"]
    assert album["nameLatin"] == "Yè Huì Měi" and album["artistLatin"] == "Zhōu Jié Lún" and album["name"] == "葉惠美"


def test_latin_replaces_names(client, tmp_path):
    found = songs(client, romanization="latin")
    assert "Yoru ni Kakeru" in found and "Qíng Tiān" in found and not latin_keys(found)
    assert found["Lemon"]["artist"] == "Yonetsu Genshi"
    index = call(client, "getArtists", romanization="latin")["artists"]["index"]
    assert {i["name"]: [a["name"] for a in i["artist"]] for i in index} == \
        {"B": ["Beta"], "Y": ["YOASOBI", "Yonetsu Genshi"], "Z": ["Zhōu Jié Lún"]}
    album = call(client, "getAlbum", id=found["Qíng Tiān"]["albumId"], romanization="latin")["album"]
    assert album["name"] == album["title"] == album["album"] == "Yè Huì Měi"


def test_oasis_fields_stay(client, tmp_path):
    album = songs(client)["晴天"]["albumId"]
    db = connect(tmp_path / "oasis.db")
    db.execute("insert into want_item(kind, ref, state, error, updated) values ('album', ?, 'failed', 'nothing found', 0)", (album,))
    db.execute("insert into playlist(name, owner, created, changed, mix) values ('マイミックス', 'admin', 0, 0, 'RDTMAK5uy_kset8DisdE7LSD4TNjEVvrKRTmG7a56sY')")
    got = call(client, "getAlbum", id=album, romanization="both", oasis="1")["album"]
    assert got["oasisWanted"] == "failed" and got["oasisWantedError"] == "nothing found"
    playlist = call(client, "getPlaylists", romanization="both", oasis="1")["playlists"]["playlist"][0]
    assert playlist["oasisMix"] is True and playlist["nameLatin"] == "Maimikkusu" and playlist["name"] == "マイミックス"
    assert "scrobblingEnabled" in call(client, "getUser", username="admin", romanization="both")["user"]


def test_search_finds_romaji(client):
    def titles(query, **params):
        return [s["title"] for s in call(client, "search3", query=query, **params)["searchResult3"]["song"]]
    assert titles("yoru kakeru") == ["夜に駆ける"]
    assert titles("qing tian") == titles("Qíng") == ["晴天"]
    assert titles("夜に") == ["夜に駆ける"] and titles("beta") == ["One"]
    assert titles("runika") == []  # no match across words (yo|ru ni ka|keru)
    assert titles("yoru", romanization="latin") == ["Yoru ni Kakeru"]
    result = call(client, "search3", query="zhou")["searchResult3"]
    assert [a["name"] for a in result["artist"]] == ["周杰倫"] and [a["name"] for a in result["album"]] == ["葉惠美"]


def test_lyrics_kinds(client):
    yoru, one = songs(client)["夜に駆ける"]["id"], songs(client)["One"]["id"]

    def lyrics(id, **params):
        return call(client, "getLyricsBySongId", id=id, **params)["lyricsList"].get("structuredLyrics", [])
    plain = lyrics(yoru)
    assert len(plain) == 1 and "kind" not in plain[0] and plain[0]["line"][0] == {"start": 1000, "value": "沈むように溶けてゆくように"}
    main, pronunciation = lyrics(yoru, enhanced="true")
    assert main == {**plain[0], "kind": "main"}
    assert pronunciation["kind"] == "pronunciation" and pronunciation["lang"] == "ja-Latn"
    assert pronunciation["line"] == [{"start": 1000, "value": "Shizumu yō ni tokete yuku yō ni"},
                                     {"start": 5000, "value": "Futari dake no sora ga hirogaru yoru ni"}]
    assert lyrics(yoru, enhanced="true", kinds="pronunciation") == [pronunciation]
    assert lyrics(yoru, enhanced="true", kinds="main") == [main]
    assert lyrics(yoru, enhanced="true", kinds="translation") == []
    assert lyrics(yoru, kinds="pronunciation") == plain  # kinds goes with enhanced
    # Lyrics already in Latin script have no pronunciation: main stands in for it.
    assert [l["kind"] for l in lyrics(one, enhanced="true")] == ["main"]
    assert [l["kind"] for l in lyrics(one, enhanced="true", kinds="pronunciation")] == ["main"]


@pytest.fixture
def karaoke(tmp_path):
    root = tmp_path / "lib"
    make_track(root / "a/1.flac", title="忙しい", artist="テスト", album="テスト", tracknumber="1")
    (root / "a/1.lrc").write_text("[00:01.00]<00:01.00>忙<00:01.40>し<00:01.60>い <00:02.00>夜に\n[00:04.00]ここ\n")
    with make_client(root, tmp_path / "oasis.db") as c:
        yield c


def test_lyrics_cue_lines(karaoke):
    id = songs(karaoke)["忙しい"]["id"]
    lyrics = lambda **params: call(karaoke, "getLyricsBySongId", id=id, **params)["lyricsList"]["structuredLyrics"]
    assert all("cueLine" not in l for l in lyrics())
    main, pronunciation = lyrics(enhanced="true")
    def cues(entry):
        return [[(c["start"], c["end"], c["value"], c["byteStart"], c["byteEnd"]) for c in l["cue"]] for l in entry["cueLine"]]
    assert [l["index"] for l in main["cueLine"]] == [0]  # ここ has no word timing: line-level only
    assert cues(main) == [[(1000, 1400, "忙", 0, 2), (1400, 1600, "し", 3, 5), (1600, 2000, "い ", 6, 9), (2000, 4000, "夜に", 10, 15)]]
    assert [l["value"] for l in pronunciation["line"]] == ["Isogashii yoru ni", "Koko"]
    assert pronunciation["cueLine"][0]["value"] == "Isogashii yoru ni"
    assert cues(pronunciation) == [[(1000, 1400, "Isoga", 0, 4), (1400, 1600, "shi", 5, 7), (1600, 2000, "i ", 8, 9), (2000, 4000, "yoru ni", 10, 16)]]
    for entry in (main, pronunciation):  # each cue's bytes are its own text
        for l in entry["cueLine"]:
            assert all(l["value"].encode()[c["byteStart"]:c["byteEnd"] + 1].decode() == c["value"] for c in l["cue"])
    assert "cueLine" in lyrics(enhanced="true", kinds="pronunciation")[0]
