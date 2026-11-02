"""Users and their linked services. Subsonic's token auth needs the plain password, so passwords are stored as given."""
import hmac
import re
import secrets
import shlex
import threading
import time

SESSION_DAYS = 30


def sync_config_users(db, users: dict[str, str]):
    """Users from oasis.toml are admins; the file's password wins."""
    for name, password in users.items():
        db.execute("delete from session where user = ? and user in (select name from user where password != ?)", (name, password))
        db.execute("insert into user(name, password, admin, created) values (?, ?, 1, ?) "
                   "on conflict(name) do update set password = excluded.password, admin = 1", (name, password, time.time()))


def get(db, name: str):
    return db.execute("select * from user where name = ?", (name,)).fetchone()


def create(db, name: str, password: str, admin: bool = False):
    if not re.fullmatch(r"[\w.@-]{1,64}", name):
        raise ValueError("Use letters, digits, '.', '@', '-' or '_' in user names.")
    if get(db, name):
        raise ValueError(f"User {name} already exists.")
    if not password:
        raise ValueError("A password is needed.")
    db.execute("insert into user(name, password, admin, created) values (?, ?, ?, ?)", (name, password, int(admin), time.time()))


def delete(db, name: str):
    db.execute("delete from user where name = ?", (name,))
    db.execute("delete from session where user = ?", (name,))


def set_field(db, name: str, **values):
    """A new password signs the user out of the dashboard everywhere."""
    for column, value in values.items():
        db.execute(f"update user set {column} = ? where name = ?", (value, name))
    if "password" in values:
        db.execute("delete from session where user = ?", (name,))


def same(a: str, b: str) -> bool:
    """a == b in constant time, so the time a comparison takes tells nothing about a password."""
    return hmac.compare_digest(a.encode(), b.encode())


class Throttle:
    """Failed sign-ins per client address, in memory: after LIMIT within WINDOW seconds the address's sign-ins are
    refused unchecked until the oldest leaves the window, so passwords can't be guessed at speed."""
    LIMIT, WINDOW = 10, 60

    def __init__(self):
        self.fails: dict[str, list[float]] = {}
        self.lock = threading.Lock()

    def blocked(self, address: str) -> bool:
        with self.lock:
            return sum(t > time.time() - self.WINDOW for t in self.fails.get(address, ())) >= self.LIMIT

    def fail(self, address: str):
        with self.lock:
            since = time.time() - self.WINDOW
            self.fails = {a: kept for a, ts in self.fails.items() if (kept := [t for t in ts if t > since])}  # quiet addresses forgotten
            self.fails.setdefault(address, []).append(time.time())


def new_session(db, name: str) -> str:
    token = secrets.token_urlsafe(32)
    db.execute("insert into session values (?, ?, ?)", (token, name, time.time() + SESSION_DAYS * 86400))
    return token


def session_user(db, token: str | None):
    if not token:
        return None
    row = db.execute("select user from session where token = ? and expires > ?", (token, time.time())).fetchone()
    return get(db, row["user"]) if row else None


def youtube_headers(pasted: str) -> dict:
    """Browser headers for ytmusicapi from what the user copied out of a signed-in music.youtube.com tab: the request
    headers, a "Copy as cURL" command, or only the Cookie value."""
    text = pasted.strip()
    headers = {}
    if text.startswith("curl "):
        words = shlex.split(text.replace("\\\n", " "))
        for flag, value in zip(words, words[1:]):
            if flag in ("-H", "--header") and ":" in value:
                key, _, val = value.partition(":")
                headers[key.strip().lower()] = val.strip()
            elif flag in ("-b", "--cookie"):
                headers["cookie"] = value
    elif re.search(r"^\s*cookie\s*:", text, re.I | re.M):
        for line in text.splitlines():
            key, sep, val = line.partition(":")
            if sep and key.strip() and not key.startswith(":"):
                headers[key.strip().lower()] = val.strip()
    else:
        headers["cookie"] = text.removeprefix("Cookie:").strip()
    cookie = headers.get("cookie", "")
    if not (sapisid := re.search(r"(?:^|;\s*)__Secure-3PAPISID=([^;\s]+)", cookie)):
        raise ValueError("That has no YouTube sign-in cookie (__Secure-3PAPISID). Copy it from music.youtube.com while signed in.")
    from ytmusicapi.helpers import get_authorization
    origin = "https://music.youtube.com"
    # ytmusicapi takes headers with a SAPISIDHASH authorization for a browser sign-in and renews it per request.
    return {"cookie": cookie, "x-goog-authuser": headers.get("x-goog-authuser", "0"), "origin": origin,
            "authorization": get_authorization(sapisid[1] + " " + origin),
            "user-agent": headers.get("user-agent", "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0")}
