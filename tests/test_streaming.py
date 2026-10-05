"""Streaming SSE parser and event dispatcher."""

from __future__ import annotations

import json
from typing import Iterable, List, Tuple
from unittest.mock import MagicMock

import pytest
import requests

from moveworks_capi.client import ConversationsApiClient
from moveworks_capi.errors import (
    AuthenticationError,
    ConversationsApiError,
    DownstreamServiceError,
    RateLimitedError,
    RequestRejectedError,
    StreamClosedError,
    StreamError,
)
from moveworks_capi.streaming import (
    OutputDelta,
    ResponseCompleted,
    ResponseCreated,
    ResponseInProgress,
    stream_response,
)


class _FakeHttpResponse:
    def __init__(
        self,
        status_code: int,
        lines: Iterable[str | bytes],
        headers: dict | None = None,
        raise_after: Exception | None = None,
    ) -> None:
        self.status_code = status_code
        self._raise_after = raise_after
        self._lines = list(lines)
        self.headers = headers or {}
        self.text = "\n".join(str(line) for line in self._lines)

    def json(self):
        return json.loads(self.text)

    def iter_lines(self, decode_unicode: bool = False) -> Iterable[str | bytes]:
        yield from self._lines
        if self._raise_after is not None:
            raise self._raise_after

    def close(self) -> None:
        pass


def _build_client(fake_response: _FakeHttpResponse) -> Tuple[ConversationsApiClient, MagicMock]:
    """A real client sending through a mock session."""
    session = MagicMock()
    session.post.return_value = fake_response
    client = ConversationsApiClient(
        base_url="https://api.example.com",
        bot_name="test-bot",
        bearer_provider=lambda: "test-bearer",
        session=session,
    )
    return client, session


def _frame(event: str, data: dict) -> List[str]:
    return [f"event: {event}", f"data: {json.dumps(data)}", ""]


def _happy_path_lines() -> List[str]:
    lines: List[str] = []
    lines += _frame(
        "RESPONSE_CREATED",
        {
            "event_type": "RESPONSE_CREATED",
            "sequence_number": 1,
            "response": {"response_id": "resp_1", "conversation_id": "conv_1"},
        },
    )
    lines += _frame(
        "RESPONSE_IN_PROGRESS",
        {
            "event_type": "RESPONSE_IN_PROGRESS",
            "sequence_number": 2,
            "response": {"response_id": "resp_1"},
        },
    )
    lines += _frame(
        "RESPONSE_OUTPUT_DELTA",
        {
            "event_type": "RESPONSE_OUTPUT_DELTA",
            "sequence_number": 3,
            "output": {
                "type": "REASONING_MESSAGE",
                "reasoning_message": {
                    "content": {"type": "MARKDOWN_TEXT", "markdown_text": {"text": "thinking..."}},
                },
            },
        },
    )
    lines += _frame(
        "RESPONSE_OUTPUT_DELTA",
        {
            "event_type": "RESPONSE_OUTPUT_DELTA",
            "sequence_number": 4,
            "output": {
                "type": "MESSAGE",
                "message": {
                    "content": {"type": "MARKDOWN_TEXT", "markdown_text": {"text": "hello!"}},
                },
            },
        },
    )
    lines += _frame(
        "RESPONSE_COMPLETED",
        {
            "event_type": "RESPONSE_COMPLETED",
            "sequence_number": 5,
            "response": {"response_id": "resp_1", "status": "COMPLETED"},
        },
    )
    return lines


class TestStreamResponse:
    def test_yields_typed_events_in_order(self) -> None:
        client, session = _build_client(_FakeHttpResponse(200, _happy_path_lines()))

        events = list(stream_response(client, "conv_1", "hi"))

        assert [type(e) for e in events] == [
            ResponseCreated,
            ResponseInProgress,
            OutputDelta,
            OutputDelta,
            ResponseCompleted,
        ]
        assert isinstance(events[0], ResponseCreated)
        assert isinstance(events[2], OutputDelta) and isinstance(events[3], OutputDelta)
        assert isinstance(events[4], ResponseCompleted)
        assert events[0].response_id == "resp_1"
        assert events[0].conversation_id == "conv_1"
        assert events[2].output_type == "REASONING_MESSAGE"
        assert events[2].text == "thinking..."
        assert events[3].output_type == "MESSAGE"
        assert events[3].text == "hello!"
        assert events[4].status == "COMPLETED"

    def test_parses_bytes_lines_as_utf8(self) -> None:
        delta = {
            "event_type": "RESPONSE_OUTPUT_DELTA",
            "sequence_number": 1,
            "output": {
                "type": "MESSAGE",
                "message": {"content": {"type": "MARKDOWN_TEXT", "markdown_text": {"text": "café ✓"}}},
            },
        }
        lines = ["event: RESPONSE_OUTPUT_DELTA", "data: " + json.dumps(delta, ensure_ascii=False), ""]
        client, _ = _build_client(_FakeHttpResponse(200, [line.encode("utf-8") for line in lines]))

        events = []
        with pytest.raises(StreamClosedError):
            for event in stream_response(client, "conv_1", "hi"):
                events.append(event)
        assert isinstance(events[0], OutputDelta)
        assert events[0].text == "café ✓"

    def test_sequence_numbers_preserved(self) -> None:
        client, session = _build_client(_FakeHttpResponse(200, _happy_path_lines()))
        events = list(stream_response(client, "conv_1", "hi"))
        assert [e.sequence_number for e in events] == [1, 2, 3, 4, 5]

    def test_returns_after_completed(self) -> None:
        lines = _happy_path_lines() + _frame(
            "RESPONSE_OUTPUT_DELTA",
            {
                "event_type": "RESPONSE_OUTPUT_DELTA",
                "sequence_number": 6,
                "output": {"type": "MESSAGE", "message": {"content": {"type": "MARKDOWN_TEXT", "markdown_text": {"text": "extra"}}}},
            },
        )
        client, session = _build_client(_FakeHttpResponse(200, lines))
        events = list(stream_response(client, "conv_1", "hi"))
        assert len(events) == 5

    def test_ignores_unknown_event_types(self) -> None:
        lines = _frame(
            "UNKNOWN_EVENT",
            {"event_type": "UNKNOWN_EVENT", "sequence_number": 99, "wat": "no"},
        ) + _happy_path_lines()
        client, session = _build_client(_FakeHttpResponse(200, lines))
        events = list(stream_response(client, "conv_1", "hi"))
        assert len(events) == 5

    def test_ignores_malformed_data(self) -> None:
        lines = ["event: RESPONSE_CREATED", "data: not-json", ""] + _happy_path_lines()
        client, session = _build_client(_FakeHttpResponse(200, lines))
        events = list(stream_response(client, "conv_1", "hi"))
        assert len(events) == 5

    def test_401_raises_authentication_error(self) -> None:
        client, session = _build_client(_FakeHttpResponse(401, [], headers={"x-trace-id": "t1"}))
        with pytest.raises(AuthenticationError):
            list(stream_response(client, "conv_1", "hi"))

    def test_500_raises_downstream_service_error(self) -> None:
        client, session = _build_client(_FakeHttpResponse(500, ["server oops"]))
        with pytest.raises(DownstreamServiceError) as exc:
            list(stream_response(client, "conv_1", "hi"))
        assert exc.value.status_code == 500

    def test_posts_to_streaming_endpoint(self) -> None:
        client, session = _build_client(_FakeHttpResponse(200, _happy_path_lines()))
        list(stream_response(client, "conv_ABC", "hi"))
        called_url = session.post.call_args[0][0]
        assert called_url == "https://api.example.com/rest/v1/conversations/conv_ABC/responses/stream"

    def test_sends_required_headers(self) -> None:
        client, session = _build_client(_FakeHttpResponse(200, _happy_path_lines()))
        list(stream_response(client, "conv_1", "hi"))
        headers = session.post.call_args.kwargs["headers"]
        assert headers["Accept"] == "text/event-stream"
        assert headers["Authorization"] == "Bearer test-bearer"
        assert headers["Assistant-Name"] == "test-bot"

    def test_sends_input_text_in_body(self) -> None:
        client, session = _build_client(_FakeHttpResponse(200, _happy_path_lines()))
        list(stream_response(client, "conv_1", "what is my PTO?"))
        body = session.post.call_args.kwargs["json"]
        assert body == {"input": {"chat": {"text": "what is my PTO?"}}}

    def test_sends_callback_as_action_input(self) -> None:
        client, session = _build_client(_FakeHttpResponse(200, _happy_path_lines()))
        list(stream_response(client, "conv_1", callback_id="cb_1"))
        assert session.post.call_args.kwargs["json"] == {"input": {"action": {"callback_id": "cb_1"}}}

    def test_attaches_file_ids_to_chat_input(self) -> None:
        client, session = _build_client(_FakeHttpResponse(200, _happy_path_lines()))
        list(stream_response(client, "conv_1", "see attached", file_ids=["file_a"]))
        body = session.post.call_args.kwargs["json"]
        assert body == {"input": {"chat": {"text": "see attached", "files": [{"file_id": "file_a"}]}}}

    def test_error_event_raises_stream_error(self) -> None:
        lines = _happy_path_lines()[:6] + _frame(
            "ERROR", {"event_type": "ERROR", "sequence_number": 3, "error": {"code": "INTERNAL", "message": "boom"}}
        )
        client, session = _build_client(_FakeHttpResponse(200, lines))
        events = []
        with pytest.raises(StreamError) as exc:
            for event in stream_response(client, "conv_1", "hi"):
                events.append(event)
        assert [type(e) for e in events] == [ResponseCreated, ResponseInProgress]
        assert exc.value.code == "INTERNAL" and "boom" in str(exc.value)

    def test_400_raises_request_rejected_with_code(self) -> None:
        body = json.dumps({"error": {"code": "INVALID_INPUT", "message": "A chat input must carry text, files, or both."}})
        client, session = _build_client(_FakeHttpResponse(400, [body], headers={"x-trace-id": "t1"}))
        with pytest.raises(RequestRejectedError) as exc:
            list(stream_response(client, "conv_1", "hi"))
        assert (exc.value.status_code, exc.value.code, exc.value.trace_id) == (400, "INVALID_INPUT", "t1")

    def test_429_at_open_raises_rate_limited(self) -> None:
        client, session = _build_client(_FakeHttpResponse(429, ["slow down"], headers={"x-trace-id": "t1"}))
        with pytest.raises(RateLimitedError) as exc:
            list(stream_response(client, "conv_1", "hi"))
        assert (exc.value.status_code, exc.value.trace_id) == (429, "t1")
        session.post.assert_called_once()

    def test_close_before_a_terminal_event_raises_with_the_response_id(self) -> None:
        lines = _happy_path_lines()[:6]
        client, _ = _build_client(_FakeHttpResponse(200, lines))
        events = []
        with pytest.raises(StreamClosedError) as exc:
            for event in stream_response(client, "conv_1", "hi"):
                events.append(event)
        assert [type(e) for e in events] == [ResponseCreated, ResponseInProgress]
        assert exc.value.response_id == "resp_1"
        assert isinstance(exc.value, ConversationsApiError)

    def test_connection_drop_mid_stream_raises_stream_closed(self) -> None:
        dropped = requests.exceptions.ChunkedEncodingError("connection broken")
        client, _ = _build_client(_FakeHttpResponse(200, _happy_path_lines()[:3], raise_after=dropped))
        with pytest.raises(StreamClosedError) as exc:
            list(stream_response(client, "conv_1", "hi"))
        assert exc.value.response_id == "resp_1"
        assert exc.value.__cause__ is dropped

    def test_close_before_any_event_has_no_response_id(self) -> None:
        client, _ = _build_client(_FakeHttpResponse(200, []))
        with pytest.raises(StreamClosedError) as exc:
            list(stream_response(client, "conv_1", "hi"))
        assert exc.value.response_id is None
