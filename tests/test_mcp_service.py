import asyncio
import contextlib
import json
import pathlib
import threading
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest import mock

import pytest

from moveworks_capi.errors import ConversationsApiError, RateLimitedError

from mcp_server import present
from mcp_server import service as service_module
from mcp_server import turns as turns_module
from mcp_server.models import Option
from mcp_server.service import MoveworksTools
from mcp_server.turns import HANDOFF_NOTE, LIVE_AGENT_NOTE, NotAccepted, TurnOutcome, outcome_from_messages

ALICE = SimpleNamespace(user_key="alice@acme.com")
BOB = SimpleNamespace(user_key="bob@acme.com")


def answer_with_file_ticket() -> TurnOutcome:
    return TurnOutcome(
        text="I couldn't find an answer.",
        sources=[{"title": "VPN guide", "url": "https://kb/1"}],
        options=[Option("File ticket", "callback", callback_id="cb_file")],
    )


class FakeTurns:
    def __init__(self) -> None:
        self.created = 0
        self.chats: List[str] = []
        self.presses: List[Optional[str]] = []
        self.chat_outcome: Any = answer_with_file_ticket
        self.press_outcome: Any = lambda: TurnOutcome(text="Your ticket INC0010099 has been filed.")
        self.gate: threading.Event = threading.Event()
        self.gate.set()
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def create_conversation(self, title: str) -> str:
        self.created += 1
        return f"capi_{self.created}"

    def _enter(self):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)

    def _exit(self):
        with self._lock:
            self.active -= 1

    def chat(self, conversation_id: str, text: str, progress) -> TurnOutcome:
        self._enter()
        try:
            self.chats.append(text)
            progress("→ Searching")
            self.gate.wait(5)
            outcome = self.chat_outcome
            if isinstance(outcome, Exception):
                raise outcome
            return outcome()
        finally:
            self._exit()

    def press(self, conversation_id: str, option: Option, progress) -> TurnOutcome:
        self._enter()
        try:
            self.presses.append(option.callback_id)
            self.gate.wait(5)
            return self.press_outcome()
        finally:
            self._exit()


def make_tools(fake: FakeTurns, **kwargs) -> MoveworksTools:
    @contextlib.contextmanager
    def factory(identity):
        yield fake

    return MoveworksTools(factory, **kwargs)


def run(coro):
    return asyncio.run(coro)


def test_ask_returns_text_sources_options_and_handle():
    tools = make_tools(FakeTurns())
    reply = run(tools.ask(ALICE, "vpn help", None))
    assert not reply.is_error
    assert reply.data["status"] == "completed"
    assert reply.data["sources"] == [{"title": "VPN guide", "url": "https://kb/1"}]
    assert reply.data["options"] == [{"option_id": "opt_1", "label": "File ticket", "kind": "button"}]
    handle = reply.data["conversation_id"]
    assert handle.startswith("c_")
    assert "[opt_1] File ticket" in reply.text
    assert f"conversation_id: {handle}" in reply.text


def test_no_conversation_id_starts_a_new_conversation_each_time():
    fake = FakeTurns()
    tools = make_tools(fake)
    first = run(tools.ask(ALICE, "one", None)).data["conversation_id"]
    second = run(tools.ask(ALICE, "two", None)).data["conversation_id"]
    assert first != second
    assert fake.created == 2


def test_follow_up_reuses_the_conversation():
    fake = FakeTurns()
    tools = make_tools(fake)

    async def scenario():
        handle = (await tools.ask(ALICE, "one", None)).data["conversation_id"]
        await tools.ask(ALICE, "and the second part?", handle)

    run(scenario())
    assert fake.created == 1
    assert fake.chats == ["one", "and the second part?"]


def test_choose_presses_once_and_a_repeat_returns_the_same_outcome():
    fake = FakeTurns()
    tools = make_tools(fake)

    async def scenario():
        handle = (await tools.ask(ALICE, "vpn", None)).data["conversation_id"]
        first = await tools.choose(ALICE, handle, "opt_1", "File ticket")
        second = await tools.choose(ALICE, handle, "opt_1", "File ticket")
        return first, second

    first, second = run(scenario())
    assert "INC0010099" in first.text and "INC0010099" in second.text
    assert fake.presses == ["cb_file"]


def test_repeat_choose_while_in_flight_shares_the_pending_turn():
    fake = FakeTurns()
    tools = make_tools(fake, deadline_seconds=0.2)

    async def scenario():
        handle = (await tools.ask(ALICE, "vpn", None)).data["conversation_id"]
        fake.gate.clear()
        first = await tools.choose(ALICE, handle, "opt_1", "File ticket")
        second = await tools.choose(ALICE, handle, "opt_1", "File ticket")
        fake.gate.set()
        done = await tools.result(ALICE, handle)
        return first, second, done

    first, second, done = run(scenario())
    assert first.data["status"] == "pending"
    assert second.data["status"] == "pending"
    assert done.data["status"] == "completed" and "INC0010099" in done.text
    assert fake.presses == ["cb_file"]


def test_slow_turn_returns_pending_then_result_delivers_it():
    fake = FakeTurns()
    fake.gate.clear()
    tools = make_tools(fake, deadline_seconds=0.2)

    async def scenario():
        pending = await tools.ask(ALICE, "slow question", None)
        threading.Timer(0.05, fake.gate.set).start()
        done = await tools.result(ALICE, pending.data["conversation_id"])
        return pending, done

    pending, done = run(scenario())
    assert pending.data["status"] == "pending"
    assert pending.data["progress"] == "→ Searching"
    assert "get_moveworks_result" in pending.text
    assert done.data["status"] == "completed"


def test_ask_while_a_turn_is_running_is_refused():
    fake = FakeTurns()
    fake.gate.clear()
    tools = make_tools(fake, deadline_seconds=0.1)

    async def scenario():
        handle = (await tools.ask(ALICE, "slow", None)).data["conversation_id"]
        refused = await tools.ask(ALICE, "another", handle)
        fake.gate.set()
        await tools.result(ALICE, handle)
        return refused

    refused = run(scenario())
    assert refused.is_error and refused.data["error"] == "still_running"
    assert fake.chats == ["slow"]


def test_stale_option_from_an_earlier_turn_is_rejected():
    fake = FakeTurns()
    tools = make_tools(fake)

    async def scenario():
        handle = (await tools.ask(ALICE, "vpn", None)).data["conversation_id"]
        await tools.ask(ALICE, "something else", handle)
        return await tools.choose(ALICE, handle, "opt_1", "File ticket")

    reply = run(scenario())
    assert reply.is_error and reply.data["error"] == "stale_option"
    assert reply.data["options"] == [{"option_id": "opt_2", "label": "File ticket", "kind": "button"}]
    assert fake.presses == []


def test_label_must_match_the_option():
    fake = FakeTurns()
    tools = make_tools(fake)

    async def scenario():
        handle = (await tools.ask(ALICE, "vpn", None)).data["conversation_id"]
        return await tools.choose(ALICE, handle, "opt_1", "Close ticket")

    reply = run(scenario())
    assert reply.is_error and reply.data["error"] == "label_mismatch"
    assert fake.presses == []


def test_guard_refuses_selections_sent_as_questions():
    fake = FakeTurns()
    tools = make_tools(fake)

    async def scenario():
        handle = (await tools.ask(ALICE, "vpn", None)).data["conversation_id"]
        replies = [await tools.ask(ALICE, text, handle) for text in ("yes", "File ticket!", "1", "opt_1", "go ahead")]
        normal = await tools.ask(ALICE, "What about the Wi-Fi?", handle)
        return replies, normal

    replies, normal = run(scenario())
    assert all(r.is_error and r.data["error"] == "use_choose_tool" for r in replies)
    assert not normal.is_error
    assert fake.chats == ["vpn", "What about the Wi-Fi?"]
    assert fake.presses == []


def test_other_users_conversation_is_unknown():
    tools = make_tools(FakeTurns())

    async def scenario():
        handle = (await tools.ask(ALICE, "vpn", None)).data["conversation_id"]
        return (
            await tools.ask(BOB, "hi", handle),
            await tools.choose(BOB, handle, "opt_1", "File ticket"),
            await tools.result(BOB, handle),
        )

    for reply in run(scenario()):
        assert reply.is_error and reply.data["error"] == "unknown_conversation"


def test_rate_limit_is_a_recoverable_error_with_retry_after():
    fake = FakeTurns()
    fake.chat_outcome = RateLimitedError("slow down", retry_after=6.0)
    tools = make_tools(fake)
    reply = run(tools.ask(ALICE, "vpn", None))
    assert reply.is_error and reply.data["error"] == "rate_limited"
    assert reply.data["retry_after"] == 6.0
    assert "Wait 6 seconds" in reply.text


def test_turns_for_one_user_never_overlap_across_conversations():
    fake = FakeTurns()

    def slow():
        time.sleep(0.2)
        return answer_with_file_ticket()

    fake.chat_outcome = slow
    tools = make_tools(fake)

    async def scenario():
        await asyncio.gather(tools.ask(ALICE, "a", None), tools.ask(ALICE, "b", None), tools.ask(BOB, "c", None))

    run(scenario())
    # Alice's two turns serialize; Bob's may run alongside one of them.
    assert fake.max_active == 2
    assert sorted(fake.chats) == ["a", "b", "c"]


def message(text: str, actions: List[Dict[str, Any]], handoff=None) -> Dict[str, Any]:
    m: Dict[str, Any] = {
        "actor": "ASSISTANT",
        "content": {"type": "MARKDOWN_TEXT", "markdown_text": {"text": text}},
        "message_id": "msg_1",
        "response_id": "resp_1",
        "actions": actions,
    }
    if handoff:
        m["handoff"] = handoff
    return m


def test_buttons_and_forms_are_choosable_links_and_live_agent_are_not():
    outcome = outcome_from_messages([message("Here are your choices.", [
        {"type": "CALLBACK_ACTION", "label": "File ticket", "callback_action": {"callback_id": "cb_file"}},
        {"type": "CALLBACK_ACTION", "label": "Start agent chat", "callback_action": {"callback_id": "cb_agent"}},
        {"type": "MODAL_ACTION", "label": "Edit/Add details", "modal_action": {"modal_id": "m1"}},
        {"type": "URL_ACTION", "label": "Open portal", "url_action": {"url": "https://portal"}},
        {"type": "HANDOFF_ACTION", "label": "Other help options", "handoff_action": {}},
    ], handoff={"handoff_categories": []})])
    assert [(o.label, o.kind) for o in outcome.options] == [("File ticket", "callback"), ("Edit/Add details", "modal")]
    assert outcome.notes == [LIVE_AGENT_NOTE, "[Open portal](https://portal)", HANDOFF_NOTE]


def test_a_live_agent_request_form_is_a_note_not_an_option():
    outcome = outcome_from_messages([message("How can I help?", [
        {"type": "MODAL_ACTION", "label": "File ticket: New request", "modal_action": {"modal_id": "m1"}},
        {"type": "MODAL_ACTION", "label": "Start agent chat: Live agent chat request", "modal_action": {"modal_id": "m2"}},
        {"type": "CALLBACK_ACTION", "label": "Live agent", "callback_action": {"callback_id": "cb_agent"}},
    ])])
    assert [o.label for o in outcome.options] == ["File ticket: New request"]
    assert outcome.notes == [LIVE_AGENT_NOTE]


def test_follow_up_poll_ignores_replies_from_earlier_turns(monkeypatch):
    from unittest import mock

    from moveworks_capi.streaming import OutputDelta, ResponseCompleted, ResponseCreated

    def assistant(message_id, text, created_at, response_id="resp_2"):
        return {"actor": "ASSISTANT", "message_id": message_id, "response_id": response_id, "created_at": created_at,
                "content": {"type": "MARKDOWN_TEXT", "markdown_text": {"text": text}}}

    earlier = assistant("msg_1", "I understand you'd like to file an IT ticket.", "2026-10-22T23:31:49Z", "resp_1")
    interim = assistant("msg_2", "Filing your ticket...", "2026-10-22T23:31:52Z")
    filed = assistant("msg_3", "I filed your ticket as INC0010099.", "2026-10-22T23:31:59Z")

    def fake_stream(client, conversation_id, text=None, *, callback_id=None, timeout_seconds=120.0):
        yield ResponseCreated(sequence_number=0, raw={}, response_id="resp_2", conversation_id=conversation_id)
        yield OutputDelta(sequence_number=1, raw={}, output_type="MESSAGE", text=None, output={"message": interim})
        yield ResponseCompleted(sequence_number=2, raw={}, response_id="resp_2", status="COMPLETED")

    histories = iter([[interim, earlier], [filed, interim, earlier]])
    monkeypatch.setattr(turns_module, "stream_response", fake_stream)
    monkeypatch.setattr(turns_module, "list_messages", lambda c, cid, limit=5: {"messages": next(histories)})
    monkeypatch.setattr(turns_module.time, "sleep", lambda s: None)

    option = Option("File ticket", "callback", callback_id="cb")
    outcome = turns_module.MoveworksTurns(mock.Mock()).press("conv_1", option, lambda line: None)

    assert "INC0010099" in outcome.text
    assert "I understand" not in outcome.text
    assert outcome.options == []


def test_a_portal_form_link_is_named_after_the_form_it_cites():
    msg = json.loads((pathlib.Path(__file__).parent / "fixtures" / "portal_form_message.json").read_text())
    short_link = next(a for a in msg["actions"] if a["type"] == "URL_ACTION")["url_action"]["url"]
    resolved = {short_link: "https://example.service-now.com/sp?id=sc_cat_item&sys_id=plan_enrollment"}
    options = present.message_options([msg], resolve=resolved.get)
    assert ("Complete this request: 401(k) Plan Enrollment", short_link) in [(o.label, o.url) for o in options]


def _raise(exc):
    def outcome():
        raise exc
    return outcome


def test_a_rejected_press_can_be_pressed_again():
    fake = FakeTurns()
    tools = make_tools(fake)
    fake.press_outcome = _raise(NotAccepted(RateLimitedError("slow down", retry_after=2.0)))

    async def scenario():
        asked = await tools.ask(ALICE, "vpn help", None)
        handle = asked.data["conversation_id"]
        limited = await tools.choose(ALICE, handle, "opt_1", "File ticket")
        fake.press_outcome = lambda: TurnOutcome(text="Your ticket INC0010099 has been filed.")
        filed = await tools.choose(ALICE, handle, "opt_1", "File ticket")
        return limited, filed

    limited, filed = run(scenario())
    assert limited.data["error"] == "rate_limited"
    assert "INC0010099" in filed.text
    assert fake.presses == ["cb_file", "cb_file"]


def test_a_press_with_an_unknown_outcome_is_never_sent_again():
    fake = FakeTurns()
    tools = make_tools(fake)
    fake.press_outcome = _raise(ConversationsApiError("bad gateway", status_code=502))

    async def scenario():
        asked = await tools.ask(ALICE, "vpn help", None)
        handle = asked.data["conversation_id"]
        first = await tools.choose(ALICE, handle, "opt_1", "File ticket")
        retry = await tools.choose(ALICE, handle, "opt_1", "File ticket")
        return first, retry

    first, retry = run(scenario())
    assert first.data["error"] == retry.data["error"] == "moveworks_error"
    assert fake.presses == ["cb_file"]


def test_a_turn_that_raises_timeout_error_is_an_error_not_pending():
    fake = FakeTurns()
    fake.chat_outcome = TimeoutError("turn timed out")
    reply = run(make_tools(fake).ask(ALICE, "vpn help", None))
    assert reply.is_error and reply.data["status"] == "error" and reply.data["error"] == "server_error"


def test_a_short_reply_that_starts_with_yes_must_use_the_choose_tool():
    tools = make_tools(FakeTurns())

    async def scenario():
        asked = await tools.ask(ALICE, "vpn help", None)
        return await tools.ask(ALICE, "yes please file it now", asked.data["conversation_id"])

    assert run(scenario()).data["error"] == "use_choose_tool"


def test_a_poll_failure_after_submit_reports_the_submit_instead_of_raising():
    client = mock.Mock()
    client.post.return_value = mock.Mock()
    turns = turns_module.MoveworksTurns(client, modal_outcome_wait_seconds=0.01)
    form = SimpleNamespace(path="/p", values={}, name="Keyboard")
    with mock.patch.object(turns_module, "list_messages", side_effect=[{"messages": []}, RateLimitedError("slow")]), \
            mock.patch.object(turns_module.time, "sleep", lambda s: None):
        outcome = turns.submit_form("conv_1", form, lambda line: None)  # type: ignore[arg-type]
    assert "accepted **Keyboard**" in outcome.text
    assert client.post.call_count == 1


def test_a_rejected_submit_raises_not_accepted_and_an_unknown_one_does_not():
    form = SimpleNamespace(path="/p", values={}, name="Keyboard")
    for error, expected in ((RateLimitedError("slow"), NotAccepted), (ConversationsApiError("boom", status_code=502), None)):
        client = mock.Mock()
        client.post.side_effect = error
        turns = turns_module.MoveworksTurns(client)
        with mock.patch.object(turns_module, "list_messages", return_value={"messages": []}):
            if expected:
                with pytest.raises(expected):
                    turns.submit_form("conv_1", form, lambda line: None)  # type: ignore[arg-type]
            else:
                outcome = turns.submit_form("conv_1", form, lambda line: None)  # type: ignore[arg-type]
                assert outcome.failed and "before submitting again" in outcome.text


@pytest.mark.parametrize("message, blocked", [
    ("yes please file it now", True),
    ("ok go ahead", True),
    ("\U0001F44D", False),
    ("?", False),
    ("ok what about vpn", False),
    ("yes but what does it cost", False),
    ("submit a ticket for my laptop", False),
    ("sure, file it?", False),
])
def test_the_selection_guard_catches_choices_but_not_new_questions(message, blocked):
    assert service_module._looks_like_selection(message, {"opt_1": Option("File ticket", "callback", callback_id="cb")}) is blocked


@pytest.mark.parametrize("label", ["Yes, file a ticket", "Submit a request", "Okay, create it"])
def test_typing_an_option_label_that_starts_with_yes_is_still_a_choice(label):
    assert service_module._looks_like_selection(label, {"opt_1": Option(label, "callback", callback_id="cb")})



def test_a_press_rejected_instantly_is_forgotten_even_before_the_caller_registers_it():
    for _ in range(50):
        fake = FakeTurns()
        tools = make_tools(fake)
        fake.press_outcome = _raise(NotAccepted(RateLimitedError("slow down")))

        async def scenario(tools=tools, fake=fake):
            asked = await tools.ask(ALICE, "vpn help", None)
            handle = asked.data["conversation_id"]
            first = await tools.choose(ALICE, handle, "opt_1", "File ticket")
            fake.press_outcome = lambda: TurnOutcome(text="Your ticket INC0010099 has been filed.")
            second = await tools.choose(ALICE, handle, "opt_1", "File ticket")
            return first, second

        first, second = run(scenario())
        assert first.data["error"] == "rate_limited"
        assert "INC0010099" in second.text
