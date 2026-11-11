"""The voice example: speech formatting, TwiML, the turn runner, and the Twilio webhooks.

Twilio and the Conversations API are both mocked: webhook requests are signed
with a test auth token, and turns run against fake CAPI calls or a real client
over a mock HTTP session.
"""

from __future__ import annotations

import json
from concurrent.futures import Executor, Future
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import MagicMock
from xml.etree import ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from examples.voice import bridge, server
from examples.voice.bridge import CallerDirectory, CallSession, CallSessions, ClientPool, Reply, TurnRunner
from examples.voice.speech import (
    chunk_for_speech,
    match_option,
    reply_text,
    speakable,
    wants_agent,
    wants_more,
    wants_repeat,
)
from examples.voice.twiml import TwiML, is_valid_signature, twilio_signature
from moveworks_capi.client import ConversationsApiClient
from moveworks_capi.errors import RateLimitedError, StreamClosedError, StreamError
from moveworks_capi.responses import Response
from moveworks_capi.streaming import ResponseCompleted, ResponseCreated

FIXTURES = Path(__file__).parent / "fixtures"
AUTH_TOKEN = "test-auth-token"
PUBLIC_URL = "https://voice.example.com"
CALLER = "+15555550100"


def _message(text: str, actions: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    return {
        "actor": "ASSISTANT",
        "content": {"type": "MARKDOWN_TEXT", "markdown_text": {"text": text}},
        "actions": actions or [],
    }


def _response(status: str = "COMPLETED", messages: Optional[List[Dict[str, Any]]] = None, response_id: str = "resp_1") -> Response:
    outputs = [{"type": "MESSAGE", "message": m} for m in (messages or [])]
    return Response.from_json({"response_id": response_id, "conversation_id": "conv_1", "status": status, "outputs": outputs})


# --- speech -----------------------------------------------------------------


class TestSpeakable:
    def test_markdown_becomes_plain_sentences(self) -> None:
        text = (
            "## Reset steps\n\n1. Open **Settings** :point_right: then [Okta](https://okta.example.com)\n"
            "- Visit https://help.example.com/reset for more\n\n| System | Status |\n|---|---|\n| VPN | Up |"
        )
        assert speakable(text) == "Reset steps. Open Settings then Okta. Visit the link for more. System, Status. VPN, Up."

    def test_slack_links_keep_their_label(self) -> None:
        assert speakable("See <https://portal.example.com/kb/1|the VPN guide>.") == "See the VPN guide."

    def test_a_list_item_ending_in_a_conjunction_runs_on(self) -> None:
        assert speakable("Either:\n1. enroll, or\n2. ask a question") == "Either: enroll, or ask a question."

    def test_reply_text_drops_the_flattened_form_copies(self) -> None:
        message = json.loads((FIXTURES / "portal_form_message.json").read_text())
        text = reply_text([message])
        assert "401(k) Plan Enrollment form" in text
        assert "Enroll in or modify your 401(k) plan" not in text
        assert "**" not in text


class TestChunkForSpeech:
    def test_short_text_is_one_chunk(self) -> None:
        assert chunk_for_speech("One. Two.") == ["One. Two."]

    def test_long_text_splits_at_sentence_ends(self) -> None:
        sentences = [f"Sentence number {i} is here." for i in range(40)]
        chunks = chunk_for_speech(" ".join(sentences), max_chars=120)
        assert len(chunks) > 1
        assert all(len(chunk) <= 120 for chunk in chunks)
        assert all(chunk.endswith(".") for chunk in chunks)
        assert " ".join(chunks) == " ".join(sentences)

    def test_a_sentence_longer_than_the_limit_splits_at_a_space(self) -> None:
        chunks = chunk_for_speech("word " * 100, max_chars=50)
        assert all(0 < len(chunk) <= 50 for chunk in chunks)
        assert " ".join(chunks).split() == ["word"] * 100


class TestCallerPhrases:
    @pytest.mark.parametrize("said", ["More.", "continue", "Keep going!", "yes"])
    def test_more(self, said: str) -> None:
        assert wants_more(said)

    def test_a_question_is_not_more(self) -> None:
        assert not wants_more("more about the VPN outage")

    def test_repeat(self) -> None:
        assert wants_repeat("Say that again?")

    @pytest.mark.parametrize("said", ["Agent.", "talk to a human", "I want a representative"])
    def test_agent(self, said: str) -> None:
        assert wants_agent(said)

    def test_a_question_about_agents_is_not_a_transfer(self) -> None:
        assert not wants_agent("how do I install the monitoring agent")

    def test_match_option_by_whole_words(self) -> None:
        options = {"Approve": "cb_yes", "No": "cb_no"}
        assert match_option("Approve it, please.", options) == "cb_yes"
        assert match_option("I don't know", options) is None

    def test_match_option_needs_exactly_one_hit(self) -> None:
        assert match_option("approve or reject", {"Approve": "a", "Reject": "r"}) is None


# --- TwiML ------------------------------------------------------------------


class TestTwiML:
    def test_text_is_escaped(self) -> None:
        xml = TwiML().say('Use <Hangup/> & "quotes"').to_xml()
        root = ET.fromstring(xml)
        assert [child.tag for child in root] == ["Say"]
        assert root[0].text == 'Use <Hangup/> & "quotes"'

    def test_gather_listens_for_speech_and_reads_the_prompt_inside(self) -> None:
        root = ET.fromstring(TwiML(voice="Polly.Joanna").gather(action="/voice/turn", prompt="Hi", hints="VPN, PTO").to_xml())
        gather = root.find("Gather")
        assert gather is not None
        assert gather.attrib["input"] == "speech"
        assert gather.attrib["action"] == "/voice/turn"
        assert gather.attrib["speechTimeout"] == "auto"
        assert gather.attrib["hints"] == "VPN, PTO"
        say = gather.find("Say")
        assert say is not None and say.text == "Hi" and say.attrib["voice"] == "Polly.Joanna"

    def test_signature_matches_twilios_reference_implementation(self) -> None:
        # Values computed with twilio.request_validator.RequestValidator.
        params = {"CallSid": "CA1234567890ABCDE", "From": "+15555550100", "SpeechResult": "reset my password", "To": "+15555550199"}
        assert twilio_signature("12345", "https://example.com/voice/turn?attempt=1", params) == "jx8f1mThn4j2xx1dtHacTeVpwNk="
        assert twilio_signature("12345", "https://example.com/voice/incoming", {"CallSid": "CA1", "From": "+15555550100"}) == (
            "p4f8tSlKgxxchJfiY+P8GhyDPBk="
        )

    def test_signature_check(self) -> None:
        params = {"CallSid": "CA1"}
        good = twilio_signature(AUTH_TOKEN, PUBLIC_URL + "/voice/incoming", params)
        assert is_valid_signature(AUTH_TOKEN, PUBLIC_URL + "/voice/incoming", params, good)
        assert not is_valid_signature(AUTH_TOKEN, PUBLIC_URL + "/voice/incoming", {"CallSid": "CA2"}, good)
        assert not is_valid_signature(AUTH_TOKEN, PUBLIC_URL + "/voice/incoming", params, None)


# --- bridge -----------------------------------------------------------------


class TestCallerDirectory:
    def test_parse_and_lookup_ignore_formatting(self) -> None:
        directory = CallerDirectory(CallerDirectory.parse("+1 (555) 555-0100=jane@example.com, +15555550101=raj@example.com"))
        assert directory.lookup("+15555550100") == "jane@example.com"
        assert directory.lookup("+1 555 555 0101") == "raj@example.com"
        assert directory.lookup("+15555550199") is None

    def test_parse_rejects_an_entry_without_a_user(self) -> None:
        with pytest.raises(ValueError):
            CallerDirectory.parse("+15555550100")

    def test_default_user(self) -> None:
        assert CallerDirectory({}, default_user="api-key user").lookup("+15555550199") == "api-key user"


class _FakeCapi:
    """Stands in for the library calls the runner makes."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.created: List[str] = []
        self.streamed: List[Dict[str, Any]] = []
        self.submitted: List[Dict[str, Any]] = []
        self.polled: List[Dict[str, Any]] = []
        self.stream_events: Callable[[], Any] = lambda: iter(
            [ResponseCreated(0, {}, "resp_1", "conv_1"), ResponseCompleted(1, {}, "resp_1", "COMPLETED")]
        )
        self.final: Any = _response(messages=[_message("Your PTO balance is 12 days.")])
        self.history: List[Dict[str, Any]] = []
        monkeypatch.setattr(bridge, "create_conversation", self._create_conversation)
        monkeypatch.setattr(bridge, "stream_response", self._stream_response)
        monkeypatch.setattr(bridge, "create_response", self._create_response)
        monkeypatch.setattr(bridge, "wait_for_completion", self._wait_for_completion)
        monkeypatch.setattr(bridge, "list_messages", lambda client, cid, limit=None: {"messages": self.history})

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


def _session() -> CallSession:
    return CallSession(call_sid="CA1", user="jane@example.com", client=MagicMock())


class TestTurnRunner:
    def test_streams_the_turn_and_reads_the_answer_once(self, capi: _FakeCapi) -> None:
        reply = TurnRunner().run(_session(), "How much PTO do I have?")
        assert reply == Reply("Your PTO balance is 12 days.")
        assert capi.streamed[0]["text"] == "How much PTO do I have?"
        assert capi.polled == [{"response_id": "resp_1", "timeout_seconds": pytest.approx(150.0, abs=1), "initial_delay_seconds": 0.0}]
        assert capi.submitted == []

    def test_one_conversation_per_call(self, capi: _FakeCapi) -> None:
        session = _session()
        runner = TurnRunner()
        runner.run(session, "first")
        runner.run(session, "second")
        assert capi.created == ["Phone call"]
        assert session.conversation_id == "conv_1"

    def test_the_120_second_stream_limit_switches_to_polling(self, capi: _FakeCapi) -> None:
        capi.stream_events = _raising(
            [ResponseCreated(0, {}, "resp_1", "conv_1")],
            StreamError("Streaming timeout", code="CONVERSATIONS_REST_API_STREAM_LISTEN_TIMEOUT"),
        )
        reply = TurnRunner().run(_session(), "Summarize my open tickets")
        assert reply.text == "Your PTO balance is 12 days."
        assert capi.polled[0]["response_id"] == "resp_1"
        assert capi.polled[0]["initial_delay_seconds"] == 1.0
        assert capi.submitted == []

    def test_a_dropped_stream_switches_to_polling(self, capi: _FakeCapi) -> None:
        capi.stream_events = _raising([], StreamClosedError("dropped", response_id="resp_1"))
        assert not TurnRunner().run(_session(), "hi").failed
        assert capi.polled[0]["response_id"] == "resp_1"

    def test_a_stream_error_before_the_turn_was_accepted_is_never_resent(self, capi: _FakeCapi) -> None:
        capi.stream_events = _raising([], StreamError("bad request"))
        reply = TurnRunner().run(_session(), "hi")
        assert reply == Reply(bridge.TROUBLE, failed=True)
        assert capi.submitted == []

    def test_a_rate_limited_stream_open_submits_through_the_shared_client(self, capi: _FakeCapi) -> None:
        capi.stream_events = _raising([], RateLimitedError("rate limited", retry_after=5.0))
        reply = TurnRunner().run(_session(), "hi")
        assert not reply.failed
        assert capi.submitted == [{"text": "hi", "callback_id": None}]
        assert capi.polled[0]["response_id"] == "resp_2"

    def test_rate_limited_after_retries_says_busy(self, capi: _FakeCapi) -> None:
        capi.final = RateLimitedError("rate limited")
        assert TurnRunner().run(_session(), "hi") == Reply(bridge.BUSY, failed=True)

    def test_a_slow_answer_says_so(self, capi: _FakeCapi) -> None:
        capi.final = TimeoutError("response resp_1 still IN_PROGRESS after 150s")
        assert TurnRunner().run(_session(), "hi") == Reply(bridge.SLOW, failed=True)

    def test_a_failed_response_speaks_its_message(self, capi: _FakeCapi) -> None:
        capi.final = _response("FAILED", [_message("I couldn't reach the HR system.")])
        assert TurnRunner().run(_session(), "hi") == Reply("I couldn't reach the HR system.", failed=True)

    def test_buttons_become_spoken_options_and_links_are_flagged(self, capi: _FakeCapi) -> None:
        capi.final = _response(messages=[_message("Submit the request?", [
            {"type": "CALLBACK_ACTION", "label": ":white_check_mark: Submit", "callback_action": {"callback_id": "cb_submit"}},
            {"type": "CALLBACK_ACTION", "label": "Cancel", "callback_action": {"callback_id": "cb_cancel"}},
            {"type": "URL_ACTION", "label": "Open ticket", "url_action": {"url": "https://portal.example.com/t/1"}},
        ])])
        reply = TurnRunner().run(_session(), "file a ticket")
        assert reply.options == {"✅ Submit": "cb_submit", "Cancel": "cb_cancel"}
        assert reply.has_link_or_form

    def test_a_button_press_is_sent_as_a_callback(self, capi: _FakeCapi) -> None:
        TurnRunner().run(_session(), callback_id="cb_submit")
        assert capi.streamed[0] == {"text": None, "callback_id": "cb_submit", "timeout_seconds": bridge.STREAM_TIMEOUT_SECONDS}

    def test_the_context_hint_goes_with_the_first_turn_only(self, capi: _FakeCapi) -> None:
        session = _session()
        runner = TurnRunner(context_hint="I'm on a phone call; keep answers short.")
        runner.run(session, "first")
        runner.run(session, "second")
        assert capi.streamed[0]["text"] == "I'm on a phone call; keep answers short.\n\nfirst"
        assert capi.streamed[1]["text"] == "second"

    def test_polling_mode_submits_then_polls(self, capi: _FakeCapi) -> None:
        TurnRunner(stream=False).run(_session(), "hi")
        assert capi.streamed == []
        assert capi.submitted == [{"text": "hi", "callback_id": None}]

    def test_an_empty_response_reads_the_late_reply_from_history(self, capi: _FakeCapi) -> None:
        capi.final = _response(messages=[])
        capi.history = [
            {"actor": "USER", "response_id": "resp_1", "created_at": "2026-01-01T00:00:00Z"},
            {**_message("The live chat has ended."), "created_at": "2026-01-01T00:00:01Z"},
        ]
        assert TurnRunner().run(_session(), callback_id="cb_end").text == "The live chat has ended."


class _FakeHttp:
    def __init__(self, status_code: int = 200, payload: Optional[dict] = None, lines: Optional[List[str]] = None) -> None:
        self.status_code = status_code
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


def test_a_turn_end_to_end_through_the_real_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """The runner over the real library, with only the HTTP session mocked."""
    monkeypatch.setattr("moveworks_capi.responses.time.sleep", lambda _seconds: None)
    completed = {
        "response_id": "resp_1", "conversation_id": "conv_1", "status": "COMPLETED",
        "outputs": [{"type": "MESSAGE", "message": _message("Your laptop **ships Friday**.")}],
    }
    http = MagicMock()
    http.request.side_effect = [
        _FakeHttp(payload={"conversation_id": "conv_1"}),  # POST /rest/v1/conversations
        _FakeHttp(payload=completed),  # GET .../responses/resp_1
    ]
    http.post.return_value = _FakeHttp(lines=[
        *_sse("RESPONSE_CREATED", {"response": {"response_id": "resp_1", "conversation_id": "conv_1"}}),
        *_sse("RESPONSE_COMPLETED", {"response": {"response_id": "resp_1", "status": "COMPLETED"}}),
    ])
    client = ConversationsApiClient("https://api.example.com", "bot", lambda: "token", session=http)
    session = CallSession(call_sid="CA1", user="jane@example.com", client=client)

    reply = TurnRunner().run(session, "When does my laptop ship?")

    assert reply == Reply("Your laptop ships Friday.")
    assert http.post.call_args.args[0] == "https://api.example.com/rest/v1/conversations/conv_1/responses/stream"
    assert http.post.call_args.kwargs["json"] == {"input": {"chat": {"text": "When does my laptop ship?"}}}
    method, url = http.request.call_args_list[1].args
    assert (method, url) == ("GET", "https://api.example.com/rest/v1/conversations/conv_1/responses/resp_1")


# --- webhooks ---------------------------------------------------------------


class _InlineExecutor(Executor):
    """Runs each turn on submit, so tests need no threads."""

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> "Future[Any]":
        future: "Future[Any]" = Future()
        future.set_result(fn(*args, **kwargs))
        return future


class _HeldExecutor(Executor):
    """Never finishes a turn, to test waiting."""

    def __init__(self) -> None:
        self.submitted = 0

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> "Future[Any]":
        self.submitted += 1
        return Future()


class _FakeRunner(TurnRunner):
    def __init__(self, *replies: Reply) -> None:
        super().__init__()
        self.replies = list(replies)
        self.calls: List[Dict[str, Any]] = []

    def run(self, session: CallSession, text: Optional[str] = None, *, callback_id: Optional[str] = None) -> Reply:
        self.calls.append({"user": session.user, "text": text, "callback_id": callback_id})
        return self.replies.pop(0)


class _Call:
    """A phone call driven through the webhooks, signed like Twilio signs them."""

    def __init__(self, *replies: Reply, executor: Optional[Executor] = None, **settings: Any) -> None:
        self.runner = _FakeRunner(*replies)
        self.sessions = CallSessions()
        self.clients = ClientPool(lambda user: MagicMock(name=user))
        app = server.create_app(
            server.VoiceSettings(twilio_auth_token=AUTH_TOKEN, public_url=PUBLIC_URL, wait_slice_seconds=0.0, **settings),
            directory=CallerDirectory({CALLER: "jane@example.com"}),
            clients=self.clients,
            runner=self.runner,
            executor=executor or _InlineExecutor(),
            sessions=self.sessions,
        )
        self.http = TestClient(app)

    def post(self, path: str, signature: Optional[str] = None, **fields: str) -> Any:
        params = {"CallSid": "CA1", "From": CALLER, "To": "+15555550199", **fields}
        signature = signature if signature is not None else twilio_signature(AUTH_TOKEN, PUBLIC_URL + path, params)
        return self.http.post(path, data=params, headers={"X-Twilio-Signature": signature})

    def twiml(self, path: str, **fields: str) -> ET.Element:
        response = self.post(path, **fields)
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("application/xml")
        return ET.fromstring(response.text)

    def say(self, path: str, **fields: str) -> str:
        """Everything the caller hears from one webhook, in order."""
        return " ".join(element.text or "" for element in self.twiml(path, **fields).iter("Say"))

    def ask(self, said: str) -> str:
        """Say something, then hear the answer."""
        self.twiml("/voice/turn", SpeechResult=said)
        return self.say("/voice/wait")


def _verbs(root: ET.Element) -> List[str]:
    return [child.tag for child in root]


class TestWebhooks:
    def test_an_unsigned_request_is_refused(self) -> None:
        call = _Call()
        assert call.post("/voice/incoming", signature="").status_code == 403
        assert call.post("/voice/incoming", signature="bm90IGEgc2lnbmF0dXJl").status_code == 403
        assert len(call.sessions) == 0

    def test_an_unknown_caller_hears_why_and_the_call_ends(self) -> None:
        call = _Call()
        root = call.twiml("/voice/incoming", From="+15555550123")
        assert _verbs(root) == ["Say", "Hangup"]
        assert len(call.sessions) == 0

    def test_a_known_caller_is_greeted_and_heard(self) -> None:
        call = _Call()
        root = call.twiml("/voice/incoming")
        assert _verbs(root) == ["Gather", "Redirect"]
        gather = root[0]
        assert gather.attrib["action"] == "/voice/turn"
        assert gather.attrib["input"] == "speech"
        assert root[1].text == "/voice/reprompt"
        assert len(call.sessions) == 1

    def test_a_turn_says_a_filler_then_speaks_the_answer(self) -> None:
        call = _Call(Reply("You have 12 days of PTO."))
        call.twiml("/voice/incoming")
        root = call.twiml("/voice/turn", SpeechResult="How much PTO do I have?")
        assert _verbs(root) == ["Say", "Redirect"]
        assert root[1].text == "/voice/wait"
        answer = call.twiml("/voice/wait")
        assert _verbs(answer) == ["Gather", "Redirect"]
        assert call.say("/voice/reprompt") == server.STILL_THERE
        assert call.runner.calls == [{"user": "jane@example.com", "text": "How much PTO do I have?", "callback_id": None}]

    def test_the_answer_is_spoken_inside_a_gather(self) -> None:
        call = _Call(Reply("You have 12 days of PTO."))
        call.twiml("/voice/incoming")
        call.twiml("/voice/turn", SpeechResult="PTO?")
        gather = call.twiml("/voice/wait").find("Gather")
        assert gather is not None
        say = gather.find("Say")
        assert say is not None and say.text == "You have 12 days of PTO."

    def test_a_long_answer_is_read_in_chunks(self) -> None:
        long_answer = " ".join(f"Step {i} is to do the next thing." for i in range(30))
        call = _Call(Reply(long_answer), max_speech_chars=200)
        call.twiml("/voice/incoming")
        first = call.ask("How do I set up the VPN?")
        assert first.startswith("Step 0") and first.endswith(server.MORE_PROMPT)
        second = call.say("/voice/turn", SpeechResult="More.")
        assert second.startswith("Step ") and not second.startswith("Step 0")
        assert call.say("/voice/turn", SpeechResult="Say that again.") == second
        assert len(call.runner.calls) == 1

    def test_a_new_question_drops_the_rest_of_a_long_answer(self) -> None:
        call = _Call(Reply(" ".join(f"Sentence {i}." for i in range(80))), Reply("It's sunny."), max_speech_chars=100)
        call.twiml("/voice/incoming")
        call.ask("Tell me everything")
        assert call.ask("What's the weather?") == "It's sunny."

    def test_buttons_are_read_out_and_pressed_by_voice(self) -> None:
        call = _Call(Reply("Submit the request?", options={"Submit": "cb_submit", "Cancel": "cb_cancel"}), Reply("Submitted."))
        call.twiml("/voice/incoming")
        assert call.ask("Order a new monitor") == "Submit the request? You can say Submit, or Cancel."
        assert call.ask("Submit it") == "Submitted."
        assert call.runner.calls[1] == {"user": "jane@example.com", "text": None, "callback_id": "cb_submit"}

    def test_a_link_or_form_is_mentioned(self) -> None:
        call = _Call(Reply("Here's the enrollment form.", has_link_or_form=True))
        call.twiml("/voice/incoming")
        assert call.ask("Enroll me in the 401k") == f"Here's the enrollment form. {server.LINK_NOTE}"

    def test_asking_for_an_agent_transfers_the_call(self) -> None:
        call = _Call(transfer_number="+15555550142")
        call.twiml("/voice/incoming")
        root = call.twiml("/voice/turn", SpeechResult="Talk to a human.")
        assert _verbs(root) == ["Say", "Dial"]
        assert root[1].text == "+15555550142"
        assert call.runner.calls == []

    def test_without_a_transfer_number_agent_is_an_ordinary_question(self) -> None:
        call = _Call(Reply("I can help with that."))
        call.twiml("/voice/incoming")
        assert call.ask("agent") == "I can help with that."

    def test_waiting_redirects_and_says_still_working_now_and_then(self) -> None:
        held = _HeldExecutor()
        call = _Call(executor=held)
        call.twiml("/voice/incoming")
        call.twiml("/voice/turn", SpeechResult="Summarize my tickets")
        says = []
        for _ in range(3):
            root = call.twiml("/voice/wait")
            assert root[-1].tag == "Redirect" and root[-1].text == "/voice/wait"
            says.append(" ".join(e.text or "" for e in root.iter("Say")))
        assert says == ["", "", server.STILL_WORKING]

    def test_a_second_utterance_while_a_turn_runs_is_not_sent(self) -> None:
        held = _HeldExecutor()
        call = _Call(executor=held)
        call.twiml("/voice/incoming")
        call.twiml("/voice/turn", SpeechResult="first")
        root = call.twiml("/voice/turn", SpeechResult="first")
        assert _verbs(root) == ["Redirect"]
        assert held.submitted == 1

    def test_silence_reprompts_then_hangs_up(self) -> None:
        call = _Call()
        call.twiml("/voice/incoming")
        for _ in range(server.MAX_SILENT_PROMPTS):
            assert _verbs(call.twiml("/voice/reprompt")) == ["Gather", "Redirect"]
        assert _verbs(call.twiml("/voice/reprompt")) == ["Say", "Hangup"]
        assert len(call.sessions) == 0

    def test_an_empty_transcript_asks_again(self) -> None:
        call = _Call()
        call.twiml("/voice/incoming")
        assert call.say("/voice/turn", SpeechResult="") == server.DIDNT_CATCH

    def test_no_ends_the_call_only_after_anything_else(self) -> None:
        call = _Call(Reply("Want me to file a ticket?"), Reply("Okay, I won't."))
        call.twiml("/voice/incoming")
        call.ask("My monitor flickers")
        assert call.ask("No.") == "Okay, I won't."
        call.twiml("/voice/reprompt")
        assert _verbs(call.twiml("/voice/turn", SpeechResult="No thanks.")) == ["Say", "Hangup"]
        assert len(call.sessions) == 0

    def test_goodbye_ends_the_call(self) -> None:
        call = _Call()
        call.twiml("/voice/incoming")
        assert _verbs(call.twiml("/voice/turn", SpeechResult="Goodbye.")) == ["Say", "Hangup"]

    def test_the_status_callback_drops_a_finished_call(self) -> None:
        call = _Call()
        call.twiml("/voice/incoming")
        assert call.post("/voice/status", CallStatus="completed").status_code == 204
        assert len(call.sessions) == 0
        assert _verbs(call.twiml("/voice/turn", SpeechResult="hello?")) == ["Say", "Hangup"]

    def test_each_caller_gets_a_client_for_their_own_user(self) -> None:
        call = _Call(Reply("Hi."))
        call.twiml("/voice/incoming")
        session = call.sessions.get("CA1")
        assert session is not None
        assert session.client is call.clients.get("jane@example.com")
