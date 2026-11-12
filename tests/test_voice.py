"""The voice demo: speech prep, tuning, the Realtime session, the orchestrator harness, and the server.

OpenAI Realtime and the Conversations API are both mocked: client secrets come
from a fake HTTP session, and orchestrator turns run against fake library
calls or a real client over a mock HTTP session.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, Generator, List, Optional
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from examples.voice import orchestrator as orch
from examples.voice import server
from examples.voice.orchestrator import Orchestrator, Sessions, VoiceSession, latest_status
from examples.voice.realtime import CALLS_URL, RealtimeError, create_client_secret, session_config
from examples.voice.speech import answer_text, sections, speakable
from examples.voice.tuning import Tuning
from moveworks_capi.client import ConversationsApiClient
from moveworks_capi.errors import RateLimitedError, StreamClosedError, StreamError
from moveworks_capi.responses import Response
from moveworks_capi.streaming import OutputDelta, ResponseCompleted, ResponseCreated

FIXTURES = Path(__file__).parent / "fixtures"


def _message(text: str, actions: Optional[List[Dict[str, Any]]] = None, message_id: str = "msg_1") -> Dict[str, Any]:
    return {
        "message_id": message_id,
        "actor": "ASSISTANT",
        "content": {"type": "MARKDOWN_TEXT", "markdown_text": {"text": text}},
        "actions": actions or [],
    }


def _response(status: str = "COMPLETED", messages: Optional[List[Dict[str, Any]]] = None, response_id: str = "resp_1") -> Response:
    outputs = [{"type": "MESSAGE", "message": m} for m in (messages or [])]
    return Response.from_json({"response_id": response_id, "conversation_id": "conv_1", "status": status, "outputs": outputs})


def _reasoning(text: str, seq: int = 1) -> OutputDelta:
    output = {"type": "REASONING_MESSAGE", "reasoning_message": {"content": {"type": "PLAIN_TEXT", "plain_text": {"text": text}}}}
    return OutputDelta(seq, {}, "REASONING_MESSAGE", text, output)


def _answer_delta(message: Dict[str, Any], seq: int = 2) -> OutputDelta:
    return OutputDelta(seq, {}, "MESSAGE", None, {"type": "MESSAGE", "message": message})


# --- speech -----------------------------------------------------------------


class TestSpeech:
    def test_markdown_becomes_plain_sentences(self) -> None:
        text = (
            "## Reset steps\n\n1. Open **Settings** :point_right: then [Okta](https://okta.example.com)\n"
            "- Visit https://help.example.com/reset for more\n\n| System | Status |\n|---|---|\n| VPN | Up |"
        )
        assert speakable(text) == "Reset steps. Open Settings then Okta. Visit the link for more. System, Status. VPN, Up."

    def test_slack_links_keep_their_label(self) -> None:
        assert speakable("See <https://portal.example.com/kb/1|the VPN guide>.") == "See the VPN guide."

    def test_a_guide_splits_at_its_phases(self) -> None:
        guide = (
            "The two VPN clients can conflict.\n\n"
            "Phase 1. Reset the order. Disconnect the VPN, then restart the proxy.\n\n"
            "Phase 2. Reconnect. Connect the VPN again.\n\n"
            "**Step 3** Call the service desk if it still drops."
        )
        assert sections(guide) == [
            "The two VPN clients can conflict.",
            "Phase 1. Reset the order. Disconnect the VPN, then restart the proxy.",
            "Phase 2. Reconnect. Connect the VPN again.",
            "Step 3 Call the service desk if it still drops.",
        ]

    def test_a_long_answer_without_structure_splits_by_length(self) -> None:
        text = " ".join(f"Sentence number {i} is here." for i in range(60))
        parts = sections(text, max_chars=200)
        assert len(parts) > 1 and all(len(p) <= 200 for p in parts)
        assert " ".join(parts) == text

    def test_answer_text_drops_the_flattened_form_copies(self) -> None:
        message = json.loads((FIXTURES / "portal_form_message.json").read_text())
        text = speakable(answer_text([message]))
        assert "401(k) Plan Enrollment form" in text
        assert "Enroll in or modify your 401(k) plan" not in text

    def test_latest_status_is_the_newest_line_of_the_trail(self) -> None:
        assert latest_status("Looking up the caller\n\n**Searching** knowledge for VPN errors") == "Searching knowledge for VPN errors."
        assert latest_status("") == ""


# --- tuning and the Realtime session ----------------------------------------


class TestTuning:
    def test_loads_every_prompt(self) -> None:
        tuning = Tuning.load(env={})
        for name in ("voice_instructions", "delegation", "welcome", "warmup", "filler", "progress"):
            assert getattr(tuning, name), name
        assert "{query}" in tuning.filler and "{status}" in tuning.progress

    def test_env_overrides(self, tmp_path: Path) -> None:
        for name in ("voice_instructions", "delegation", "welcome", "warmup", "filler", "progress"):
            (tmp_path / f"{name}.md").write_text(f"my {name}")
        tuning = Tuning.load(env={
            "VOICE_PROMPTS_DIR": str(tmp_path), "OPENAI_REALTIME_MODEL": "gpt-realtime-mini", "VOICE_REALTIME_VOICE": "cedar",
            "VOICE_WARMUP": "off", "VOICE_CANCEL_ON_BARGE_IN": "false", "VOICE_PROGRESS_MIN_MS": "3000", "VOICE_GLOSSARY": "Okta, PTO",
        })
        assert tuning.voice_instructions == "my voice_instructions"
        assert (tuning.model, tuning.voice, tuning.warmup, tuning.cancel_lookup_on_barge_in) == ("gpt-realtime-mini", "cedar", "", False)
        assert tuning.progress_min_ms == 3000 and tuning.glossary == "Okta, PTO"

    def test_the_client_view_has_timing_and_wording_only(self) -> None:
        view = Tuning.load(env={}).client_view()
        assert set(view) == {
            "welcome", "warmup", "filler", "progress", "upfront_filler_ms", "progress_min_ms", "progress_max_ms",
            "cancel_lookup_on_barge_in", "warmup_wait_ms",
        }


class TestRealtime:
    def test_session_config(self) -> None:
        config = session_config(Tuning.load(env={}))
        assert config["type"] == "realtime" and config["model"] == "gpt-realtime-2.1"
        assert config["audio"]["input"]["turn_detection"]["type"] == "semantic_vad"
        assert config["audio"]["input"]["turn_detection"]["interrupt_response"] is True
        assert config["audio"]["output"]["voice"] == "marin"
        [tool] = config["tools"]
        assert tool["name"] == "ask_orchestrator"
        assert tool["parameters"]["required"] == ["query"]
        assert set(tool["parameters"]["properties"]) == {"query", "choice", "expected_duration"}

    def test_a_glossary_biases_transcription_and_pronunciation(self) -> None:
        config = session_config(Tuning.load(env={"VOICE_GLOSSARY": "Okta, Workday"}))
        assert config["audio"]["input"]["transcription"]["prompt"] == "Okta, Workday"
        assert "Okta, Workday" in config["instructions"]

    def test_mints_a_client_secret_with_the_server_key(self) -> None:
        http = MagicMock()
        http.post.return_value = MagicMock(status_code=200, json=lambda: {"value": "ek_123", "expires_at": 1900000000})
        assert create_client_secret("sk-test", {"type": "realtime"}, http=http) == {"value": "ek_123", "expires_at": 1900000000}
        url = http.post.call_args.args[0]
        assert url == "https://api.openai.com/v1/realtime/client_secrets"
        assert http.post.call_args.kwargs["headers"]["Authorization"] == "Bearer sk-test"
        assert http.post.call_args.kwargs["json"]["session"] == {"type": "realtime"}

    def test_a_refused_secret_raises(self) -> None:
        http = MagicMock()
        http.post.return_value = MagicMock(status_code=401, text="invalid key")
        with pytest.raises(RealtimeError):
            create_client_secret("sk-bad", {}, http=http)


# --- the orchestrator harness -----------------------------------------------


class _FakeCapi:
    """Stands in for the library calls the orchestrator makes."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.created: List[str] = []
        self.streamed: List[Dict[str, Any]] = []
        self.submitted: List[Dict[str, Any]] = []
        self.polled: List[Dict[str, Any]] = []
        self.stream_events: Callable[[], Any] = lambda: iter([
            ResponseCreated(0, {}, "resp_1", "conv_1"),
            _reasoning("Looking up your calendar"),
            _reasoning("Looking up your calendar\nReading Monday's events"),
            _answer_delta(_message("You have **three** meetings on Monday.")),
            ResponseCompleted(3, {}, "resp_1", "COMPLETED"),
        ])
        self.final: Any = _response(messages=[_message("Polled answer.")])
        self.history: List[Dict[str, Any]] = []
        monkeypatch.setattr(orch, "create_conversation", self._create_conversation)
        monkeypatch.setattr(orch, "stream_response", self._stream_response)
        monkeypatch.setattr(orch, "create_response", self._create_response)
        monkeypatch.setattr(orch, "wait_for_completion", self._wait_for_completion)
        monkeypatch.setattr(orch, "list_messages", lambda client, cid, limit=None: {"messages": self.history})

    def _create_conversation(self, client: Any, *, title: Optional[str] = None) -> Dict[str, Any]:
        self.created.append(title or "")
        return {"conversation_id": "conv_1"}

    def _stream_response(self, client: Any, conversation_id: str, text: Optional[str] = None, **kwargs: Any) -> Any:
        self.streamed.append({"text": text, **kwargs})
        return self.stream_events()

    def _create_response(self, client: Any, conversation_id: str, text: Optional[str] = None, **kwargs: Any) -> Dict[str, Any]:
        self.submitted.append({"text": text, **kwargs})
        return {"response_id": "resp_2", "status": "IN_PROGRESS"}

    def _wait_for_completion(self, client: Any, conversation_id: str, response_id: str, **kwargs: Any) -> Response:
        self.polled.append({"response_id": response_id, **kwargs})
        if isinstance(self.final, Exception):
            raise self.final
        return self.final


def _raising(events: List[Any], error: Exception) -> Callable[[], Any]:
    def generate() -> Any:
        yield from events
        raise error

    return generate


@pytest.fixture
def capi(monkeypatch: pytest.MonkeyPatch) -> _FakeCapi:
    return _FakeCapi(monkeypatch)


def _ask(query: str = "What's on my calendar Monday?", session: Optional[VoiceSession] = None, **kwargs: Any) -> List[Dict[str, Any]]:
    return list(Orchestrator(MagicMock(), **kwargs).ask(session or VoiceSession("s1"), query))


def _result(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    assert events[-1]["type"] == "result"
    return events[-1]["output"]


class TestOrchestrator:
    def test_streams_progress_then_the_answer_from_the_stream(self, capi: _FakeCapi) -> None:
        events = _ask()
        assert [e for e in events if e["type"] == "progress"] == [
            {"type": "progress", "text": "Looking up your calendar."},
            {"type": "progress", "text": "Reading Monday's events."},
        ]
        assert _result(events) == {"status": "answered", "answer": "You have three meetings on Monday."}
        assert capi.polled == [] and capi.submitted == []

    def test_one_conversation_per_voice_session(self, capi: _FakeCapi) -> None:
        session = VoiceSession("s1")
        orchestrator = Orchestrator(MagicMock())
        list(orchestrator.ask(session, "first"))
        list(orchestrator.ask(session, "second"))
        assert capi.created == ["Voice call"]
        assert [s["text"] for s in capi.streamed] == ["first", "second"]

    def test_the_120_second_stream_limit_switches_to_polling(self, capi: _FakeCapi) -> None:
        capi.stream_events = _raising(
            [ResponseCreated(0, {}, "resp_1", "conv_1"), _reasoning("Searching knowledge")],
            StreamError("Streaming timeout", code="CONVERSATIONS_REST_API_STREAM_LISTEN_TIMEOUT"),
        )
        assert _result(_ask())["answer"] == "Polled answer."
        assert capi.polled[0]["response_id"] == "resp_1" and capi.polled[0]["initial_delay_seconds"] == 1.0
        assert capi.submitted == []

    def test_a_dropped_stream_switches_to_polling(self, capi: _FakeCapi) -> None:
        capi.stream_events = _raising([], StreamClosedError("dropped", response_id="resp_1"))
        assert _result(_ask())["status"] == "answered"
        assert capi.polled[0]["response_id"] == "resp_1"

    def test_a_stream_error_before_the_turn_was_accepted_is_never_resent(self, capi: _FakeCapi) -> None:
        capi.stream_events = _raising([], StreamError("bad request"))
        assert _result(_ask())["status"] == "error"
        assert capi.submitted == []

    def test_a_rate_limited_stream_open_submits_through_the_shared_client(self, capi: _FakeCapi) -> None:
        capi.stream_events = _raising([], RateLimitedError("rate limited", retry_after=5.0))
        assert _result(_ask("hi"))["answer"] == "Polled answer."
        assert capi.submitted == [{"text": "hi", "callback_id": None}]
        assert capi.polled[0]["response_id"] == "resp_2"

    def test_rate_limited_after_retries_is_busy(self, capi: _FakeCapi) -> None:
        capi.stream_events = _raising([], RateLimitedError("rate limited"))
        capi.final = RateLimitedError("rate limited")
        assert _result(_ask())["status"] == "busy"

    def test_a_slow_turn_is_timed_out_not_failed(self, capi: _FakeCapi) -> None:
        capi.stream_events = _raising([ResponseCreated(0, {}, "resp_1", "conv_1")], StreamError("timeout"))
        capi.final = TimeoutError("still IN_PROGRESS")
        assert _result(_ask())["status"] == "timed_out"

    def test_a_failed_response_keeps_its_message(self, capi: _FakeCapi) -> None:
        capi.stream_events = lambda: iter([
            ResponseCreated(0, {}, "resp_1", "conv_1"),
            _answer_delta(_message("I couldn't move the meeting. The calendar service returned an error.")),
            ResponseCompleted(2, {}, "resp_1", "FAILED"),
        ])
        assert _result(_ask()) == {"status": "failed", "answer": "I couldn't move the meeting. The calendar service returned an error."}

    def test_a_guide_comes_back_in_sections(self, capi: _FakeCapi) -> None:
        capi.stream_events = lambda: iter([
            _answer_delta(_message("They conflict.\n\nPhase 1. Disconnect the VPN.\n\nPhase 2. Reconnect it.")),
            ResponseCompleted(2, {}, "resp_1", "COMPLETED"),
        ])
        output = _result(_ask("How do I fix my VPN?"))
        assert output["sections"] == ["They conflict.", "Phase 1. Disconnect the VPN.", "Phase 2. Reconnect it."]
        assert output["answer"] == "They conflict. Phase 1. Disconnect the VPN. Phase 2. Reconnect it."

    def test_buttons_are_offered_then_pressed_by_label(self, capi: _FakeCapi) -> None:
        capi.stream_events = lambda: iter([
            _answer_delta(_message("Submit the request?", [
                {"type": "CALLBACK_ACTION", "label": "Submit", "callback_action": {"callback_id": "cb_submit"}},
                {"type": "URL_ACTION", "label": "Open it", "url_action": {"url": "https://portal.example.com/r/1"}},
            ])),
            ResponseCompleted(2, {}, "resp_1", "COMPLETED"),
        ])
        session = VoiceSession("s1")
        orchestrator = Orchestrator(MagicMock())
        output = _result(list(orchestrator.ask(session, "Order a monitor")))
        assert output["buttons"] == ["Submit"] and output["has_link_or_form"] is True
        list(orchestrator.ask(session, "submit it", choice="submit"))
        assert capi.streamed[1] == {"text": None, "callback_id": "cb_submit", "timeout_seconds": orch.STREAM_TIMEOUT_SECONDS}

    def test_an_empty_response_reads_the_late_reply_from_history(self, capi: _FakeCapi) -> None:
        capi.stream_events = lambda: iter([ResponseCreated(0, {}, "resp_1", "conv_1"), ResponseCompleted(1, {}, "resp_1", "COMPLETED")])
        capi.final = _response(messages=[])
        capi.history = [
            {"actor": "USER", "response_id": "resp_1", "created_at": "2026-01-01T00:00:00Z"},
            {**_message("The live chat has ended."), "created_at": "2026-01-01T00:00:01Z"},
        ]
        assert _result(_ask())["answer"] == "The live chat has ended."
        assert capi.polled[0]["initial_delay_seconds"] == 0.0

    def test_polling_mode_submits_then_polls(self, capi: _FakeCapi) -> None:
        assert _result(_ask("hi", stream=False))["answer"] == "Polled answer."
        assert capi.streamed == [] and capi.submitted == [{"text": "hi", "callback_id": None}]

    def test_a_cancelled_lookup_closes_the_stream_and_frees_the_session(self, capi: _FakeCapi) -> None:
        closed: List[bool] = []

        def events() -> Any:
            try:
                yield ResponseCreated(0, {}, "resp_1", "conv_1")
                yield _reasoning("Searching")
                yield _reasoning("Searching\nStill searching")
            finally:
                closed.append(True)

        capi.stream_events = events
        session = VoiceSession("s1")
        lookup = Orchestrator(MagicMock()).ask(session, "slow question")
        assert next(lookup)["type"] == "progress"
        lookup.close()
        assert closed == [True]
        assert session.lock.acquire(blocking=False)

    def test_warm_up_returns_the_answer_as_context(self, capi: _FakeCapi) -> None:
        context = Orchestrator(MagicMock()).warm_up(VoiceSession("s1"), "Who am I talking to?")
        assert context == "You have three meetings on Monday."


class _FakeHttp:
    def __init__(self, payload: Optional[dict] = None, lines: Optional[List[str]] = None) -> None:
        self.status_code = 200
        self._payload = payload or {}
        self._lines = lines or []
        self.headers: Dict[str, str] = {}
        self.text = json.dumps(self._payload)

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        pass

    def iter_lines(self, decode_unicode: bool = False) -> Any:
        yield from self._lines

    def close(self) -> None:
        pass


def _sse(event: str, data: dict) -> List[str]:
    return [f"event: {event}", f"data: {json.dumps({'event_type': event, **data})}", ""]


def test_an_orchestrator_turn_end_to_end_through_the_real_client() -> None:
    """The orchestrator over the real library, with only the HTTP session mocked."""
    message = _message("Your laptop **ships Friday**.")
    http = MagicMock()
    http.request.return_value = _FakeHttp(payload={"conversation_id": "conv_1"})
    http.post.return_value = _FakeHttp(lines=[
        *_sse("RESPONSE_CREATED", {"response": {"response_id": "resp_1", "conversation_id": "conv_1"}}),
        *_sse("RESPONSE_OUTPUT_DELTA", {"output": {
            "type": "REASONING_MESSAGE",
            "reasoning_message": {"content": {"type": "PLAIN_TEXT", "plain_text": {"text": "Checking your order"}}},
        }}),
        *_sse("RESPONSE_OUTPUT_DELTA", {"output": {"type": "MESSAGE", "message": message}}),
        *_sse("RESPONSE_COMPLETED", {"response": {"response_id": "resp_1", "status": "COMPLETED"}}),
    ])
    client = ConversationsApiClient("https://api.example.com", "bot", lambda: "token", session=http)

    events = list(Orchestrator(client).ask(VoiceSession("s1"), "When does my laptop ship?"))

    assert events == [
        {"type": "progress", "text": "Checking your order."},
        {"type": "result", "output": {"status": "answered", "answer": "Your laptop ships Friday."}},
    ]
    assert http.post.call_args.args[0] == "https://api.example.com/rest/v1/conversations/conv_1/responses/stream"
    assert http.post.call_args.kwargs["json"] == {"input": {"chat": {"text": "When does my laptop ship?"}}}
    assert http.request.call_count == 1  # created the conversation; the answer came from the stream


# --- the server -------------------------------------------------------------


class _FakeOrchestrator(Orchestrator):
    def __init__(self, events: Optional[List[Dict[str, Any]]] = None, context: str = "Caller: Sam Lee. Use first name Sam.") -> None:
        super().__init__(MagicMock())
        self.events = events or [
            {"type": "progress", "text": "Reading your calendar."},
            {"type": "result", "output": {"status": "answered", "answer": "Three meetings."}},
        ]
        self.context = context
        self.asked: List[Dict[str, Any]] = []

    def ask(self, session: VoiceSession, query: str, *, choice: Optional[str] = None) -> Generator[Dict[str, Any], None, None]:
        self.asked.append({"session": session.session_id, "query": query, "choice": choice})
        yield from self.events

    def warm_up(self, session: VoiceSession, query: str) -> str:
        return self.context


@pytest.fixture
def secrets_http() -> MagicMock:
    http = MagicMock()
    http.post.return_value = MagicMock(status_code=200, json=lambda: {"value": "ek_test", "expires_at": 1900000000})
    return http


def _app(secrets_http: MagicMock, **kwargs: Any) -> Any:
    return server.create_app(
        openai_api_key="sk-server-only",
        tuning=kwargs.pop("tuning", Tuning.load(env={})),
        orchestrator=kwargs.pop("orchestrator", _FakeOrchestrator()),
        user="sam@example.com",
        http=secrets_http,
        **kwargs,
    )


@pytest.fixture
def local(secrets_http: MagicMock) -> TestClient:
    return TestClient(_app(secrets_http), base_url="http://127.0.0.1:8095")


def _start(client: TestClient) -> Dict[str, Any]:
    response = client.post("/api/session", json={})
    assert response.status_code == 200, response.text
    return response.json()


class TestServer:
    def test_serves_the_page_and_script(self, local: TestClient) -> None:
        assert "voice.js" in local.get("/").text
        assert "ask_orchestrator" in local.get("/static/voice.js").text

    def test_fillers_cannot_call_tools(self, local: TestClient) -> None:
        # Filler prompts quote the caller's query and the orchestrator's status; a filler must never act on them.
        script = local.get("/static/voice.js").text
        assert 'tool_choice: "none"' in script
        assert "JSON.stringify(String(text" in script

    def test_a_foreign_host_is_refused(self, secrets_http: MagicMock) -> None:
        rebound = TestClient(_app(secrets_http), base_url="http://attacker.example:8095")
        assert rebound.post("/api/session", json={}).status_code == 400

    def test_cross_origin_and_non_json_writes_are_refused(self, local: TestClient) -> None:
        assert local.post("/api/session", json={}, headers={"Origin": "https://attacker.example"}).status_code == 403
        assert local.post("/api/session", content="{}", headers={"Content-Type": "text/plain"}).status_code == 415

    def test_a_session_gets_a_client_secret_never_the_api_key(self, local: TestClient, secrets_http: MagicMock) -> None:
        body = _start(local)
        assert body["client_secret"] == "ek_test"
        assert body["calls_url"] == CALLS_URL
        assert body["user"] == "sam@example.com"
        assert body["tuning"]["progress_min_ms"] == 5000
        assert "sk-server-only" not in json.dumps(body)
        sent = secrets_http.post.call_args.kwargs["json"]["session"]
        assert sent["tools"][0]["name"] == "ask_orchestrator"

    def test_a_refused_secret_is_a_502(self, secrets_http: MagicMock) -> None:
        secrets_http.post.return_value = MagicMock(status_code=401, text="bad key")
        client = TestClient(_app(secrets_http), base_url="http://127.0.0.1:8095")
        assert client.post("/api/session", json={}).status_code == 502

    def test_ask_streams_progress_then_the_result_as_ndjson(self, secrets_http: MagicMock) -> None:
        fake = _FakeOrchestrator()
        client = TestClient(_app(secrets_http, orchestrator=fake), base_url="http://127.0.0.1:8095")
        session_id = _start(client)["session_id"]
        response = client.post("/api/ask", json={"session_id": session_id, "query": "What's on my calendar?"})
        assert response.headers["content-type"].startswith("application/x-ndjson")
        lines = [json.loads(line) for line in response.text.splitlines()]
        assert lines == fake.events
        assert fake.asked == [{"session": session_id, "query": "What's on my calendar?", "choice": None}]

    def test_ask_needs_a_live_session_and_a_query(self, local: TestClient) -> None:
        assert local.post("/api/ask", json={"session_id": "nope", "query": "hi"}).status_code == 404
        session_id = _start(local)["session_id"]
        assert local.post("/api/ask", json={"session_id": session_id, "query": "  "}).status_code == 400

    def test_warm_up_returns_the_caller_context(self, local: TestClient) -> None:
        session_id = _start(local)["session_id"]
        assert local.post("/api/warmup", json={"session_id": session_id}).json() == {"context": "Caller: Sam Lee. Use first name Sam."}

    def test_warm_up_off_returns_nothing(self, secrets_http: MagicMock) -> None:
        client = TestClient(_app(secrets_http, tuning=Tuning.load(env={"VOICE_WARMUP": "off"})), base_url="http://127.0.0.1:8095")
        session_id = _start(client)["session_id"]
        assert client.post("/api/warmup", json={"session_id": session_id}).json() == {"context": ""}

    def test_ending_a_session(self, secrets_http: MagicMock) -> None:
        sessions = Sessions()
        client = TestClient(_app(secrets_http, sessions=sessions), base_url="http://127.0.0.1:8095")
        session_id = _start(client)["session_id"]
        assert len(sessions) == 1
        assert client.post("/api/end", json={"session_id": session_id}).json() == {"ended": True}
        assert len(sessions) == 0
