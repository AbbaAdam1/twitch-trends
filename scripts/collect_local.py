"""Collect one snapshot on this machine, using the Twitch credentials stored in SSM."""

import argparse  # reads options typed on the command line, like --max-pages 5
import logging  # prints status messages labeled INFO or ERROR, instead of bare print() calls
import sys  # lets us hand an exit code back to the terminal
from datetime import UTC, datetime
from pathlib import Path

import boto3  # Amazon's official Python library for talking to AWS
from botocore.exceptions import BotoCoreError, ClientError  # errors boto3 can raise

# The project's own code. This script only wires these pieces together, and the
# real logic lives in src/twitch_trends/, where the tests cover it and the AWS
# version of the collector can reuse it in the next milestone.
from twitch_trends.collector.snapshot import (
    DEFAULT_MAX_PAGES,
    DEFAULT_MIN_VIEWERS,
    collect_snapshot,
    snapshot_key,
    write_local,
)
from twitch_trends.common.twitch_client import TwitchAPIError, TwitchClient

# A named logger, so messages can be traced back to this script.
logger = logging.getLogger("collect_local")


def main() -> int:
    """Run one collection. Returns an exit code: 0 means success, 1 means failure."""

    # Step 1: read the command-line options. Each has a default, so running the
    # script with no options works. Example with options:
    #   python scripts/collect_local.py --max-pages 2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="twitch-trends", help="AWS CLI profile")
    parser.add_argument("--output-dir", type=Path, default=Path("data"))
    parser.add_argument("--min-viewers", type=int, default=DEFAULT_MIN_VIEWERS)
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    args = parser.parse_args()

    # Show INFO-level messages and above, formatted like "INFO Wrote data\...".
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    # Anything in this "try" block that fails with one of the errors listed in
    # the "except" below jumps there, instead of crashing with a long traceback.
    try:
        # Step 2: fetch the Twitch credentials from AWS SSM Parameter Store, so
        # they never have to appear in this file or anywhere on GitHub.
        # The profile is the login you set up with `aws configure sso`.
        ssm = boto3.Session(profile_name=args.profile).client("ssm")
        client_id = ssm.get_parameter(Name="/twitch-trends/client-id")["Parameter"]["Value"]
        # The secret is stored encrypted, so WithDecryption=True asks AWS to
        # decrypt it. It stays in memory only and is never printed or saved.
        client_secret = ssm.get_parameter(Name="/twitch-trends/client-secret", WithDecryption=True)[
            "Parameter"
        ]["Value"]

        # Step 3: record the current time in UTC, then collect the live streams.
        # UTC keeps dates consistent regardless of where the code runs. Your
        # laptop and AWS are in different time zones, but both will agree on UTC.
        # Locally there's no watchlist, so this collects the top streams only.
        now = datetime.now(UTC)
        snapshot = collect_snapshot(
            TwitchClient(client_id, client_secret),
            now,
            watchlist=None,
            min_viewers=args.min_viewers,
            max_pages=args.max_pages,
        )
    except (BotoCoreError, ClientError, TwitchAPIError) as error:
        # Expected kinds of failure: AWS login expired, no internet, Twitch
        # rejected the request. Show a short message and report failure.
        logger.error("Collection failed: %s", error)
        return 1

    # Step 4: save the snapshot as a compressed file under data/. That folder is
    # listed in .gitignore, so collected data never gets committed.
    path = write_local(snapshot, args.output_dir, snapshot_key(now))

    # Step 5: report what happened. truncated=True means the page limit stopped
    # collection while there were still streams above the viewer floor.
    logger.info(
        "Wrote %s: %d streams, %d pages, truncated=%s, %d bytes",
        path,
        snapshot["stream_count"],
        snapshot["pages_fetched"],
        snapshot["truncated"],
        path.stat().st_size,
    )
    return 0


# True only when this file is run directly, not when another file imports it.
# sys.exit passes main's 0 or 1 back to the terminal, and tools like CI use that
# number to tell whether the script succeeded.
if __name__ == "__main__":
    sys.exit(main())
