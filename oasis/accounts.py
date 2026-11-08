"""Users and their linked services. Subsonic's token auth needs the plain password, so passwords are stored as given."""
import hmac
import re
import secrets
import threading
import time

SESSION_DAYS = 30


def sync_config_users(db, users: dict[str, str]):
    """Users from oasis.toml are admins; the file's password wins."""
    for name, password in users.items():
        user = get(db, name)
        if user is None:
            db.execute("insert into user(name, password, admin, created) values (?, ?, 1, ?)", (name, password, time.time()))
            continue
        if user["password"] != password:
            set_field(db, name, password=password)
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
    db.execute("insert into user(name, password, admin, created) values (?, ?, ?, ?)", (name, password, int(admin), time.time()))


def check_password(password: str):
    """ValueError unless there is a password: Subsonic's token auth would take an empty one."""
    if not password:
        raise ValueError("A password is needed.")


def set_password(db, name: str, password: str):
    """ValueError when the password is empty (check_password). The new one signs the user out of the dashboard everywhere."""
    check_password(password)
    set_field(db, name, password=password)


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
            self.fails = {a: kept for a, ts in self.fails.items() if (kept := [t for t in ts if t > since])}
            self.fails.setdefault(address, []).append(time.time())


class Refused(Exception):
    def __init__(self, throttled: bool):
        """throttled: the address failed too often lately, so the password wasn't even checked."""
        self.throttled = throttled


def client_address(request) -> str:
    return request.client.host if request.client else ""


def sign_in(db, throttle: Throttle, address: str, name: str, matches):
    """The user, when matches(their stored password) is true; else Refused, a failure the throttle counts. Unknown
    users are compared too (with ""), so the time a sign-in takes doesn't tell which names exist."""
    if throttle.blocked(address):
        raise Refused(throttled=True)
    user = get(db, name)
    ok = matches(user["password"] if user else "")
    if user is None or not ok:
        throttle.fail(address)
        raise Refused(throttled=False)
    return user


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
