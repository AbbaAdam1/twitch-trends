"""Collect one snapshot on this machine, using the Twitch credentials stored in SSM."""

import argparse
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from twitch_trends.collector.snapshot import (
    DEFAULT_MAX_PAGES,
    DEFAULT_MIN_VIEWERS,
    collect_top_streams,
    snapshot_key,
    write_local,
)
from twitch_trends.common.twitch_client import TwitchAPIError, TwitchClient

logger = logging.getLogger("collect_local")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="twitch-trends", help="AWS CLI profile")
    parser.add_argument("--output-dir", type=Path, default=Path("data"))
    parser.add_argument("--min-viewers", type=int, default=DEFAULT_MIN_VIEWERS)
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    try:
        ssm = boto3.Session(profile_name=args.profile).client("ssm")
        client_id = ssm.get_parameter(Name="/twitch-trends/client-id")["Parameter"]["Value"]
        client_secret = ssm.get_parameter(Name="/twitch-trends/client-secret", WithDecryption=True)[
            "Parameter"
        ]["Value"]

        now = datetime.now(UTC)
        snapshot = collect_top_streams(
            TwitchClient(client_id, client_secret),
            now,
            min_viewers=args.min_viewers,
            max_pages=args.max_pages,
        )
    except (BotoCoreError, ClientError, TwitchAPIError) as error:
        logger.error("Collection failed: %s", error)
        return 1

    path = write_local(snapshot, args.output_dir, snapshot_key(now))
    logger.info(
        "Wrote %s: %d streams, %d pages, truncated=%s, %d bytes",
        path,
        snapshot["stream_count"],
        snapshot["pages_fetched"],
        snapshot["truncated"],
        path.stat().st_size,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
