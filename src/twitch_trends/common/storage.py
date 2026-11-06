"""Where things live in the S3 bucket, and how the Lambda functions get their credentials."""

import os
from datetime import UTC, datetime

from twitch_trends.common.twitch_client import TwitchClient

# The current list of Twitch game IDs to count in full. The release sync job rewrites it daily
# and the collector reads it on every run.
WATCHLIST_KEY = "ref/watchlist.json"


def igdb_games_key(now: datetime) -> str:
    """One file per day, so release dates and genres are kept as they were on that day.
    Release dates move often, and the analysis needs the history."""
    return f"ref/igdb/dt={now.astimezone(UTC):%Y-%m-%d}/games.json.gz"


def twitch_client_from_ssm(ssm) -> TwitchClient:
    """Build a Twitch client from the credentials in SSM Parameter Store."""
    client_id = ssm.get_parameter(Name=os.environ["CLIENT_ID_PARAM"])["Parameter"]["Value"]
    client_secret = ssm.get_parameter(Name=os.environ["CLIENT_SECRET_PARAM"], WithDecryption=True)[
        "Parameter"
    ]["Value"]
    return TwitchClient(client_id, client_secret)
