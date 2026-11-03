# Notes for contributors and agents

- Tracker and indexer names, their rules, and credentials (cookies, passkeys, API keys) live only in the live
  config: oasis.toml, oasis's database (the indexers page) or Prowlarr. Never put them in the repo: not in code,
  tests, fixtures, comments, docs or commit messages. Tests use made-up names (Tracker-A, Some-Tracker).
- The repo is public.
