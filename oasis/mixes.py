"""Each user's YouTube Music mixes and library playlists, mirrored as read-only playlists shown as owned by "YouTube
Music" and read again every REFRESH. Mixes always show; each library playlist shows or stays hidden as its owner chose
on the dashboard (playlist.hidden), and one that appears later as user.ytm_new_shown says. Each user's SoundCloud
likes and playlists are mirrored the same way (SoundCloudPlaylists), all shown, and so are the playlists ListenBrainz
makes for the user and the user's Tidal mixes (MadeFor), whose songs are found as radios find theirs."""
import html
import logging
import re

from . import accounts, playlists, worker
from .forward import ListenBrainz
from .playlists import MADE_FOR, set_entries
from .recommend import recording_of
from .sources import SoundCloud
from .youtube import remember_song
from .ytm import LIKED, MIX_PREFIX, is_mix

log = logging.getLogger(__name__)

OWNER = "YouTube Music"
SOUNDCLOUD = "soundcloud:"  # the start of playlist.mix for a playlist mirrored from SoundCloud: then its id there, or likes
LISTENBRAINZ, TIDAL = MADE_FOR
# Who the playlists mirrored from other services than YouTube Music show as owned by, by the start of their mix.
OWNERS = {SOUNDCLOUD: "SoundCloud", LISTENBRAINZ: "ListenBrainz", TIDAL: "Tidal"}
OTHERS = "".join(f" and mix not like '{prefix}%'" for prefix in OWNERS)  # SQL: a mirrored playlist is YouTube Music's
REFRESH = 6 * 3600
# YouTube Music's fixed personal mix ids in its own order (the names are localized), as in roma's Mixes.kt.
ORDER = [
    "RDTMAK5uy_kset8DisdE7LSD4TNjEVvrKRTmG7a56sY",  # My Supermix
    "RDTMAK5uy_nGQKSMIkpr4o9VI_2i56pkGliD6FQRo50",  # My Mix 1
    "RDTMAK5uy_lz2owBgwWf1mjzyn_NbxzMViQzIg8IAIg",  # My Mix 2
    "RDTMAK5uy_k5UUl0lmrrfrjMpsT0CoMpdcBz1ruAO1k",  # My Mix 3
    "RDTMAK5uy_nTsa0Irmcu2li2-qHBoZxtrpG9HuC3k_Q",  # My Mix 4
    "RDTMAK5uy_lfZhS7zmIcmUhsKtkWylKzc0EN0LW90-s",  # My Mix 5
    "RDTMAK5uy_k78ni6Y4fyyl0r2eiKkBEICh9Q5wJdfXk",  # My Mix 6
    "RDTMAK5uy_lfhhWWw9v71CPrR7MRMHgZzbH6Vku9iJc",  # My Mix 7
    "RDTMAK5uy_n_5IN6hzAOwdCnM8D8rzrs3vDl12UcZpA",  # Discover Mix
    "RDTMAK5uy_lr0LWzGrq6FU9GIxWvFHTRPQD2LHMqlFA",  # New Release Mix
    "RDTMAK5uy_nilrsVWxrKskY0ZUpVZ3zpB0u4LwWTVJ4",  # Replay Mix
]


def owner(mix: str) -> str:
    """Who a mirrored playlist shows as owned by."""
    return next((name for prefix, name in OWNERS.items() if mix.startswith(prefix)), OWNER)


def from_youtube(mix: str | None) -> bool:
    """Whether a playlist is mirrored from YouTube Music."""
    return bool(mix) and not mix.startswith(tuple(OWNERS))


def mirror(db, user: str, mix: str, title: str, cover: str | None, ids: list[str], comment: str | None = None):
    """Keeps a service's playlist as the user's read-only playlist of that mix, with those songs."""
    row = db.execute("select id from playlist where owner = ? and mix = ?", (user, mix)).fetchone()
    if row:
        playlists.rename(db, row["id"], title)
        db.execute("update playlist set cover = ?, comment = ? where id = ?", (cover, comment, row["id"]))
        playlist_id = row["id"]
    else:
        playlist_id = playlists.create(db, user, title, comment=comment, mix=mix, cover=cover)
    set_entries(db, playlist_id, ids)


def drop(db, user: str, prefix: str, keep=()):
    """Deletes the user's playlists mirrored from the service whose mixes start with prefix, but those in keep."""
    for gone in db.execute(f"select id from playlist where owner = ? and mix like '{prefix}%' and mix not in ({','.join('?' * len(keep))})",
                           (user, *keep)).fetchall():
        playlists.delete(db, gone["id"])


def playlist_order(r) -> tuple:
    """Mixes first in YouTube Music's order, then Liked Music, then everything else by name."""
    mix = r["mix"] or ""
    if is_mix(mix):
        return 0, ORDER.index(mix) if mix in ORDER else len(ORDER), r["name"].casefold()
    return (1 if mix == "LM" else 2), 0, r["name"].casefold()


class Mixes:
    def __init__(self, connect, ytm, services, fetch=None):
        """services: each user's services (sources.Sources): only users with YouTube Music's account features have mixes
        read. fetch(video
        id): for set_entries, the streaming tracks to download."""
        self.connect, self.ytm, self.services, self.fetch = connect, ytm, services, fetch

    def start(self):
        worker.start("mix refresh", self.refresh_all, REFRESH)

    def refresh_all(self):
        db = self.connect()
        users = [r["name"] for r in db.execute("select name from user where ytm_auth is not null")]
        db.close()
        for user in users:
            self.refresh(user)

    def refresh(self, user: str):
        """Mirrors the user's mixes and library playlists, while the user has YouTube Music's account features; a
        failure is logged and leaves them as they were. A library playlist seen first is shown or hidden as the user's
        ytm_new_shown says (Liked Music is shown); a hidden one's songs aren't read till it is shown."""
        db = self.connect()
        try:
            account = accounts.get(db, user)
            if not (self.ytm and account and account["ytm_auth"] and self.services.uses(db, user, "ytm", "account")):
                return
            client = self.ytm.client(account["ytm_auth"])
            mixes = self.ytm.playlists(client)
            for mix in mixes:
                found = db.execute("select id, hidden from playlist where owner = ? and mix = ?", (user, mix["id"])).fetchone()
                if found:
                    playlist_id, hidden = found["id"], found["hidden"]
                    playlists.rename(db, playlist_id, mix["title"])
                    db.execute("update playlist set comment = ?, cover = ? where id = ?", (mix["description"], mix["cover"], playlist_id))
                else:
                    hidden = int(not (is_mix(mix["id"]) or mix["id"] == LIKED or account["ytm_new_shown"]))
                    playlist_id = playlists.create(db, user, mix["title"], comment=mix["description"], mix=mix["id"], cover=mix["cover"],
                                                   hidden=hidden)
                if not hidden:
                    tracks = list(dict.fromkeys(remember_song(db, song) for song in self.ytm.playlist_tracks(client, mix["id"])))
                    set_entries(db, playlist_id, tracks, self.fetch)
            self.drop(db, user, keep=[m["id"] for m in mixes])
            log.info("mixes for %s: %d refreshed", user, len(mixes))
        except Exception as e:
            log.warning("mix refresh for %s failed: %s", user, e)
        finally:
            db.close()

    def drop(self, db, user, keep=()):
        """Deletes the user's mirrored YouTube Music playlists but those in keep."""
        for gone in db.execute(f"select id from playlist where owner = ? and mix is not null{OTHERS} "
                               f"and mix not in ({','.join('?' * len(keep))})", (user, *keep)).fetchall():
            playlists.delete(db, gone["id"], self.fetch)


class SoundCloudPlaylists:
    """Each user's SoundCloud likes and playlists (their own and those they liked), mirrored while they can use
    SoundCloud's account features, with the songs SoundCloud plays them in full (with Go+ songs only for its signed-in
    songs)."""

    def __init__(self, connect, services):
        """services: each user's services (sources.Sources)."""
        self.connect, self.services = connect, services

    def start(self):
        worker.start("soundcloud playlists", self.refresh_all, REFRESH)

    def refresh_all(self):
        db = self.connect()
        users = [r[0] for r in db.execute("select user from user_service where service = 'soundcloud' and account is not null")]
        db.close()
        for user in users:
            self.refresh(user)

    def refresh(self, user: str):
        """Mirrors the user's likes and playlists; a failure is logged and leaves them as they were."""
        db = self.connect()
        try:
            account = self.services.account(db, user, "soundcloud")
            if not account:
                return
            reader = SoundCloud(account)
            player = reader if self.services.uses(db, user, "soundcloud", "signed-in") else SoundCloud()
            found = [("likes", "SoundCloud likes", None, reader.likes())]
            found += [(str(p["id"]), p.get("title") or str(p["id"]), p.get("artwork_url"), reader.playlist_tracks(p["id"])) for p in reader.playlists()]
            for key, title, cover, tracks in found:
                ids = list(dict.fromkeys(self.services.remember(db, player, player.song(t)) for t in tracks if player.full(t)))
                mirror(db, user, SOUNDCLOUD + key, title, cover, ids)
            self.drop(db, user, keep=[SOUNDCLOUD + key for key, *_ in found])
            log.info("soundcloud playlists for %s: %d refreshed", user, len(found))
        except Exception as e:
            log.warning("soundcloud playlists for %s failed: %s", user, e)
        finally:
            db.close()

    def drop(self, db, user: str, keep=()):
        """Deletes the user's mirrored SoundCloud playlists but those in keep."""
        drop(db, user, SOUNDCLOUD, keep)


class MadeFor:
    """The playlists a service makes for each user, mirrored while the user can use that service's account features:
    ListenBrainz's (Weekly Jams, Weekly Exploration, Daily Jams...) and Tidal's mixes. Their songs are found as radios
    find theirs (Recommender.resolve): from the library, else the user's streaming services; a song none has comes as
    one nothing plays for a user who sees those (user.missing_songs)."""
    name = prefix = ""  # the service, and the start of its playlists' mix

    def __init__(self, connect, services, recommender):
        """services: each user's services (sources.Sources)."""
        self.connect, self.services, self.recommender = connect, services, recommender

    def start(self):
        worker.start(f"{self.name} playlists", self.refresh_all, REFRESH)

    def refresh_all(self):
        db = self.connect()
        users = [r[0] for r in db.execute("select user from user_service where service = ?", (self.name,))]
        db.close()
        for user in users:
            self.refresh(user)

    def lists(self, db, user: str) -> list[dict]:
        """The service's playlists for the user: key (the kind of playlist, or its id), title, comment, cover and songs,
        as (artist, title) or (artist, title, MusicBrainz recording id)."""
        raise NotImplementedError

    def refresh(self, user: str):
        """Mirrors the user's playlists; a failure is logged and leaves them as they were."""
        db = self.connect()
        try:
            if not self.services.uses(db, user, self.name, "account"):
                return
            found = self.lists(db, user)
            unplayable = bool(accounts.get(db, user)["missing_songs"])
            for p in found:
                ids = list(dict.fromkeys(self.recommender.resolve(db, p["songs"], user, unplayable=unplayable)))
                mirror(db, user, self.prefix + p["key"], p["title"], p["cover"], ids, p["comment"])
            drop(db, user, self.prefix, keep=[self.prefix + p["key"] for p in found])
            log.info("%s playlists for %s: %d refreshed", self.name, user, len(found))
        except Exception as e:
            log.warning("%s playlists for %s failed: %s", self.name, user, e)
        finally:
            db.close()

    def drop(self, db, user: str):
        drop(db, user, self.prefix)


class ListenBrainzPlaylists(MadeFor):
    name, prefix = "listenbrainz", LISTENBRAINZ

    def lists(self, db, user: str) -> list[dict]:
        found = []
        for p in ListenBrainz(accounts.get(db, user)["listenbrainz_token"]).made_for():
            songs = [(t["creator"], t["title"], recording_of(t)) for t in p.get("track") or [] if t.get("creator") and t.get("title")]
            found.append({"key": p["kind"], "title": p["title"], "comment": plain(p.get("annotation") or "") or None, "cover": None, "songs": songs})
        return found


def plain(annotation: str) -> str:
    """A ListenBrainz playlist's annotation, which may be HTML, as text."""
    return html.unescape(re.sub(r"<[^>]+>", " ", annotation)).strip()


class TidalMixes(MadeFor):
    name, prefix = "tidal", TIDAL

    def lists(self, db, user: str) -> list[dict]:
        client = self.services.client(db, user, "tidal")
        if client is None:
            raise LookupError("Tidal doesn't start for this user")
        return [{"key": m["id"], **m} for m in client.mixes()]


def library_playlists(db, user: str) -> list:
    """The user's mirrored YouTube Music library playlists (mixes aside), Liked Music first: id, name, mix, hidden."""
    return db.execute(f"select id, name, mix, hidden from playlist where owner = ? and mix is not null and mix not like '{MIX_PREFIX}%'{OTHERS} "
                      "order by mix != ?, name collate nocase", (user, LIKED)).fetchall()


def show(db, user: str, shown: set[int], new_shown: bool) -> bool:
    """Shows the user's library playlists in shown and hides the others, and sets whether ones that appear later show.
    The changed playlists' songs join or leave the library at once. True when one was shown again: its songs are
    read from YouTube Music by a refresh."""
    accounts.set_field(db, user, ytm_new_shown=int(new_shown))
    changed, again = [], False
    for r in library_playlists(db, user):
        hidden = int(r["id"] not in shown)
        if hidden != r["hidden"]:
            db.execute("update playlist set hidden = ? where id = ?", (hidden, r["id"]))
            changed.append(r["id"])
            again = again or not hidden
    playlists.touch(db, [t for (t,) in db.execute(f"select track_id from playlist_entry where playlist_id in ({','.join('?' * len(changed))})", changed)])
    return again
