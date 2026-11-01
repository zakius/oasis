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
    assert "Linked as <b>zak</b>" in client.get("/account/lastfm/callback", params={"token": "t"}).text
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
    assert "Downloading 1:" in page and "Mili - Hue [FLAC]" in page and "TorBox downloading" in page
    assert "✓ Rammstein - Mutter [FLAC]" in page and "dead torrent: no seeds, no progress" in page
