import copy
import json
import urllib.parse
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent.parent / "fixtures"


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
