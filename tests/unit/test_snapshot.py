import gzip
import json
from datetime import UTC, datetime, timedelta, timezone

from twitch_trends.collector.snapshot import (
    collect_top_streams,
    snapshot_key,
    write_local,
)
from twitch_trends.common.twitch_client import TwitchClient

NOW = datetime(2026, 11, 1, 14, 58, 9, tzinfo=UTC)


def collect(fake, **kwargs) -> dict:
    return collect_top_streams(TwitchClient("id", "secret", transport=fake), NOW, **kwargs)


def test_collects_all_pages_when_data_runs_out(fake_twitch):
    fake_twitch.queue_page([900, 800], cursor="abc")
    fake_twitch.queue_page([700])

    snapshot = collect(fake_twitch)

    assert snapshot["stream_count"] == 3
    assert snapshot["pages_fetched"] == 2
    assert snapshot["truncated"] is False
    assert snapshot["snapshot_ts"] == "2026-11-01T14:58:09Z"


def test_stops_at_viewer_floor_and_drops_streams_below_it(fake_twitch):
    fake_twitch.queue_page([900, 60], cursor="abc")
    fake_twitch.queue_page([55, 40], cursor="def")
    fake_twitch.queue_page([30])

    snapshot = collect(fake_twitch, min_viewers=50)

    assert [s["viewer_count"] for s in snapshot["streams"]] == [900, 60, 55]
    assert snapshot["pages_fetched"] == 2
    assert snapshot["truncated"] is False


def test_marks_truncated_when_page_cap_hit_with_more_available(fake_twitch):
    fake_twitch.queue_page([900], cursor="abc")
    fake_twitch.queue_page([800], cursor="def")

    snapshot = collect(fake_twitch, max_pages=2)

    assert snapshot["pages_fetched"] == 2
    assert snapshot["truncated"] is True
    assert len(fake_twitch.api_requests) == 2


def test_not_truncated_when_last_allowed_page_is_the_final_page(fake_twitch):
    fake_twitch.queue_page([900], cursor="abc")
    fake_twitch.queue_page([800])

    assert collect(fake_twitch, max_pages=2)["truncated"] is False


def test_stream_seen_on_two_pages_is_kept_once(fake_twitch):
    fake_twitch.queue_page([900, 800], cursor="abc", ids=["a", "b"])
    fake_twitch.queue_page([790, 700], ids=["b", "c"])

    snapshot = collect(fake_twitch)

    assert [s["id"] for s in snapshot["streams"]] == ["a", "b", "c"]
    assert snapshot["streams"][1]["viewer_count"] == 800


def test_empty_response_gives_empty_snapshot(fake_twitch):
    fake_twitch.api_responses.append((200, b'{"data":[],"pagination":{}}'))

    snapshot = collect(fake_twitch)

    assert snapshot["stream_count"] == 0
    assert snapshot["truncated"] is False


def test_snapshot_key_is_partitioned_by_utc_date_and_hour():
    assert snapshot_key(NOW) == "raw/streams/dt=2026-11-01/hour=14/20261101T145809Z.json.gz"


def test_snapshot_key_converts_local_time_to_utc():
    # 22:30 at UTC-5 on Oct 31 is already Nov 1 in UTC, so it must land in Nov 1's partition.
    local = datetime(2026, 10, 31, 22, 30, 0, tzinfo=timezone(timedelta(hours=-5)))

    assert snapshot_key(local) == "raw/streams/dt=2026-11-01/hour=03/20261101T033000Z.json.gz"


def test_write_local_round_trips_through_gzip(tmp_path):
    snapshot = {"streams": [{"id": "a"}], "stream_count": 1}

    path = write_local(snapshot, tmp_path, snapshot_key(NOW))

    assert json.loads(gzip.decompress(path.read_bytes())) == snapshot
