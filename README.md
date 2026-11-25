# splits-happen

Talk to your Garmin running data. See the math behind every number.

Open source and self-hosted, built on Garmin's official
[Connect Personal API](https://developer.garmin.com/gc-developer-program/personal/personal-api)
(read-only, pull-only).

## Status

Discovery spike. We're mapping what the API returns before designing anything.
See `DISCOVERY.md` once it lands.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12+.

```bash
cp .env.example .env   # then paste your Personal API key into .env
uv sync
```

`.env` and `data/raw/` are gitignored. Raw responses contain personal health
data and must never be committed.
