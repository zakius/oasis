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


def load(path: Path) -> Config:
    data = tomllib.loads(path.read_text())
    base = path.parent
    server = data.get("server", {})
    return Config(
        library=[(base / Path(p).expanduser()).resolve() for p in data.get("library", {}).get("paths", [])],
        users={u["name"]: u["password"] for u in data.get("users", [])},
        database=base / Path(server.get("database", "oasis.db")).expanduser(),
        host=server.get("host", "0.0.0.0"),
        port=server.get("port", 4533),
        ytm_cache=base / Path(ytm.get("cache", "cache")).expanduser() if (ytm := data.get("ytmusic", {})).get("enabled") else None,
    )
