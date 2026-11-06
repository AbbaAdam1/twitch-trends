"""AWS Lambda entry point: collects one snapshot and saves it to S3."""

# AWS calls handler() every 10 minutes. It reuses the same logic as the local script, so
# only the storage differs: S3 here, the data/ folder there.

import json
import logging
import os
from datetime import UTC, datetime

import boto3

from twitch_trends.collector.snapshot import collect_snapshot, encode_snapshot, snapshot_key
from twitch_trends.common.storage import WATCHLIST_KEY, twitch_client_from_ssm
from twitch_trends.common.twitch_client import TwitchClient

METRIC_NAMESPACE = "TwitchTrends"
METRIC_NAME = "StreamsCollected"
WATCHLIST_METRIC_NAME = "WatchlistStreamsCollected"

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Lambda keeps this variable alive between runs while the function stays "warm", so most
# runs reuse the Twitch token instead of asking for a new one every 10 minutes.
_twitch_client: TwitchClient | None = None


def _get_twitch_client() -> TwitchClient:
    global _twitch_client
    if _twitch_client is None:
        _twitch_client = twitch_client_from_ssm(boto3.client("ssm"))
    return _twitch_client


def load_watchlist(s3, bucket: str) -> dict | None:
    """Read the watchlist from S3, or return None if the release sync hasn't written one yet."""
    try:
        response = s3.get_object(Bucket=bucket, Key=WATCHLIST_KEY)
    except s3.exceptions.NoSuchKey:
        # Only expected before the first release sync. Top streams are still worth collecting.
        logger.warning("No watchlist at %s; collecting top streams only", WATCHLIST_KEY)
        return None
    with response["Body"] as body:
        return json.loads(body.read())


def run(client: TwitchClient, s3, cloudwatch, bucket: str, now: datetime) -> dict:
    """Do one collection. Takes its dependencies as arguments so tests can pass in fakes."""
    snapshot = collect_snapshot(client, now, load_watchlist(s3, bucket))

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

    # The "collection stopped" alarm watches the first number. It's only sent after a successful
    # save, so any failure along the way shows up as missing data. The watchlist count is for
    # charts: a sudden drop means new games are going uncounted.
    watchlist = snapshot["watchlist"]
    metrics = [{"MetricName": METRIC_NAME, "Value": snapshot["stream_count"], "Unit": "Count"}]
    if watchlist:
        metrics.append(
            {
                "MetricName": WATCHLIST_METRIC_NAME,
                "Value": watchlist["stream_count"],
                "Unit": "Count",
            }
        )
    cloudwatch.put_metric_data(Namespace=METRIC_NAMESPACE, MetricData=metrics)

    summary = {
        "key": key,
        "stream_count": snapshot["stream_count"],
        "pages_fetched": snapshot["pages_fetched"],
        "truncated": snapshot["truncated"],
        "watchlist_streams": watchlist["stream_count"] if watchlist else None,
        "watchlist_pages": watchlist["pages_fetched"] if watchlist else None,
        "watchlist_truncated_chunks": watchlist["truncated_chunks"] if watchlist else None,
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
