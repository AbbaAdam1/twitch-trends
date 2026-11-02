"""Minimal Twitch Helix client. Standard library only, so Lambda needs no bundled dependencies."""

# "Helix" is the name of Twitch's public API. This file handles talking to it:
# signing in, sending requests, and walking through pages of results.

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator

TOKEN_URL = "https://id.twitch.tv/oauth2/token"  # where we exchange credentials for a token
STREAMS_URL = "https://api.twitch.tv/helix/streams"  # lists live streams, most viewers first
MAX_PAGE_SIZE = 100  # Twitch returns at most 100 streams per request
TIMEOUT_SECONDS = 10  # give up on a request after this long, rather than hanging forever

# A "transport" is any function that takes a request and returns (status code, body).
# Making it swappable is what lets the tests pass in a fake, so they never touch the internet.
Transport = Callable[[urllib.request.Request], tuple[int, bytes]]


class TwitchAPIError(Exception):
    """Twitch returned a response we cannot recover from."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Twitch API returned {status}: {message}")
        self.status = status  # kept so callers and tests can check the exact code, e.g. 401


def _urllib_transport(request: urllib.request.Request) -> tuple[int, bytes]:
    """The real transport: sends the request over the internet."""
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        # urllib raises an exception for codes like 401 or 503. We turn those back into
        # normal return values, so _get can decide in one place how to handle each code.
        return error.code, error.read()


class TwitchClient:
    """Authenticates with an app access token and pages through Helix endpoints."""

    def __init__(
        self, client_id: str, client_secret: str, transport: Transport = _urllib_transport
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._transport = transport  # the real one by default, a fake one in tests
        self._token: str | None = None  # no token until the first request needs one

    def _fetch_token(self) -> str:
        """Exchange the client ID and secret for an access token.

        Twitch calls this the "client credentials" flow: the app signs in as itself, not
        as a user. That's enough for public data like live streams.
        """
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
            # Only Twitch's reply goes in the error, never our request, so the secret
            # can't leak into logs. [:200] keeps a long reply from flooding them.
            raise TwitchAPIError(status, payload[:200].decode(errors="replace"))
        return json.loads(payload)["access_token"]

    def _get(self, url: str, params: dict[str, str | int]) -> dict:
        """Send one GET request and return the parsed JSON response."""
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
                # 401 means "not authorized". Clear the token so the next loop gets a new one.
                self._token = None
                continue
            if status != 200:
                # Any other failure, or a second 401. The run fails here on purpose: the next
                # one starts in 10 minutes, and a failed run is better than saving bad data.
                raise TwitchAPIError(status, payload[:200].decode(errors="replace"))
            return json.loads(payload)
        # The loop always returns or raises. This line just makes that explicit.
        raise AssertionError("unreachable")

    def iter_stream_pages(self, page_size: int = MAX_PAGE_SIZE) -> Iterator[dict]:
        """Yield raw Get Streams responses, highest viewer counts first, until pages run out."""
        # `yield` makes this a generator: it hands back one page, then pauses until the caller
        # asks for the next. So if the caller stops early, no more requests get sent.
        cursor: str | None = None
        while True:
            params: dict[str, str | int] = {"first": page_size}
            # Each response includes a cursor, a bookmark meaning "continue from here".
            # Sending it back as `after` gets the next page. The first request has none.
            if cursor:
                params["after"] = cursor
            page = self._get(STREAMS_URL, params)
            yield page
            cursor = page.get("pagination", {}).get("cursor")
            if not cursor:
                return  # no cursor means this was the last page
