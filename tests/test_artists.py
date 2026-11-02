from starlette.testclient import TestClient

from conftest import call, make_track
from oasis import artists
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
    db.execute("insert into stat(user, id, starred) values ('admin', ?, 1)", (make_id("ar-", "Kenshi Yonezu"),))
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
        similar = call(client, "getArtistInfo2", id=make_id("ar-", "米津玄師"), oasis="1")["artistInfo2"]["similarArtist"]
        assert [(a["name"], a["oasisLink"]) for a in similar] == [("ハチ", "persona")]
        similar = call(client, "getArtistInfo2", id=make_id("ar-", "DEMONDICE"), oasis="1")["artistInfo2"]["similarArtist"]
        assert [(a["name"], a["oasisLink"]) for a in similar] == [("Mori Calliope", "related")]
        band = call(client, "getArtistInfo2", id=make_id("ar-", "Band"), oasis="1")["artistInfo2"]["similarArtist"]
        assert [(a["name"], a["oasisLink"]) for a in band] == [("John Bender", "member")]  # tributes aren't links
        member = client.app.state.api._db().execute("select artist_id from mb_artist where mbid = 'jb-2'").fetchone()[0]
        assert [(a["name"], a["oasisLink"]) for a in call(client, "getArtistInfo2", id=member, oasis="1")["artistInfo2"]["similarArtist"]] == [("Band", "member")]
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
        assert "similarArtist" not in call(client, "getArtistInfo2", id=make_id("ar-", "DEMONDICE"), oasis="1")["artistInfo2"]
        assert len(call(client, "getArtist", id=make_id("ar-", "Beta"))["artist"]["album"]) == 2

        client.post("/admin/artists", data={"related": "", "merge": "", "separate": ""})
        assert [a["name"] for a in call(client, "getArtist", id=make_id("ar-", "Beta"))["artist"]["album"]] == ["One"]
        assert [a["name"] for a in call(client, "getArtist", id=make_id("ar-", "Beta Band"))["artist"]["album"]] == ["Two"]
        assert "<span>artists</span>" in client.get("/").text and "Downloading" in client.get("/").text


def stream(id, artist, title, guest=""):
    return {"id": f"yt-{id}", "source": "ytm", "ref": id, "title": title, "artist": artist + guest, "album_artist": artist, "album": title,
            "artist_id": make_id("ar-", artist), "album_id": make_id("al-", artist, title), "duration": 200, "library": 1}


def spelled_client(tmp_path, streams):
    config = Config(library=[tmp_path / "music"], users={"admin": "secret"}, database=tmp_path / "oasis.db")
    client = TestClient(create_app(config, background=False, services={}))
    db = connect(config.database)
    for s in streams:
        remember(db, s)
    return db, client


def test_spellings_of_one_name_are_one_artist_under_the_accented_one_on_a_tie(tmp_path):
    plain, accented = "Agnieszka Chylinska", "Agnieszka Chylińska"
    db, client = spelled_client(tmp_path, [stream("a1", accented, "Zero"), stream("a2", accented, "Hand"),
                                           stream("p1", plain, "Kiedy"), stream("p2", plain, "Duet", ", Guest")])
    db.execute("insert into stat(user, id, starred) values ('admin', ?, 1)", (make_id("ar-", plain),))
    with client:
        artists.unify(db)
        index = call(client, "getArtists")["artists"]["index"]
        found = [a for i in index for a in i["artist"]]
        assert [(a["id"], a["name"]) for a in found] == [(make_id("ar-", accented), accented)]
        old = call(client, "getArtist", id=make_id("ar-", plain))["artist"]  # an id a client kept from before
        assert old["id"] == make_id("ar-", accented) and old["starred"] and old["name"] == accented
        songs = {s["id"]: s for s in call(client, "search3", query="Chylinska")["searchResult3"].get("song", [])}
        assert {s["artist"] for s in songs.values()} == {accented, f"{accented}, Guest"}
        assert {s["artistId"] for s in songs.values()} == {make_id("ar-", accented)}
        # A new spelling from YouTube Music joins the artist as it arrives.
        assert remember(db, stream("p3", "AGNIESZKA CHYLINSKA", "Nowa")) == "yt-p3"
        assert call(client, "getSong", id="yt-p3")["song"]["artistId"] == make_id("ar-", accented)


def test_youtubes_stylization_loses_to_the_spelling_on_more_tracks_and_musicbrainz_names_the_artist(tmp_path):
    db, client = spelled_client(tmp_path, [stream("y1", "Jaÿ‐Z", "Run This Town"), stream("j1", "Jay‐Z", "Izzo"),
                                           stream("j2", "Jay‐Z", "Encore"), stream("u1", "JAY‐Z", "99 Problems")])
    jay = make_id("ar-", "Jaÿ‐Z")  # the spelling seen first gave the artist its id, and it keeps it
    with client:
        artists.unify(db)
        assert [(a["id"], a["name"]) for i in call(client, "getArtists")["artists"]["index"] for a in i["artist"]] == [(jay, "Jay‐Z")]
        assert {(s["artistId"], s["artist"]) for s in (call(client, "getSong", id=f"yt-{v}")["song"] for v in ("y1", "u1"))} == {(jay, "Jay‐Z")}
        db.execute("insert into mb_artist(mbid, artist_id, name) values ('jayz', ?, 'JAŸ-Z')", (jay,))  # MusicBrainz's name for him
        artists.unify(db)
        assert [(a["id"], a["name"]) for i in call(client, "getArtists")["artists"]["index"] for a in i["artist"]] == [(jay, "JAŸ-Z")]
        assert call(client, "getSong", id="yt-y1")["song"]["artist"] == "JAŸ-Z"
