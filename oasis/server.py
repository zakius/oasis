"""The running oasis: the loaded config, the database, YouTube Music and every periodic job and task the workers run
(worker.py), wired once and shared by both front ends, the Subsonic API (api.py) and the dashboard (web.py), and by the
workers. What both front ends do alike lives here too:
wanting music, linking YouTube Music, SoundCloud and Bandcamp, turning services off, connecting the Last.fm app oasis
signs users in with, recording plays."""
import json
import logging
import time
from contextlib import asynccontextmanager, contextmanager

from starlette.applications import Starlette

from . import accounts, acquire, api, artists, download, doctor, enrich, forward, lastfm, latin, library, lyrics, missing, mixes, musicbrainz, playlists, \
    recommend, ready, remove, servers, sources, tasks, translate, upload, web, youtube
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
from .lastfm import elsewhere
from .sources import SoundCloud, Sources
from .torbox import from_rdtclient
from .worker import ASK_WAIT, THREADS, Answer, Job, Worker
from .upload import Uploads
from .youtube import YouTubeCatalogue
from .ytm import YouTubeMusic

log = logging.getLogger(__name__)

MISSING_IDLE = 300  # seconds between looks for songs nothing can play whose album isn't known yet
STREAMS_EVERY = 86400  # seconds between prunes of other services' cached audio
INDEX_EVERY = 60  # seconds between updates of the romanized names search matches (latin.index), besides each scan's
LASTFM_READ = 60  # seconds the Last.fm app read from the database is used: an admin may connect another in another process
LOOKUPS = {"find", "artist", "release_groups", "release_group", "editions"}  # musicbrainz.py's lookups the front ends ask workers for
CATALOGUE = {"complete_album", "artist_page", "search", "top_songs"}  # youtube.py's
PROWLARR = {"overview", "indexer", "test", "every", "save"}  # prowlarr.py's, for the indexers page


class Oasis:
    def __init__(self, config: Config, ytm=None, services=None, background=True):
        """services: every user's play-forwarding services, by name, in place of what they linked (tests). background:
        queues tasks as users act (loading a user's mixes); tests run those themselves."""
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
        self.lastfm_app, self.lastfm_read = None, 0.0
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
        self.scanner = Scanner(self.connect, config.library, config.dedupe, lambda db: tasks.wake(db, enrich.JOB))
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

    @contextmanager
    def opened(self):
        """A connection of its own, for a task."""
        db = self.connect()
        try:
            yield db
        finally:
            db.close()

    @property
    def lastfm(self):
        """oasis's Last.fm app, or None."""
        if time.time() - self.lastfm_read > LASTFM_READ:
            with self.opened() as db:
                self.lastfm_app = lastfm.app(db, self.config.lastfm_key, self.config.lastfm_secret)
            self.lastfm_read = time.time()
        return self.lastfm_app

    @lastfm.setter
    def lastfm(self, app):
        self.lastfm_app, self.lastfm_read = app, time.time()

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

    def jobs(self) -> list[Job]:
        """The periodic jobs. Acquisition's share what they learn between runs (download.py's progress), so they share a lane."""
        found = [Job(library.JOB, self.scanner.step, None), Job(forward.JOB, self.forwarder.flush, 60),
                 Job("soundcloud playlists", self.soundcloud.refresh_all, mixes.REFRESH),
                 *(Job(f"{w.name} playlists", w.refresh_all, mixes.REFRESH) for w in (self.listenbrainz_playlists, self.tidal_mixes)),
                 Job("bandcamp", self.bandcamp.refresh_all, mixes.REFRESH), Job(recommend.JOB, self.recommender.top_up_pass, recommend.EVERY),
                 Job("ready-made playlists", self.ready.refresh_all, ready.DAY), Job("missing songs", self.fill_missing, MISSING_IDLE),
                 Job("stream cache", self.prune_streams, STREAMS_EVERY), Job("upload cleanup", self.uploads.clean_up, upload.CLEAN_EVERY),
                 Job(enrich.JOB, self.enrichment.fill, enrich.IDLE), Job("search index", self.index, INDEX_EVERY)]
        if self.ytm:
            found += [Job("mix refresh", self.mixes.refresh_all, mixes.REFRESH), Job("album fill", self.complete_next_album, youtube.ALBUM_IDLE)]
        if self.acquire:
            found.append(Job(acquire.JOB, self.acquire.step, 60, "acquisition"))
        if self.downloads:
            found.append(Job("downloads poll", self.downloads.cycle, download.INTERVAL, "acquisition"))  # TorBox down: next poll
        if self.doctor:
            found.append(Job("doctor cycle", self.doctor.cycle, doctor.INTERVAL, "acquisition"))  # TorBox restarts: next cycle
        return found

    def kinds(self) -> dict:
        """The one-off tasks' functions, by kind (worker.Worker): the background ones, and the interactive ones the front
        ends ask workers for (ask), which call other services: a slow service holds up a worker's thread, not the API."""
        return {"refresh playlists": self.refresh_playlists, "now playing": self.now_playing, "youtube": self.youtube_call,
                "musicbrainz": self.musicbrainz, "lyrics": self.fetch_lyrics, "translate lyrics": self.translate_lyrics,
                "similar": self.similar, "radio": self.fill_radio, "artist info": self.artist_info, "link": self.link,
                "listenbrainz token": self.listenbrainz_token, "lastfm sign-in": self.lastfm_sign_in, "server sign-in": self.server_sign_in,
                "lastfm app": self.connect_lastfm_app, "qobuz sign-in": self.qobuz_sign_in, "prowlarr": self.prowlarr,
                "block download": self.block_download, "scan": self.scan}

    def asking(self, kind: str, key: str | None = None, /, **args) -> Answer:
        """An interactive task (kinds), its result to come; key: see tasks.add."""
        return Answer(self.connect, kind, args, self.kinds()[kind], key)

    def ask(self, kind: str, /, **args):
        """An interactive task's result, waited for up to ASK_WAIT."""
        return self.asking(kind, **args).result(ASK_WAIT)

    def now_playing(self, user: str, track_id: str, at: float):
        with self.opened() as db:
            if r := db.execute("select * from track where id = ?", (track_id,)).fetchone():
                self.forwarder.now_playing(db, user, forward.listen_for(r, at))

    def youtube_call(self, call: str, args: list):
        """One of CATALOGUE: YouTube Music's catalogue as oasis keeps it (youtube.py)."""
        if call not in CATALOGUE:
            raise ValueError(call)
        with self.opened() as db:
            return getattr(self.youtube, call)(db, *args)

    def musicbrainz(self, call: str, args: list):
        """One of LOOKUPS, cached in mb_cache as musicbrainz.py keeps them."""
        if call not in LOOKUPS:
            raise ValueError(call)
        with self.opened() as db:
            return getattr(musicbrainz, call)(*args, db=db)

    def fetch_lyrics(self, track_id: str):
        """Fetches the song's lyrics into the lyrics table (lyrics.for_track), for the front end to read."""
        with self.opened() as db:
            if r := db.execute("select * from track where id = ?", (track_id,)).fetchone():
                lyrics.found_for_track(db, r, self.youtube_music(db))

    def translate_lyrics(self, track_id: str, texts: list[str], lang: str) -> list[str] | None:
        with self.opened() as db:
            return self.translations.lines(db, track_id, texts, lang)

    def similar(self, seed: str, count: int, user: str, source: str, wait: float | None = None, unplayable: bool = False) -> list[str]:
        """Recommender.similar for the song seed (its id) and the user (their name)."""
        with self.opened() as db:
            r = db.execute("select * from track where id = ?", (seed,)).fetchone()
            return self.recommender.similar(db, r, count, accounts.get(db, user), source=source, wait=wait, unplayable=unplayable) if r else []

    def fill_radio(self, playlist: int):
        """Fills a radio just made (Recommender.top_up)."""
        with self.opened() as db:
            if row := db.execute("select * from playlist where id = ?", (playlist,)).fetchone():
                self.recommender.top_up(db, row)

    def artist_info(self, artist_id: str, name: str, user: str) -> dict:
        """What the other services say of an artist, for getArtistInfo: its YouTube Music page fetched (artist_page),
        Last.fm's info (lastfm: None when it has none or failed), the names of Deezer's related artists for a user with
        Deezer's radio (deezer)."""
        with self.opened() as db:
            self.youtube.artist_page(db, artist_id, name)
            related = []
            if self.sources.uses(db, user, "deezer", "radio"):
                try:
                    related = self.recommender.answer(("deezer artists", name), lambda: sources.Deezer().related(name))
                except Exception as e:
                    log.info("Deezer related artists failed: %s", e)
        info = None
        if self.lastfm:
            try:
                info = self.lastfm.artist_info(name)
            except Exception as e:
                log.info("Last.fm artist info failed: %s", e)
        return {"lastfm": info, "deezer": related}

    def link(self, service: str, user: str, secret) -> str:
        """A sign-in to YouTube Music, SoundCloud or Bandcamp pasted on the dashboard (link_youtube...); the account's name."""
        with self.opened() as db:
            return {"ytm": self.link_youtube, "soundcloud": self.link_soundcloud, "bandcamp": self.link_bandcamp}[service](db, user, secret)

    @staticmethod
    def listenbrainz_token(token: str, api: str | None = None) -> bool:
        return forward.ListenBrainz(token, *([api] if api else [])).valid()

    def lastfm_sign_in(self, token: str) -> list[str]:
        """The session key and account name of a Last.fm sign-in through oasis's app."""
        return list(self.lastfm.sign_in(token))

    @staticmethod
    def server_sign_in(api: str, auth: str, token: str) -> list[str]:
        """The session and account name of a sign-in to a Last.fm-like server (servers.py)."""
        return list(elsewhere(api, auth).sign_in(token))

    def connect_lastfm_app(self, key: str, secret: str):
        with self.opened() as db:
            self.set_lastfm_app(db, key, secret)

    @staticmethod
    def qobuz_sign_in(email: str, password: str, samples: bool) -> dict:
        return sources.Qobuz.link(email, password, samples)

    def prowlarr(self, call: str, args: list):
        """One of PROWLARR, for the indexers page. Prowlarr's answers keyed by indexer id come keyed by its text."""
        if call not in PROWLARR:
            raise ValueError(call)
        return getattr(self.downloads.prowlarr, call)(*args)

    def block_download(self, rg: str) -> bool:
        with self.opened() as db:
            return self.downloads.block(db, rg)

    def scan(self):
        """A scan, done on return (Scanner.now)."""
        with self.opened() as db:
            self.scanner.now(db)

    def worker(self, threads: int = THREADS) -> Worker:
        return Worker(self.connect, self.jobs(), self.kinds(), threads)

    def refresh_playlists(self, service: str, user: str):
        """Mirrors the user's playlists of a service (one of mixes.py's, or Bandcamp's), as they were just linked."""
        {"ytm": self.mixes, "soundcloud": self.soundcloud, "listenbrainz": self.listenbrainz_playlists, "tidal": self.tidal_mixes,
         "bandcamp": self.bandcamp}[service].refresh(user)

    def refresh_soon(self, db, service: str, user: str):
        """refresh_playlists in a worker."""
        if self.background:
            tasks.add(db, "refresh playlists", {"service": service, "user": user}, f"refresh playlists {service} {user}")

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

    def index(self):
        """The romanized names of library songs that came other than by a scan (mixes, imports of streams), for search."""
        with self.opened() as db:
            latin.index(db)

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
            self.acquire.wake(db)

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
        self.refresh_soon(db, "ytm", user)
        return name

    def show_youtube_playlists(self, db, user: str, shown: set[int], new_shown: bool) -> bool:
        """Shows the user's YouTube Music library playlists in shown and hides the others (mixes.show); one shown again
        has its songs read now. True when one was."""
        again = mixes.show(db, user, shown, new_shown)
        if again:
            self.refresh_soon(db, "ytm", user)
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
        self.refresh_soon(db, "soundcloud", user)
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
        self.refresh_soon(db, "bandcamp", user)
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

    def set_lastfm_app(self, db, key: str, secret: str):
        """Checks a Last.fm API key and secret, then keeps and uses them."""
        self.lastfm = lastfm.connect_app(db, key, secret)

    def forget_lastfm_app(self):
        """The Last.fm app is read again: a worker connected another."""
        self.lastfm_read = 0.0

    def played(self, db, user: str, plays: list[tuple[str, float]]):
        """Counts the user's plays of tracks (id, when), forwards them to the services the user linked and moves
        their radios on."""
        for id, played in plays:
            record_play(db, user, id, played)
            self.forwarder.enqueue(db, user, id, played)
        self.recommender.played(db, user, [id for id, _ in plays])


def create_app(config: Config, background: bool = True, ytm=None, services=None, worker: bool = True) -> Starlette:
    """background: a scan at start, and with worker the periodic jobs and tasks in threads of this process (a worker of
    its own); without, worker processes (`oasis worker`) run them. Tests drive those themselves."""
    oasis = Oasis(config, ytm, services, background)

    @asynccontextmanager
    async def lifespan(app):
        if background:
            db = oasis.connect()
            tasks.schedule(db, oasis.jobs())
            db.close()
            oasis.scanner.start()
            if worker:
                running = oasis.worker()
                running.start()
        yield
        if background and worker:
            running.stop()

    app = Starlette(routes=[*api.routes(oasis), *web.routes(oasis)], lifespan=lifespan)
    app.state.oasis = oasis
    return app
