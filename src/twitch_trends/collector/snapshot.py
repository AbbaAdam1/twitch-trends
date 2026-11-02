"""Builds one point-in-time snapshot of live streams and works out where to store it."""

# A "snapshot" is a record of what was live on Twitch at one moment. Comparing many
# snapshots over time is what will eventually show how a game's audience changes.

import gzip
import json
from datetime import UTC, datetime
from pathlib import Path

from twitch_trends.common.twitch_client import TwitchClient

# Stored in every file. If the format ever changes, later code can tell old files from new.
SCHEMA_VERSION = 1
DEFAULT_MIN_VIEWERS = 50  # ignore streams smaller than this
DEFAULT_MAX_PAGES = 20  # read at most this many pages (100 streams each) per run


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
    # A dict keyed by stream ID, which makes removing duplicates easy (see below).
    streams: dict[str, dict] = {}
    pages_fetched = 0
    truncated = False

    for page in client.iter_stream_pages():
        pages_fetched += 1
        rows = page.get("data", [])
        # Viewer counts shift while paging, so a stream can appear on two pages. Keep the first.
        for stream in rows:
            if stream["viewer_count"] >= min_viewers:
                # setdefault only adds the stream if its ID isn't already in the dict.
                streams.setdefault(stream["id"], stream)

        # Stop if the page was empty, or its last stream is below the floor. Results are
        # sorted by viewers, so every later page would be below the floor too.
        if not rows or rows[-1]["viewer_count"] < min_viewers:
            break
        # Stop at the page limit. It only counts as truncated if Twitch had more to give,
        # which a cursor tells us. bool() turns the cursor (or None) into True or False.
        if pages_fetched >= max_pages:
            truncated = bool(page.get("pagination", {}).get("cursor"))
            break

    # The stream data is kept as Twitch sent it, plus some details about the run itself.
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
    # The dt=.../hour=... folders are called partitions. Athena can use them to read only
    # the days a query asks about, which is what keeps queries cheap.
    utc = now.astimezone(UTC)
    return f"raw/streams/dt={utc:%Y-%m-%d}/hour={utc:%H}/{utc:%Y%m%dT%H%M%SZ}.json.gz"


def encode_snapshot(snapshot: dict) -> bytes:
    """Turn a snapshot into compressed JSON bytes, ready to save."""
    # separators removes the spaces JSON normally adds, making the file a little smaller.
    return gzip.compress(json.dumps(snapshot, separators=(",", ":")).encode())


def write_local(snapshot: dict, root: Path, key: str) -> Path:
    """Save a snapshot under the root folder and return the file's path."""
    path = root / key
    # Create the dt=.../hour=... folders if they don't exist yet.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encode_snapshot(snapshot))
    return path
