import urllib.parse

import pytest

from conftest import call, make_client
from oasis import accounts


class FakeYtm:
    def __init__(self):
        self.clients = {}

    def client(self, auth):
        if "bad" in auth:
            raise ValueError("expired")
        return auth

    def account_name(self, client):
        return "Tester"

    def match(self, track):
        return None


@pytest.fixture
def client(library, tmp_path):
    with make_client(library, tmp_path / "oasis.db", FakeYtm()) as c:
        yield c


def sign_in(client, name="admin", password="secret"):
    response = client.post("/login", data={"name": name, "password": password}, follow_redirects=False)
    assert response.status_code == 303
    return response


def test_youtube_headers_from_any_copy():
    curl = "curl 'https://music.youtube.com/youtubei/v1/browse' -H 'x-goog-authuser: 1' -H 'cookie: SID=a; __Secure-3PAPISID=b' --compressed"
    assert accounts.youtube_headers(curl)["x-goog-authuser"] == "1"
    raw = "Host: music.youtube.com\nCookie: SID=a; __Secure-3PAPISID=b\nX-Goog-AuthUser: 2"
    assert accounts.youtube_headers(raw)["x-goog-authuser"] == "2"
    assert accounts.youtube_headers("SID=a; __Secure-3PAPISID=b")["cookie"] == "SID=a; __Secure-3PAPISID=b"
    with pytest.raises(ValueError):
        accounts.youtube_headers("SID=a")


def test_web_sign_in_and_link_services(client):
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    assert "Wrong user" in client.post("/login", data={"name": "admin", "password": "x"}).text
    sign_in(client)
    page = client.get("/").text
    assert "Signed in as <b>admin</b>" in page and "Copy as cURL" in page
    linked = client.post("/account/youtube", data={"headers": "SID=a; __Secure-3PAPISID=b"}).text
    assert "YouTube Music linked as Tester" in linked
    assert "Wrong" not in client.post("/account/youtube", data={"headers": "nothing"}).text
    assert "no YouTube sign-in cookie" in client.post("/account/youtube", data={"headers": "nothing"}).text
    assert "unlinked" in client.post("/account/youtube/unlink").text
    assert "last.fm/api/account/create" in client.get("/").text


def test_an_admin_connects_oasis_to_lastfm_on_the_dashboard(client, monkeypatch):
    from oasis.forward import LastFm, Rejected

    def call(self, params):
        if self.secret != "right":
            raise Rejected("Last.fm: Invalid method signature supplied")
        return {"session": {"key": "sk", "name": "zak"}} if params["method"] == "auth.getSession" else {"token": "t"}
    monkeypatch.setattr(LastFm, "call", call)
    sign_in(client)
    assert "Invalid method signature" in client.post("/account/lastfm/app", data={"key": "k", "secret": "wrong"}).text
    assert "Sign in with Last.fm" in client.post("/account/lastfm/app", data={"key": "k", "secret": "right"}).text
    assert client.app.state.api.recommender.lastfm.api_key == "k"
    callback = urllib.parse.parse_qs(urllib.parse.urlsplit(client.get("/account/lastfm", follow_redirects=False).headers["location"]).query)["cb"][0]
    assert "start here" in client.get("/account/lastfm/callback", params={"token": "t"}).text  # another page's link
    assert "start here" in client.get("/account/lastfm/callback", params={"token": "t", "state": "forged"}).text
    assert "Linked as <b>zak</b>" in client.get(callback + "&token=t").text  # Last.fm adds its token to the callback
    db = client.app.state.api._db()
    assert dict(db.execute("select key, value from setting").fetchall()) == {"lastfm_key": "k", "lastfm_secret": "right"}


def test_admin_manages_users_who_then_use_subsonic(client):
    sign_in(client)
    assert "Created bob" in client.post("/admin/users", data={"name": "bob", "password": "pw"}).text
    assert "already exists" in client.post("/admin/users", data={"name": "bob", "password": "pw"}).text
    assert call(client, "ping", u="bob", p="pw")["status"] == "ok"
    assert call(client, "getUser", u="bob", p="pw")["user"]["adminRole"] is False
    assert call(client, "getUsers", u="bob", p="pw")["error"]["code"] == 50
    client.cookies.clear()
    sign_in(client, "bob", "pw")
    assert "Only admins" in client.get("/admin").text
    assert "Password changed" in client.post("/account/password", data={"password": "new"}).text
    assert call(client, "ping", u="bob", p="new")["status"] == "ok"
    client.cookies.clear()
    sign_in(client)
    assert "Deleted bob" in client.post("/admin/users/bob/delete").text
    assert call(client, "ping", u="bob", p="new")["error"]["code"] == 40
    assert "only be removed there" in client.post("/admin/users/admin/delete").text


def test_subsonic_user_management(client):
    assert call(client, "createUser", username="carol", password="enc:" + b"pw".hex(), adminRole="true")["status"] == "ok"
    assert {u["username"]: u["adminRole"] for u in call(client, "getUsers")["users"]["user"]} == {"admin": True, "carol": True}
    call(client, "changePassword", username="carol", password="pw2")
    assert call(client, "ping", u="carol", p="pw2")["status"] == "ok"
    assert call(client, "changePassword", username="admin", password="x")["error"]["code"] == 50
    call(client, "deleteUser", username="carol")
    assert [u["username"] for u in call(client, "getUsers")["users"]["user"]] == ["admin"]


def test_client_links_youtube_with_its_cookie(client):
    assert call(client, "oasisLinkYouTubeMusic", cookie="SID=a; __Secure-3PAPISID=b", authUser="1")["youtubeMusic"] == {"account": "Tester"}
    assert "error" in call(client, "oasisLinkYouTubeMusic", cookie="bad; __Secure-3PAPISID=b")


def test_rejected_sign_in_gives_a_short_message(client):
    error = call(client, "oasisLinkYouTubeMusic", cookie="bad; __Secure-3PAPISID=b")["error"]
    assert error["message"] == "YouTube Music didn't accept that sign-in. Sign in again and retry."


def test_status_shows_oasis_downloads_and_what_arrived_lately(client):
    db = client.app.state.api._db()
    rows = [("h1", "Mili - Hue [FLAC]", "downloading", None), ("h2", "Rammstein - Mutter [FLAC]", "done", None),
            ("h3", "Mili - Hue [MPC]", "failed", "dead torrent: no seeds, no progress")]
    db.executemany("insert into download(hash, album_id, title, state, error, created, updated) values (?, 1, ?, ?, ?, strftime('%s'), strftime('%s'))", rows)
    sign_in(client)
    page = client.get("/").text
    assert 'Downloading <span class="count">1<' in page and "Mili - Hue [FLAC]" in page and "downloading on TorBox" in page
    assert '✓</span><div class="grow">Rammstein - Mutter [FLAC]' in page and "dead torrent: no seeds, no progress" in page


def test_get_user_reports_lastfm_forwarding(client):
    api, db = client.app.state.api, client.app.state.api._db()
    assert call(client, "getUser")["user"]["scrobblingEnabled"] is False
    api.config.lastfm_key, api.config.lastfm_secret = "k", "s"
    assert call(client, "getUser")["user"]["scrobblingEnabled"] is False
    db.execute("update user set lastfm_session = 'sk' where name = 'admin'")
    db.commit()
    assert call(client, "getUser")["user"]["scrobblingEnabled"] is True


def test_stars_ratings_and_plays_are_each_users(client):
    call(client, "createUser", username="bob", password="pw")
    song = call(client, "getRandomSongs", size=1)["randomSongs"]["song"][0]
    album_id = song["albumId"]
    call(client, "star", id=song["id"], albumId=album_id)
    call(client, "setRating", id=song["id"], rating=4)
    call(client, "scrobble", id=song["id"])
    mine, bobs = call(client, "getSong", id=song["id"])["song"], call(client, "getSong", id=song["id"], u="bob", p="pw")["song"]
    assert mine["starred"] and mine["userRating"] == 4 and mine["playCount"] == 1
    assert "starred" not in bobs and "userRating" not in bobs and bobs["playCount"] == 0
    assert call(client, "getStarred2", u="bob", p="pw")["starred2"] == {"artist": [], "album": [], "song": []}
    assert [a["id"] for a in call(client, "getStarred2")["starred2"]["album"]] == [album_id]
    call(client, "star", albumId=album_id, u="bob", p="pw")
    call(client, "unstar", albumId=album_id)
    db = client.app.state.api._db()
    assert [tuple(r) for r in db.execute("select user, kind, ref from wanted")] == [("bob", "album", album_id)]  # acquisition wants bob's star
    assert [a["id"] for a in call(client, "getStarred2", u="bob", p="pw")["starred2"]["album"]] == [album_id]


def test_password_change_signs_out_other_dashboard_sessions(client, library, tmp_path):
    call(client, "createUser", username="bob", password="pw")
    sign_in(client, "bob", "pw")
    with make_client(library, tmp_path / "oasis.db") as other:
        sign_in(other, "bob", "pw")
        assert "Password changed" in client.post("/account/password", data={"password": "new"}).text
        assert "Signed in as <b>bob</b>" in client.get("/").text  # the session that changed it stays signed in
        assert other.get("/", follow_redirects=False).headers["location"] == "/login"
        sign_in(other, "bob", "new")
        call(client, "updateUser", username="bob", password="newer")  # by an admin
        assert other.get("/", follow_redirects=False).headers["location"] == "/login"


def test_failed_sign_ins_are_throttled_per_address(client):
    for _ in range(accounts.Throttle.LIMIT):
        assert call(client, "ping", p="wrong")["error"]["code"] == 40
    error = call(client, "ping")["error"]  # the right password too, until the window passes
    assert error["code"] == 0 and "Too many" in error["message"]
    response = client.post("/login", data={"name": "admin", "password": "secret"}, follow_redirects=False)
    assert response.status_code == 429 and "Too many" in response.text
    client.app.state.api.throttle.fails.clear()
    assert call(client, "ping")["status"] == "ok" and sign_in(client)


def test_the_dashboard_sign_in_counts_towards_the_throttle(client):
    for _ in range(accounts.Throttle.LIMIT):
        client.post("/login", data={"name": "nobody", "password": "x"})
    assert call(client, "ping")["error"]["code"] == 0


def test_an_api_key_is_not_a_supported_mechanism(client):
    error = call(client, "ping", apiKey="k", u=None, p=None)["error"]
    assert error == {"code": 42, "message": "Provided authentication mechanism not supported"}


def test_token_auth(client):
    import hashlib
    assert call(client, "ping", p=None, t=hashlib.md5(b"secretsalt").hexdigest().upper(), s="salt")["status"] == "ok"
    assert call(client, "ping", p=None, t="ü" * 32, s="salt")["error"]["code"] == 40  # non-ASCII compared, not crashing
