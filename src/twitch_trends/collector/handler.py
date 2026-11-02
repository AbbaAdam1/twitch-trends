"""AWS Lambda entry point: collects one snapshot and saves it to S3."""

# AWS calls handler() every 10 minutes. It reuses the same logic as the local script, so
# only the storage differs: S3 here, the data/ folder there.

import logging
import os
from datetime import UTC, datetime

import boto3

from twitch_trends.collector.snapshot import collect_top_streams, encode_snapshot, snapshot_key
from twitch_trends.common.twitch_client import TwitchClient

METRIC_NAMESPACE = "TwitchTrends"
METRIC_NAME = "StreamsCollected"

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Lambda keeps this variable alive between runs while the function stays "warm", so most
# runs reuse the Twitch token instead of asking for a new one every 10 minutes.
_twitch_client: TwitchClient | None = None


def _get_twitch_client() -> TwitchClient:
    global _twitch_client
    if _twitch_client is None:
        ssm = boto3.client("ssm")
        client_id = ssm.get_parameter(Name=os.environ["CLIENT_ID_PARAM"])["Parameter"]["Value"]
        client_secret = ssm.get_parameter(
            Name=os.environ["CLIENT_SECRET_PARAM"], WithDecryption=True
        )["Parameter"]["Value"]
        _twitch_client = TwitchClient(client_id, client_secret)
    return _twitch_client


def run(client: TwitchClient, s3, cloudwatch, bucket: str, now: datetime) -> dict:
    """Do one collection. Takes its dependencies as arguments so tests can pass in fakes."""
    snapshot = collect_top_streams(client, now)

    # Twitch always has streams live, so an empty result means something is wrong. Failing
    # here counts as a Lambda error, and it keeps a useless file out of the dataset.
    if snapshot["stream_count"] == 0:
        raise RuntimeError("Twitch returned no streams")

    key = snapshot_key(now)
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=encode_snapshot(snapshot),
        ContentType="application/gzip",
    )

    # The "collection stopped" alarm watches this number. It's only sent after a successful
    # save, so any failure along the way shows up as missing data.
    cloudwatch.put_metric_data(
        Namespace=METRIC_NAMESPACE,
        MetricData=[
            {"MetricName": METRIC_NAME, "Value": snapshot["stream_count"], "Unit": "Count"}
        ],
    )

    summary = {
        "key": key,
        "stream_count": snapshot["stream_count"],
        "pages_fetched": snapshot["pages_fetched"],
        "truncated": snapshot["truncated"],
    }
    logger.info("Saved snapshot: %s", summary)
    return summary


def handler(event: dict, context: object) -> dict:
    """The function AWS calls. `event` and `context` are required by Lambda but unused here."""
    return run(
        client=_get_twitch_client(),
        s3=boto3.client("s3"),
        cloudwatch=boto3.client("cloudwatch"),
        bucket=os.environ["BUCKET_NAME"],
        now=datetime.now(UTC),
    )
