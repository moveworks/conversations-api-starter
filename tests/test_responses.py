"""Response polling and text extraction."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from moveworks_capi import responses
from moveworks_capi.client import ConversationsApiClient
from moveworks_capi.errors import RateLimitedError, ResponseFailedError
from moveworks_capi.responses import (
    MAX_TEXT_LENGTH,
    Response,
    create_response,
    extract_text,
    HandoffItem,
    message_actions,
    message_handoff,
    user_input,
    wait_for_completion,
)


class _FakeHttpResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers: dict = {}
        self.text = ""

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._payload


def _response_body(status: str, outputs: list | None = None) -> dict:
    return {
        "response_id": "resp_1",
        "conversation_id": "conv_1",
        "status": status,
        "outputs": outputs or [],
    }


def _message_output(content_type: str, text: str) -> dict:
    return {
        "type": "MESSAGE",
        "message": {
            "message_id": "msg_1",
            "conversation_id": "conv_1",
            "response_id": "resp_1",
            "actor": "ASSISTANT",
            "content": {"type": content_type, content_type.lower(): {"text": text}},
        },
    }


class TestWaitForCompletion:
    @pytest.fixture(autouse=True)
    def sleeps(self, monkeypatch) -> list:
        recorded: list = []
        monkeypatch.setattr(responses.time, "sleep", recorded.append)
        return recorded

    def test_waits_before_the_first_poll_then_every_two_seconds(self, sleeps: list) -> None:
        client = Mock()
        client.get.side_effect = [
            _FakeHttpResponse(_response_body("IN_PROGRESS")),
            _FakeHttpResponse(_response_body("COMPLETED")),
        ]
        wait_for_completion(client, "conv_1", "resp_1")
        assert sleeps == [1.0, 2.0]

    def test_a_429_while_polling_waits_and_polls_again(self, sleeps: list) -> None:
        client = Mock()
        client.get.side_effect = [RateLimitedError("rate limited"), _FakeHttpResponse(_response_body("COMPLETED"))]
        result = wait_for_completion(client, "conv_1", "resp_1", timeout_seconds=30.0)
        assert result.status == "COMPLETED"
        assert sleeps == [1.0, responses.RATE_LIMIT_FLOOR_SECONDS]

    def test_works_with_client_retries_off(self, sleeps: list) -> None:
        session = Mock()
        session.request.side_effect = [
            _FakeHttpResponse({}, status_code=429),
            _FakeHttpResponse(_response_body("COMPLETED")),
        ]
        client = ConversationsApiClient("https://api.example.com", "bot", lambda: "t", max_retries=0, session=session)
        assert wait_for_completion(client, "conv_1", "resp_1", timeout_seconds=30.0).status == "COMPLETED"
        assert session.request.call_count == 2

    def test_returns_immediately_when_already_completed(self) -> None:
        client = Mock()
        client.get.return_value = _FakeHttpResponse(_response_body("COMPLETED"))
        result = wait_for_completion(client, "conv_1", "resp_1", timeout_seconds=5.0)
        assert result.status == "COMPLETED"
        assert client.get.call_count == 1

    def test_polls_until_terminal(self) -> None:
        client = Mock()
        client.get.side_effect = [
            _FakeHttpResponse(_response_body("CREATED")),
            _FakeHttpResponse(_response_body("IN_PROGRESS")),
            _FakeHttpResponse(_response_body("COMPLETED")),
        ]
        result = wait_for_completion(
            client, "conv_1", "resp_1", timeout_seconds=5.0, poll_interval_seconds=0.0
        )
        assert result.status == "COMPLETED"
        assert client.get.call_count == 3

    def test_returns_on_failed(self) -> None:
        client = Mock()
        client.get.return_value = _FakeHttpResponse(_response_body("FAILED"))
        result = wait_for_completion(client, "conv_1", "resp_1", timeout_seconds=5.0)
        assert result.status == "FAILED"

    def test_raises_on_timeout(self) -> None:
        client = Mock()
        client.get.return_value = _FakeHttpResponse(_response_body("IN_PROGRESS"))
        with pytest.raises(TimeoutError):
            wait_for_completion(
                client, "conv_1", "resp_1", timeout_seconds=0.05, poll_interval_seconds=0.01
            )

    def test_hits_the_expected_path(self) -> None:
        client = Mock()
        client.get.return_value = _FakeHttpResponse(_response_body("COMPLETED"))
        wait_for_completion(client, "conv_XYZ", "resp_ABC", timeout_seconds=1.0)
        client.get.assert_called_with("/rest/v1/conversations/conv_XYZ/responses/resp_ABC")


class TestExtractText:
    def test_pulls_markdown_text(self) -> None:
        response = Response.from_json(_response_body(
            "COMPLETED",
            outputs=[_message_output("MARKDOWN_TEXT", "hello **world**")],
        ))
        assert extract_text(response) == "hello **world**"

    def test_pulls_plain_text(self) -> None:
        response = Response.from_json(_response_body(
            "COMPLETED",
            outputs=[_message_output("PLAIN_TEXT", "hello world")],
        ))
        assert extract_text(response) == "hello world"

    def test_pulls_commonmark_text(self) -> None:
        response = Response.from_json(_response_body(
            "COMPLETED",
            outputs=[_message_output("COMMONMARK_TEXT", "*emph*")],
        ))
        assert extract_text(response) == "*emph*"

    def test_joins_multiple_messages(self) -> None:
        response = Response.from_json(_response_body(
            "COMPLETED",
            outputs=[
                _message_output("MARKDOWN_TEXT", "first"),
                _message_output("PLAIN_TEXT", "second"),
            ],
        ))
        assert extract_text(response) == "first\n\nsecond"

    def test_skips_reasoning_outputs(self) -> None:
        response = Response.from_json(_response_body(
            "COMPLETED",
            outputs=[
                {"type": "REASONING_MESSAGE", "message": {"content": {"type": "PLAIN_TEXT", "plain_text": {"text": "thinking..."}}}},
                _message_output("MARKDOWN_TEXT", "actual reply"),
            ],
        ))
        assert extract_text(response) == "actual reply"

    def test_raises_on_failed_response(self) -> None:
        response = Response.from_json(_response_body("FAILED"))
        with pytest.raises(ResponseFailedError) as exc:
            extract_text(response)
        assert exc.value.response is response

    def test_returns_empty_string_when_no_outputs(self) -> None:
        response = Response.from_json(_response_body("COMPLETED", outputs=[]))
        assert extract_text(response) == ""


class TestExtractActions:
    """Action shapes as the API returns them."""

    def _message_with_actions(self, actions: list) -> dict:
        out = _message_output("MARKDOWN_TEXT", "pick one")
        out["message"]["actions"] = actions
        return out

    def test_extracts_url_callback_and_modal_actions(self) -> None:
        from moveworks_capi.responses import extract_actions

        body = _response_body(
            "COMPLETED",
            [
                self._message_with_actions(
                    [
                        {
                            "label": "File ticket",
                            "type": "MODAL_ACTION",
                            "modal_action": {"modal_id": "modal_abc"},
                        },
                        {
                            "label": "Ask GPT",
                            "type": "CALLBACK_ACTION",
                            "callback_action": {"callback_id": "cb_1"},
                        },
                        {
                            "label": "Docs",
                            "type": "URL_ACTION",
                            "url_action": {"url": "https://example.com/docs"},
                        },
                    ]
                )
            ],
        )
        actions = extract_actions(Response.from_json(body))
        assert [a.type for a in actions] == ["MODAL_ACTION", "CALLBACK_ACTION", "URL_ACTION"]
        assert actions[0].modal_id == "modal_abc"
        assert actions[1].callback_id == "cb_1"
        assert actions[2].url == "https://example.com/docs"
        assert not any(a.is_auth_link for a in actions)

    def test_flags_uca_consent_link_as_auth(self) -> None:
        from moveworks_capi.responses import extract_actions

        consent_url = (
            "https://acme.moveworks.com/connections"
            "?integrationId=my_connector&encryptedSourceCallbackContext=AAAAAx"
        )
        body = _response_body(
            "COMPLETED",
            [
                self._message_with_actions(
                    [
                        {
                            "label": "my_connector",
                            "type": "URL_ACTION",
                            "url_action": {"url": consent_url},
                        }
                    ]
                )
            ],
        )
        actions = extract_actions(Response.from_json(body))
        assert len(actions) == 1
        assert actions[0].is_auth_link
        assert actions[0].url == consent_url

    def test_no_actions_returns_empty(self) -> None:
        from moveworks_capi.responses import extract_actions

        body = _response_body("COMPLETED", [_message_output("MARKDOWN_TEXT", "hi")])
        assert extract_actions(Response.from_json(body)) == []

    def test_skips_reasoning_outputs(self) -> None:
        from moveworks_capi.responses import extract_actions

        reasoning = {
            "type": "REASONING_MESSAGE",
            "reasoning_message": {"content": {"type": "MARKDOWN_TEXT", "markdown_text": {"text": "Thinking"}}},
        }
        body = _response_body("COMPLETED", [reasoning])
        assert extract_actions(Response.from_json(body)) == []


def test_message_actions_normalizes_one_message():
    message = {"actions": [
        {"label": "File ticket", "type": "CALLBACK_ACTION", "callback_action": {"callback_id": "cb"}},
        {"label": "Connect", "type": "URL_ACTION", "url_action": {"url": "https://acme.moveworks.com/connections?x=1"}},
    ]}

    actions = message_actions(message)

    assert [(a.type, a.callback_id, a.is_auth_link) for a in actions] == [
        ("CALLBACK_ACTION", "cb", False),
        ("URL_ACTION", None, True),
    ]


def test_consent_purpose_flags_an_auth_link_on_any_url():
    message = {"actions": [
        {"label": "Connect", "type": "URL_ACTION",
         "url_action": {"url": "https://idp.example.com/authorize?x=1", "purpose": "USER_CONSENT_AUTH"}},
    ]}

    [action] = message_actions(message)

    assert (action.purpose, action.is_auth_link) == ("USER_CONSENT_AUTH", True)


def test_absent_purpose_falls_back_to_the_url_check():
    message = {"actions": [
        {"label": "Connect", "type": "URL_ACTION", "url_action": {"url": "https://acme.moveworks.com/connections?x=1"}},
        {"label": "Docs", "type": "URL_ACTION", "url_action": {"url": "https://example.com/vpn"}},
    ]}

    assert [(a.purpose, a.is_auth_link) for a in message_actions(message)] == [(None, True), (None, False)]


def test_unknown_purpose_is_treated_as_unclassified():
    message = {"actions": [
        {"label": "Connect", "type": "URL_ACTION",
         "url_action": {"url": "https://acme.moveworks.com/connections?x=1", "purpose": "FUTURE_PURPOSE"}},
        {"label": "Docs", "type": "URL_ACTION",
         "url_action": {"url": "https://example.com/vpn", "purpose": "FUTURE_PURPOSE"}},
    ]}

    assert [a.is_auth_link for a in message_actions(message)] == [True, False]


def test_message_handoff_normalizes_action_and_text_items():
    message = {"handoff": {"handoff_categories": [
        {"category_id": "c1", "name": "IT", "description": "Reach the service desk.", "handoff_items": [
            {"item_id": "i1", "title": "File a ticket", "type": "ACTION",
             "action": {"type": "MODAL_ACTION", "label": "File a ticket", "modal_action": {"modal_id": "m1"}}},
            {"item_id": "i2", "title": "Call us", "type": "TEXT",
             "text": {"type": "PLAIN_TEXT", "plain_text": {"text": "Call extension 1234."}}},
        ]},
    ]}}

    [category] = message_handoff(message)

    assert (category.name, category.description) == ("IT", "Reach the service desk.")
    [ticket, call] = category.items
    assert ticket.action is not None
    assert (ticket.type, ticket.action.modal_id, ticket.text) == ("ACTION", "m1", None)
    assert call == HandoffItem(title="Call us", type="TEXT", text="Call extension 1234.")


def test_message_handoff_is_empty_without_handoff_options():
    assert message_handoff({"actions": []}) == []


class TestUserInput:
    def test_text_is_a_chat_turn(self) -> None:
        assert user_input("hi") == {"chat": {"text": "hi"}}

    def test_files_alone_are_a_chat_turn(self) -> None:
        assert user_input(file_ids=["file_a", "file_b"]) == {"chat": {"files": [{"file_id": "file_a"}, {"file_id": "file_b"}]}}

    def test_callback_is_an_action_turn(self) -> None:
        assert user_input(callback_id="cb") == {"action": {"callback_id": "cb"}}

    @pytest.mark.parametrize("kwargs", [{}, {"text": "hi", "callback_id": "cb"}, {"file_ids": ["f"], "callback_id": "cb"}])
    def test_rejects_ambiguous_turns(self, kwargs: dict) -> None:
        with pytest.raises(ValueError):
            user_input(**kwargs)

    def test_enforces_documented_limits(self) -> None:
        with pytest.raises(ValueError):
            user_input("x" * (MAX_TEXT_LENGTH + 1))
        with pytest.raises(ValueError):
            user_input(file_ids=[f"file_{i}" for i in range(6)])


def test_create_response_posts_chat_input() -> None:
    client = Mock()
    client.post.return_value = _FakeHttpResponse({"response_id": "resp_1", "status": "IN_PROGRESS"})
    assert create_response(client, "conv_1", "hi")["response_id"] == "resp_1"
    client.post.assert_called_once_with("/rest/v1/conversations/conv_1/responses", json={"input": {"chat": {"text": "hi"}}})
