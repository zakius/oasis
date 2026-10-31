import mutagen
import pytest
from starlette.testclient import TestClient

from conftest import call, make_track
from oasis.api import create_app
from oasis.config import Config
from oasis.credits import Credits
from oasis.db import connect, make_id
from oasis.scan import scan

RELEASE = {
    "id": "rel-1", "title": "Songs", "artist-credit": [{"name": "Various Artists", "artist": {"id": "va", "name": "Various Artists"}, "joinphrase": ""}],
    "label-info": [{"label": {"name": "Roswell Records"}}],
    "release-group": {"primary-type": "Album", "secondary-types": ["Compilation", "Soundtrack"], "first-release-date": "2017-09-15",
                      "genres": [{"name": "rock", "count": 3}, {"name": "hard rock", "count": 5}]},
    "relations": [{"type": "mix", "artist": {"id": "mixer", "name": "Darrell Thorp"}, "attributes": []}],
    "media": [{"tracks": [{
        "id": "track-1", "title": "Run",
        "artist-credit": [{"name": "Gura", "artist": {"id": "gura", "name": "Gawr Gura", "type": "Character"}, "joinphrase": " feat. "},
                          {"name": "Calli", "artist": {"id": "calli", "name": "Mori Calliope", "type": "Character"}, "joinphrase": ""}],
        "recording": {"id": "rec-1", "isrcs": ["USRW31700013"], "relations": [
            {"type": "instrument", "attributes": ["guitar"], "artist": {"id": "dave", "name": "Dave Grohl"}},
            {"type": "vocal", "attributes": ["lead vocals"], "artist": {"id": "dave", "name": "Dave Grohl"}},
            {"type": "producer", "attributes": [], "artist": {"id": "greg", "name": "Greg Kurstin"}, "target-credit": "G. Kurstin"},
            {"type": "performance", "work": {"title": "Run", "relations": [
                {"type": "writer", "artist": {"id": "dave", "name": "Dave Grohl"}},
                {"type": "composer", "artist": {"id": "taylor", "name": "Taylor Hawkins"}}]}}]}}]}],
}


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "music"
    make_track(root / "Songs/01.flac", title="Run", artist="Gura feat. Calli", albumartist="Various Artists", album="Songs",
               musicbrainz_albumid="rel-1", musicbrainz_releasetrackid="track-1", composer="Someone Already Tagged")
    make_track(root / "Other/01.flac", title="Plain", artist="Solo", album="Other", composer="Tag Composer", lyricist="Tag Lyricist")
    return root


@pytest.fixture
def client(library, tmp_path):
    config = Config(library=[library], users={"admin": "secret"}, database=tmp_path / "oasis.db")
    scan(connect(config.database), config.library)
    with TestClient(create_app(config, background=False, services={})) as c:
        yield c


def fill(client):
    credits = Credits(client.app.state.api._db, fetch=lambda mbid: RELEASE)
    assert credits.step() and not credits.step()


def song_named(client, title):
    return next(s for s in call(client, "search3", query=title)["searchResult3"]["song"] if s["title"] == title)


def test_musicbrainz_credits_reach_clients_and_fill_the_files_tags(client, library):
    fill(client)
    s = song_named(client, "Run")
    assert s["displayArtist"] == "Gura feat. Calli"  # characters stay credited as such
    assert s["artists"] == [{"id": make_id("ar-", "Gawr Gura"), "name": "Gura"}, {"id": make_id("ar-", "Mori Calliope"), "name": "Calli"}]
    assert {(c["role"], c.get("subRole"), c["artist"]["name"]) for c in s["contributors"]} == {
        ("performer", "guitar", "Dave Grohl"), ("performer", "lead vocals", "Dave Grohl"), ("producer", None, "G. Kurstin"),
        ("writer", None, "Dave Grohl"), ("composer", None, "Taylor Hawkins"), ("mixer", None, "Darrell Thorp")}
    assert s["displayComposer"] == "Taylor Hawkins" and s["isrc"] == ["USRW31700013"] and s["musicBrainzId"] == "rec-1"
    assert [g["name"] for g in s["genres"]] == ["hard rock", "rock"]

    a = call(client, "getAlbum", id=s["albumId"])["album"]
    assert a["isCompilation"] and a["releaseTypes"] == ["Album", "Compilation", "Soundtrack"]
    assert a["recordLabels"] == [{"name": "Roswell Records"}] and a["originalReleaseDate"] == {"year": 2017, "month": 9, "day": 15}

    tags = mutagen.File(library / "Songs/01.flac").tags
    assert tags["COMPOSER"] == ["Someone Already Tagged"]  # what the file had stays
    assert tags["PERFORMER"] == ["Dave Grohl (guitar)", "Dave Grohl (lead vocals)"] and tags["WRITER"] == ["Dave Grohl"]
    assert tags["ARTISTS"] == ["Gura", "Calli"] and tags["LABEL"] == ["Roswell Records"] and tags["ISRC"] == ["USRW31700013"]


def test_a_member_only_credited_on_songs_has_an_artist_page(client):
    fill(client)
    page = call(client, "getArtist", id=make_id("ar-", "Dave Grohl"))["artist"]
    assert page["name"] == "Dave Grohl" and [a["name"] for a in page["album"]] == ["Songs"]


def test_credits_survive_rescans_and_files_without_musicbrainz_use_their_tags(client, library):
    fill(client)
    scan(client.app.state.api._db(), [library])  # writing the tags changed the file
    assert any(c["role"] == "producer" for c in song_named(client, "Run")["contributors"])
    plain = song_named(client, "Plain")
    assert plain["displayComposer"] == "Tag Composer"
    assert {(c["role"], c["artist"]["name"]) for c in plain["contributors"]} == {("composer", "Tag Composer"), ("lyricist", "Tag Lyricist")}
