"""Streaming variant of ``POST /responses``.

The streaming endpoint at
``POST /rest/v1/conversations/{id}/responses/stream`` emits Server-Sent
Events as the assistant thinks and generates its reply. Compared to the
non-streaming create + poll pattern in ``responses.py`` this gives the caller
real-time visibility into reasoning steps (tool calls, resource searches),
which is what most user-facing chat surfaces want.

See:
https://docs.moveworks.com/api-reference/conversations-api/conversations-api/responses/create-response-stream

Wire format:

- ``Content-Type: text/event-stream; charset=utf-8``
- Each frame is a standard SSE event with both an ``event:`` name line and a
  ``data:`` line carrying a JSON payload
- Sequence is ``RESPONSE_CREATED`` -> ``RESPONSE_IN_PROGRESS`` ->
  one or more ``RESPONSE_OUTPUT_DELTA`` -> ``RESPONSE_COMPLETED``
  (or ``RESPONSE_FAILED``, surfaced here as ``ResponseCompleted`` with
  ``status="FAILED"`` so callers always get a terminal event)
- An ``ERROR`` event (``{"error": {"code", "message"}}``) ends the stream
  with a server-side failure; it is raised here as ``StreamError``
- The stream closes after the terminal event. There is no ``[DONE]`` marker.
  A stream that closes, or a connection that drops, before the terminal
  event raises ``StreamClosedError``; the response may still finish, so poll
  it with ``responses.wait_for_completion``.
- Each ``RESPONSE_OUTPUT_DELTA`` carries a complete new ``output`` object,
  not a patch. ``REASONING_MESSAGE`` outputs contain a running progress trail;
  the final ``MESSAGE`` output contains the assistant's user-facing reply.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, Optional, Sequence

import requests

from .client import ConversationsApiClient, _parse_retry_after, error_code
from .errors import (
    AuthenticationError,
    ConversationsApiError,
    DownstreamServiceError,
    RateLimitedError,
    RequestRejectedError,
    StreamClosedError,
    StreamError,
)
from .responses import user_input

STREAM_PATH_TEMPLATE = "/rest/v1/conversations/{conversation_id}/responses/stream"


@dataclass(frozen=True)
class StreamEvent:
    """Base class for events emitted by ``stream_response``."""

    sequence_number: int
    raw: Dict[str, Any] = field(repr=False)


@dataclass(frozen=True)
class ResponseCreated(StreamEvent):
    response_id: str
    conversation_id: str


@dataclass(frozen=True)
class ResponseInProgress(StreamEvent):
    response_id: str


@dataclass(frozen=True)
class OutputDelta(StreamEvent):
    """A single ``RESPONSE_OUTPUT_DELTA`` frame.

    ``output_type`` is ``"REASONING_MESSAGE"`` or ``"MESSAGE"``.
    ``text`` is the ``content.markdown_text.text`` (or plain-text equivalent)
    pulled out for convenience; ``output`` holds the full object if you need
    actions, feedback callback IDs, or citations.
    """

    output_type: str
    text: Optional[str]
    output: Dict[str, Any]


@dataclass(frozen=True)
class ResponseCompleted(StreamEvent):
    response_id: str
    status: str


def stream_response(
    client: ConversationsApiClient,
    conversation_id: str,
    text: Optional[str] = None,
    *,
    callback_id: Optional[str] = None,
    file_ids: Sequence[str] = (),
    timeout_seconds: float = 120.0,
) -> Iterator[StreamEvent]:
    """POST a message and yield SSE events until the response completes.

    Args:
        client: A ``ConversationsApiClient`` with a valid bearer provider.
        conversation_id: The conversation to append the message to.
        text: The user's message (sent as ``input.chat.text``).
        callback_id: Submit a CALLBACK_ACTION from a prior assistant message
            instead of free text (e.g. pressing an "Ask GPT" button), sent as
            ``input.action.callback_id``. See ``responses.user_input``.
        file_ids: Files API upload ids to attach to a ``text`` turn.
        timeout_seconds: Idle timeout: the longest wait for the connection
            and then for each next chunk, not a cap on the whole stream.

    Yields:
        Typed ``StreamEvent`` instances in wire order. The iterator returns
        once ``RESPONSE_COMPLETED`` has been yielded.

    Raises:
        StreamError: the server sent an ``ERROR`` event mid-stream.
        StreamClosedError: the stream ended or the connection dropped before
            a terminal event. ``response_id`` is set if one was announced.
        RateLimitedError: 429 when opening the stream.
        AuthenticationError: 401 when opening the stream, after one retry
            with a refreshed bearer.
        RequestRejectedError: any other 4xx when opening the stream.
        DownstreamServiceError: 5xx when opening the stream.

    Note:
        Opens the request with ``client.open_stream`` rather than
        ``client.post`` and never resends it: the POST submits the turn, so
        a retry could post the user's message twice.
    """
    body = {"input": user_input(text, callback_id=callback_id, file_ids=file_ids)}
    path = STREAM_PATH_TEMPLATE.format(conversation_id=conversation_id)
    try:
        response = client.open_stream(path, json=body, timeout=timeout_seconds)
    except requests.RequestException as exc:
        raise ConversationsApiError(f"network error opening stream: {exc}") from exc

    status = response.status_code
    if status >= 400:
        detail = response.text[:500]
        code = error_code(response)
        trace_id = response.headers.get("x-trace-id")
        retry_after = _parse_retry_after(response)
        response.close()
        message = f"stream open failed: {status} {detail}"
        if status == 401:
            raise AuthenticationError(message, status_code=401, code=code, trace_id=trace_id)
        if status == 429:
            raise RateLimitedError(message, retry_after=retry_after, code=code, trace_id=trace_id)
        if status < 500:
            raise RequestRejectedError(message, status_code=status, code=code, trace_id=trace_id)
        raise DownstreamServiceError(message, status_code=status, trace_id=trace_id, code=code)

    response_id: Optional[str] = None
    try:
        try:
            for frame in _iter_sse_frames(response):
                event = _parse_event(frame)
                if event is None:
                    continue
                if isinstance(event, ResponseCreated) and event.response_id:
                    response_id = event.response_id
                yield event
                if isinstance(event, ResponseCompleted):
                    return
        except requests.RequestException as exc:
            raise StreamClosedError(f"stream connection dropped: {exc}", response_id=response_id) from exc
        raise StreamClosedError("stream closed before a terminal event", response_id=response_id)
    finally:
        response.close()


def _iter_sse_frames(response: requests.Response) -> Iterator[Dict[str, str]]:
    """Parse the SSE byte stream into ``{event, data}`` dicts.

    Follows the SSE spec: fields split on the first colon, blank line
    dispatches the accumulated event, ``data`` fields concatenate with
    newlines when repeated.
    """
    current: Dict[str, str] = {}
    for raw_line in response.iter_lines():
        if raw_line is None:
            continue
        # SSE is UTF-8 by spec, so decode here rather than trust the
        # Content-Type charset that decode_unicode depends on.
        raw = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
        if raw == "":
            if current:
                yield current
                current = {}
            continue
        if raw.startswith(":"):
            continue
        field_name, _, value = raw.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field_name == "data":
            current["data"] = current.get("data", "") + value
        elif field_name == "event":
            current["event"] = value
    if current:
        yield current


def _parse_event(frame: Dict[str, str]) -> Optional[StreamEvent]:
    data_str = frame.get("data")
    if not data_str:
        return None
    try:
        data = json.loads(data_str)
    except json.JSONDecodeError:
        return None

    event_type = data.get("event_type") or frame.get("event") or ""
    seq = int(data.get("sequence_number", 0))

    if event_type == "RESPONSE_CREATED":
        resp = data.get("response") or {}
        return ResponseCreated(
            sequence_number=seq,
            raw=data,
            response_id=resp.get("response_id", ""),
            conversation_id=resp.get("conversation_id", ""),
        )
    if event_type == "RESPONSE_IN_PROGRESS":
        resp = data.get("response") or {}
        return ResponseInProgress(
            sequence_number=seq,
            raw=data,
            response_id=resp.get("response_id", ""),
        )
    if event_type == "RESPONSE_OUTPUT_DELTA":
        output = data.get("output") or {}
        output_type = output.get("type", "")
        return OutputDelta(
            sequence_number=seq,
            raw=data,
            output_type=output_type,
            text=_extract_output_text(output),
            output=output,
        )
    if event_type == "ERROR":
        error = data.get("error") or {}
        raise StreamError(
            error.get("message") or error.get("code") or "the stream reported an error",
            code=error.get("code"),
        )
    if event_type in ("RESPONSE_COMPLETED", "RESPONSE_FAILED"):
        resp = data.get("response") or {}
        return ResponseCompleted(
            sequence_number=seq,
            raw=data,
            response_id=resp.get("response_id", ""),
            status=resp.get("status", "") or ("FAILED" if event_type == "RESPONSE_FAILED" else ""),
        )
    return None


def _extract_output_text(output: Dict[str, Any]) -> Optional[str]:
    """Pull the text out of a ``REASONING_MESSAGE`` or ``MESSAGE`` output."""
    output_type = output.get("type")
    if not output_type:
        return None
    body = output.get(output_type.lower())
    if not isinstance(body, dict):
        return None
    content = body.get("content") or {}
    content_type = content.get("type")
    if not content_type:
        return None
    content_body = content.get(content_type.lower())
    if isinstance(content_body, dict):
        text = content_body.get("text")
        if isinstance(text, str):
            return text
    return None
