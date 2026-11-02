import gzip
import json
from datetime import UTC, datetime

import pytest

from twitch_trends.collector.handler import METRIC_NAME, METRIC_NAMESPACE, run
from twitch_trends.common.twitch_client import TwitchAPIError, TwitchClient

NOW = datetime(2026, 11, 2, 15, 0, 0, tzinfo=UTC)


class FakeS3:
    def __init__(self) -> None:
        self.objects: list[dict] = []

    def put_object(self, **kwargs) -> None:
        self.objects.append(kwargs)


class FakeCloudWatch:
    def __init__(self) -> None:
        self.metrics: list[dict] = []

    def put_metric_data(self, **kwargs) -> None:
        self.metrics.append(kwargs)


def run_with(fake_twitch, s3, cloudwatch) -> dict:
    client = TwitchClient("id", "secret", transport=fake_twitch)
    return run(client, s3, cloudwatch, "the-bucket", NOW)


def test_saves_snapshot_to_partitioned_key_and_publishes_metric(fake_twitch):
    fake_twitch.queue_page([900, 800])
    s3, cloudwatch = FakeS3(), FakeCloudWatch()

    summary = run_with(fake_twitch, s3, cloudwatch)

    saved = s3.objects[0]
    assert saved["Bucket"] == "the-bucket"
    assert saved["Key"] == "raw/streams/dt=2026-11-02/hour=15/20261102T150000Z.json.gz"
    assert json.loads(gzip.decompress(saved["Body"]))["stream_count"] == 2
    assert summary["stream_count"] == 2

    metric = cloudwatch.metrics[0]
    assert metric["Namespace"] == METRIC_NAMESPACE
    assert metric["MetricData"][0]["MetricName"] == METRIC_NAME
    assert metric["MetricData"][0]["Value"] == 2


def test_empty_response_fails_and_saves_nothing(fake_twitch):
    fake_twitch.api_responses.append((200, b'{"data":[],"pagination":{}}'))
    s3, cloudwatch = FakeS3(), FakeCloudWatch()

    with pytest.raises(RuntimeError):
        run_with(fake_twitch, s3, cloudwatch)

    assert s3.objects == []
    assert cloudwatch.metrics == []


def test_twitch_failure_saves_nothing_and_publishes_no_metric(fake_twitch):
    fake_twitch.api_responses.append((503, b"unavailable"))
    s3, cloudwatch = FakeS3(), FakeCloudWatch()

    with pytest.raises(TwitchAPIError):
        run_with(fake_twitch, s3, cloudwatch)

    assert s3.objects == []
    assert cloudwatch.metrics == []
