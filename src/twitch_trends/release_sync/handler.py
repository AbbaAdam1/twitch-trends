"""AWS Lambda entry point: once a day, finds recent and upcoming game releases and rebuilds
the watchlist the collector uses."""

# IGDB knows when games come out and what genre they are. Twitch knows which category each
# game is streamed under. This job joins the two, so the collector knows which Twitch
# categories are new games worth counting in full.

import gzip
import json
import logging
import os
import time
from datetime import UTC, datetime, timedelta

import boto3

from twitch_trends.common.storage import WATCHLIST_KEY, igdb_games_key, twitch_client_from_ssm
from twitch_trends.common.twitch_client import TwitchClient

WINDOW_PAST_DAYS = 60  # long enough to watch hype fade, the point of the project
WINDOW_FUTURE_DAYS = 14  # so a game is already watched on launch day, even if dates shift
# IGDB game types that can have a launch of their own: main game, standalone expansion,
# remake, remaster, expanded game. DLC, bundles, mods and ports are left out.
GAME_TYPES = (0, 4, 8, 9, 10)
IGDB_PAGE_SIZE = 500  # IGDB's maximum
IGDB_PAUSE_SECONDS = 0.25  # IGDB allows 4 requests per second

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def fetch_releases(
    client: TwitchClient, start: datetime, end: datetime, pause: float = IGDB_PAUSE_SECONDS
) -> list[dict]:
    """Return IGDB games first released between start and end."""
    games: list[dict] = []
    offset = 0
    while True:
        # Sorting by ID keeps pages stable, so no game is skipped or repeated between pages.
        page = client.igdb(
            "games",
            "fields name,first_release_date,game_type,genres.name,hypes;"
            f" where first_release_date >= {int(start.timestamp())}"
            f" & first_release_date < {int(end.timestamp())}"
            f" & game_type = ({','.join(str(t) for t in GAME_TYPES)});"
            f" sort id asc; limit {IGDB_PAGE_SIZE}; offset {offset};",
        )
        games += page
        if len(page) < IGDB_PAGE_SIZE:
            return games
        offset += IGDB_PAGE_SIZE
        time.sleep(pause)


def build_games(client: TwitchClient, igdb_games: list[dict]) -> list[dict]:
    """Flatten IGDB games into simple rows and attach each one's Twitch category ID."""
    twitch_games = client.get_games_by_igdb_ids([game["id"] for game in igdb_games])
    twitch_id_by_igdb_id = {
        int(game["igdb_id"]): game["id"] for game in twitch_games if game.get("igdb_id")
    }
    return [
        {
            "igdb_id": game["id"],
            "name": game["name"],
            "first_release_date": datetime.fromtimestamp(game["first_release_date"], UTC).strftime(
                "%Y-%m-%d"
            ),
            "game_type": game.get("game_type"),
            "genres": [genre["name"] for genre in game.get("genres", [])],
            "hypes": game.get("hypes", 0),
            # None when Twitch has no category for the game yet. Kept, since that can change.
            "twitch_game_id": twitch_id_by_igdb_id.get(game["id"]),
        }
        for game in igdb_games
    ]


def run(
    client: TwitchClient, s3, bucket: str, now: datetime, pause: float = IGDB_PAUSE_SECONDS
) -> dict:
    """Do one sync. Takes its dependencies as arguments so tests can pass in fakes.

    Re-running on the same day overwrites that day's files, so it's always safe to retry.
    """
    start = now - timedelta(days=WINDOW_PAST_DAYS)
    end = now + timedelta(days=WINDOW_FUTURE_DAYS)
    games = build_games(client, fetch_releases(client, start, end, pause))

    game_ids = sorted({game["twitch_game_id"] for game in games if game["twitch_game_id"]})
    # An empty list almost certainly means a broken response, not a quiet month. Failing keeps
    # yesterday's good watchlist in place instead of replacing it with nothing.
    if not game_ids:
        raise RuntimeError("No releases matched a Twitch category")

    games_key = igdb_games_key(now)
    # One game per line ("JSON Lines"), which is the layout Athena reads. A single JSON list
    # would look to Athena like one enormous row.
    lines = "\n".join(json.dumps(game, separators=(",", ":")) for game in games)
    s3.put_object(
        Bucket=bucket,
        Key=games_key,
        Body=gzip.compress(lines.encode()),
        ContentType="application/gzip",
    )
    watchlist = {
        "generated_dt": f"{now.astimezone(UTC):%Y-%m-%d}",
        "window_start": f"{start.astimezone(UTC):%Y-%m-%d}",
        "window_end": f"{end.astimezone(UTC):%Y-%m-%d}",
        "game_ids": game_ids,
    }
    # Written last, so the collector never sees a watchlist without that day's games file.
    s3.put_object(
        Bucket=bucket,
        Key=WATCHLIST_KEY,
        Body=json.dumps(watchlist).encode(),
        ContentType="application/json",
    )

    summary = {"games_key": games_key, "releases": len(games), "watchlist_games": len(game_ids)}
    logger.info("Release sync done: %s", summary)
    return summary


def handler(event: dict, context: object) -> dict:
    """The function AWS calls. `event` and `context` are required by Lambda but unused here."""
    return run(
        client=twitch_client_from_ssm(boto3.client("ssm")),
        s3=boto3.client("s3"),
        bucket=os.environ["BUCKET_NAME"],
        now=datetime.now(UTC),
    )
