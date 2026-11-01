from starlette.testclient import TestClient

from conftest import call, make_track
from oasis.api import create_app
from oasis.config import Config
from oasis.credits import Credits
from oasis.db import connect, make_id, remember
from oasis.scan import scan


def release(id, artist, mbid, engineer_id):
    return {"id": id, "title": id, "artist-credit": [{"name": artist, "artist": {"id": mbid, "name": artist}, "joinphrase": ""}],
            "media": [{"tracks": [{"id": f"{id}-t", "title": "Song", "recording": {"id": f"{id}-r", "relations": [
                {"type": "recording", "attributes": [], "artist": {"id": engineer_id, "name": "John Bender"}}]}}]}]}


RELEASES = {"rel-k": release("rel-k", "米津玄師", "kenshi", "jb-1"), "rel-b": release("rel-b", "Band", "band", "jb-2"),
            "rel-h": release("rel-h", "ハチ", "hachi", "jb-1")}
ARTISTS = {
    "kenshi": {"id": "kenshi", "name": "米津玄師", "type": "Person",
               "aliases": [{"name": "Kenshi Yonezu", "type": "Artist name"}, {"name": "켄시 요네즈", "type": "Search hint"}],
               "relations": [{"type": "is person", "artist": {"id": "hachi", "name": "ハチ"}}]},
    "band": {"id": "band", "name": "Band", "type": "Group", "relations": [
        {"type": "member of band", "artist": {"id": "jb-2", "name": "John Bender"}},
        {"type": "tribute", "artist": {"id": "tribute", "name": "Band Tribute"}}]},
}


def client_for(tmp_path, **settings):
    root = tmp_path / "music"
    for folder, artist, mbid, rel in (("Kenshi", "米津玄師", "kenshi", "rel-k"), ("Band", "Band", "band", "rel-b"), ("Hachi", "ハチ", "hachi", "rel-h")):
        make_track(root / folder / "01.flac", title="Song", artist=artist, albumartist=artist, album=folder,
                   musicbrainz_albumartistid=mbid, musicbrainz_albumid=rel, musicbrainz_releasetrackid=f"{rel}-t")
    for album, artist in (("Myth", "Mori Calliope"), ("Dice", "DEMONDICE"), ("One", "Beta"), ("Two", "Beta Band")):
        make_track(root / album / "01.flac", title=album, artist=artist, album=album)
    config = Config(library=[root], users={"admin": "secret"}, database=tmp_path / "oasis.db", artists=settings)
    db = connect(config.database)
    scan(db, config.library)
    # Seen before MusicBrainz said whose name it is: a starred stream under the English spelling.
    remember(db, {"id": "yt-v1", "source": "ytm", "ref": "v1", "title": "Lemon", "artist": "Kenshi Yonezu", "album_artist": "Kenshi Yonezu",
                  "album": "Lemon", "artist_id": make_id("ar-", "Kenshi Yonezu"), "album_id": make_id("al-", "Kenshi Yonezu", "Lemon"), "duration": 250})
    db.execute("insert into stat(id, starred) values (?, 1)", (make_id("ar-", "Kenshi Yonezu"),))
    return TestClient(create_app(config, background=False, services={}))


def fill(client):
    credits = Credits(client.app.state.api._db, fetch=RELEASES.__getitem__, fetch_artist=ARTISTS.__getitem__)
    while credits.step():
        pass


def test_one_musicbrainz_artist_is_one_page_and_namesakes_are_two(tmp_path):
    with client_for(tmp_path) as client:
        fill(client)
        kenshi = make_id("ar-", "米津玄師")
        page = call(client, "getArtist", id=make_id("ar-", "Kenshi Yonezu"))["artist"]  # an id a client kept from before
        assert page["id"] == kenshi and page["starred"]
        stream = call(client, "getSong", id="yt-v1")["song"]
        assert stream["artistId"] == kenshi
        later = {"id": "yt-v2", "source": "ytm", "ref": "v2", "title": "Uma to Shika", "artist": "Kenshi Yonezu", "album_artist": "Kenshi Yonezu",
                 "album": "x", "artist_id": make_id("ar-", "Kenshi Yonezu"), "album_id": make_id("al-", "Kenshi Yonezu", "x"), "duration": 250}
        db = client.app.state.api._db()
        remember(db, later)
        assert call(client, "getSong", id="yt-v2")["song"]["artistId"] == kenshi
        hint = make_id("ar-", "켄시 요네즈")
        assert call(client, "getArtist", id=hint)["status"] == "failed"  # search hints aren't names it goes by

        engineers = {}
        for album in ("Kenshi", "Band"):
            id = db.execute("select id from track where album = ?", (album,)).fetchone()[0]
            engineer = next(c for c in call(client, "getSong", id=id)["song"]["contributors"] if c["role"] == "engineer")
            engineers[album] = engineer["artist"]["id"]
        assert engineers["Kenshi"] != engineers["Band"]  # two people called John Bender
        assert [a["name"] for a in call(client, "getArtist", id=engineers["Band"])["artist"]["album"]] == ["Band"]


def test_personas_related_artists_and_members_come_first_among_similar_artists(tmp_path):
    with client_for(tmp_path, related=[["Mori Calliope", "DEMONDICE"]]) as client:
        fill(client)
        similar = call(client, "getArtistInfo2", id=make_id("ar-", "米津玄師"))["artistInfo2"]["similarArtist"]
        assert [(a["name"], a["oasisLink"]) for a in similar] == [("ハチ", "persona")]
        similar = call(client, "getArtistInfo2", id=make_id("ar-", "DEMONDICE"))["artistInfo2"]["similarArtist"]
        assert [(a["name"], a["oasisLink"]) for a in similar] == [("Mori Calliope", "related")]
        band = call(client, "getArtistInfo2", id=make_id("ar-", "Band"))["artistInfo2"]["similarArtist"]
        assert [(a["name"], a["oasisLink"]) for a in band] == [("John Bender", "member")]  # tributes aren't links
        member = client.app.state.api._db().execute("select artist_id from mb_artist where mbid = 'jb-2'").fetchone()[0]
        assert [(a["name"], a["oasisLink"]) for a in call(client, "getArtistInfo2", id=member)["artistInfo2"]["similarArtist"]] == [("Band", "member")]
        radio = call(client, "getSimilarSongs2", id=make_id("ar-", "Mori Calliope"))["similarSongs2"]["song"]
        assert "DEMONDICE" in [s["artist"] for s in radio]


def test_the_users_merges_and_splits_win(tmp_path):
    with client_for(tmp_path, merge=[["Beta", "Beta Band"]], separate=["Kenshi Yonezu"]) as client:
        fill(client)
        page = call(client, "getArtist", id=make_id("ar-", "Beta"))["artist"]
        assert [a["name"] for a in page["album"]] == ["One", "Two"]
        assert call(client, "getArtist", id=make_id("ar-", "Kenshi Yonezu"))["artist"]["id"] == make_id("ar-", "Kenshi Yonezu")


def test_artist_settings_on_the_dashboard_apply_at_once_and_merges_can_be_taken_back(tmp_path):
    with client_for(tmp_path, related=[["Mori Calliope", "DEMONDICE"]]) as client:
        client.post("/login", data={"name": "admin", "password": "secret"})
        page = client.get("/admin/artists").text
        assert "Mori Calliope; DEMONDICE" in page  # oasis.toml's groups until saved here
        saved = client.post("/admin/artists", data={"related": "Gawr Gura; Senzawa\nlonely", "merge": "Beta; Beta Band", "separate": ""}).text
        assert "Saved" in saved and "Gawr Gura; Senzawa" in saved and "lonely" not in saved
        assert "similarArtist" not in call(client, "getArtistInfo2", id=make_id("ar-", "DEMONDICE"))["artistInfo2"]
        assert len(call(client, "getArtist", id=make_id("ar-", "Beta"))["artist"]["album"]) == 2

        client.post("/admin/artists", data={"related": "", "merge": "", "separate": ""})
        assert [a["name"] for a in call(client, "getArtist", id=make_id("ar-", "Beta"))["artist"]["album"]] == ["One"]
        assert [a["name"] for a in call(client, "getArtist", id=make_id("ar-", "Beta Band"))["artist"]["album"]] == ["Two"]
        assert "<span>artists</span>" in client.get("/").text and "Downloading" in client.get("/").text
