import argparse
import logging
import time
from pathlib import Path

import uvicorn

from . import config as configuration, scan
from .api import LIDARR_KEYS, create_app
from .db import connect
from .lidarr import Lidarr, adopt, adoptable


def main():
    parser = argparse.ArgumentParser(prog="oasis", description="OpenSubsonic server for a merged local and streaming library")
    parser.add_argument("-c", "--config", type=Path, default=Path("oasis.toml"))
    parser.add_argument("command", choices=["serve", "scan", "adopt-lidarr"], nargs="?", default="serve")
    parser.add_argument("--user", help="adopt-lidarr: who follows the artists added in Lidarr itself")
    parser.add_argument("--dry-run", action="store_true", help="adopt-lidarr: only list them")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = configuration.load(args.config)
    if args.command == "scan":
        scan.scan(connect(config.database), config.library)
    elif args.command == "adopt-lidarr":
        adopt_lidarr(config, args)
    else:
        uvicorn.run(create_app(config), host=config.host, port=config.port, access_log=False)  # Subsonic puts credentials in query strings


def adopt_lidarr(config, args):
    db = connect(config.database)
    if not config.lidarr or not args.user or not db.execute("select 1 from user where name = ?", (args.user,)).fetchone():
        raise SystemExit("adopt-lidarr needs [lidarr] in the config and --user naming an oasis user")
    lidarr = Lidarr(**{k: v for k, v in config.lidarr.items() if k in LIDARR_KEYS})
    artists = adoptable(db, lidarr)
    for a in artists:
        print(f"{a['artistName']}\t{a['foreignArtistId']}\tLidarr {a['id']}")
    if artists and not args.dry_run:
        snapshot = config.database.parent / f"adopt-lidarr-{time.strftime('%Y%m%d-%H%M%S')}.json"
        adopt(db, lidarr, args.user, artists, snapshot)
        print(f"{args.user} follows {len(artists)} artists; their Lidarr monitoring was {snapshot}")


if __name__ == "__main__":
    main()
