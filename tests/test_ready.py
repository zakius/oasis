import time

import pytest

from conftest import call, make_client, sign_in
from oasis import accounts, ready
from oasis.db import record_play
from oasis.recommend import PERIOD
from test_accounts import add_user
from test_recommend import LastFmUser

DAY = 86400


@pytest.fixture
def client(library, tmp_path):
    with make_client(library, tmp_path / "oasis.db", turned_on=["lastfm"]) as c:
        yield c


@pytest.fixture
def api(client):
    return client.app.state.oasis


def track(db, title: str) -> str:
    return db.execute("select id from track where title = ?", (title,)).fetchone()[0]


def play(db, title: str, times: int, days_ago: float, user: str = "admin"):
    for i in range(times):
        record_play(db, user, track(db, title), time.time() - days_ago * DAY - i)


def stream(db, id: str, source: str, title: str) -> str:
    """A streaming song (or one nothing plays, source missing) that no service of the test's has cached."""
    db.execute("insert into track(id, source, ref, library, title, artist, album_artist, album) values (?, ?, ?, 0, ?, 'Remote', 'Remote', 'Far')",
               (id, source, f"{source}:{id}", title))
    return id


def shown(client) -> dict:
    """The admin's ready-made playlists as apps see them, by name, with their songs' titles."""
    found = {}
    for p in call(client, "getPlaylists")["playlists"]["playlist"]:
        if p["owner"] == "oasis":
            entries = call(client, "getPlaylist", id=p["id"])["playlist"].get("entry", [])
            found[p["name"]] = [s["title"] for s in entries]
    return found


def test_top_tracks_are_the_most_played_of_the_last_three_months_that_play(api, client, monkeypatch):
    db = api.connect()
    play(db, "Two", 1, 2)
    play(db, "One", 3, 10)
    play(db, "Song A", 2, 80)
    play(db, "Song B", 9, 100)  # before the window
    stream(db, "sc-1", "soundcloud", "Cloud Song")
    stream(db, "mi-1", "missing", "Nowhere Song")
    for id in ("sc-1", "mi-1"):
        for _ in range(5):
            record_play(db, "admin", id, time.time() - DAY)
    api.ready.refresh(db, "admin")
    assert shown(client)["Your top tracks"] == ["One", "Song A", "Two"]  # SoundCloud isn't on for the user

    api.sources.turn_on(db, "admin", "soundcloud")
    monkeypatch.setattr(ready, "SIZE", 2)
    api.ready.refresh(db, "admin")
    assert shown(client)["Your top tracks"] == ["Cloud Song", "One"]
    playlist = next(p for p in call(client, "getPlaylists")["playlists"]["playlist"] if p["name"] == "Your top tracks")
    assert playlist["readonly"] is True and playlist["comment"].startswith("Your most played") and "oasisMix" not in playlist
    oasis_top = next(p for p in call(client, "getPlaylists", oasis="1")["playlists"]["playlist"] if p["name"] == "Your top tracks")
    assert oasis_top["oasisMix"] is True and oasis_top["coverArt"].startswith("al-")  # first covered song's cover
    assert call(client, "deletePlaylist", id=playlist["id"])["error"]["message"] == "oasis's playlists are read-only"
    assert db.execute("select library from track where id = 'sc-1'").fetchone()[0] == 0  # a playlist made for the user


def test_forgotten_favourites_were_played_often_but_not_for_three_months(api, client):
    db = api.connect()
    play(db, "Song B", 5, 150)
    play(db, "Song A", 6, 120)
    play(db, "Two", 9, 200)
    play(db, "Two", 1, 3)       # played again lately
    play(db, "One", 2, 200)     # too few plays
    stream(db, "sc-1", "soundcloud", "Cloud Song")
    for _ in range(8):
        record_play(db, "admin", "sc-1", time.time() - 120 * DAY)
    api.ready.refresh(db, "admin")
    found = shown(client)
    assert found["Forgotten favourites"] == ["Song A", "Song B"]
    assert found["Your top tracks"] == ["Two"]


def test_a_user_with_no_plays_gets_no_empty_playlists(api, client):
    api.ready.refresh(api.connect(), "admin")
    assert shown(client) == {}


def test_the_playlists_from_plays_are_made_again_daily(api, client):
    [job] = [j for j in api.jobs() if j.name == "ready-made playlists"]
    step = job.step
    assert job.every == DAY
    db = api.connect()
    play(db, "One", 1, 1)
    step()
    assert shown(client)["Your top tracks"] == ["One"]
    play(db, "Two", 2, 0)
    step()
    assert shown(client)["Your top tracks"] == ["Two", "One"]


def test_each_user_chooses_which_show_and_unticking_removes_one(api, client):
    db = api.connect()
    play(db, "One", 1, 1)
    play(db, "Two", 5, 200)
    add_user(client, "bob")
    play(db, "Song A", 1, 1, user="bob")
    sign_in(client)
    page = client.get("/").text
    assert "Ready-made playlists" in page and 'name="show-top" checked' in page and 'name="show-forgotten" checked' in page
    assert "show-lastfm" not in page  # no Last.fm linked
    client.post("/account/ready", data={"show-forgotten": "on"})
    assert shown(client) == {"Forgotten favourites": ["Two"]}
    assert 'name="show-top">' in client.get("/").text
    client.post("/account/ready", data={"show-forgotten": "on", "show-top": "on"})
    assert shown(client) == {"Forgotten favourites": ["Two"], "Your top tracks": ["One"]}
    client.post("/account/ready", data={})
    assert shown(client) == {}
    api.ready.refresh_all()
    assert shown(client) == {}  # the daily refresh keeps them hidden
    bob = call(client, "getPlaylists", u="bob", p="app")["playlists"]["playlist"]
    assert [p["name"] for p in bob] == ["Your top tracks"]


def test_the_lastfm_stations_come_while_the_user_can_have_them(api, client):
    db = api.connect()
    sign_in(client)
    client.post("/account/ready", data={"show-top": "on", "show-forgotten": "on"})
    assert db.execute("select count(*) from playlist where station is not null").fetchone()[0] == 0
    api.lastfm = LastFmUser()
    accounts.set_field(db, "admin", lastfm_session="s", lastfm_name="me")
    assert 'name="show-lastfm-top" checked' in client.get("/").text
    api.ready.refresh(db, "admin")
    rows = db.execute("select name, owner, radio, station, period, mix from playlist where station is not null order by station").fetchall()
    assert [tuple(r) for r in rows] == [("Last.fm loved tracks", "admin", 1, "lastfm-loved", None, "oasis:lastfm-loved"),
                                        ("Last.fm top tracks", "admin", 1, "lastfm-top", PERIOD, "oasis:lastfm-top")]
    api.recommender.top_up_all(db)  # grows as any station does
    stations = {p["name"]: p for p in call(client, "getPlaylists", oasis="1")["playlists"]["playlist"] if p.get("oasisStation")}
    assert stations["Last.fm top tracks"]["owner"] == "oasis" and stations["Last.fm top tracks"]["songCount"] > 0
    assert stations["Last.fm top tracks"]["oasisMix"] is True and stations["Last.fm loved tracks"]["oasisMix"] is True
    assert ("me", True, PERIOD) in api.lastfm.asked

    client.post("/account/ready", data={"show-lastfm-top": "on"})
    assert [r[0] for r in db.execute("select station from playlist where station is not null")] == ["lastfm-top"]
    api.ready.refresh(db, "admin")  # a refresh leaves the station as it grew
    assert db.execute("select count(*) from playlist_entry e join playlist p on p.id = e.playlist_id where p.station = 'lastfm-top'").fetchone()[0] > 0

    own = call(client, "createPlaylist", oasis="1", oasisStation="lastfm-loved", name="Mine")["playlist"]  # an app's own station
    api.turn_off(db, "admin", "lastfm")
    assert [r[0] for r in db.execute("select id from playlist where station is not null")] == [int(own["id"])]
    client.post("/account/ready", data={})
    accounts.set_field(db, "admin", lastfm_session="s", lastfm_name="me")
    api.sources.turn_on(db, "admin", "lastfm")
    api.ready.refresh(db, "admin")
    assert db.execute("select count(*) from playlist where mix like 'oasis:lastfm-%'").fetchone()[0] == 1  # loved stays hidden


def test_frequent_and_recent_albums_follow_each_users_plays(client):
    add_user(client, "bob")
    songs = {s["title"]: s["id"] for s in call(client, "search3", query="")["searchResult3"]["song"]}

    def albums(kind, **auth):
        return [a["name"] for a in call(client, "getAlbumList2", type=kind, **auth)["albumList2"]["album"]]

    call(client, "scrobble", id=[songs["One"], songs["Two"]], time=[1_700_000_000_000, 1_700_000_001_000])
    call(client, "scrobble", id=songs["Song A"], time=1_700_000_100_000)
    call(client, "scrobble", id=songs["untagged"], time=1_600_000_000_000, u="bob", p="app")
    assert albums("frequent") == ["First", "Second"] and albums("recent") == ["Second", "First"]
    assert albums("frequent", u="bob", p="app") == albums("recent", u="bob", p="app") == ["Loose"]
    old = call(client, "getAlbumList", type="recent", u="bob", p="app")["albumList"]["album"]
    assert [a["title"] for a in old] == ["Loose"]
