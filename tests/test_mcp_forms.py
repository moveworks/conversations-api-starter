import asyncio
import contextlib
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from moveworks_capi.errors import RateLimitedError

from mcp_server import forms
from mcp_server.models import Option
from mcp_server.service import MoveworksTools
from mcp_server.turns import NotAccepted, TurnOutcome

ALICE = SimpleNamespace(user_key="alice@acme.com")

KEYBOARD_FORM = {
    "title": "New request",
    "fields": [
        {"type": "DISPLAY", "visible": True, "display": {"text": {"style": "HEADER", "text": "Wireless keyboard and mouse"}}},
        {"type": "DISPLAY", "visible": True, "display": {"text": {"style": "SUBTLE", "text": "Price: $40.00"}}},
        {"name": "requested_for", "label": "Requested for", "type": "CHOICE", "required": True, "visible": True,
         "choice": {"prefill": [{"display_value": "Alice: alice@acme.com", "value": "alice@acme.com"}]}},
        {"name": "justification", "label": "Business justification", "type": "TEXT", "required": True, "visible": True,
         "text": {"multiline": True}},
        {"name": "color", "label": "Color", "type": "CHOICE", "required": False, "visible": True,
         "choice": {"options": [{"value": "blk", "display_value": "Black"}, {"value": "wht", "display_value": "White"}]}},
        {"name": "urgent", "label": "Urgent", "type": "BOOLEAN", "visible": True},
        {"type": "DISPLAY", "visible": True, "display": {"text": {"style": "SUBTLE",
         "text": 'Having issues? <a href="https://portal.example.com/r?x1" target="_blank">Complete this request on the portal</a>'}}},
    ],
}


def test_form_is_named_by_its_header_and_prefills_lookup_fields():
    form = forms.open_form("opt_1", "/path", KEYBOARD_FORM, "Complete this request")
    assert form.name == "Wireless keyboard and mouse"
    assert form.values == {"requested_for": {"option": "alice@acme.com"}}
    assert form.portal_url == "https://portal.example.com/r?x1"
    view = forms.describe(form)
    assert [f["name"] for f in view["fields"]] == ["requested_for", "justification", "color", "urgent"]
    assert view["fields"][0]["value"] == "Alice: alice@acme.com"
    assert view["fields"][0]["settable"] is False
    assert view["details"] == ["Price: $40.00"]


def test_values_are_encoded_per_field_type_by_name_or_label():
    form = forms.open_form("opt_1", "/path", KEYBOARD_FORM, "x")
    forms.apply_values(form, {"BusinessJustification": "Keys stick", "color": "White", "urgent": "yes"})
    assert form.values["justification"] == {"text": "Keys stick"}
    assert form.values["color"] == {"option": "wht"}
    assert form.values["urgent"] == {"bool": True}
    assert forms.missing_required(form) == []
    assert forms.blockers(form) == ["'Requested for' is a lookup field this server can't fill in"]


@pytest.mark.parametrize("values,message", [
    ({"color": "Purple"}, "not a choice"),
    ({"shoe size": "9"}, "not a field"),
    ({"requested_for": "bob@acme.com"}, "can't be set"),
])
def test_bad_values_are_explained(values, message):
    form = forms.open_form("opt_1", "/path", KEYBOARD_FORM, "x")
    with pytest.raises(forms.FormInputError, match=message):
        forms.apply_values(form, values)


def test_required_lookup_or_required_file_blocks_submission():
    modal = {"fields": [
        {"name": "contact", "label": "Primary contact", "type": "CHOICE", "required": True, "visible": True, "choice": {}},
        {"name": "files", "label": "Screenshot", "type": "FILE", "required": True, "visible": True},
    ]}
    form = forms.open_form("opt_1", "/path", modal, "x")
    assert forms.blockers(form) == ["'Primary contact' is a lookup field this server can't fill in",
                                    "'Screenshot' needs a file attachment"]


class FakeTurns:
    def __init__(self) -> None:
        self.submits: List[Dict[str, Any]] = []

    def create_conversation(self, title: str) -> str:
        return "capi_1"

    def chat(self, conversation_id, text, progress) -> TurnOutcome:
        return TurnOutcome(text="Pick a keyboard.", options=[
            Option("Complete this request: Wireless keyboard and mouse", "modal", modal_id="m1",
                   response_id="r1", message_id="msg1", modal=SUBMITTABLE_FORM),
            Option("File ticket", "callback", callback_id="cb"),
        ])

    def modal_path(self, conversation_id, option) -> str:
        return f"/rest/v1/conversations/{conversation_id}/modals/{option.modal_id}"

    def fetch_form(self, conversation_id, option):
        raise AssertionError("the form was already fetched to name it")

    def submit_form(self, conversation_id, form, progress) -> TurnOutcome:
        self.submits.append(dict(form.values))
        return TurnOutcome(text="Submitted **Wireless keyboard and mouse**.\n\nRequest RITM0010099 created.")


SUBMITTABLE_FORM = {**KEYBOARD_FORM, "fields": [
    {**f, "required": False} if f.get("name") == "requested_for" else f for f in KEYBOARD_FORM["fields"]]}


def make_tools(fake):
    @contextlib.contextmanager
    def factory(identity):
        yield fake

    return MoveworksTools(factory)


def test_open_fill_submit_once_and_retry_returns_the_same_outcome():
    fake = FakeTurns()
    tools = make_tools(fake)

    async def scenario():
        asked = await tools.ask(ALICE, "I need a new keyboard", None)
        handle = asked.data["conversation_id"]
        opened = await tools.open_form(ALICE, handle, "opt_1")
        missing = await tools.submit_form(ALICE, handle, {}, "submit")
        done = await tools.submit_form(ALICE, handle, {"justification": "Keys stick", "color": "Black"}, "submit")
        retry = await tools.submit_form(ALICE, handle, {"justification": "Keys stick"}, "submit")
        return asked, opened, missing, done, retry

    asked, opened, missing, done, retry = asyncio.run(scenario())
    assert asked.data["options"][0] == {"option_id": "opt_1", "label": "Complete this request: Wireless keyboard and mouse",
                                        "kind": "form"}
    assert "(form)" in asked.text
    assert opened.data["status"] == "form_open"
    assert opened.data["form"]["name"] == "Wireless keyboard and mouse"
    assert missing.is_error and missing.data["error"] == "missing_required"
    assert "Business justification" in missing.text
    assert "RITM0010099" in done.text
    assert "RITM0010099" in retry.text
    assert fake.submits == [{"requested_for": {"option": "alice@acme.com"}, "justification": {"text": "Keys stick"},
                             "color": {"option": "blk"}}]


def test_choose_refuses_forms_and_open_refuses_buttons():
    tools = make_tools(FakeTurns())

    async def scenario():
        handle = (await tools.ask(ALICE, "keyboard", None)).data["conversation_id"]
        return (await tools.choose(ALICE, handle, "opt_1", "Complete this request: Wireless keyboard and mouse"),
                await tools.open_form(ALICE, handle, "opt_2"))

    choose, open_button = asyncio.run(scenario())
    assert choose.data["error"] == "use_open_form"
    assert open_button.data["error"] == "use_choose_tool"


def test_cancel_discards_the_form_without_submitting():
    fake = FakeTurns()
    tools = make_tools(fake)

    async def scenario():
        handle = (await tools.ask(ALICE, "keyboard", None)).data["conversation_id"]
        await tools.open_form(ALICE, handle, "opt_1")
        cancelled = await tools.submit_form(ALICE, handle, None, "cancel")
        after = await tools.submit_form(ALICE, handle, {"justification": "x"}, "submit")
        return cancelled, after

    cancelled, after = asyncio.run(scenario())
    assert cancelled.data["status"] == "cancelled"
    assert after.data["error"] == "no_open_form"
    assert fake.submits == []


def test_required_lookup_form_returns_the_portal_link_instead_of_submitting():
    fake = FakeTurns()
    def chat(cid, text, progress):
        return TurnOutcome(text="Pick.", options=[
            Option("Complete this request: Wireless keyboard and mouse", "modal", modal_id="m1",
                   response_id="r1", message_id="msg1", modal=KEYBOARD_FORM)])

    fake.chat = chat  # pyright: ignore[reportAttributeAccessIssue]
    tools = make_tools(fake)

    async def scenario():
        handle = (await tools.ask(ALICE, "keyboard", None)).data["conversation_id"]
        await tools.open_form(ALICE, handle, "opt_1")
        return await tools.submit_form(ALICE, handle, {"justification": "Keys stick"}, "submit")

    reply = asyncio.run(scenario())
    assert reply.data["error"] == "form_unsupported"
    assert "https://portal.example.com/r?x1" in reply.text
    assert fake.submits == []


def test_a_read_only_field_rejects_edits():
    form = forms.open_form("opt_1", "/path", {"fields": [
        {"name": "asset", "label": "Asset", "type": "TEXT", "visible": True, "read_only": True,
         "text": {"prefill": "LAPTOP-1"}},
    ]}, "Edit details")
    with pytest.raises(forms.FormInputError):
        forms.apply_values(form, {"asset": "LAPTOP-2"})


def test_a_submit_that_never_left_can_be_retried_or_cancelled():
    fake = FakeTurns()
    tools = make_tools(fake)
    attempts: List[int] = []

    def flaky_submit(conversation_id, form, progress) -> TurnOutcome:
        attempts.append(1)
        if len(attempts) == 1:
            raise NotAccepted(RateLimitedError("slow down"))
        fake.submits.append(dict(form.values))
        return TurnOutcome(text="Submitted **Wireless keyboard and mouse**.\n\nRequest RITM0010099 created.")

    fake.submit_form = flaky_submit  # pyright: ignore[reportAttributeAccessIssue]

    async def scenario():
        asked = await tools.ask(ALICE, "I need a new keyboard", None)
        handle = asked.data["conversation_id"]
        await tools.open_form(ALICE, handle, "opt_1")
        values = {"justification": "Keys stick", "color": "Black"}
        limited = await tools.submit_form(ALICE, handle, values, "submit")
        done = await tools.submit_form(ALICE, handle, values, "submit")
        return limited, done

    limited, done = asyncio.run(scenario())
    assert limited.data["error"] == "rate_limited"
    assert "RITM0010099" in done.text
    assert len(fake.submits) == 1


def test_cancel_after_a_rejected_submit_closes_the_form_and_forgets_the_error():
    fake = FakeTurns()
    tools = make_tools(fake)

    def rejected(conversation_id, form, progress) -> TurnOutcome:
        raise NotAccepted(RateLimitedError("slow down"))

    fake.submit_form = rejected  # pyright: ignore[reportAttributeAccessIssue]

    async def scenario():
        asked = await tools.ask(ALICE, "I need a new keyboard", None)
        handle = asked.data["conversation_id"]
        await tools.open_form(ALICE, handle, "opt_1")
        await tools.submit_form(ALICE, handle, {"justification": "Keys stick", "color": "Black"}, "submit")
        cancelled = await tools.submit_form(ALICE, handle, None, "cancel")
        after = await tools.submit_form(ALICE, handle, None, "submit")
        return cancelled, after

    cancelled, after = asyncio.run(scenario())
    assert cancelled.data["status"] == "cancelled"
    assert after.data["error"] == "no_open_form"
