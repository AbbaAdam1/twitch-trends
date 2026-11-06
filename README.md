# Twitch Trends Pipeline

A serverless AWS data pipeline answering one question: **how long does a new game's launch hype last on Twitch?**

Work in progress. The pipeline collects data on AWS every 10 minutes. Analysis-ready tables come next.

## Architecture

- **Release sync (daily):** a Lambda function asks IGDB, Twitch's game database, for games released in the last 60 days or due in the next 14, and matches each to its Twitch category. It saves that day's release list and writes a watchlist of about 3,700 Twitch game IDs.
- **Collector (every 10 minutes):** a Lambda function collects the 2,000 most-watched streams, then every live stream for the watchlist games, including ones with no viewers. New games often start too small to appear among the top streams, so asking for them by ID is what makes it possible to measure a launch from day one. Each snapshot is saved to S3 as gzipped JSON.
- **Compactor (daily):** a Lambda function checks each record from the previous day and writes three Parquet tables under `curated/`: `stream_snapshots` (one row per stream per snapshot), `game_daily` (average and peak viewers per watchlist game) and `snapshots` (counts per snapshot, for data-quality checks). Rejected records are counted by reason and sent as a metric. Each day's tables are a single file at a fixed name, so running a day again replaces it instead of adding duplicates.
- **Query layer:** Glue tables over the curated files and the daily IGDB pull, queried with SQL in Athena. Tables use partition projection, so new days are queryable with no crawler, and the `twitch-trends` workgroup stops any query that would scan more than 1 GB. Saved queries from `sql/` appear in the Athena console.
- **Alerts:** CloudWatch alarms send an email if snapshots stop arriving, runs keep failing, or the release sync or compactor fails.

Credentials come from SSM Parameter Store. All of it is defined in code with AWS CDK (`infra/pipeline_stack.py`).

## Deploy

Requires the setup under "Run locally", Node.js, and the AWS CDK CLI, plus a one-time `cdk bootstrap`.

```
aws sso login --profile twitch-trends
cdk deploy -c alert_email=you@example.com --profile twitch-trends
```

To compact past days, for example after a fix, run the deployed compactor over a date range:

```
python scripts/backfill.py --start 2026-11-02 --end 2026-11-05
```

`cdk destroy` removes everything except the S3 bucket, because raw snapshots can't be collected again. Delete the bucket separately if you no longer need the data.

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
