"""Exception types raised by the starter code.

``ConversationsApiClient`` maps HTTP errors by status: 401 to
``AuthenticationError``, 429 to ``RateLimitedError`` and 5xx to
``DownstreamServiceError`` once retries run out, and other 4xx to
``RequestRejectedError``. Every error carries the optional ``status_code``,
``code`` (the body's ``error.code``) and ``trace_id`` (the ``x-trace-id``
header) when the server sent them; branch on those, not on the message.
Streaming, failed responses, and webhook verification raise their own types
below.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .responses import Response


class ConversationsApiError(Exception):
    """Base class for all Conversations API starter errors."""

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        code: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.trace_id = trace_id


class AuthenticationError(ConversationsApiError):
    """The auth exchange failed or the bearer token was rejected."""


class RateLimitedError(ConversationsApiError):
    """Received a 429. ``retry_after`` is seconds to wait if the server sent one."""

    def __init__(
        self,
        message: str,
        retry_after: Optional[float] = None,
        *,
        status_code: Optional[int] = 429,
        code: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> None:
        super().__init__(message, status_code=status_code, code=code, trace_id=trace_id)
        self.retry_after = retry_after


class RequestRejectedError(ConversationsApiError):
    """The API rejected the request with a 4xx other than 401 or 429."""

    def __init__(
        self,
        message: str,
        status_code: int,
        code: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> None:
        super().__init__(message, status_code=status_code, code=code, trace_id=trace_id)
        self.status_code: int = status_code


class DownstreamServiceError(ConversationsApiError):
    """The API returned a 5xx."""

    def __init__(
        self,
        message: str,
        status_code: int,
        trace_id: Optional[str] = None,
        *,
        code: Optional[str] = None,
    ) -> None:
        super().__init__(message, status_code=status_code, code=code, trace_id=trace_id)
        self.status_code: int = status_code


class StreamError(ConversationsApiError):
    """The streaming endpoint sent an ``ERROR`` event mid-stream."""

    def __init__(self, message: str, code: Optional[str] = None) -> None:
        super().__init__(message, code=code)


class StreamClosedError(ConversationsApiError):
    """The stream ended, or the connection dropped, before a terminal event.

    The response may still be running server-side. ``response_id`` is set when
    ``RESPONSE_CREATED`` arrived first, so the caller can poll for the answer
    with ``responses.wait_for_completion``.
    """

    def __init__(self, message: str, response_id: Optional[str] = None) -> None:
        super().__init__(message)
        self.response_id = response_id


class ResponseFailedError(ConversationsApiError):
    """A response reached ``FAILED``. ``response`` is the full ``Response``."""

    def __init__(self, message: str, response: "Response") -> None:
        super().__init__(message)
        self.response = response


class SignatureVerificationError(ConversationsApiError):
    """Outbound webhook signature did not verify against any candidate key."""


class InvalidWebhookTimestampError(ConversationsApiError):
    """The ``webhook-timestamp`` header was missing, malformed, or outside tolerance."""


class UnknownKidError(ConversationsApiError):
    """The ``webhook-kid`` header referenced a key not present in the tenant JWKS."""


class JwksFetchError(ConversationsApiError):
    """The tenant JWKS could not be fetched or parsed.

    Unlike the verification errors above, this says nothing about the
    delivery itself; answer with a 5xx so the sender retries.
    """
