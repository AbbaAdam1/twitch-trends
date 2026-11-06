"""Checks raw snapshots and turns them into clean rows. No AWS calls, so it's easy to test."""

# Raw snapshots are kept exactly as Twitch sent them. This is the one place that decides what
# counts as a good record. Anything rejected is counted by reason, never silently dropped.

from collections import Counter
from datetime import UTC, date, datetime, timedelta

SCHEMA_VERSIONS = {1, 2}  # version 1 had no watchlist; see collector/snapshot.py
# Twitch's clock and Lambda's can differ slightly, so a stream may seem to start just after
# the snapshot was taken. Anything later than this is a bad timestamp.
CLOCK_TOLERANCE = timedelta(minutes=10)


def parse_ts(value: object) -> datetime | None:
    """Parse a timestamp like 2026-11-02T13:00:00Z, or return None if it isn't one."""
    if not isinstance(value, str):
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    # A timestamp without a time zone is ambiguous, so it counts as bad.
    return ts if ts.tzinfo else None


def check_snapshot(snapshot: object, dt: date) -> datetime | None:
    """Return the snapshot's time if the file is usable for day `dt`, otherwise None."""
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") not in SCHEMA_VERSIONS:
        return None
    snapshot_ts = parse_ts(snapshot.get("snapshot_ts"))
    # A file filed under the wrong day would be counted twice, or on the wrong day.
    if snapshot_ts is None or snapshot_ts.astimezone(UTC).date() != dt:
        return None
    if not isinstance(snapshot.get("streams"), list):
        return None
    watchlist = snapshot.get("watchlist")
    if watchlist is not None and not isinstance(watchlist.get("streams"), list):
        return None
    return snapshot_ts


def reject_reason(stream: dict, snapshot_ts: datetime) -> str | None:
    """Return why a stream record is bad, or None if it's good."""
    if not stream.get("id"):
        return "missing_id"
    # Streams with no category set. They can't be tied to a game, which is the whole analysis.
    if not stream.get("game_id"):
        return "missing_game_id"
    viewers = stream.get("viewer_count")
    # bool is a kind of int in Python, so True would otherwise pass as 1 viewer.
    if not isinstance(viewers, int) or isinstance(viewers, bool) or viewers < 0:
        return "bad_viewer_count"
    started_at = parse_ts(stream.get("started_at"))
    if started_at is None or started_at > snapshot_ts + CLOCK_TOLERANCE:
        return "bad_started_at"
    return None


def snapshot_rows(snapshot: dict, snapshot_ts: datetime, rejected: Counter) -> list[dict]:
    """One row per stream in the snapshot, with flags for which list it came from.

    A popular stream for a watchlist game is in both lists. It becomes one row with both flags
    set, so adding up viewers never counts it twice.
    """
    rows: dict[str, dict] = {}
    watchlist = snapshot.get("watchlist") or {}
    for flag, streams in (
        ("in_top", snapshot["streams"]),
        ("in_watchlist", watchlist.get("streams", [])),
    ):
        seen: set[str] = set()
        for stream in streams:
            reason = (
                reject_reason(stream, snapshot_ts) if isinstance(stream, dict) else "bad_record"
            )
            # The collector already removes duplicates, so one here means something changed.
            if reason is None and stream["id"] in seen:
                reason = "duplicate"
            if reason:
                rejected[reason] += 1
                continue
            seen.add(stream["id"])
            row = rows.get(stream["id"])
            if row is None:
                row = rows[stream["id"]] = {
                    "snapshot_ts": snapshot_ts,
                    "stream_id": stream["id"],
                    "user_id": stream.get("user_id"),
                    "game_id": stream["game_id"],
                    "game_name": stream.get("game_name"),
                    "viewer_count": stream["viewer_count"],
                    "started_at": parse_ts(stream["started_at"]),
                    "language": stream.get("language"),
                    "in_top": False,
                    "in_watchlist": False,
                }
            row[flag] = True
    return list(rows.values())


class GameDaily:
    """Adds up each watchlist game's viewers snapshot by snapshot, for daily averages and peaks.

    Averages divide by every watchlist snapshot of the day, not just the ones where the game was
    live. A game with 100 viewers in half the snapshots and none in the rest averages 50.
    """

    def __init__(self) -> None:
        self.snapshots = 0
        self.games: dict[str, dict] = {}

    def add_snapshot(self, rows: list[dict]) -> None:
        """Count one snapshot that had a watchlist. Only watchlist rows are complete per game."""
        self.snapshots += 1
        totals: dict[str, list] = {}
        for row in rows:
            if row["in_watchlist"]:
                total = totals.setdefault(row["game_id"], [row["game_name"], 0, 0])
                total[1] += row["viewer_count"]
                total[2] += 1
        for game_id, (name, viewers, streams) in totals.items():
            game = self.games.setdefault(
                game_id,
                {
                    "live_snapshots": 0,
                    "viewer_sum": 0,
                    "peak_viewers": 0,
                    "stream_sum": 0,
                    "peak_streams": 0,
                },
            )
            game["game_name"] = name  # games get renamed now and then; keep the latest
            game["live_snapshots"] += 1
            game["viewer_sum"] += viewers
            game["peak_viewers"] = max(game["peak_viewers"], viewers)
            game["stream_sum"] += streams
            game["peak_streams"] = max(game["peak_streams"], streams)

    def rows(self) -> list[dict]:
        return [
            {
                "game_id": game_id,
                "game_name": game["game_name"],
                "watchlist_snapshots": self.snapshots,
                "live_snapshots": game["live_snapshots"],
                "avg_viewers": game["viewer_sum"] / self.snapshots,
                "peak_viewers": game["peak_viewers"],
                "avg_streams": game["stream_sum"] / self.snapshots,
                "peak_streams": game["peak_streams"],
            }
            for game_id, game in sorted(self.games.items())
        ]
