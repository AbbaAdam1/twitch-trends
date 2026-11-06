"""AWS Lambda entry point: turns one day of raw snapshots into Parquet tables in curated/."""

# Runs daily just after midnight UTC for the day before. Backfills call it with a chosen day.
#
# Three tables, one file each per day:
#   stream_snapshots  one row per stream per snapshot (the detail)
#   game_daily        one row per watchlist game per day (small, what the analysis reads)
#   snapshots         one row per snapshot, with its counts (for checking data quality)

import gzip
import json
import logging
import os
from collections import Counter
from datetime import UTC, date, datetime, timedelta

import boto3
import pyarrow as pa
import pyarrow.parquet as pq

from twitch_trends.collector.handler import METRIC_NAMESPACE
from twitch_trends.common.storage import curated_key, raw_day_prefix
from twitch_trends.compactor.transform import GameDaily, check_snapshot, snapshot_rows

# Parquet stores each column's type, so queries know a viewer count is a number without guessing.
TIMESTAMP = pa.timestamp("ms", tz="UTC")
STREAM_SNAPSHOTS_SCHEMA = pa.schema(
    [
        ("snapshot_ts", TIMESTAMP),
        ("stream_id", pa.string()),
        ("user_id", pa.string()),
        ("game_id", pa.string()),
        ("game_name", pa.string()),
        ("viewer_count", pa.int32()),
        ("started_at", TIMESTAMP),
        ("language", pa.string()),
        ("in_top", pa.bool_()),
        ("in_watchlist", pa.bool_()),
    ]
)
GAME_DAILY_SCHEMA = pa.schema(
    [
        ("game_id", pa.string()),
        ("game_name", pa.string()),
        ("watchlist_snapshots", pa.int32()),
        ("live_snapshots", pa.int32()),
        ("avg_viewers", pa.float64()),
        ("peak_viewers", pa.int32()),
        ("avg_streams", pa.float64()),
        ("peak_streams", pa.int32()),
    ]
)
SNAPSHOTS_SCHEMA = pa.schema(
    [
        ("snapshot_ts", TIMESTAMP),
        ("source_key", pa.string()),
        ("schema_version", pa.int32()),
        ("top_stream_count", pa.int32()),
        ("top_pages", pa.int32()),
        ("top_truncated", pa.bool_()),
        ("watchlist_generated_dt", pa.string()),
        ("watchlist_stream_count", pa.int32()),
        ("watchlist_truncated_chunks", pa.int32()),
        ("rows_written", pa.int32()),
        ("rows_rejected", pa.int32()),
    ]
)
# A day has about 2 million stream rows. Holding them all at once would need gigabytes, so
# they're written in groups of this many. Larger groups compress better and scan faster.
ROW_GROUP_ROWS = 200_000

SNAPSHOTS_METRIC = "SnapshotsCompacted"
REJECTED_METRIC = "RecordsRejected"

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def list_snapshot_keys(s3, bucket: str, dt: date) -> list[str]:
    pages = s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=raw_day_prefix(dt))
    keys = [obj["Key"] for page in pages for obj in page.get("Contents", [])]
    return sorted(key for key in keys if key.endswith(".json.gz"))


def read_snapshot(s3, bucket: str, key: str) -> object | None:
    """Download and decode one snapshot, or return None if the file is damaged."""
    with s3.get_object(Bucket=bucket, Key=key)["Body"] as body:
        data = body.read()
    try:
        return json.loads(gzip.decompress(data))
    # Bad gzip raises OSError or EOFError; bad JSON or text raises a kind of ValueError.
    except (OSError, EOFError, ValueError):
        return None


def to_parquet(rows: list[dict], schema: pa.Schema) -> bytes:
    sink = pa.BufferOutputStream()
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), sink)
    return sink.getvalue().to_pybytes()


def run(s3, cloudwatch, bucket: str, dt: date) -> dict:
    """Compact one day. Takes its dependencies as arguments so tests can pass in fakes."""
    keys = list_snapshot_keys(s3, bucket, dt)
    rejected: Counter = Counter()
    game_daily = GameDaily()
    snapshot_summaries: list[dict] = []
    rows_written = 0
    pending: list[dict] = []

    # The stream table is built in memory and uploaded at the end, so a failure part way
    # through leaves the previous version of the day untouched.
    sink = pa.BufferOutputStream()
    with pq.ParquetWriter(sink, STREAM_SNAPSHOTS_SCHEMA) as writer:
        for key in keys:
            snapshot = read_snapshot(s3, bucket, key)
            snapshot_ts = check_snapshot(snapshot, dt)
            if snapshot_ts is None:
                logger.warning("Skipping unusable snapshot %s", key)
                rejected["bad_snapshot"] += 1
                continue

            before = sum(rejected.values())
            rows = snapshot_rows(snapshot, snapshot_ts, rejected)
            watchlist = snapshot.get("watchlist") or {}
            if watchlist:
                game_daily.add_snapshot(rows)
            snapshot_summaries.append(
                {
                    "snapshot_ts": snapshot_ts,
                    "source_key": key,
                    "schema_version": snapshot["schema_version"],
                    "top_stream_count": snapshot.get("stream_count"),
                    "top_pages": snapshot.get("pages_fetched"),
                    "top_truncated": snapshot.get("truncated"),
                    "watchlist_generated_dt": watchlist.get("generated_dt"),
                    "watchlist_stream_count": watchlist.get("stream_count"),
                    "watchlist_truncated_chunks": watchlist.get("truncated_chunks"),
                    "rows_written": len(rows),
                    "rows_rejected": sum(rejected.values()) - before,
                }
            )

            rows_written += len(rows)
            pending.extend(rows)
            if len(pending) >= ROW_GROUP_ROWS:
                writer.write_table(pa.Table.from_pylist(pending, schema=STREAM_SNAPSHOTS_SCHEMA))
                pending = []
        if pending:
            writer.write_table(pa.Table.from_pylist(pending, schema=STREAM_SNAPSHOTS_SCHEMA))

    # Collection runs every 10 minutes, so a day with nothing usable means something broke.
    # Failing keeps the old files in place and counts as a Lambda error for the alarm.
    if not snapshot_summaries:
        raise RuntimeError(f"No usable snapshots for {dt} ({len(keys)} files found)")

    outputs = {
        "stream_snapshots": sink.getvalue().to_pybytes(),
        "game_daily": to_parquet(game_daily.rows(), GAME_DAILY_SCHEMA),
        "snapshots": to_parquet(snapshot_summaries, SNAPSHOTS_SCHEMA),
    }
    # Every table is written even when it's empty (game_daily before the watchlist existed),
    # so re-running a day always replaces all three.
    for table, body in outputs.items():
        s3.put_object(
            Bucket=bucket,
            Key=curated_key(table, dt),
            Body=body,
            ContentType="application/vnd.apache.parquet",
        )

    rows_rejected = sum(count for reason, count in rejected.items() if reason != "bad_snapshot")
    cloudwatch.put_metric_data(
        Namespace=METRIC_NAMESPACE,
        MetricData=[
            {"MetricName": SNAPSHOTS_METRIC, "Value": len(snapshot_summaries), "Unit": "Count"},
            {"MetricName": REJECTED_METRIC, "Value": rows_rejected, "Unit": "Count"},
        ],
    )

    summary = {
        "dt": dt.isoformat(),
        "snapshot_files": len(keys),
        "snapshots_compacted": len(snapshot_summaries),
        "rows_written": rows_written,
        "rows_rejected": rows_rejected,
        "rejected_by_reason": dict(sorted(rejected.items())),
        "watchlist_games": len(game_daily.games),
    }
    logger.info("Compacted day: %s", summary)
    return summary


def resolve_dt(event: dict, now: datetime) -> date:
    """The day to compact: {"dt": "YYYY-MM-DD"} from a backfill, otherwise yesterday (UTC)."""
    if event.get("dt"):
        return date.fromisoformat(event["dt"])
    return (now.astimezone(UTC) - timedelta(days=1)).date()


def handler(event: dict, context: object) -> dict:
    return run(
        s3=boto3.client("s3"),
        cloudwatch=boto3.client("cloudwatch"),
        bucket=os.environ["BUCKET_NAME"],
        dt=resolve_dt(event, datetime.now(UTC)),
    )
