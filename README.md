# Twitch Trends Pipeline

A serverless AWS data pipeline answering one question: **how long does a new game's launch hype last on Twitch?**

Work in progress. So far, the collector runs locally and is covered by tests; AWS deployment comes next.

## Run locally

Requires Python 3.12 and an AWS CLI profile that can read the Twitch credentials from SSM Parameter Store.

```
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"

python scripts/collect_local.py   # writes one snapshot under data/
pytest
ruff check .
```

Each snapshot is gzipped JSON at `raw/streams/dt=YYYY-MM-DD/hour=HH/<timestamp>.json.gz`, the same path it will use in S3.
