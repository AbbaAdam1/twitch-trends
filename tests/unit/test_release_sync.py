import gzip
import json
from datetime import UTC, datetime

import pytest
from conftest import FakeS3, query_of

from twitch_trends.common.storage import WATCHLIST_KEY
from twitch_trends.common.twitch_client import TwitchClient
from twitch_trends.release_sync.handler import IGDB_PAGE_SIZE, fetch_releases, run

NOW = datetime(2026, 11, 6, 6, 0, 0, tzinfo=UTC)
RELEASE_TS = int(datetime(2026, 11, 1, tzinfo=UTC).timestamp())


def igdb_game(igdb_id: int, **overrides) -> dict:
    game = {
        "id": igdb_id,
        "name": f"Game {igdb_id}",
        "first_release_date": RELEASE_TS,
        "game_type": 0,
        "genres": [{"id": 31, "name": "Adventure"}],
        "hypes": 10,
    }
    return {**game, **overrides}


def queue_json(fake, payload) -> None:
    fake.api_responses.append((200, json.dumps(payload).encode()))


def client(fake) -> TwitchClient:
    return TwitchClient("id", "secret", transport=fake)


def test_igdb_query_covers_the_window_and_launch_game_types(fake_twitch):
    queue_json(fake_twitch, [igdb_game(1)])
    start, end = datetime(2026, 9, 7, tzinfo=UTC), datetime(2026, 11, 20, tzinfo=UTC)

    fetch_releases(client(fake_twitch), start, end, pause=0)

    request = fake_twitch.api_requests[0]
    query = request.data.decode()
    assert request.full_url == "https://api.igdb.com/v4/games"
    assert request.get_method() == "POST"
    assert f"first_release_date >= {int(start.timestamp())}" in query
    assert f"first_release_date < {int(end.timestamp())}" in query
    assert "game_type = (0,4,8,9,10)" in query
    assert "sort id asc" in query


def test_igdb_pages_until_a_short_page(fake_twitch):
    queue_json(fake_twitch, [igdb_game(n) for n in range(IGDB_PAGE_SIZE)])
    queue_json(fake_twitch, [igdb_game(9999)])

    games = fetch_releases(client(fake_twitch), NOW, NOW, pause=0)

    assert len(games) == IGDB_PAGE_SIZE + 1
    assert "offset 500;" in fake_twitch.api_requests[1].data.decode()


def test_writes_daily_games_file_and_watchlist_of_matched_games(fake_twitch):
    queue_json(fake_twitch, [igdb_game(1), igdb_game(2), igdb_game(3)])
    # Twitch has categories for games 1 and 3 only.
    queue_json(
        fake_twitch, {"data": [{"id": "900", "igdb_id": "1"}, {"id": "300", "igdb_id": "3"}]}
    )
    s3 = FakeS3()

    summary = run(client(fake_twitch), s3, "the-bucket", NOW, pause=0)

    assert [put["Key"] for put in s3.puts] == [
        "ref/igdb/dt=2026-11-06/games.json.gz",
        WATCHLIST_KEY,
    ]
    lines = gzip.decompress(s3.puts[0]["Body"]).decode().splitlines()
    games = [json.loads(line) for line in lines]  # one game per line, for Athena
    assert len(games) == 3
    assert games[0] == {
        "igdb_id": 1,
        "name": "Game 1",
        "first_release_date": "2026-11-01",
        "game_type": 0,
        "genres": ["Adventure"],
        "hypes": 10,
        "twitch_game_id": "900",
    }
    assert games[1]["twitch_game_id"] is None  # kept, since Twitch may add it later

    watchlist = json.loads(s3.puts[1]["Body"])
    assert watchlist["game_ids"] == ["300", "900"]
    assert watchlist["generated_dt"] == "2026-11-06"
    assert watchlist["window_start"] == "2026-09-07"
    assert watchlist["window_end"] == "2026-11-20"
    assert summary == {
        "games_key": "ref/igdb/dt=2026-11-06/games.json.gz",
        "releases": 3,
        "watchlist_games": 2,
    }
    assert query_of(fake_twitch.api_requests[1])["igdb_id"] == ["1", "2", "3"]


def test_no_matches_fails_and_keeps_the_old_watchlist(fake_twitch):
    queue_json(fake_twitch, [igdb_game(1)])
    queue_json(fake_twitch, {"data": []})
    old = json.dumps({"generated_dt": "2026-11-05", "game_ids": ["900"]}).encode()
    s3 = FakeS3({WATCHLIST_KEY: old})

    with pytest.raises(RuntimeError):
        run(client(fake_twitch), s3, "the-bucket", NOW, pause=0)

    assert s3.puts == []
    assert s3.objects[WATCHLIST_KEY] == old
