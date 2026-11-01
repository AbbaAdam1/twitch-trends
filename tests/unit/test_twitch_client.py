import pytest
from conftest import query_of

from twitch_trends.common.twitch_client import TwitchAPIError, TwitchClient


def make_client(fake) -> TwitchClient:
    return TwitchClient("the-client-id", "the-secret", transport=fake)


def test_sends_token_and_client_id_and_reuses_token(fake_twitch):
    fake_twitch.queue_page([500], cursor="next")
    fake_twitch.queue_page([400])

    list(make_client(fake_twitch).iter_stream_pages())

    assert fake_twitch.token_count == 1
    first_call = fake_twitch.api_requests[0]
    assert first_call.get_header("Authorization") == "Bearer token-1"
    assert first_call.get_header("Client-id") == "the-client-id"


def test_follows_cursor_until_pages_run_out(fake_twitch):
    fake_twitch.queue_page([500], cursor="abc")
    fake_twitch.queue_page([400])

    pages = list(make_client(fake_twitch).iter_stream_pages())

    assert len(pages) == 2
    assert "after" not in query_of(fake_twitch.api_requests[0])
    assert query_of(fake_twitch.api_requests[1])["after"] == ["abc"]


def test_refreshes_token_once_on_401(fake_twitch):
    fake_twitch.api_responses.append((401, b'{"message":"Invalid token"}'))
    fake_twitch.queue_page([500])

    pages = list(make_client(fake_twitch).iter_stream_pages())

    assert len(pages) == 1
    assert fake_twitch.token_count == 2
    assert fake_twitch.api_requests[1].get_header("Authorization") == "Bearer token-2"


def test_gives_up_after_second_401(fake_twitch):
    fake_twitch.api_responses.append((401, b"expired"))
    fake_twitch.api_responses.append((401, b"expired"))

    with pytest.raises(TwitchAPIError) as error:
        list(make_client(fake_twitch).iter_stream_pages())

    assert error.value.status == 401


def test_server_error_raises_without_retry(fake_twitch):
    fake_twitch.api_responses.append((503, b"unavailable"))

    with pytest.raises(TwitchAPIError) as error:
        list(make_client(fake_twitch).iter_stream_pages())

    assert error.value.status == 503
    assert len(fake_twitch.api_requests) == 1


def test_failed_token_request_does_not_expose_secret(fake_twitch):
    fake_twitch.token_responses.append((403, b'{"message":"invalid client secret"}'))

    with pytest.raises(TwitchAPIError) as error:
        list(make_client(fake_twitch).iter_stream_pages())

    assert error.value.status == 403
    assert "the-secret" not in str(error.value)
