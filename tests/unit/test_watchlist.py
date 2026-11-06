from conftest import query_of

from twitch_trends.collector.snapshot import WATCHLIST_FIELDS, collect_watchlist_streams
from twitch_trends.common.twitch_client import TwitchClient


def collect(fake, game_ids, **kwargs) -> dict:
    return collect_watchlist_streams(
        TwitchClient("id", "secret", transport=fake), game_ids, **kwargs
    )


def test_keeps_every_stream_even_with_zero_viewers(fake_twitch):
    fake_twitch.queue_page([40, 1, 0])

    result = collect(fake_twitch, ["111"])

    assert [s["viewer_count"] for s in result["streams"]] == [40, 1, 0]
    assert result["game_count"] == 1


def test_stores_only_the_fields_the_analysis_needs(fake_twitch):
    fake_twitch.queue_page([40])

    stream = collect(fake_twitch, ["111"])["streams"][0]

    assert tuple(stream) == WATCHLIST_FIELDS
    assert "title" not in stream


def test_asks_for_at_most_100_games_per_request(fake_twitch):
    game_ids = [str(n) for n in range(250)]
    for _ in range(3):
        fake_twitch.queue_page([5])

    collect(fake_twitch, game_ids)

    sent = [query_of(r)["game_id"] for r in fake_twitch.api_requests]
    assert [len(ids) for ids in sent] == [100, 100, 50]
    assert sum(sent, []) == game_ids


def test_follows_pages_within_a_chunk(fake_twitch):
    fake_twitch.queue_page([50, 40], cursor="abc")
    fake_twitch.queue_page([3])

    result = collect(fake_twitch, ["111"])

    assert result["stream_count"] == 3
    assert result["pages_fetched"] == 2
    assert result["truncated_chunks"] == 0


def test_counts_chunks_cut_short_by_the_page_cap(fake_twitch):
    fake_twitch.queue_page([50], cursor="abc")
    fake_twitch.queue_page([40], cursor="def")

    result = collect(fake_twitch, ["111"], max_pages_per_chunk=2)

    assert result["pages_fetched"] == 2
    assert result["truncated_chunks"] == 1
    assert len(fake_twitch.api_requests) == 2


def test_stream_seen_on_two_pages_is_kept_once(fake_twitch):
    fake_twitch.queue_page([50, 40], cursor="abc", ids=["a", "b"])
    fake_twitch.queue_page([39], ids=["b"])

    result = collect(fake_twitch, ["111"])

    assert [s["id"] for s in result["streams"]] == ["a", "b"]


def test_empty_watchlist_sends_no_requests(fake_twitch):
    result = collect(fake_twitch, [])

    assert result["stream_count"] == 0
    assert fake_twitch.api_requests == []
