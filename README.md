# Twitch Trends Pipeline

A serverless AWS data pipeline answering one question: **how long does a new game's launch hype last on Twitch?**

Work in progress. The collector runs on AWS every 10 minutes, saving raw snapshots to S3. Game release dates and analysis-ready tables come next.

## Architecture

EventBridge starts a Lambda function every 10 minutes. It reads Twitch credentials from SSM Parameter Store, collects live streams from the Twitch API, and saves a gzipped JSON snapshot to S3. CloudWatch alarms send an email if snapshots stop arriving or runs keep failing. All of it is defined in code with AWS CDK (`infra/pipeline_stack.py`).

## Deploy

Requires the setup under "Run locally", Node.js, and the AWS CDK CLI, plus a one-time `cdk bootstrap`.

```
aws sso login --profile twitch-trends
cdk deploy -c alert_email=you@example.com --profile twitch-trends
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
