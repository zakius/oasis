# Workers

oasis's background work runs in workers, which take it from a task queue in oasis's database (`oasis/tasks.py`,
`oasis/worker.py`). `oasis serve` keeps a worker of its own in the same process; `oasis serve --no-worker` leaves the
work to `oasis worker` processes, as many as you like, on the same database.

## What runs where

The API (`serve`) streams, takes scrobbles and stars, and answers the Subsonic API and the dashboard from the database.

The workers run:

- **The periodic jobs**: the scan (when woken: at start, after an import, on `startScan`), dedupe and the search index
  of romanized names, MusicBrainz enrichment (credits, artists' names, Latin names), acquisition (the catalogue pass,
  the download poll, the doctor), mixes and other services' playlists, the ready-made playlists, the Bandcamp sync,
  forwarding plays and likes (the outbox), radio top-ups, finding the albums of songs nothing plays, YouTube Music album
  pages, and pruning cached audio and stale uploads. Each job runs on one worker at a time; its next run is kept in
  the database. Acquisition's three jobs stay on one worker while it runs, as they keep what they learn between runs
  (a torrent's stall timer, recent Prowlarr searches) in its memory.
- **The calls to other services that requests wait for**: YouTube Music's search, album pages and artist pages,
  MusicBrainz's searches and lookups, lyrics and their translation, similar songs and radios, artist info, every
  service sign-in, Prowlarr for the indexers page, and the scan an upload's commit waits for. The request adds an
  interactive task, which workers take before any background one, and waits for its result: the request reads the
  task's row every 2 ms, so the answer comes back within a few milliseconds of the worker finishing.

A worker runs each task in a thread of its own, up to `--threads` (32) at once. A worker's heartbeat renews its claim
on what it runs; when a worker dies, others take its tasks once the claim lapses (a minute), and its jobs once it
has missed its heartbeats for 20 seconds. A worker stopped with SIGTERM lets the others take its jobs at once, but
for those it was running, whose claims lapse first.

When no worker is alive, or none takes an interactive task within 2 seconds, the API runs the task itself. So
`serve --no-worker` alone still answers everything, but nothing runs the periodic jobs.

## Rate limits

MusicBrainz (one request a second, everyone waiting after a 5xx), Apple Music's search (20 a minute) and TorBox's
file links (one every 3 s, none for 5 minutes after a 429) count every process's requests: each service's next free
turn is a row of the `pace` table, taken in one transaction (`oasis/pace.py`).

## Deployment

Every container needs the same `/config` (the database, the cached audio in `streams`, uploads) and the library. The
workers write to the library: imports, dedupe and tags. The API writes to it only for the upload extension's commits;
mount the library read-only in the API container if you don't use that. The workers also need TorBox's downloads
folder (`[torbox] downloads`); mount it in the API container too, read-only, for the dashboard to show how much of a
download is copied.

SQLite shares the database between processes on one host only: keep every container on the same machine, with
`/config` on a local filesystem, not a network share.

```yaml
services:
  oasis:
    image: oasis
    command: ["serve", "--no-worker"]
    restart: unless-stopped
    ports:
      - "4533:4533"
    volumes:
      - ./config:/config
      - ~/Music:/music:ro
      - ./downloads:/downloads:ro

  worker:
    image: oasis
    command: ["worker"]
    restart: unless-stopped
    deploy:
      replicas: 2
    healthcheck:
      disable: true            # the image's check asks the API's port
    volumes:
      - ./config:/config
      - ~/Music:/music
      - ./downloads:/downloads
```

`oasis worker --threads N` sets how many tasks a worker runs at once.
