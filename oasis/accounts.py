"""Users and their linked services. A user's password signs in to the dashboard, and is stored hashed. Apps sign in
with app passwords: Subsonic's token login needs the secret itself, so these are stored as given, and each is random, so
one read from the database opens oasis's API and nothing else."""
import hashlib
import hmac
import re
import secrets
import threading
import time

SESSION_DAYS = 30
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}
OLD_APPS = "Apps signed in before app passwords"  # a user's password from then, kept for the apps that have it
APP_ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"  # no 0, o, 1 or l: app passwords are typed off a screen


def hashed(password: str) -> str:
    salt = secrets.token_bytes(16)
    return f"scrypt${salt.hex()}${hashlib.scrypt(password.encode(), salt=salt, **SCRYPT).hex()}"


def verifies(stored: str, password: str) -> bool:
    """password is the one hashed() turned into stored."""
    _, salt, digest = stored.split("$")
    return same(hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), **SCRYPT).hex(), digest)


UNKNOWN = hashed(secrets.token_urlsafe())  # checked for names no user has, so they take as long as a wrong password


def hash_plain_passwords(db):
    """Users from before app passwords, whose password was stored as given: it goes on signing their apps in, as an app
    password, and becomes their dashboard password, hashed."""
    for user in db.execute("select name, password from user where password not like 'scrypt$%'").fetchall():
        add_app(db, user["name"], OLD_APPS, user["password"])
        db.execute("update user set password = ? where name = ?", (hashed(user["password"]), user["name"]))


def sync_config_users(db, users: dict[str, str]):
    """Users from oasis.toml are admins; the file's password wins."""
    for name, password in users.items():
        user = get(db, name)
        if user is None:
            db.execute("insert into user(name, password, admin, created) values (?, ?, 1, ?)", (name, hashed(password), time.time()))
            continue
        if not verifies(user["password"], password):
            set_field(db, name, password=hashed(password))
        set_field(db, name, admin=1)


CONFIG_ONLY = {"remove": "Users from oasis.toml can only be removed there", "password": "This password is set in oasis.toml",
               "admin": "Users from oasis.toml are always admins"}


def check_editable(config_users: dict[str, str], name: str, change: str):
    """Users from oasis.toml are changed there: ValueError saying so when change ("remove", "password" or "admin") is
    asked of one of them anywhere else."""
    if name in config_users:
        raise ValueError(CONFIG_ONLY[change])


def get(db, name: str):
    return db.execute("select * from user where name = ?", (name,)).fetchone()


def create(db, name: str, password: str, admin: bool = False):
    if not re.fullmatch(r"[\w.@-]{1,64}", name):
        raise ValueError("Use letters, digits, '.', '@', '-' or '_' in user names.")
    if get(db, name):
        raise ValueError(f"User {name} already exists.")
    check_password(password)
    db.execute("insert into user(name, password, admin, created) values (?, ?, ?, ?)", (name, hashed(password), int(admin), time.time()))


def check_password(password: str):
    """ValueError unless there is a password."""
    if not password:
        raise ValueError("A password is needed.")


def set_password(db, name: str, password: str):
    """ValueError when the password is empty (check_password). The new one signs the user out of the dashboard everywhere."""
    check_password(password)
    set_field(db, name, password=hashed(password))


def delete(db, name: str):
    db.execute("delete from user where name = ?", (name,))
    db.execute("delete from session where user = ?", (name,))
    db.execute("delete from app_password where user = ?", (name,))
    db.execute("delete from user_service where user = ?", (name,))
    db.execute("delete from play_server where user = ?", (name,))


def new_app_password(db, user: str, name: str) -> str:
    """A random password for the app called name: its secret, which the dashboard shows once. ValueError without a name."""
    name = name.strip()
    if not name:
        raise ValueError("Name the app, so you know which password to revoke later.")
    secret = "-".join("".join(secrets.choice(APP_ALPHABET) for _ in range(5)) for _ in range(4))
    add_app(db, user, name, secret)
    return secret


def add_app(db, user: str, name: str, secret: str):
    db.execute("insert into app_password(user, name, secret, created) values (?, ?, ?, ?)", (user, name, secret, time.time()))


def app_passwords(db, user: str):
    return db.execute("select id, name, created from app_password where user = ? order by created", (user,)).fetchall()


def revoke_app_password(db, user: str, id: int):
    db.execute("delete from app_password where user = ? and id = ?", (user, id))


def app_secrets(db, user: str) -> list[str]:
    return [r["secret"] for r in db.execute("select secret from app_password where user = ?", (user,))]


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
            self.fails = {a: kept for a, ts in self.fails.items() if (kept := [t for t in ts if t > since])}
            self.fails.setdefault(address, []).append(time.time())


class Refused(Exception):
    def __init__(self, throttled: bool):
        """throttled: the address failed too often lately, so the password wasn't even checked."""
        self.throttled = throttled


def client_address(request) -> str:
    return request.client.host if request.client else ""


def sign_in(db, throttle: Throttle, address: str, name: str, matches):
    """The user, when matches(user) is true; else Refused, a failure the throttle counts. matches gets None for a name
    no user has, and should take as long as for a user, so the time a sign-in takes doesn't tell which names exist."""
    if throttle.blocked(address):
        raise Refused(throttled=True)
    user = get(db, name)
    if not matches(user):
        throttle.fail(address)
        raise Refused(throttled=False)
    return user


def password_matches(password: str):
    """sign_in's matches for the dashboard: the user's own password."""
    return lambda user: verifies(user["password"] if user else UNKNOWN, password)


def sign_in_with_key(db, throttle: Throttle, address: str, key: str):
    """sign_in with an API key: any app password but a user's old password (OLD_APPS), which needed their name too."""
    rows = db.execute("select user from app_password where secret = ? and name != ?", (key, OLD_APPS)).fetchall()
    return sign_in(db, throttle, address, rows[0]["user"] if len(rows) == 1 else "", lambda user: user is not None)


def new_session(db, name: str) -> str:
    token = secrets.token_urlsafe(32)
    db.execute("insert into session values (?, ?, ?)", (token, name, time.time() + SESSION_DAYS * 86400))
    return token


def end_session(db, token: str | None):
    db.execute("delete from session where token = ?", (token,))


def session_user(db, token: str | None):
    if not token:
        return None
    row = db.execute("select user from session where token = ? and expires > ?", (token, time.time())).fetchone()
    return get(db, row["user"]) if row else None
