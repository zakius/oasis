import argparse
import datetime
import logging
import os
import subprocess
from pathlib import Path

import uvicorn

from . import config as configuration, library, quality, relocate, scan
from .server import create_app
from .db import connect


def main():
    parser = argparse.ArgumentParser(prog="oasis", description="OpenSubsonic server for a merged local and streaming library")
    parser.add_argument("-c", "--config", type=Path, default=Path("oasis.toml"))
    parser.add_argument("command", choices=["serve", "scan", "relocate", "quality"], nargs="?", default="serve")
    parser.add_argument("--apply", action="store_true", help="relocate: move the files (else only plan and check the moves)")
    parser.add_argument("--out", type=Path, help="relocate: where the old and new paths go (TSV)")
    parser.add_argument("--jobs", type=int, default=quality.JOBS, help="quality: files checked at once")
    parser.add_argument("--delete-lossy", action="store_true", help="quality: delete lossy-sourced FLACs (else only list them)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = configuration.load(args.config)
    if args.command == "scan":
        scan.scan(connect(config.database), config.library)
    elif args.command == "relocate":  # stop the server first: its scan mustn't race the moves
        root, today = config.library[0], datetime.date.today().isoformat()
        out = args.out or (root / f".oasis-renames-{today}.tsv" if args.apply else config.database.parent / f"relocate-plan-{today}.tsv")
        relocate.run(connect(config.database), root, out, args.apply)
    elif args.command == "quality":  # runs beside the server, at the lowest CPU and I/O priority; resumes where it stopped
        os.nice(19)
        subprocess.run(["ionice", "-c", "3", "-p", str(os.getpid())], check=False)
        db = connect(config.database)
        library.check_quality(db, config.library, args.jobs, args.delete_lossy)
    else:
        uvicorn.run(create_app(config), host=config.host, port=config.port, access_log=False)  # Subsonic puts credentials in query strings


if __name__ == "__main__":
    main()
