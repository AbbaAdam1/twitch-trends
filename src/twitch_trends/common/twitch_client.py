"""Minimal Twitch Helix and IGDB client. Standard library only, so Lambda needs no bundled
dependencies."""

# "Helix" is the name of Twitch's public API. IGDB is the game database Twitch owns; it
# accepts the same app token. This file handles signing in, sending requests, and walking
# through pages of results.

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from typing import Any

TOKEN_URL = "https://id.twitch.tv/oauth2/token"  # where we exchange credentials for a token
STREAMS_URL = "https://api.twitch.tv/helix/streams"  # lists live streams, most viewers first
GAMES_URL = "https://api.twitch.tv/helix/games"  # looks up Twitch categories
IGDB_URL = "https://api.igdb.com/v4"
MAX_PAGE_SIZE = 100  # Twitch returns at most 100 streams per request
MAX_IDS_PER_REQUEST = 100  # Twitch accepts at most 100 game IDs in one request
TIMEOUT_SECONDS = 10  # give up on a request after this long, rather than hanging forever

# A "transport" is any function that takes a request and returns (status code, body).
# Making it swappable is what lets the tests pass in a fake, so they never touch the internet.
Transport = Callable[[urllib.request.Request], tuple[int, bytes]]

# Query parameters as (name, value) pairs rather than a dict, because some Twitch parameters
# repeat, e.g. game_id=1&game_id=2.
Params = list[tuple[str, str | int]]


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
        # normal return values, so _request can decide in one place how to handle each code.
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

    def _request(self, url: str, params: Params | None = None, body: bytes | None = None) -> Any:
        """Send one request (GET, or POST when there's a body) and return the parsed JSON."""
        full_url = f"{url}?{urllib.parse.urlencode(params)}" if params else url
        # Tokens can expire or be revoked between runs, so a 401 gets one refresh and one retry.
        for attempt in range(2):
            if self._token is None:
                self._token = self._fetch_token()
            request = urllib.request.Request(
                full_url,
                data=body,
                headers={"Authorization": f"Bearer {self._token}", "Client-Id": self._client_id},
                method="POST" if body is not None else "GET",
            )
            status, payload = self._transport(request)
            if status == 401 and attempt == 0:
                # 401 means "not authorized". Clear the token so the next loop gets a new one.
                self._token = None
                continue
            if status != 200:
                # Any other failure, or a second 401. The run fails here on purpose: a failed
                # run is better than saving incomplete data that looks complete.
                raise TwitchAPIError(status, payload[:200].decode(errors="replace"))
            return json.loads(payload)
        # The loop always returns or raises. This line just makes that explicit.
        raise AssertionError("unreachable")

    def iter_stream_pages(
        self, game_ids: list[str] | None = None, page_size: int = MAX_PAGE_SIZE
    ) -> Iterator[dict]:
        """Yield raw Get Streams responses, highest viewer counts first, until pages run out.

        With game_ids (at most 100), only streams playing those games are returned.
        """
        # `yield` makes this a generator: it hands back one page, then pauses until the caller
        # asks for the next. So if the caller stops early, no more requests get sent.
        cursor: str | None = None
        while True:
            params: Params = [("first", page_size)]
            params += [("game_id", game_id) for game_id in game_ids or []]
            # Each response includes a cursor, a bookmark meaning "continue from here".
            # Sending it back as `after` gets the next page. The first request has none.
            if cursor:
                params.append(("after", cursor))
            page = self._request(STREAMS_URL, params)
            yield page
            cursor = page.get("pagination", {}).get("cursor")
            if not cursor:
                return  # no cursor means this was the last page

    def get_games_by_igdb_ids(self, igdb_ids: list[int]) -> list[dict]:
        """Return the Twitch categories that match these IGDB game IDs. Unmatched IDs are
        simply absent from the result."""
        games: list[dict] = []
        for start in range(0, len(igdb_ids), MAX_IDS_PER_REQUEST):
            chunk = igdb_ids[start : start + MAX_IDS_PER_REQUEST]
            games += self._request(GAMES_URL, [("igdb_id", igdb_id) for igdb_id in chunk])["data"]
        return games

    def igdb(self, endpoint: str, query: str) -> list[dict]:
        """Run an IGDB query. IGDB uses its own query language, sent as the request body."""
        return self._request(f"{IGDB_URL}/{endpoint}", body=query.encode())
