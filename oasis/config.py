import dataclasses
import tomllib
from dataclasses import dataclass
from pathlib import Path


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
    lidarr: dict | None = None  # url, api_key, and optionally root_folder, quality_profile, metadata_profile
    torbox: dict | None = None  # rdt_db (rdtclient's database, which holds the TorBox key), stall_hours, dry_run (see doctor.py),
                                # downloads, max_active, import ("oasis" or "lidarr": who imports oasis's downloads; see download.py)
    prowlarr: dict | None = None  # url, api_key, tag (the music indexers'): oasis searches them itself (see releases.py)
    acquire: dict = dataclasses.field(default_factory=dict)  # catalogue ("oasis" or "lidarr"), secondary_types (see acquire.py)
    artists: dict = dataclasses.field(default_factory=dict)  # merge, separate and related names (see artists.py)
    dedupe: bool = False        # store each audio stream once (see dedupe.py); needs the library writable, on btrfs or XFS


def load(path: Path) -> Config:
    data = tomllib.loads(path.read_text())
    base = path.parent
    server, ytm, lastfm = data.get("server", {}), data.get("ytmusic", {}), data.get("lastfm", {})
    return Config(
        library=[(base / Path(p).expanduser()).resolve() for p in data.get("library", {}).get("paths", [])],
        users={u["name"]: u["password"] for u in data.get("users", [])},
        database=base / Path(server.get("database", "oasis.db")).expanduser(),
        host=server.get("host", "0.0.0.0"),
        port=server.get("port", 4533),
        ytm_cache=base / Path(ytm.get("cache", "cache")).expanduser() if ytm.get("enabled") else None,
        lastfm_key=lastfm.get("api_key") or None,
        lastfm_secret=lastfm.get("secret") or None,
        artists=data.get("artists", {}),
        acquire=data.get("acquire", {}),
        dedupe=data.get("library", {}).get("dedupe", False),
        torbox=data["torbox"] if data.get("torbox", {}).get("rdt_db") else None,
        prowlarr=data["prowlarr"] if data.get("prowlarr", {}).get("url") and data["prowlarr"].get("api_key") else None,
        lidarr=data["lidarr"] if data.get("lidarr", {}).get("url") and data["lidarr"].get("api_key") else None,
    )
