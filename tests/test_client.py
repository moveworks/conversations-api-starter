"""HTTP client: error mapping, retry policy, re-authentication."""

from __future__ import annotations

from typing import List, Union
from unittest.mock import MagicMock

import pytest
import requests

from moveworks_capi import client as client_module
from moveworks_capi.client import USER_AGENT, ConversationsApiClient
from moveworks_capi.errors import (
    AuthenticationError,
    ConversationsApiError,
    DownstreamServiceError,
    RateLimitedError,
    RequestRejectedError,
)


class _Response:
    def __init__(self, status_code: int, body: object = None, headers: dict | None = None) -> None:
        self.status_code = status_code
        self._body = body
        self.headers = {"x-trace-id": "trace_1", **(headers or {})}
        self.text = str(body)
        self.closed = False

    def json(self) -> object:
        if isinstance(self._body, (dict, list)):
            return self._body
        raise ValueError("not json")

    def raise_for_status(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


Scripted = Union[_Response, Exception]


class _Session:
    """Plays back scripted responses or exceptions, one per request."""

    def __init__(self, *script: Scripted) -> None:
        self._script: List[Scripted] = list(script)
        self.calls: List[tuple] = []
        self.closed = False

    def _next(self, method: str, url: str, kwargs: dict) -> _Response:
        self.calls.append((method, url, kwargs))
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def request(self, method: str, url: str, **kwargs) -> _Response:
        return self._next(method, url, kwargs)

    def post(self, url: str, **kwargs) -> _Response:
        return self._next("POST", url, kwargs)

    def close(self) -> None:
        self.closed = True


class _CachingBearer:
    def __init__(self) -> None:
        self.issued = 0
        self.invalidated = 0

    def __call__(self) -> str:
        self.issued += 1
        return f"token_{self.invalidated}"

    def invalidate(self) -> None:
        self.invalidated += 1


@pytest.fixture(autouse=True)
def sleeps(monkeypatch) -> list:
    recorded: list = []
    monkeypatch.setattr(client_module.time, "sleep", recorded.append)
    return recorded


def _client(*script: Scripted, max_retries: int = 3, bearer=lambda: "token") -> tuple:
    session = _Session(*script)
    client = ConversationsApiClient(
        "https://api.example.com", "test-bot", bearer, max_retries=max_retries, session=session  # type: ignore[arg-type]
    )
    return client, session


def test_4xx_carries_status_code_and_error_code() -> None:
    body = {"error": {"code": "INVALID_REQUEST", "message": "starting_after must be before ending_at"}}
    client, _ = _client(_Response(400, body), max_retries=0)
    with pytest.raises(RequestRejectedError) as exc:
        client.get("/rest/v1/conversations/-/events")
    assert (exc.value.status_code, exc.value.code, exc.value.trace_id) == (400, "INVALID_REQUEST", "trace_1")
    assert "starting_after must be before ending_at" in str(exc.value)
    assert isinstance(exc.value, ConversationsApiError)


def test_4xx_without_json_body_has_no_code() -> None:
    client, _ = _client(_Response(404, "API not found"), max_retries=0)
    with pytest.raises(RequestRejectedError) as exc:
        client.get("/rest/v1/conversations")
    assert (exc.value.status_code, exc.value.code) == (404, None)


class TestRetryPolicy:
    def test_429_then_success_is_retried_after_the_floor(self, sleeps: list) -> None:
        client, session = _client(_Response(429, "slow down"), _Response(200, {"ok": True}))
        assert client.get("/rest/v1/conversations").json() == {"ok": True}
        assert len(session.calls) == 2
        assert sleeps[0] >= client_module.RATE_LIMIT_FLOOR_SECONDS

    def test_429_is_retried_on_post_because_the_server_did_not_process_it(self) -> None:
        client, session = _client(_Response(429, "slow down"), _Response(202, {"response_id": "r1"}))
        assert client.post("/rest/v1/conversations/c1/responses", json={}).json() == {"response_id": "r1"}
        assert len(session.calls) == 2

    def test_retry_after_header_is_honored(self, sleeps: list) -> None:
        client, _ = _client(_Response(429, "", headers={"Retry-After": "2"}), _Response(200, {}))
        client.get("/rest/v1/conversations")
        assert sleeps == [2.0]

    def test_429_after_retries_carries_status_and_trace(self) -> None:
        client, session = _client(_Response(429, "slow down"), max_retries=0)
        with pytest.raises(RateLimitedError) as exc:
            client.get("/rest/v1/conversations")
        assert (exc.value.status_code, exc.value.trace_id, exc.value.retry_after) == (429, "trace_1", None)
        assert len(session.calls) == 1

    def test_5xx_is_retried_on_get(self) -> None:
        client, session = _client(_Response(503, "busy"), _Response(200, {}))
        client.get("/rest/v1/conversations")
        assert len(session.calls) == 2

    def test_5xx_is_retried_on_patch(self) -> None:
        client, session = _client(_Response(502, "bad gateway"), _Response(200, {}))
        client.request("PATCH", "/rest/v1/conversations/c1", json={"title": "t"})
        assert len(session.calls) == 2

    def test_5xx_is_not_retried_on_post(self) -> None:
        client, session = _client(_Response(500, {"error": {"code": "INTERNAL", "message": "boom"}}), _Response(202, {}))
        with pytest.raises(DownstreamServiceError) as exc:
            client.post("/rest/v1/conversations/c1/responses", json={})
        assert (exc.value.status_code, exc.value.code, exc.value.trace_id) == (500, "INTERNAL", "trace_1")
        assert len(session.calls) == 1

    def test_network_error_is_retried_on_get(self) -> None:
        client, session = _client(requests.ConnectionError("reset"), _Response(200, {}))
        client.get("/rest/v1/conversations")
        assert len(session.calls) == 2

    def test_network_error_is_not_retried_on_post(self) -> None:
        client, session = _client(requests.Timeout("read timed out"), _Response(202, {}))
        with pytest.raises(ConversationsApiError, match="may have processed it"):
            client.post("/rest/v1/conversations/c1/responses", json={})
        assert len(session.calls) == 1

    def test_retries_stop_at_max_retries(self) -> None:
        client, session = _client(_Response(503), _Response(503), _Response(503), max_retries=2)
        with pytest.raises(DownstreamServiceError):
            client.get("/rest/v1/conversations")
        assert len(session.calls) == 3

    def test_backoff_is_jittered_and_capped(self) -> None:
        delays = {client_module._backoff_seconds(3) for _ in range(20)}
        assert len(delays) > 1 and all(4.0 <= d <= 8.0 for d in delays)
        assert client_module._backoff_seconds(20) <= client_module.MAX_BACKOFF_SECONDS


class TestReauthentication:
    def test_401_invalidates_the_bearer_and_retries_once(self) -> None:
        bearer = _CachingBearer()
        client, session = _client(_Response(401, "expired"), _Response(200, {}), max_retries=0, bearer=bearer)
        client.get("/rest/v1/conversations")
        assert bearer.invalidated == 1
        assert [call[2]["headers"]["Authorization"] for call in session.calls] == ["Bearer token_0", "Bearer token_1"]

    def test_a_second_401_raises_with_status(self) -> None:
        bearer = _CachingBearer()
        client, session = _client(_Response(401, "no"), _Response(401, "still no"), bearer=bearer)
        with pytest.raises(AuthenticationError) as exc:
            client.get("/rest/v1/conversations")
        assert (exc.value.status_code, exc.value.trace_id) == (401, "trace_1")
        assert bearer.invalidated == 1 and len(session.calls) == 2

    def test_401_without_invalidate_is_not_retried(self) -> None:
        client, session = _client(_Response(401, "no"), _Response(200, {}))
        with pytest.raises(AuthenticationError):
            client.get("/rest/v1/conversations")
        assert len(session.calls) == 1

    def test_open_stream_retries_a_401_once(self) -> None:
        bearer = _CachingBearer()
        first = _Response(401, "expired")
        client, session = _client(first, _Response(200, ""), bearer=bearer)
        assert client.open_stream("/rest/v1/conversations/c1/responses/stream", json={}).status_code == 200
        assert first.closed and bearer.invalidated == 1 and len(session.calls) == 2


def test_url_for_joins_base_and_path() -> None:
    client = ConversationsApiClient("https://api.example.com/", "test-bot", lambda: "token")
    assert client.url_for("/rest/v1/conversations") == "https://api.example.com/rest/v1/conversations"


def test_headers_source_a_fresh_bearer_each_call() -> None:
    tokens = iter(["t1", "t2"])
    client = ConversationsApiClient("https://api.example.com", "test-bot", lambda: next(tokens))
    assert client.headers()["Authorization"] == "Bearer t1"
    second = client.headers(accept="text/event-stream", has_body=True)
    assert second == {
        "Authorization": "Bearer t2",
        "Assistant-Name": "test-bot",
        "Accept": "text/event-stream",
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
    }


def test_user_agent_names_the_starter_and_its_version() -> None:
    from moveworks_capi import __version__

    assert USER_AGENT == f"moveworks-capi-starter/{__version__}"


def test_open_stream_posts_once_without_status_mapping() -> None:
    client, session = _client(_Response(500, "boom"), _Response(200, ""))
    response = client.open_stream("/rest/v1/conversations/c1/responses/stream", json={"input": {}}, timeout=5)
    assert response.status_code == 500
    assert len(session.calls) == 1
    kwargs = session.calls[0][2]
    assert kwargs["stream"] is True and kwargs["timeout"] == 5
    assert kwargs["headers"]["Accept"] == "text/event-stream"


class TestLifecycle:
    def test_context_manager_closes_an_owned_session(self) -> None:
        with ConversationsApiClient("https://api.example.com", "bot", lambda: "t") as client:
            owned = client._session
            owned.close = MagicMock()
        owned.close.assert_called_once()

    def test_an_injected_session_is_left_open(self) -> None:
        client, session = _client()
        client.close()
        assert session.closed is False
