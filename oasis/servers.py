"""Each user's own lists of other servers that take their plays, besides Last.fm and ListenBrainz themselves: servers
that speak Last.fm's API (Libre.fm, GNU FM's) and servers that take ListenBrainz's submissions (Maloja, Koito...). Each
entry has the user's name for it, its API's address, the sign-in there and an on/off switch; plays go to every entry
switched on, each on its own (forward.py). An admin offers each protocol's list or not (sources.CAPABILITIES)."""
import json
import urllib.parse

PROTOCOLS = {"lastfm": "lastfm-servers", "listenbrainz": "listenbrainz-servers"}  # each protocol's list, as an admin offers it


def add(db, user: str, protocol: str, name: str, url: str, account: dict) -> int:
    """Adds a server, switched on: its id. name: the host when empty."""
    cursor = db.execute("insert into play_server(user, protocol, name, url, account) values (?, ?, ?, ?, ?)",
                        (user, protocol, name.strip() or urllib.parse.urlsplit(url).hostname, url, json.dumps(account)))
    return cursor.lastrowid


def entries(db, user: str, protocol: str) -> list:
    return db.execute("select * from play_server where user = ? and protocol = ? order by id", (user, protocol)).fetchall()


def switch(db, user: str, id: int, on: bool) -> bool:
    """Switches one of the user's servers on or off; False when the user has no such server."""
    return db.execute("update play_server set enabled = ? where id = ? and user = ?", (int(on), id, user)).rowcount > 0


def remove(db, user: str, id: int) -> bool:
    return db.execute("delete from play_server where id = ? and user = ?", (id, user)).rowcount > 0


def sending(db, user: str, offered: dict) -> list:
    """The user's servers switched on, of the lists an admin offers (sources.Sources.offered)."""
    return [r for r in db.execute("select * from play_server where user = ? and enabled = 1 order by id", (user,))
            if PROTOCOLS[r["protocol"]] in offered]
