"""Minimal Twitch Helix client. Standard library only, so Lambda needs no bundled dependencies."""

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
STREAMS_URL = "https://api.twitch.tv/helix/streams"
MAX_PAGE_SIZE = 100
TIMEOUT_SECONDS = 10

Transport = Callable[[urllib.request.Request], tuple[int, bytes]]


class TwitchAPIError(Exception):
    """Twitch returned a response we cannot recover from."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Twitch API returned {status}: {message}")
        self.status = status


def _urllib_transport(request: urllib.request.Request) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


class TwitchClient:
    """Authenticates with an app access token and pages through Helix endpoints."""

    def __init__(
        self, client_id: str, client_secret: str, transport: Transport = _urllib_transport
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._transport = transport
        self._token: str | None = None

    def _fetch_token(self) -> str:
        body = urllib.parse.urlencode(
            {
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "grant_type": "client_credentials",
            }
        ).encode()
        request = urllib.request.Request(TOKEN_URL, data=body, method="POST")
        status, payload = self._transport(request)
        if status != 200:
            raise TwitchAPIError(status, payload[:200].decode(errors="replace"))
        return json.loads(payload)["access_token"]

    def _get(self, url: str, params: dict[str, str | int]) -> dict:
        full_url = f"{url}?{urllib.parse.urlencode(params)}"
        # Tokens can expire or be revoked between runs, so a 401 gets one refresh and one retry.
        for attempt in range(2):
            if self._token is None:
                self._token = self._fetch_token()
            request = urllib.request.Request(
                full_url,
                headers={"Authorization": f"Bearer {self._token}", "Client-Id": self._client_id},
            )
            status, payload = self._transport(request)
            if status == 401 and attempt == 0:
                self._token = None
                continue
            if status != 200:
                raise TwitchAPIError(status, payload[:200].decode(errors="replace"))
            return json.loads(payload)
        raise AssertionError("unreachable")

    def iter_stream_pages(self, page_size: int = MAX_PAGE_SIZE) -> Iterator[dict]:
        """Yield raw Get Streams responses, highest viewer counts first, until pages run out."""
        cursor: str | None = None
        while True:
            params: dict[str, str | int] = {"first": page_size}
            if cursor:
                params["after"] = cursor
            page = self._get(STREAMS_URL, params)
            yield page
            cursor = page.get("pagination", {}).get("cursor")
            if not cursor:
                return
