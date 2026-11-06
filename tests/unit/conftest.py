import copy
import io
import json
import urllib.parse
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent.parent / "fixtures"


class FakeS3:
    """Stands in for an S3 client: keeps objects in memory and records each upload."""

    class exceptions:  # mirrors boto3's client.exceptions.NoSuchKey
        class NoSuchKey(Exception):
            pass

    def __init__(self, objects: dict[str, bytes] | None = None) -> None:
        self.objects = dict(objects or {})
        self.puts: list[dict] = []

    def put_object(self, **kwargs) -> None:
        self.puts.append(kwargs)
        self.objects[kwargs["Key"]] = kwargs["Body"]

    def get_object(self, Bucket: str, Key: str) -> dict:  # noqa: N803, matches boto3's names
        if Key not in self.objects:
            raise self.exceptions.NoSuchKey(Key)
        return {"Body": io.BytesIO(self.objects[Key])}

    def get_paginator(self, name: str):
        assert name == "list_objects_v2"
        return self

    def paginate(self, Bucket: str, Prefix: str):  # noqa: N803
        keys = sorted(key for key in self.objects if key.startswith(Prefix))
        # Like S3, a listing with no matches has no "Contents" at all.
        yield {"Contents": [{"Key": key} for key in keys]} if keys else {}


class FakeTwitch:
    """Stands in for the network: serves queued responses and records each request."""

    def __init__(self) -> None:
        self.token_responses: list[tuple[int, bytes]] = []
        self.api_responses: list[tuple[int, bytes]] = []
        self.requests: list = []

    def __call__(self, request) -> tuple[int, bytes]:
        self.requests.append(request)
        if "oauth2/token" in request.full_url:
            if self.token_responses:
                return self.token_responses.pop(0)
            return 200, json.dumps({"access_token": f"token-{self.token_count}"}).encode()
        return self.api_responses.pop(0)

    @property
    def token_count(self) -> int:
        return sum("oauth2/token" in r.full_url for r in self.requests)

    @property
    def api_requests(self) -> list:
        return [r for r in self.requests if "oauth2/token" not in r.full_url]

    def queue_page(self, viewer_counts: list[int], cursor: str | None = None, ids=None) -> None:
        template = json.loads((FIXTURES / "helix_streams_page.json").read_text())["data"][0]
        rows = []
        for i, viewers in enumerate(viewer_counts):
            row = copy.deepcopy(template)
            row["id"] = str(ids[i]) if ids else f"{len(self.api_responses)}-{i}"
            row["viewer_count"] = viewers
            rows.append(row)
        pagination = {"cursor": cursor} if cursor else {}
        self.api_responses.append(
            (200, json.dumps({"data": rows, "pagination": pagination}).encode())
        )


def query_of(request) -> dict:
    return urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)


@pytest.fixture
def fake_twitch() -> FakeTwitch:
    return FakeTwitch()
