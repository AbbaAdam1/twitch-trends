import gzip
import io
import json
from datetime import UTC, date, datetime

import pyarrow.parquet as pq
import pytest
from conftest import FakeS3
from test_handler import FakeCloudWatch

from twitch_trends.compactor.handler import (
    REJECTED_METRIC,
    SNAPSHOTS_METRIC,
    resolve_dt,
    run,
)

DT = date(2026, 11, 6)


def stream(stream_id: str, game_id: str = "100", viewers: int = 10, **overrides) -> dict:
    row = {
        "id": stream_id,
        "user_id": f"u{stream_id}",
        "game_id": game_id,
        "game_name": f"Game {game_id}",
        "viewer_count": viewers,
        "started_at": "2026-11-06T09:00:00Z",
        "language": "en",
    }
    return {**row, **overrides}


def snapshot(minute: int, top: list[dict], watchlist: list[dict] | None = None) -> dict:
    snap = {
        "schema_version": 2,
        "snapshot_ts": f"2026-11-06T12:{minute:02d}:00Z",
        "stream_count": len(top),
        "pages_fetched": 1,
        "truncated": False,
        "streams": top,
        "watchlist": None,
    }
    if watchlist is not None:
        snap["watchlist"] = {
            "generated_dt": "2026-11-06",
            "stream_count": len(watchlist),
            "truncated_chunks": 0,
            "streams": watchlist,
        }
    return snap


def raw_key(minute: int) -> str:
    return f"raw/streams/dt=2026-11-06/hour=12/20261106T12{minute:02d}00Z.json.gz"


def bucket_with(*snapshots: dict) -> FakeS3:
    objects = {}
    for snap in snapshots:
        minute = int(snap["snapshot_ts"][14:16])
        objects[raw_key(minute)] = gzip.compress(json.dumps(snap).encode())
    return FakeS3(objects)


def table(s3: FakeS3, name: str) -> list[dict]:
    body = s3.objects[f"curated/{name}/dt=2026-11-06/part-0.parquet"]
    return pq.read_table(io.BytesIO(body)).to_pylist()


def test_stream_in_both_lists_becomes_one_row_with_both_flags():
    s3 = bucket_with(
        snapshot(0, top=[stream("a", viewers=900)], watchlist=[stream("a"), stream("b")])
    )

    run(s3, FakeCloudWatch(), "the-bucket", DT)

    rows = {row["stream_id"]: row for row in table(s3, "stream_snapshots")}
    assert rows["a"]["in_top"] and rows["a"]["in_watchlist"]
    assert rows["a"]["viewer_count"] == 900  # the top-streams copy is kept
    assert not rows["b"]["in_top"] and rows["b"]["in_watchlist"]
    assert rows["a"]["snapshot_ts"] == datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


def test_bad_records_are_counted_by_reason_and_left_out():
    top = [
        stream("good"),
        stream("no-category", game_id=""),
        stream("negative", viewers=-1),
        stream("future", started_at="2026-11-06T13:00:00Z"),
        stream("", game_id="100"),
        stream("good"),
    ]
    s3, cloudwatch = bucket_with(snapshot(0, top=top)), FakeCloudWatch()

    summary = run(s3, cloudwatch, "the-bucket", DT)

    assert [row["stream_id"] for row in table(s3, "stream_snapshots")] == ["good"]
    assert summary["rejected_by_reason"] == {
        "bad_started_at": 1,
        "bad_viewer_count": 1,
        "duplicate": 1,
        "missing_game_id": 1,
        "missing_id": 1,
    }
    metrics = {m["MetricName"]: m["Value"] for m in cloudwatch.metrics[0]["MetricData"]}
    assert metrics == {SNAPSHOTS_METRIC: 1, REJECTED_METRIC: 5}
    assert table(s3, "snapshots")[0]["rows_rejected"] == 5


def test_damaged_and_misfiled_snapshots_are_skipped():
    good = snapshot(0, top=[stream("a")])
    wrong_day = snapshot(10, top=[stream("b")])
    s3 = bucket_with(good, wrong_day)
    s3.objects[raw_key(10)] = gzip.compress(
        json.dumps({**wrong_day, "snapshot_ts": "2026-11-05T12:10:00Z"}).encode()
    )
    s3.objects[raw_key(20)] = b"not gzip"

    summary = run(s3, FakeCloudWatch(), "the-bucket", DT)

    assert summary["snapshot_files"] == 3
    assert summary["snapshots_compacted"] == 1
    assert summary["rejected_by_reason"] == {"bad_snapshot": 2}
    assert [row["stream_id"] for row in table(s3, "stream_snapshots")] == ["a"]


def test_game_daily_averages_over_every_watchlist_snapshot():
    s3 = bucket_with(
        snapshot(0, top=[], watchlist=[stream("a", viewers=100), stream("b", viewers=50)]),
        snapshot(10, top=[], watchlist=[stream("a", viewers=300)]),
        snapshot(20, top=[], watchlist=[]),  # nothing live for the game this time
        snapshot(30, top=[], watchlist=[stream("c", game_id="200", viewers=7)]),
    )

    run(s3, FakeCloudWatch(), "the-bucket", DT)

    game = {row["game_id"]: row for row in table(s3, "game_daily")}["100"]
    assert game["watchlist_snapshots"] == 4
    assert game["live_snapshots"] == 2
    assert game["avg_viewers"] == (150 + 300) / 4
    assert game["peak_viewers"] == 300
    assert game["avg_streams"] == 3 / 4
    assert game["peak_streams"] == 2


def test_days_before_the_watchlist_still_write_every_table():
    snap = snapshot(0, top=[stream("a")])
    snap["schema_version"] = 1
    del snap["watchlist"]
    s3 = bucket_with(snap)

    summary = run(s3, FakeCloudWatch(), "the-bucket", DT)

    assert summary["watchlist_games"] == 0
    assert table(s3, "game_daily") == []
    assert table(s3, "stream_snapshots")[0]["in_top"]
    assert table(s3, "snapshots")[0]["watchlist_generated_dt"] is None


def test_running_a_day_twice_replaces_its_files_instead_of_adding_to_them():
    s3 = bucket_with(snapshot(0, top=[stream("a")], watchlist=[stream("b")]))

    run(s3, FakeCloudWatch(), "the-bucket", DT)
    first = {key: body for key, body in s3.objects.items() if key.startswith("curated/")}
    run(s3, FakeCloudWatch(), "the-bucket", DT)
    second = {key: body for key, body in s3.objects.items() if key.startswith("curated/")}

    assert sorted(first) == [
        "curated/game_daily/dt=2026-11-06/part-0.parquet",
        "curated/snapshots/dt=2026-11-06/part-0.parquet",
        "curated/stream_snapshots/dt=2026-11-06/part-0.parquet",
    ]
    assert first == second


def test_a_day_with_no_usable_snapshots_fails_and_writes_nothing():
    s3, cloudwatch = FakeS3({raw_key(0): b"not gzip"}), FakeCloudWatch()

    with pytest.raises(RuntimeError):
        run(s3, cloudwatch, "the-bucket", DT)

    assert s3.puts == []
    assert cloudwatch.metrics == []


def test_scheduled_runs_compact_yesterday_and_backfills_choose_the_day():
    just_after_midnight = datetime(2026, 11, 7, 0, 30, tzinfo=UTC)
    assert resolve_dt({"detail-type": "Scheduled Event"}, just_after_midnight) == DT
    assert resolve_dt({"dt": "2026-11-02"}, just_after_midnight) == date(2026, 11, 2)
