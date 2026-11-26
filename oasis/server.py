"""The running oasis: the loaded config, the database, YouTube Music and every background worker, wired once and shared
by both front ends, the Subsonic API (api.py) and the dashboard (web.py). What both front ends do alike lives here too:
wanting music, linking YouTube Music, SoundCloud and Bandcamp, turning services off, connecting the Last.fm app oasis
signs users in with, recording plays."""
import json
import logging
import threading
import time
from contextlib import asynccontextmanager

from starlette.applications import Starlette

from . import accounts, acquire, api, artists, download, forward, lastfm, missing, mixes, playlists, remove, servers, translate, web, worker, youtube
from .acquire import Acquire
from .bandcamp import Bandcamp, Fan
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
from .ready import ReadyMade
from .recommend import Recommender
from .sources import SoundCloud, Sources
from .torbox import from_rdtclient
from .upload import Uploads
from .youtube import YouTubeCatalogue
from .ytm import YouTubeMusic

log = logging.getLogger(__name__)

MISSING_IDLE = 300  # seconds between looks for songs nothing can play whose album isn't known yet


class Oasis:
    def __init__(self, config: Config, ytm=None, services=None, background=True):
        """services: every user's play-forwarding services, by name, in place of what they linked (tests). background:
        starts threads as users act (loading a user's mixes); tests run those themselves."""
        self.config, self.background = config, background
        # YouTube Music's client exists whatever an admin allows: the switch on the dashboard works while oasis runs.
        self.ytm = ytm or (YouTubeMusic(config.ytm_cache) if config.ytm_cache else None)
        # Each user's services and other streaming services' audio, cached when played
        self.sources = Sources(config.database.parent / "streams", config.ytm_allowed, self.connect)
        self.youtube = YouTubeCatalogue(self.ytm, lambda db: self.sources.allowed(db, "ytm"))
        self.throttle = accounts.Throttle()  # the API's and the dashboard's sign-ins
        self.translations = translate.Translations(translate.Google())
        db = self.connect()
        accounts.hash_plain_passwords(db)
        accounts.sync_config_users(db, config.users)
        self.lastfm = lastfm.app(db, config.lastfm_key, config.lastfm_secret)  # oasis's Last.fm app, or None
        artists.from_config(db, config.artists)
        artists.configure(db, artists.load_settings(db, config.artists))
        playlists.demix(db)
        artists.unify(db)
        db.close()
        services_for = (lambda db, user: services) if services is not None else self.user_services
        self.forwarder = Forwarder(self.connect, services_for, self.ytm)
        self.recommender = Recommender(self.connect, self.ytm, lambda: self.lastfm, self.sources)
        self.uploads = Uploads(self.connect, config.library[0], config.database.parent / "uploads")
        self.mixes = Mixes(self.connect, self.ytm, self.sources, self.youtube.fetch_soon)
        self.soundcloud = mixes.SoundCloudPlaylists(self.connect, self.sources)
        self.listenbrainz_playlists = mixes.ListenBrainzPlaylists(self.connect, self.sources, self.recommender)
        self.tidal_mixes = mixes.TidalMixes(self.connect, self.sources, self.recommender)
        self.ready = ReadyMade(self.connect, self.recommender)
        self.enrichment = Enrichment(self.connect)
        self.scanner = Scanner(self.connect, config.library, config.dedupe, self.enrichment.wake.set)
        self.bandcamp = Bandcamp(self.connect, self.sources, Importer(self.connect, config.library[0]), config.database.parent / "bandcamp",
                                 lambda db, user, ids: self.want(db, user, "album", ids, time.time()), self.scanner.start)
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

    def user_services(self, db, user: str) -> dict:
        """The services the user's plays and likes go to, by name (forward.user_services)."""
        return forward.user_services(self.lastfm, self.ytm, accounts.get(db, user), self.sources.usable(db, user),
                                     servers.sending(db, user, self.sources.offered(db)), self.sources.liking(db, user), self.recommender.kept)

    def youtube_music(self, db, user: str | None = None):
        """YouTube Music's client while an admin offers it; with user, for its full songs, while they are allowed and
        that user has it on too. Else None."""
        if not self.ytm:
            return None
        on = self.sources.uses(db, user, "ytm", "songs") if user else self.sources.allowed(db, "ytm")
        return self.ytm if on else None

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
        self.soundcloud.start()
        self.listenbrainz_playlists.start()
        self.tidal_mixes.start()
        self.bandcamp.start()
        self.recommender.start()
        self.ready.start()
        worker.start("missing songs", self.fill_missing, MISSING_IDLE)
        worker.start("stream cache", self.prune_streams, 86400)
        self.uploads.start()
        self.enrichment.start()

    def complete_next_album(self) -> float | None:
        """One album page for the album fill worker: the next comes ALBUM_GAP later, or ALBUM_IDLE when none was left."""
        db = self.connect()
        try:
            return youtube.ALBUM_GAP if self.youtube.complete_next_album(db) else None
        finally:
            db.close()

    def fill_missing(self) -> float | None:
        """Finds the albums of songs nothing can play, one MusicBrainz search at a time, till none is left (missing.py)."""
        db = self.connect()
        try:
            while missing.fill_next(db, lambda db, user, ids, value: self.want(db, user, "album", ids, value)):
                pass
        finally:
            db.close()
        return None

    def prune_streams(self):
        """Deletes the cached audio of other services' songs that no track plays any more (Sources.prune), daily."""
        db = self.connect()
        try:
            self.sources.prune(db)
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

    def remove_album(self, db, rg: str) -> int:
        """Deletes the album's files, and nobody wants it any more: every user's stars of it are taken back, and followed
        artists' catalogues leave it out. The number of files deleted."""
        for (user,) in db.execute("select name from user").fetchall():
            self.give_up(db, user, rg)
        db.execute("delete from want_album where rg = ?", (rg,))  # one that arrived is searched again for lossless
        return remove.delete(db, rg, self.config.library)

    def link_youtube(self, db, user: str, headers: dict) -> str:
        """Checks the sign-in by reading the account name, saves it with YouTube Music turned on, and loads the user's
        mixes; ValueError saying why when it can't."""
        if not (self.ytm and self.sources.allowed(db, "ytm", "account")):
            raise ValueError("YouTube Music accounts aren't offered on this server")
        auth = json.dumps(headers)
        try:
            name = self.ytm.account_name(self.ytm.client(auth))
        except Exception as e:
            self.ytm.forget_client(auth)
            log.warning("YouTube Music sign-in for %s rejected: %s", user, e)
            raise ValueError("YouTube Music didn't accept that sign-in. Sign in again and retry.")
        accounts.set_field(db, user, ytm_auth=auth, ytm_name=name)
        self.sources.turn_on(db, user, "ytm")
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

    def link_soundcloud(self, db, user: str, token: str) -> str:
        """Keeps the user's SoundCloud sign-in once SoundCloud accepts it, and mirrors their likes and playlists; the
        account's name. ValueError saying why when it can't."""
        if not (self.sources.allowed(db, "soundcloud", "signed-in") or self.sources.allowed(db, "soundcloud", "account")):
            raise ValueError("SoundCloud sign-ins aren't offered on this server")
        account = SoundCloud.link(token.strip().removeprefix("OAuth ").strip())
        self.sources.link(db, user, "soundcloud", account)
        if self.background:
            threading.Thread(target=self.soundcloud.refresh, args=(user,), daemon=True).start()
        return account["name"]

    def link_bandcamp(self, db, user: str, identity: str) -> str:
        """Keeps the user's Bandcamp sign-in once Bandcamp accepts it, and reads their purchases and wishlist; the fan's
        name. ValueError saying why when it can't."""
        if not self.sources.allowed(db, "bandcamp", "account"):
            raise ValueError("Bandcamp accounts aren't offered on this server")
        identity = identity.strip().removeprefix("identity=")
        try:
            found = Fan(identity).summary()
        except OSError as e:
            raise ValueError(f"Bandcamp didn't answer: {e}")
        name = (found.get("collection_summary") or {}).get("username") or "Bandcamp"
        self.sources.link(db, user, "bandcamp", {"identity": identity, "fan_id": found["fan_id"], "name": name})
        if self.background:
            threading.Thread(target=self.bandcamp.refresh, args=(user,), daemon=True).start()
        return name

    def unlink(self, db, user: str, name: str):
        """Forgets the user's sign-in to a service, which stays on; SoundCloud's mirrored playlists go."""
        self.sources.unlink(db, user, name)
        if name == "soundcloud":
            self.soundcloud.drop(db, user)

    def turn_off(self, db, user: str, name: str):
        """Turns the service off for the user, forgetting their sign-in to it; the playlists mirrored from it go."""
        if name == "ytm":
            self.unlink_youtube(db, user)
        elif name == "lastfm":
            accounts.set_field(db, user, lastfm_session=None, lastfm_name=None)
            self.ready.refresh(db, user)
        elif name == "listenbrainz":
            accounts.set_field(db, user, listenbrainz_token=None)
            self.listenbrainz_playlists.drop(db, user)
        elif name == "soundcloud":
            self.soundcloud.drop(db, user)
        elif name == "tidal":
            self.tidal_mixes.drop(db, user)
        self.sources.turn_off(db, user, name)

    def refresh_soon(self, worker, user: str):
        """Mirrors the user's playlists of a service in the background (one of mixes.py's), as they were just linked."""
        if self.background:
            threading.Thread(target=worker.refresh, args=(user,), daemon=True).start()

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
