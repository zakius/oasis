"""The running oasis: the loaded config, the database, YouTube Music and every background worker, wired once and shared
by both front ends, the Subsonic API (api.py) and the dashboard (web.py). What both front ends do alike lives here too:
wanting music, linking YouTube Music, connecting the Last.fm app oasis signs users in with, recording plays."""
import json
import logging
import threading
from contextlib import asynccontextmanager

from starlette.applications import Starlette

from . import accounts, acquire, api, artists, download, forward, lastfm, mixes, playlists, translate, web, worker, youtube
from .acquire import Acquire
from .config import Config
from .enrich import Enrichment
from .db import connect, record_play, set_stat
from .doctor import Doctor
from .download import Downloads
from .forward import Forwarder
from .importer import Importer
from .library import Scanner
from .mixes import Mixes
from .prowlarr import Prowlarr
from .recommend import Recommender
from .torbox import from_rdtclient
from .upload import Uploads
from .youtube import YouTubeCatalogue
from .ytm import YouTubeMusic

log = logging.getLogger(__name__)


class Oasis:
    def __init__(self, config: Config, ytm=None, services=None, background=True):
        """services: every user's play-forwarding services, by name, in place of what they linked (tests). background:
        starts threads as users act (loading a user's mixes); tests run those themselves."""
        self.config, self.background = config, background
        self.ytm = ytm or (YouTubeMusic(config.ytm_cache) if config.ytm_cache else None)
        self.youtube = YouTubeCatalogue(self.ytm)
        self.throttle = accounts.Throttle()  # the API's and the dashboard's sign-ins
        self.translations = translate.Translations(translate.Google())
        db = self.connect()
        accounts.sync_config_users(db, config.users)
        self.lastfm = lastfm.app(db, config.lastfm_key, config.lastfm_secret)  # oasis's Last.fm app, or None
        artists.configure(db, artists.load_settings(db, config.artists))
        playlists.demix(db)
        artists.unify(db)
        db.close()
        services_for = (lambda db, user: services) if services is not None else \
            (lambda db, user: forward.user_services(self.lastfm, self.ytm, accounts.get(db, user)))
        self.forwarder = Forwarder(self.connect, services_for, self.ytm)
        self.recommender = Recommender(self.connect, self.ytm, lambda: self.lastfm)
        self.uploads = Uploads(self.connect, config.library[0], config.database.parent / "uploads")
        self.mixes = Mixes(self.connect, self.ytm, self.youtube.fetch_soon)
        self.enrichment = Enrichment(self.connect)
        self.scanner = Scanner(self.connect, config.library, config.dedupe, self.enrichment.wake.set)
        torbox, prowlarr = config.torbox, config.prowlarr
        self.doctor = Doctor(self.connect, torbox, download.tracked) if torbox else None
        self.downloads = self.acquire = None
        # oasis's search, download and import: all three, or no acquisition.
        if torbox and torbox.downloads and prowlarr:
            self.downloads = Downloads(self.connect, lambda: from_rdtclient(torbox.rdt_db), Importer(self.connect, config.library[0]), Prowlarr(prowlarr),
                                       torbox, imported=self.scanner.start, seeding=config.seeding)
            self.acquire = Acquire(self.connect, self.downloads, config.acquire)

    def connect(self):
        return connect(self.config.database)

    def start(self):
        self.scanner.start()
        self.forwarder.start()
        if self.acquire:
            self.acquire.start()
        if self.doctor:
            self.doctor.start()
        if self.downloads:
            self.downloads.start()
        if self.ytm:
            self.mixes.start()
            worker.start("album fill", self.complete_next_album, youtube.ALBUM_IDLE)
        self.recommender.start()
        self.uploads.start()
        self.enrichment.start()

    def complete_next_album(self) -> float | None:
        """One album page for the album fill worker: the next comes ALBUM_GAP later, or ALBUM_IDLE when none was left."""
        db = self.connect()
        try:
            return youtube.ALBUM_GAP if self.youtube.complete_next_album(db) else None
        finally:
            db.close()

    def want(self, db, user: str, kind: str, ids: list[str], value: float | None):
        """Stars albums or artists, or takes the star back (value None): a starred album or artist is one the user
        wants in the library (see acquire.py). The dashboard's want and follow are stars too."""
        for id in ids:
            set_stat(db, user, id, "starred", value)
        acquire.want(db, user, kind, ids, bool(value))
        if self.acquire and ids:
            self.acquire.wake()

    def give_up(self, db, user: str, rg: str):
        """No more searches for the album: the user's stars of it are taken back, and its followed artist's catalogue
        leaves it out (acquire.give_up). Files already here stay."""
        self.want(db, user, "album", acquire.wanted_by(db, "album", rg, user), None)
        acquire.give_up(db, rg)

    def link_youtube(self, db, user: str, headers: dict) -> str:
        """Checks the sign-in by reading the account name, saves it, and loads the user's mixes; ValueError saying why
        when it can't."""
        if not self.ytm:
            raise ValueError("YouTube Music is not enabled on this server")
        auth = json.dumps(headers)
        try:
            name = self.ytm.account_name(self.ytm.client(auth))
        except Exception as e:
            self.ytm.forget_client(auth)
            log.warning("YouTube Music sign-in for %s rejected: %s", user, e)
            raise ValueError("YouTube Music didn't accept that sign-in. Sign in again and retry.")
        accounts.set_field(db, user, ytm_auth=auth, ytm_name=name)
        if self.background:
            threading.Thread(target=self.mixes.refresh, args=(user,), daemon=True).start()
        return name

    def show_youtube_playlists(self, db, user: str, shown: set[int], new_shown: bool) -> bool:
        """Shows the user's YouTube Music library playlists in shown and hides the others (mixes.show); one shown again
        has its songs read now. True when one was."""
        again = mixes.show(db, user, shown, new_shown)
        if again and self.background:
            threading.Thread(target=self.mixes.refresh, args=(user,), daemon=True).start()
        return again

    def unlink_youtube(self, db, user: str):
        accounts.set_field(db, user, ytm_auth=None, ytm_name=None)
        self.mixes.drop(db, user)

    def set_lastfm_app(self, db, key: str, secret: str):
        """Checks a Last.fm API key and secret, then keeps and uses them."""
        self.lastfm = lastfm.connect_app(db, key, secret)

    def played(self, db, user: str, plays: list[tuple[str, float]]):
        """Counts the user's plays of tracks (id, when), forwards them to the services the user linked and moves
        their radios on."""
        for id, played in plays:
            record_play(db, user, id, played)
            self.forwarder.enqueue(db, user, id, played)
        self.recommender.played(db, user, [id for id, _ in plays])


def create_app(config: Config, background: bool = True, ytm=None, services=None) -> Starlette:
    """background starts every worker (Oasis.start); tests drive those themselves."""
    oasis = Oasis(config, ytm, services, background)

    @asynccontextmanager
    async def lifespan(app):
        if background:
            oasis.start()
        yield

    app = Starlette(routes=[*api.routes(oasis), *web.routes(oasis)], lifespan=lifespan)
    app.state.oasis = oasis
    return app
