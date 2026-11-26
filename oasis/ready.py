"""Ready-made playlists: what oasis offers each user as playlists, which every Subsonic app shows, each shown till the
user unticks it on the dashboard (ready_hidden). Two come from the user's plays here, read-only and made again daily:
their top tracks of the last WINDOW, and the favourites they haven't played for as long. The others are the Last.fm
stations (recommend.STATIONS) for users who can have them, started as an app would start one and growing as any
station does; hiding one deletes it."""
import logging
import time

from . import accounts, playlists
from .mixes import OASIS, drop, mirror
from .playlists import RADIO
from .recommend import PERIOD, STATIONS

log = logging.getLogger(__name__)

DAY = 86400
WINDOW = 91 * DAY  # three months
SIZE = 50
FAVOURITE = 5      # plays that make a song a favourite
FROM_PLAYS = {"top": ("Your top tracks", "Your most played songs of the last three months"),
              "forgotten": ("Forgotten favourites", "Songs you played often, and not for three months")}


def hidden(db, user: str) -> set[str]:
    """The ready-made playlists the user unticked."""
    return {r[0] for r in db.execute("select kind from ready_hidden where user = ?", (user,))}


class ReadyMade:
    def __init__(self, connect, recommender):
        """recommender: for the stations, and the user's services (Sources) and YouTube Music, which say what plays."""
        self.connect, self.recommender = connect, recommender

    def refresh_all(self):
        db = self.connect()
        try:
            for (user,) in db.execute("select name from user").fetchall():
                try:  # one failing leaves the others be
                    self.refresh(db, user)
                except Exception:
                    log.exception("ready-made playlists for %s failed", user)
        finally:
            db.close()

    def offered(self, db, user: str) -> list[str]:
        """The ready-made playlists the user can have, shown or not: the stations need their Last.fm."""
        return [*FROM_PLAYS, *self.recommender.stations(db, accounts.get(db, user))]

    def show(self, db, user: str, shown: set[str]):
        """Shows the offered playlists in shown and hides the other offered ones, then makes or deletes them. The choice
        of one not offered now stays for when it is."""
        for kind in self.offered(db, user):
            db.execute("delete from ready_hidden where user = ? and kind = ?", (user, kind))
            if kind not in shown:
                db.execute("insert into ready_hidden values (?, ?)", (user, kind))
        self.refresh(db, user)

    def refresh(self, db, user: str):
        """Makes the user's shown playlists from their plays again, an empty one deleted, and starts the shown stations
        they lack; the hidden ones, and stations the user can't have now, are deleted."""
        unticked = hidden(db, user)
        shown = [kind for kind in self.offered(db, user) if kind not in unticked]
        for kind, (title, comment) in FROM_PLAYS.items():
            ids = self.songs(db, user, kind) if kind in shown else []
            if ids:
                mirror(db, user, OASIS + kind, title, None, ids, comment)
            else:
                drop(db, user, OASIS + kind)
        stations = [kind for kind in shown if kind in STATIONS]
        drop(db, user, OASIS + "lastfm-", keep=[OASIS + kind for kind in stations])
        account = accounts.get(db, user)
        for kind in stations:
            if not db.execute("select 1 from playlist where owner = ? and mix = ?", (user, OASIS + kind)).fetchone():
                playlists.create(db, user, STATIONS[kind], RADIO, mix=OASIS + kind, source=account["radio_source"], station=kind,
                                 period=PERIOD if kind == "lastfm-top" else None)
                self.recommender.wake(db)

    def songs(self, db, user: str, kind: str) -> list[str]:
        """The songs of one of FROM_PLAYS for the user, most played first, those that don't play for the user left out."""
        since = time.time() - WINDOW
        if kind == "top":
            rows = db.execute("select t.id, t.source, t.ref from play p join track t on t.id = p.track_id where p.user = ? and p.played > ? "
                              "group by t.id order by count(*) desc, max(p.played) desc", (user, since))
        else:
            rows = db.execute("select t.id, t.source, t.ref from stat s join track t on t.id = s.id where s.user = ? and s.play_count >= ? "
                              "and s.played <= ? order by s.play_count desc, s.played desc", (user, FAVOURITE, since))
        services, music = self.recommender.services, self.recommender.ytm
        usable = services.usable(db, user)
        return [r["id"] for r in rows if r["source"] == "local" or services.plays(usable, music, r["source"], r["ref"])][:SIZE]
