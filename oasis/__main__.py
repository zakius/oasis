import argparse
import logging
from pathlib import Path

import uvicorn

from . import config as configuration, forward, scan
from .api import create_app
from .db import connect


def main():
    parser = argparse.ArgumentParser(prog="oasis", description="OpenSubsonic server for a merged local and streaming library")
    parser.add_argument("-c", "--config", type=Path, default=Path("oasis.toml"))
    parser.add_argument("command", choices=["serve", "scan", "lastfm"], nargs="?", default="serve",
                        help="lastfm signs in to Last.fm for play forwarding")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = configuration.load(args.config)
    if args.command == "scan":
        scan.scan(connect(config.database), config.library)
    elif args.command == "lastfm":
        forward.lastfm_login(config, connect(config.database))
    else:
        uvicorn.run(create_app(config), host=config.host, port=config.port, access_log=False)  # Subsonic puts credentials in query strings


if __name__ == "__main__":
    main()
