"""Thin HTTP client for the Moveworks Conversations API.

Handles the concerns every caller needs regardless of endpoint: the
``Assistant-Name`` header, a bearer token sourced from a pluggable callable,
and retries that never resend a write the server may have processed.

Retry policy, per request:

- 429: retried on every method, because the server rejected the request
  before processing it. The wait honors ``Retry-After`` when present;
  otherwise it is a jittered backoff with a floor of
  ``RATE_LIMIT_FLOOR_SECONDS``.
- 5xx and network errors: retried only on ``GET``, ``HEAD`` and ``PATCH``. A
  ``POST`` that timed out or got a 5xx may already have been accepted, and
  resending ``POST .../responses`` would post the user's message twice.
- 401: the provider's ``invalidate()`` is called if it has one, and the
  request is sent once more with a fresh bearer.

``max_retries=0`` turns off 429/5xx/network retries entirely.

Auth is a ``Callable[[], str]`` so callers can
plug in whichever bearer provider they need (self-signed JWT, HTTP Connector
API Key, or a cached token from an interactive flow) without this module having
to know about any of them.

The client is synchronous. Call it from a worker thread
(``asyncio.to_thread``) in async code, as ``app/`` does.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple, Union
from urllib.parse import urljoin

import requests

from . import __version__
from .errors import (
    AuthenticationError,
    ConversationsApiError,
    DownstreamServiceError,
    RateLimitedError,
    RequestRejectedError,
)

MultipartContent = Union[str, bytes]
MultipartParts = Sequence[
    Tuple[str, Union[Tuple[Optional[str], MultipartContent], Tuple[Optional[str], MultipartContent, str]]]
]
"""Ordered multipart parts as ``(field, (filename, content[, content_type]))``.

A ``None`` filename makes a plain form field. Parts are sent in the order
given.
"""

BearerProvider = Callable[[], str]
"""A callable that returns a currently-valid bearer token string.

It may also expose ``invalidate()`` to drop a cached token; the client calls
it after a 401 before retrying once.
"""

USER_AGENT = f"moveworks-capi-starter/{__version__}"
RETRYABLE_ON_SERVER_ERROR = frozenset({"GET", "HEAD", "PATCH"})
RATE_LIMIT_FLOOR_SECONDS = 6.0
MAX_BACKOFF_SECONDS = 30.0

log = logging.getLogger(__name__)


class ConversationsApiClient:
    """Minimal client for the Conversations API.

    Args:
        base_url: e.g. ``https://api.moveworks.ai``. See the Base URLs table:
            https://docs.moveworks.com/api-reference/overview#base-urls
        bot_name: Bot Name of the Conversations API chatbot you created in
            Moveworks Setup; sent as the
            ``Assistant-Name`` header on every request.
        bearer_provider: Callable that returns a bearer token. The client
            invokes this on every request, so caching belongs inside the
            provider (see the ``moveworks_capi.auth`` module).
        max_retries: Retries after a 429, or after a 5xx or network error on a
            method that is safe to resend. ``0`` disables them.
        timeout: Per-request timeout in seconds.
        session: A ``requests.Session`` to send through, for connection
            pooling, proxies, or tests. The client closes only a session it
            created itself.

    Use as a context manager, or call :meth:`close`, to release connections.
    """

    def __init__(
        self,
        base_url: str,
        bot_name: str,
        bearer_provider: BearerProvider,
        *,
        max_retries: int = 3,
        timeout: float = 30.0,
        session: Optional[requests.Session] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/") + "/"
        self._bot_name = bot_name
        self._bearer_provider = bearer_provider
        self._max_retries = max_retries
        self._timeout = timeout
        self._owns_session = session is None
        self._session = session if session is not None else requests.Session()

    def __enter__(self) -> "ConversationsApiClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_session:
            self._session.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        json: Optional[Mapping[str, Any]] = None,
        files: Optional[MultipartParts] = None,
        timeout: Optional[float] = None,
    ) -> requests.Response:
        """Send one request with the retry policy above.

        ``files`` sends a ``multipart/form-data`` body instead of ``json``;
        ``requests`` sets its ``Content-Type`` and boundary. Part content is
        bytes or text rather than a stream, so a resend after a 429 or 401
        carries the same body. ``timeout`` overrides the client's per-request
        timeout for this call.
        """
        if json is not None and files is not None:
            raise ValueError("send json or files, not both")
        method = method.upper()
        url = self.url_for(path)
        resend_after_server_error = method in RETRYABLE_ON_SERVER_ERROR
        reauthenticated = False
        attempt = 0

        while True:
            headers: Dict[str, Optional[str]] = dict(self.headers(has_body=json is not None))
            body: Dict[str, Any] = {"json": json}
            if files is not None:
                # None removes a session-level Content-Type, so requests sets the multipart boundary.
                headers["Content-Type"] = None
                body = {"files": files}
            try:
                response = self._session.request(
                    method,
                    url,
                    params=params,
                    headers=headers,
                    timeout=self._timeout if timeout is None else timeout,
                    **body,
                )
            except requests.RequestException as exc:
                if not resend_after_server_error or attempt >= self._max_retries:
                    outcome = "" if resend_after_server_error else " (the server may have processed it)"
                    raise ConversationsApiError(f"network error on {method} {path}{outcome}: {exc}") from exc
                self._sleep(_backoff_seconds(attempt), f"network error, retrying: {exc}")
                attempt += 1
                continue

            status = response.status_code
            if status == 401:
                if not reauthenticated and self._invalidate_bearer():
                    reauthenticated = True
                    continue
                raise AuthenticationError(
                    _error_message(response, "authentication failed"),
                    status_code=401,
                    code=error_code(response),
                    trace_id=_trace_id(response),
                )
            if status == 429:
                retry_after = _parse_retry_after(response)
                if attempt >= self._max_retries:
                    raise RateLimitedError(
                        _error_message(response, "rate limited"),
                        retry_after=retry_after,
                        code=error_code(response),
                        trace_id=_trace_id(response),
                    )
                wait = retry_after if retry_after is not None else rate_limit_backoff_seconds(attempt)
                self._sleep(wait, "rate limited")
                attempt += 1
                continue
            if 500 <= status < 600:
                if not resend_after_server_error or attempt >= self._max_retries:
                    raise DownstreamServiceError(
                        _error_message(response, "server error"),
                        status_code=status,
                        trace_id=_trace_id(response),
                        code=error_code(response),
                    )
                self._sleep(_backoff_seconds(attempt), f"server error {status}")
                attempt += 1
                continue
            if 400 <= status < 500:
                # CAPI 400s usually say exactly what is wrong (unsupported
                # filter, bad field), so surface the server's own message.
                raise RequestRejectedError(
                    _error_message(response, f"request rejected ({status})"),
                    status_code=status,
                    code=error_code(response),
                    trace_id=_trace_id(response),
                )
            response.raise_for_status()
            return response

    def url_for(self, path: str) -> str:
        """Absolute URL for an API path such as ``/rest/v1/conversations``."""
        return urljoin(self._base_url, path.lstrip("/"))

    def headers(self, *, accept: str = "application/json", has_body: bool = False) -> Dict[str, str]:
        """Headers every request carries, with a freshly sourced bearer."""
        headers = {
            "Authorization": f"Bearer {self._bearer_provider()}",
            "Assistant-Name": self._bot_name,
            "Accept": accept,
            "User-Agent": USER_AGENT,
        }
        if has_body:
            headers["Content-Type"] = "application/json"
        return headers

    def open_stream(
        self,
        path: str,
        *,
        json: Optional[Mapping[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> requests.Response:
        """POST with ``stream=True`` and return the raw, unread response.

        Never resent after a network error, 429 or 5xx: a streaming POST
        submits a turn. The one exception is a 401, which is retried once
        after ``invalidate()`` because the server did not accept the turn.
        Check ``status_code`` and ``close()`` the response when done.
        """
        reauthenticated = False
        while True:
            response = self._session.post(
                self.url_for(path),
                headers=self.headers(accept="text/event-stream", has_body=json is not None),
                json=json,
                stream=True,
                timeout=self._timeout if timeout is None else timeout,
            )
            if response.status_code == 401 and not reauthenticated and self._invalidate_bearer():
                response.close()
                reauthenticated = True
                continue
            return response

    def get(self, path: str, *, params: Optional[Mapping[str, Any]] = None) -> requests.Response:
        return self.request("GET", path, params=params)

    def post(
        self,
        path: str,
        *,
        json: Optional[Mapping[str, Any]] = None,
        files: Optional[MultipartParts] = None,
        timeout: Optional[float] = None,
    ) -> requests.Response:
        return self.request("POST", path, json=json, files=files, timeout=timeout)

    def _invalidate_bearer(self) -> bool:
        invalidate = getattr(self._bearer_provider, "invalidate", None)
        if not callable(invalidate):
            return False
        log.info("401 from the API; refreshing the bearer and retrying once")
        invalidate()
        return True

    @staticmethod
    def _sleep(seconds: float, reason: str) -> None:
        log.warning("%s; sleeping %.1fs", reason, seconds)
        time.sleep(seconds)


def _backoff_seconds(attempt: int) -> float:
    """Exponential backoff with equal jitter, so parallel callers spread out."""
    ceiling = min(2.0**attempt, MAX_BACKOFF_SECONDS)
    return ceiling / 2 + random.uniform(0, ceiling / 2)


def rate_limit_backoff_seconds(attempt: int) -> float:
    """Wait after a 429 that carried no ``Retry-After``."""
    return max(RATE_LIMIT_FLOOR_SECONDS, _backoff_seconds(attempt)) + random.uniform(0, 1.0)


def _parse_retry_after(response: requests.Response) -> Optional[float]:
    header = response.headers.get("Retry-After")
    if not header:
        return None
    try:
        return float(header)
    except ValueError:
        return None


def _trace_id(response: requests.Response) -> Optional[str]:
    return response.headers.get("x-trace-id")


def error_code(response: requests.Response) -> Optional[str]:
    """The ``error.code`` from a ``{"error": {"code", "message"}}`` body, if any."""
    try:
        payload = response.json()
    except ValueError:
        return None
    error = payload.get("error") if isinstance(payload, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) and code else None


def _error_message(response: requests.Response, fallback: str) -> str:
    try:
        payload = response.json()
    except ValueError:
        return f"{fallback}: {response.status_code} {response.text[:200]}"
    if isinstance(payload, dict):
        # API errors are shaped {"error": {"code": ..., "message": ...}}.
        error = payload.get("error")
        if isinstance(error, dict):
            msg = error.get("message") or error.get("code")
            if isinstance(msg, str) and msg:
                return f"{fallback}: {response.status_code} {msg}"
        for key in ("error", "message", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    return f"{fallback}: {response.status_code}"
