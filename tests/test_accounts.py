import hashlib
import re
import time
import urllib.parse

import pytest

from conftest import call, make_client, sign_in
from oasis import accounts, ytm
from oasis.lastfm import LastFm
from oasis.remote import Rejected


class FakeYtm:
    def client(self, auth):
        if "bad" in auth:
            raise ValueError("expired")
        return auth

    def forget_client(self, auth):
        pass

    def account_name(self, client):
        return "Tester"

    def match(self, track):
        return None


@pytest.fixture
def client(library, tmp_path):
    with make_client(library, tmp_path / "oasis.db", FakeYtm()) as c:
        yield c


def test_youtube_headers_from_any_copy():
    curl = "curl 'https://music.youtube.com/youtubei/v1/browse' -H 'x-goog-authuser: 1' -H 'cookie: SID=a; __Secure-3PAPISID=b' --compressed"
    assert ytm.youtube_headers(curl)["x-goog-authuser"] == "1"
    raw = "Host: music.youtube.com\nCookie: SID=a; __Secure-3PAPISID=b\nX-Goog-AuthUser: 2"
    assert ytm.youtube_headers(raw)["x-goog-authuser"] == "2"
    assert ytm.youtube_headers("SID=a; __Secure-3PAPISID=b")["cookie"] == "SID=a; __Secure-3PAPISID=b"
    with pytest.raises(ValueError):
        ytm.youtube_headers("SID=a")


def test_web_sign_in_and_link_services(client):
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    assert "Wrong user" in client.post("/login", data={"name": "admin", "password": "x"}).text
    sign_in(client)
    page = client.get("/").text
    assert "Signed in as <b>admin</b>" in page and 'href="/services"' in page and "Copy as cURL" not in page
    assert "Copy as cURL" in client.get("/services").text
    linked = client.post("/services/ytm/link", data={"headers": "SID=a; __Secure-3PAPISID=b"}).text
    assert "YouTube Music linked as Tester" in linked
    assert "Wrong" not in client.post("/services/ytm/link", data={"headers": "nothing"}).text
    assert "no YouTube sign-in cookie" in client.post("/services/ytm/link", data={"headers": "nothing"}).text
    assert "unlinked" in client.post("/services/ytm/unlink").text
    assert "last.fm/api/account/create" not in client.get("/services").text  # Last.fm is off
    assert "last.fm/api/account/create" in client.post("/services/lastfm/on").text


@pytest.fixture
def lastfm(monkeypatch):
    """Last.fm, taking the secret 'right' and the token 't'."""
    def call(self, params):
        if self.secret != "right":
            raise Rejected("Last.fm: Invalid method signature supplied")
        return {"session": {"key": "sk", "name": "zak"}} if params["method"] == "auth.getSession" else {"token": "t"}
    monkeypatch.setattr(LastFm, "call", call)


def link_lastfm(client) -> str:
    """Signs the dashboard's user in with Last.fm; returns the page it lands on."""
    location = client.get("/services/lastfm/link", follow_redirects=False).headers["location"]
    callback = urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)["cb"][0]
    return client.get(callback + "&token=t").text  # Last.fm adds its token to the callback


def test_an_admin_connects_oasis_to_lastfm_on_the_dashboard(client, lastfm, library, tmp_path):
    sign_in(client)
    client.post("/services/lastfm/on")
    assert "Invalid method signature" in client.post("/services/lastfm/app", data={"key": "k", "secret": "wrong"}).text
    assert "Sign in with Last.fm" in client.post("/services/lastfm/app", data={"key": "k", "secret": "right"}).text
    location = client.get("/services/lastfm/link", follow_redirects=False).headers["location"]
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)["api_key"] == ["k"]
    assert "start here" in client.get("/services/lastfm/callback", params={"token": "t"}).text  # another page's link
    assert "start here" in client.get("/services/lastfm/callback", params={"token": "t", "state": "forged"}).text
    assert "Linked as <b>zak</b>" in link_lastfm(client)
    with make_client(library, tmp_path / "oasis.db") as restarted:  # the key and secret are kept
        sign_in(restarted)
        restarted.post("/services/lastfm/unlink")
        assert "Linked as <b>zak</b>" in link_lastfm(restarted)


def ping_with_key(client, key: str) -> dict:
    return client.get("/rest/ping.view", params={"apiKey": key, "f": "json"}).json()["subsonic-response"]


def add_user(client, name: str):
    """A user whose own password is "pw" and whose app signs in with "app"."""
    db = client.app.state.oasis.connect()
    accounts.create(db, name, "pw")
    accounts.add_app(db, name, "tests", "app")


def test_users_make_app_passwords_for_their_apps(client):
    sign_in(client)
    assert "Created bob" in client.post("/admin/users", data={"name": "bob", "password": "pw"}).text
    assert "already exists" in client.post("/admin/users", data={"name": "bob", "password": "pw"}).text
    assert call(client, "ping", u="bob", p="app")["error"]["code"] == 40  # a user's own password is the dashboard's only
    client.cookies.clear()
    sign_in(client, "bob", "pw")
    assert "Only admins" in client.get("/admin").text
    assert "Name the app" in client.post("/account/apps", data={"name": " "}).text
    page = client.post("/account/apps", data={"name": "Phone"}).text
    secret = re.search(r"<code>([a-z2-9]{5}(?:-[a-z2-9]{5}){3})</code>", page)[1]
    assert secret not in client.get("/account/apps").text  # shown once
    assert call(client, "ping", u="bob", p=secret)["status"] == "ok"
    assert call(client, "getUser", u="bob", p=secret)["user"]["adminRole"] is False
    assert call(client, "getUsers", u="bob", p=secret)["error"]["code"] == 50
    assert "Wrong user" in client.post("/login", data={"name": "bob", "password": secret}).text  # nor the dashboard's
    assert "Password changed" in client.post("/account/password", data={"password": "new"}).text
    assert call(client, "ping", u="bob", p=secret)["status"] == "ok"  # apps stay signed in
    client.post("/account/apps", data={"name": "Laptop"})
    phone = re.search(r'/account/apps/(\d+)/revoke', client.get("/account/apps").text)[1]
    assert "Revoked" in client.post(f"/account/apps/{phone}/revoke").text
    assert call(client, "ping", u="bob", p=secret)["error"]["code"] == 40
    client.cookies.clear()
    sign_in(client)
    assert client.post(f"/account/apps/{phone}/revoke").status_code == 200  # someone else's: nothing to revoke
    assert "Deleted bob" in client.post("/admin/users/bob/delete").text
    assert client.app.state.oasis.connect().execute("select count(*) from app_password where user = 'bob'").fetchone()[0] == 0
    assert "only be removed there" in client.post("/admin/users/admin/delete").text


def test_apps_cannot_manage_users_or_passwords(client):
    for method, params in (("createUser", {"username": "carol", "password": "pw", "adminRole": "true"}), ("updateUser", {"username": "admin", "password": "x"}),
                           ("deleteUser", {"username": "admin"}), ("changePassword", {"username": "admin", "password": "x"})):
        assert call(client, method, **params)["error"] == {"code": 50, "message": "Users and passwords are managed on the oasis dashboard"}
    assert [u["username"] for u in call(client, "getUsers")["users"]["user"]] == ["admin"]
    sign_in(client)


def test_no_way_sets_an_empty_password(client):
    sign_in(client)
    assert "A password is needed" in client.post("/admin/users", data={"name": "dave", "password": ""}).text
    client.post("/admin/users", data={"name": "dave", "password": "pw"})
    client.cookies.clear()
    sign_in(client, "dave", "pw")
    assert "A password is needed" in client.post("/account/password", data={"password": ""}).text
    client.cookies.clear()
    sign_in(client, "dave", "pw")


def test_a_password_from_before_app_passwords_keeps_apps_signed_in(client, library, tmp_path):
    db = client.app.state.oasis.connect()
    db.execute("insert into user(name, password, admin, created) values ('old', 'pw', 0, 0)")
    with make_client(library, tmp_path / "oasis.db") as restarted:
        assert call(restarted, "ping", u="old", p="pw")["status"] == "ok"
        assert ping_with_key(restarted, "pw")["error"]["code"] == 44  # not a key: it needs the name too
        sign_in(restarted, "old", "pw")
        assert accounts.OLD_APPS in restarted.get("/account/apps").text
    assert db.execute("select password from user where name = 'old'").fetchone()[0].startswith("scrypt$")


def test_client_links_youtube_with_its_cookie(client):
    assert call(client, "oasisLinkYouTubeMusic", cookie="SID=a; __Secure-3PAPISID=b", authUser="1")["youtubeMusic"] == {"account": "Tester"}


def test_rejected_sign_in_gives_a_short_message(client):
    error = call(client, "oasisLinkYouTubeMusic", cookie="bad; __Secure-3PAPISID=b")["error"]
    assert error["message"] == "YouTube Music didn't accept that sign-in. Sign in again and retry."


def test_status_shows_oasis_downloads_and_what_arrived_lately(client):
    db = client.app.state.oasis.connect()
    rows = [("h1", "Mili - Hue [FLAC]", "downloading", None), ("h2", "Rammstein - Mutter [FLAC]", "done", None),
            ("h3", "Mili - Hue [MPC]", "failed", "dead torrent: no seeds, no progress")]
    db.executemany("insert into download(hash, album_id, title, state, error, created, updated) values (?, 1, ?, ?, ?, strftime('%s'), strftime('%s'))", rows)
    sign_in(client)
    page = client.get("/").text
    assert 'Downloading <span class="count">1<' in page and "Mili - Hue [FLAC]" in page and "downloading on TorBox" in page
    assert '✓</span><div class="grow">Rammstein - Mutter [FLAC]' in page and "dead torrent: no seeds, no progress" in page


def test_get_user_reports_lastfm_forwarding(client, lastfm):
    assert call(client, "getUser")["user"]["scrobblingEnabled"] is False
    sign_in(client)
    client.post("/services/lastfm/on")
    client.post("/services/lastfm/app", data={"key": "k", "secret": "right"})
    assert call(client, "getUser")["user"]["scrobblingEnabled"] is False
    link_lastfm(client)
    assert call(client, "getUser")["user"]["scrobblingEnabled"] is True
    client.post("/services/lastfm/off")
    assert call(client, "getUser")["user"]["scrobblingEnabled"] is False


def test_stars_ratings_and_plays_are_each_users(client):
    add_user(client, "bob")
    song = call(client, "getRandomSongs", size=1)["randomSongs"]["song"][0]
    album_id = song["albumId"]
    call(client, "star", id=song["id"], albumId=album_id)
    call(client, "setRating", id=song["id"], rating=4)
    call(client, "scrobble", id=song["id"])
    mine, bobs = call(client, "getSong", id=song["id"])["song"], call(client, "getSong", id=song["id"], u="bob", p="app")["song"]
    assert mine["starred"] and mine["userRating"] == 4 and mine["playCount"] == 1
    assert "starred" not in bobs and "userRating" not in bobs and bobs["playCount"] == 0
    assert call(client, "getStarred2", u="bob", p="app")["starred2"] == {"artist": [], "album": [], "song": []}
    assert [a["id"] for a in call(client, "getStarred2")["starred2"]["album"]] == [album_id]
    call(client, "star", albumId=album_id, u="bob", p="app")
    call(client, "unstar", albumId=album_id)
    db = client.app.state.oasis.connect()
    assert [tuple(r) for r in db.execute("select user, kind, ref from wanted")] == [("bob", "album", album_id)]  # acquisition wants bob's star
    assert [a["id"] for a in call(client, "getStarred2", u="bob", p="app")["starred2"]["album"]] == [album_id]


def test_password_change_signs_out_other_dashboard_sessions(client, library, tmp_path):
    add_user(client, "bob")
    sign_in(client, "bob", "pw")
    with make_client(library, tmp_path / "oasis.db") as other:
        sign_in(other, "bob", "pw")
        assert "Password changed" in client.post("/account/password", data={"password": "new"}).text
        assert "Signed in as <b>bob</b>" in client.get("/").text  # the session that changed it stays signed in
        assert other.get("/", follow_redirects=False).headers["location"] == "/login"


def test_failed_sign_ins_are_throttled_per_address(client, monkeypatch):
    for _ in range(accounts.Throttle.LIMIT):
        assert call(client, "ping", p="wrong")["error"]["code"] == 40
    error = call(client, "ping")["error"]  # the right password too, until the window passes
    assert error["code"] == 0 and "Too many" in error["message"]
    response = client.post("/login", data={"name": "admin", "password": "secret"}, follow_redirects=False)
    assert response.status_code == 429 and "Too many" in response.text
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + accounts.Throttle.WINDOW)
    assert call(client, "ping")["status"] == "ok" and sign_in(client)


def test_the_dashboard_sign_in_counts_towards_the_throttle(client):
    for _ in range(accounts.Throttle.LIMIT):
        client.post("/login", data={"name": "nobody", "password": "x"})
    assert call(client, "ping")["error"]["code"] == 0


def test_an_app_password_is_also_an_api_key(client):
    assert ping_with_key(client, "secret")["status"] == "ok"
    assert call(client, "ping", apiKey="secret")["error"]["code"] == 43  # with a user name too
    assert ping_with_key(client, "nope")["error"] == {"code": 44, "message": "Invalid API key"}
    extensions = {e["name"] for e in call(client, "getOpenSubsonicExtensions")["openSubsonicExtensions"]}
    assert "apiKeyAuthentication" in extensions


def test_token_auth(client):
    assert call(client, "ping", p=None, t=hashlib.md5(b"secretsalt").hexdigest().upper(), s="salt")["status"] == "ok"
    assert call(client, "ping", p=None, t="ü" * 32, s="salt")["error"]["code"] == 40  # non-ASCII compared, not crashing
