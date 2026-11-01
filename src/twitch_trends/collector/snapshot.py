"""Builds one point-in-time snapshot of live streams and works out where to store it."""

import gzip
import json
from datetime import UTC, datetime
from pathlib import Path

from twitch_trends.common.twitch_client import TwitchClient

SCHEMA_VERSION = 1
DEFAULT_MIN_VIEWERS = 50
DEFAULT_MAX_PAGES = 20


def collect_top_streams(
    client: TwitchClient,
    now: datetime,
    min_viewers: int = DEFAULT_MIN_VIEWERS,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> dict:
    """Collect streams with at least min_viewers viewers, reading at most max_pages pages.

    `truncated` is true when max_pages cut collection short while streams above the viewer
    floor remained, so later stages can tell a capped snapshot from a complete one.
    """
    streams: dict[str, dict] = {}
    pages_fetched = 0
    truncated = False

    for page in client.iter_stream_pages():
        pages_fetched += 1
        rows = page.get("data", [])
        # Viewer counts shift while paging, so a stream can appear on two pages. Keep the first.
        for stream in rows:
            if stream["viewer_count"] >= min_viewers:
                streams.setdefault(stream["id"], stream)

        if not rows or rows[-1]["viewer_count"] < min_viewers:
            break
        if pages_fetched >= max_pages:
            truncated = bool(page.get("pagination", {}).get("cursor"))
            break

    return {
        "schema_version": SCHEMA_VERSION,
        "snapshot_ts": now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "min_viewers": min_viewers,
        "pages_fetched": pages_fetched,
        "truncated": truncated,
        "stream_count": len(streams),
        "streams": list(streams.values()),
    }


def snapshot_key(now: datetime) -> str:
    """Return the date-partitioned path for a snapshot, shared by local runs and S3."""
    utc = now.astimezone(UTC)
    return f"raw/streams/dt={utc:%Y-%m-%d}/hour={utc:%H}/{utc:%Y%m%dT%H%M%SZ}.json.gz"


def encode_snapshot(snapshot: dict) -> bytes:
    return gzip.compress(json.dumps(snapshot, separators=(",", ":")).encode())


def write_local(snapshot: dict, root: Path, key: str) -> Path:
    path = root / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encode_snapshot(snapshot))
    return path
