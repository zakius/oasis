"""oasis.toml, as loaded: read once at start and never changed while oasis runs."""
import dataclasses
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass
class TorBox:
    """[torbox]: rdtclient's database, which holds the TorBox key, and how the doctor treats stalled torrents (see
    doctor.py). With downloads, the folder TorBox's downloads land in, and [prowlarr], oasis acquires music itself
    (see download.py)."""
    rdt_db: str
    stall_hours: float = 1
    dry_run: bool = False
    downloads: str | None = None
    max_active: int = 8


@dataclass
class Prowlarr:
    """[prowlarr]: oasis searches its music indexers itself (see releases.py); tag is the Prowlarr tag they carry."""
    url: str
    api_key: str
    tag: int = 2
    ui_url: str = ""  # Prowlarr's own pages, for the indexers page's link; by default its url's port on the host oasis is reached at


@dataclass
class Acquisition:
    """[acquire] (see acquire.py): the secondary release types wanted besides none, and whether discographies and
    bundles are searched too."""
    secondary_types: tuple[str, ...] = ("Soundtrack",)  # as Lidarr's metadata profile had them
    bundles: bool = True


@dataclass
class Config:
    library: list[Path]
    users: dict[str, str]
    database: Path = Path("oasis.db")
    host: str = "0.0.0.0"
    port: int = 4533
    ytm_cache: Path | None = None
    lastfm_key: str | None = None
    lastfm_secret: str | None = None
    torbox: TorBox | None = None
    prowlarr: Prowlarr | None = None
    acquire: Acquisition = dataclasses.field(default_factory=Acquisition)
    artists: dict = dataclasses.field(default_factory=dict)  # merge, separate and related names (see artists.py)
    dedupe: bool = False        # store each audio stream once (see dedupe.py); needs the library writable, on btrfs or XFS
    seeding: dict = dataclasses.field(default_factory=dict)  # per indexer (Prowlarr's name): mode, ratio, days, min_hours, min_ratio (see download.py)


def section(kind, data: dict):
    """The keys of a table that kind knows; others are ignored."""
    return kind(**{f.name: data[f.name] for f in dataclasses.fields(kind) if f.name in data})


def load(path: Path) -> Config:
    data = tomllib.loads(path.read_text())
    base = path.parent
    server, ytm, lastfm = data.get("server", {}), data.get("ytmusic", {}), data.get("lastfm", {})
    found = Config(
        library=[(base / Path(p).expanduser()).resolve() for p in data.get("library", {}).get("paths", [])],
        users={u["name"]: u["password"] for u in data.get("users", [])},
        ytm_cache=base / Path(ytm.get("cache", "cache")).expanduser() if ytm.get("enabled") else None,
        lastfm_key=lastfm.get("api_key") or None,
        lastfm_secret=lastfm.get("secret") or None,
        artists=data.get("artists", {}),
        acquire=section(Acquisition, data.get("acquire", {})),
        dedupe=data.get("library", {}).get("dedupe", False),
        seeding=data.get("seeding", {}),
        torbox=section(TorBox, data["torbox"]) if data.get("torbox", {}).get("rdt_db") else None,
        prowlarr=section(Prowlarr, data["prowlarr"]) if data.get("prowlarr", {}).get("url") and data["prowlarr"].get("api_key") else None,
    )
    found.database = base / Path(server.get("database", found.database)).expanduser()
    found.host = server.get("host", found.host)
    found.port = server.get("port", found.port)
    return found
