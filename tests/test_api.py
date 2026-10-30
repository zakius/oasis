import hashlib
from xml.etree import ElementTree

from conftest import NS, PNG, call, make_track
from oasis.db import connect
from oasis.scan import scan


def ids_by_title(client):
    return {s["title"]: s for s in call(client, "search3", query="")["searchResult3"]["song"]}


def test_auth(client):
    salt = "abc123"
    token = hashlib.md5(b"secret" + salt.encode()).hexdigest()
    ok = client.get("/rest/ping", params={"u": "admin", "t": token, "s": salt, "f": "json"}).json()
    assert ok["subsonic-response"]["status"] == "ok"
    assert call(client, "ping", p="enc:" + b"secret".hex())["status"] == "ok"
    bad = call(client, "ping", p="nope")
    assert bad["status"] == "failed" and bad["error"]["code"] == 40


def test_artists_indexed_ignoring_articles(client):
    index = call(client, "getArtists")["artists"]["index"]
    names = {i["name"]: [a["name"] for a in i["artist"]] for i in index}
    assert names == {"A": ["The Alpha"], "B": ["Beta"], "U": ["Unknown Artist"]}


def test_album_tracks_ordered_with_tags(client):
    beta = next(a for i in call(client, "getArtists")["artists"]["index"] for a in i["artist"] if a["name"] == "Beta")
    albums = call(client, "getArtist", id=beta["id"])["artist"]["album"]
    assert [a["name"] for a in albums] == ["First"]
    album = call(client, "getAlbum", id=albums[0]["id"])["album"]
    assert [s["title"] for s in album["song"]] == ["One", "Two"]
    assert album["song"][0]["year"] == 2001 and album["song"][0]["genre"] == "Rock"
    assert album["song"][0]["path"] == "Beta/First/01.flac"


def test_untagged_file_falls_back_to_names(client):
    song = ids_by_title(client)["untagged"]
    assert song["album"] == "Loose" and song["artist"] == "Unknown Artist" and song["suffix"] == "ogg"


def test_cover_art_embedded_and_folder(client):
    songs = ids_by_title(client)
    for title in ("One", "Song A"):
        response = client.get("/rest/getCoverArt", params={"u": "admin", "p": "secret", "id": songs[title]["coverArt"]})
        assert response.status_code == 200 and response.content == PNG
    assert "coverArt" not in songs["untagged"]


def test_stream_supports_range(client, library):
    song = ids_by_title(client)["One"]
    response = client.get("/rest/stream", params={"u": "admin", "p": "secret", "id": song["id"]}, headers={"Range": "bytes=0-9"})
    assert response.status_code == 206
    assert response.content == (library / "Beta/First/01.flac").read_bytes()[:10]


def test_search_terms_match_across_fields(client):
    result = call(client, "search3", query="beta two")["searchResult3"]
    assert [s["title"] for s in result["song"]] == ["Two"]
    assert [a["name"] for a in result["artist"]] == []
    assert len(call(client, "search3", query='""')["searchResult3"]["song"]) == 5


def test_star_rate_scrobble(client):
    song = ids_by_title(client)["Two"]
    call(client, "star", id=song["id"], albumId=song["albumId"])
    call(client, "setRating", id=song["id"], rating=4)
    call(client, "scrobble", id=song["id"], time=1_700_000_000_000)
    starred = call(client, "getStarred2")["starred2"]
    assert [s["title"] for s in starred["song"]] == ["Two"] and [a["name"] for a in starred["album"]] == ["First"]
    updated = call(client, "getSong", id=song["id"])["song"]
    assert updated["userRating"] == 4 and updated["playCount"] == 1 and updated["played"].startswith("2023-11-14")
    frequent = call(client, "getAlbumList2", type="frequent")["albumList2"]["album"]
    assert [a["name"] for a in frequent] == ["First"]


def test_album_lists(client):
    def names(**params):
        return [a["name"] for a in call(client, "getAlbumList2", size=10, **params)["albumList2"]["album"]]
    assert names(type="alphabeticalByName") == ["First", "Loose", "Second"]
    assert names(type="byYear", fromYear=2000, toYear=1990) == ["Second"]
    assert names(type="byGenre", genre="Rock") == ["First"]


def test_playlists(client):
    songs = ids_by_title(client)
    created = call(client, "createPlaylist", name="Mix", songId=[songs["One"]["id"], songs["Song A"]["id"]])["playlist"]
    call(client, "updatePlaylist", playlistId=created["id"], songIndexToRemove=0, songIdToAdd=songs["Two"]["id"], name="Mix 2")
    playlist = call(client, "getPlaylist", id=created["id"])["playlist"]
    assert playlist["name"] == "Mix 2" and [s["title"] for s in playlist["entry"]] == ["Song A", "Two"]
    call(client, "deletePlaylist", id=created["id"])
    assert call(client, "getPlaylists")["playlists"]["playlist"] == []


def test_xml_format(client):
    root = ElementTree.fromstring(call(client, "getGenres", fmt="xml").content)
    assert root.tag == NS + "subsonic-response" and root.get("status") == "ok"
    genre = root.find(f"{NS}genres/{NS}genre")
    assert genre.text == "Rock" and genre.get("songCount") == "2"


def test_post_form_and_unknown_method(client):
    response = client.post("/rest/ping", data={"u": "admin", "p": "secret", "f": "json"}).json()
    assert response["subsonic-response"]["status"] == "ok"
    assert call(client, "_db")["status"] == "failed"


def test_rescan_picks_up_changes(client, library, tmp_path):
    db = connect(tmp_path / "oasis.db")
    extra = library / "Beta/First/03.flac"
    make_track(extra, title="Three", artist="Beta", album="First", tracknumber="3")
    try:
        assert scan(db, [library]) == 1
        assert "Three" in ids_by_title(client)
    finally:
        extra.unlink()
    assert scan(db, [library]) == 1
    assert "Three" not in ids_by_title(client)
