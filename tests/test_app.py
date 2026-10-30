"""Reference web app server: security rules, message presentation, turns."""

from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path
from typing import Any, AsyncGenerator, cast

import pytest
from fastapi.testclient import TestClient

import app.present as present
import app.server as server
import app.turns as turns
from moveworks_capi.errors import RequestRejectedError, StreamClosedError, StreamError
from moveworks_capi.notifications import Event
from moveworks_capi.responses import Response
from moveworks_capi.streaming import OutputDelta, ResponseCompleted, ResponseCreated
from tests.test_rendering import PORTAL_REDIRECTS

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def local():
    return TestClient(server.app, base_url="http://127.0.0.1:8090")


@pytest.fixture(autouse=True)
def fresh_session():
    server.session.reset()
    yield
    server.session.reset()


class TestSecurity:
    def test_local_requests_are_served(self, local):
        assert local.get("/api/session").status_code == 200

    def test_foreign_host_header_is_refused(self):
        # DNS rebinding: a hostile name resolving to 127.0.0.1.
        rebound = TestClient(server.app, base_url="http://attacker.example:8090")
        assert rebound.get("/api/session").status_code == 400

    def test_cross_origin_post_is_refused(self, local):
        resp = local.post("/api/disconnect", json={}, headers={"Origin": "https://attacker.example"})
        assert resp.status_code == 403

    def test_non_json_post_is_refused(self, local):
        # A plain form or text/plain POST needs no CORS preflight.
        resp = local.post("/api/disconnect", content="{}", headers={"Content-Type": "text/plain"})
        assert resp.status_code == 415

    def test_another_local_port_is_a_different_origin(self, local):
        resp = local.post("/api/disconnect", json={}, headers={"Origin": "http://127.0.0.1:9999"})
        assert resp.status_code == 403

    def test_same_origin_json_post_is_served(self, local):
        resp = local.post("/api/disconnect", json={}, headers={"Origin": "http://127.0.0.1:8090"})
        assert resp.status_code == 200

    def test_page_and_static_files_revalidate(self, local):
        for path in ("/", "/static/js/chat.js", "/static/styles.css"):
            assert local.get(path).headers["cache-control"] == "no-cache", path

    def test_api_responses_keep_their_own_headers(self, local):
        assert "cache-control" not in local.get("/api/session").headers


def _form_message() -> dict:
    intro = "I also found a **Corp VPN** form ."
    return {
        "message_id": "msg_1",
        "response_id": "resp_1",
        "actor": "ASSISTANT",
        "content": {"type": "MARKDOWN_TEXT", "markdown_text": {
            "text": f"{intro}\n\n👉 **Which device?**\n\n**Corp VPN**\nRemote access to Internal Corporate Systems",
        }},
        "citations": [
            {"citation_id": "cite_form", "url": "https://example.service-now.com/form", "display": {
                "title": {"markdown_text": {"text": "**<https://example.service-now.com/form|Corp VPN>**"}},
                "body": {"markdown_text": {"text": "Remote access to Internal Corporate Systems\nMore."}},
            }},
            {"citation_id": "cite_kb", "url": "https://example.service-now.com/kb", "display": {
                "title": {"markdown_text": {"text": "<https://example.service-now.com/kb|KB0001>: VPN setup"}},
                "body": {"markdown_text": {"text": "Open <https://example.com|settings> :point_right: now"}},
                "attributes": [{"key": "TICKET_STATE", "value": "NEW"}],
            }},
        ],
        "content_references": [{"citation_id": "cite_form", "offset": len(intro)}],
        "actions": [],
    }


class TestSession:
    def test_session_lists_the_data_center_base_urls(self, local):
        urls = local.get("/api/session").json()["known_base_urls"]
        assert urls[0] == "https://api.moveworks.ai"
        assert "https://api.am-eu-central.moveworks.ai" in urls
        assert all(url.startswith("https://api.") for url in urls)


class TestConnect:
    @pytest.fixture
    def env_file(self, tmp_path, monkeypatch):
        (tmp_path / "key.pem").write_text("-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n")
        env = tmp_path / ".env"
        env.write_text(
            "MW_BASE_URL=https://api.moveworks.ai\nMW_BOT_NAME=acme-capi\n"
            "MW_S2S_ISSUER=iss\nMW_S2S_AUDIENCE=aud\nMW_S2S_KEY_ID=kid\n"
            "MW_S2S_PRIVATE_KEY_PATH=key.pem\nMW_S2S_USER_EMAIL=pat@example.com\n"
        )
        monkeypatch.setattr(server, "ENV_FILE", env)
        monkeypatch.setattr(server, "REPO_ROOT", tmp_path)

        async def no_polling():
            return None

        monkeypatch.setattr(server, "_polling_task", no_polling)
        return env

    def test_session_shows_the_env_user_but_no_key_material(self, local, env_file):
        env = local.get("/api/session").json()["env"]

        assert env == {"base_url": "https://api.moveworks.ai", "bot_name": "acme-capi",
                       "s2s_user": "pat@example.com", "s2s_missing": []}

    def test_s2s_connect_builds_the_bearer_from_env_and_proves_it(self, local, env_file, monkeypatch):
        built, proved = [], []

        class FakeBearer:
            def __init__(self, **kwargs):
                built.append(kwargs)

            def __call__(self):
                return "token"

        monkeypatch.setattr(server, "S2SJwtBearer", FakeBearer)
        monkeypatch.setattr(server, "list_conversations", lambda client, **kw: proved.append((client, kw)) or {})

        body = {"auth": "s2s", "base_url": "https://api.am-eu-central.moveworks.ai", "bot_name": "other-bot"}
        resp = local.post("/api/connect", json=body)

        assert resp.status_code == 200
        assert built[0]["base_url"] == "https://api.am-eu-central.moveworks.ai"
        assert built[0]["user_identifier"] == "pat@example.com"
        assert built[0]["private_key"] == env_file.parent / "key.pem"
        assert proved[0][1] == {"limit": 1}
        assert server.session.bot_name == "other-bot"

    def test_s2s_connect_names_missing_env_values(self, local, env_file):
        env_file.write_text("MW_S2S_ISSUER=iss\n")

        resp = local.post("/api/connect", json={"auth": "s2s", "base_url": "https://api.moveworks.ai", "bot_name": "b"})

        assert resp.status_code == 400
        assert "MW_S2S_AUDIENCE" in resp.json()["detail"]

    def test_api_key_connect_requires_a_key(self, local):
        resp = local.post("/api/connect", json={"base_url": "https://api.moveworks.ai", "bot_name": "b", "api_key": " "})
        assert resp.status_code == 400


class TestPresentMessage:
    def test_smart_handoff_payload(self):
        message = json.loads((FIXTURES / "smart_handoff_message.json").read_text())

        shown = present.present_message(message)

        assert [(a["label"], a["type"]) for a in shown["actions"]] == [
            ("File ticket", "CALLBACK_ACTION"),
            ("Edit/Add details", "MODAL_ACTION"),
            ("Other help options", "HANDOFF_ACTION"),
        ]
        # The HANDOFF_ACTION opens the menu carried on the same message.
        assert [i["title"] for i in shown["handoff"][0]["items"]] == ["File an IT ticket", "Start agent chat"]
        assert shown["feedback"] == {"helpful": "callback_fixture", "unhelpful": "callback_fixture"}
        assert shown["text"].startswith("I understand you’d like to file a ticket")

    def test_omitted_handoff_action_keeps_form_links_paired(self, offline_short_links):
        offline_short_links.update(PORTAL_REDIRECTS)
        message = json.loads((FIXTURES / "portal_form_message.json").read_text())
        message["actions"].insert(0, {"type": "HANDOFF_ACTION", "label": "Other help options", "handoff_action": {}})

        shown = present.present_message(message)

        assert [a["type"] for a in shown["actions"]] == ["URL_ACTION"] * 3
        assert shown["actions"][0]["form"]["title"] == "401(k) Retirement Plan Inquiry"

    def test_handoff_action_with_only_empty_categories_is_omitted(self):
        message = json.loads((FIXTURES / "smart_handoff_message.json").read_text())
        message["handoff"]["handoff_categories"][0]["handoff_items"] = []

        assert "HANDOFF_ACTION" not in [a["type"] for a in present.present_message(message)["actions"]]

    def test_handoff_action_without_handoff_options_is_omitted(self):
        message = json.loads((FIXTURES / "smart_handoff_message.json").read_text())
        del message["handoff"]

        shown = present.present_message(message)

        assert [a["type"] for a in shown["actions"]] == ["CALLBACK_ACTION", "MODAL_ACTION"]

    def test_handoff_items_with_nothing_to_render_are_dropped(self):
        message = json.loads((FIXTURES / "smart_handoff_message.json").read_text())
        category = message["handoff"]["handoff_categories"][0]
        category["handoff_items"].append({"item_id": "empty", "title": "Nothing here", "type": "TEXT"})
        message["handoff"]["handoff_categories"].append(
            {"category_id": "c_empty", "name": "Empty", "handoff_items": [{"item_id": "x", "title": "No payload", "type": "ACTION"}]}
        )

        shown = present.present_message(message)

        assert [c["name"] for c in shown["handoff"]] == ["IT (Recommended)"]
        assert [i["title"] for i in shown["handoff"][0]["items"]] == ["File an IT ticket", "Start agent chat"]

    def test_handoff_action_with_only_unrenderable_items_is_omitted(self):
        message = json.loads((FIXTURES / "smart_handoff_message.json").read_text())
        message["handoff"]["handoff_categories"][0]["handoff_items"] = [{"item_id": "x", "title": "No payload", "type": "TEXT"}]

        shown = present.present_message(message)

        assert shown["handoff"] == []
        assert "HANDOFF_ACTION" not in [a["type"] for a in shown["actions"]]

    def test_url_actions_must_be_web_links(self):
        link = {"type": "URL_ACTION", "label": "Open", "url_action": {"url": "javascript:alert(1)", "purpose": "USER_CONSENT_AUTH"}}
        message = {
            "actions": [link, {"type": "URL_ACTION", "label": "Docs", "url_action": {"url": "https://example.com/vpn"}}],
            "handoff": {"handoff_categories": [{"category_id": "c1", "name": "IT", "handoff_items": [
                {"item_id": "i1", "title": "Open", "type": "ACTION", "action": link},
            ]}]},
        }

        shown = present.present_message(message, resolve_links=False)

        assert [(a["url"], a["is_auth_link"]) for a in shown["actions"]] == [(None, False), ("https://example.com/vpn", False)]
        assert shown["handoff"][0]["items"][0]["action"]["url"] is None

    def test_user_messages_carry_plain_text(self):
        user = {"actor": "USER", "content": {"type": "PLAIN_TEXT", "plain_text": {"text": "I need a laptop"}}}
        assert present.present_message(user)["text"] == "I need a laptop"

    def test_user_attachments_are_listed_by_name(self):
        user = {"actor": "USER", "content": {"type": "PLAIN_TEXT", "plain_text": {
            "text": "see attached", "files": [{"file_id": "file_a", "file_name": "vpn-error.png"}, {"file_id": "file_b"}]}}}
        shown = present.present_message(user)
        assert (shown["text"], shown["files"], shown["is_action"]) == ("see attached", ["vpn-error.png", "file_b"], False)

    def test_button_presses_in_history_are_flagged(self):
        user = {"actor": "USER", "content": {"type": "CALLBACK_ACTION_CONTENT", "callback_action": {"callback_id": "cb"}}}
        shown = present.present_message(user)
        assert (shown["text"], shown["is_action"]) == ("", True)

    def test_flattened_form_copy_is_dropped_and_the_question_stays_last(self):
        shown = present.present_message(_form_message())

        assert shown["text"] == "I also found a **Corp VPN** form.\n\n👉 **Which device?**"
        assert [s["title"] for s in shown["sources"]] == ["Corp VPN", "KB0001: VPN setup"]
        assert shown["sources"][1]["snippet"] == "Open [settings](https://example.com) 👉 now"

    def test_portal_form_links_carry_the_form_they_open(self, offline_short_links):
        offline_short_links.update(PORTAL_REDIRECTS)
        shown = present.present_message(json.loads((FIXTURES / "portal_form_message.json").read_text()))

        assert [a["form"]["title"] for a in shown["actions"]] == [
            "401(k) Retirement Plan Inquiry",
            "401(k) Plan Enrollment Inquiry",
            "401(k) Plan Enrollment",
        ]
        assert [a["form"]["summary"] for a in shown["actions"]] == [
            "Ask a question about your existing plan",
            "Ask a question about your retirement plan options or enrollment process",
            "Enroll in or modify your 401(k) plan",
        ]

    def test_form_summary_joins_a_description_that_wraps(self):
        citation = {"display": {"body": {"markdown_text": {
            "text": "Request a reset of a password for a service or an\n\t\t\tapplication.\nRequest a reset.",
        }}}}

        assert present.form_summary(citation) == "Request a reset of a password for a service or an application."

    def test_streamed_snapshots_skip_short_link_lookups(self, offline_short_links):
        offline_short_links.update(PORTAL_REDIRECTS)
        message = json.loads((FIXTURES / "portal_form_message.json").read_text())

        shown = present.present_message(message, resolve_links=False)

        assert not any("form" in a for a in shown["actions"])

    def test_live_agent_messages_carry_the_agent_identity(self):
        # sender_info shape per the API reference.
        message = {
            "message_id": "msg_agent", "actor": "LIVE_AGENT",
            "content": {"type": "MARKDOWN_TEXT", "markdown_text": {"text": "Hi, I'm looking at your VPN issue now."}},
            "sender_info": {"display_name": "Dana Fielding", "avatar_url": "https://example.com/dana.png"},
        }

        shown = present.present_message(message)

        assert shown["actor"] == "LIVE_AGENT"
        assert shown["sender"] == {"display_name": "Dana Fielding", "avatar_url": "https://example.com/dana.png"}
        assert present.present_message({"actor": "ASSISTANT"})["sender"] is None

    def test_agent_avatar_must_be_https(self):
        for url in ("http://example.com/dana.png", "javascript:alert(1)", "data:image/png;base64,AAAA", None):
            sender = present.present_sender({"display_name": "Dana Fielding", "avatar_url": url})
            assert sender == {"display_name": "Dana Fielding", "avatar_url": ""}

    def test_get_help_menu_from_message_handoff(self):
        message = {"handoff": {"handoff_categories": [{
            "category_id": "it", "name": ":computer: IT", "description": "Hardware and access",
            "handoff_items": [
                {"title": "File a ticket", "type": "ACTION", "action": {
                    "label": "File a ticket", "type": "MODAL_ACTION", "modal_action": {"modal_id": "modal_1"}}},
                {"title": "Hours", "type": "TEXT", "text": {"type": "MARKDOWN_TEXT", "markdown_text": {"text": "See <https://example.com|hours>"}}},
            ],
        }]}}

        menu = present.present_message(message)["handoff"]

        assert menu[0]["name"] == "IT"
        assert menu[0]["items"][0]["action"]["modal_id"] == "modal_1"
        assert menu[0]["items"][1]["text"] == "See [hours](https://example.com)"

    def test_modal_display_html_becomes_markdown(self):
        modal = {"title": ":memo: New request", "fields": [
            {"type": "DISPLAY", "display": {"text": {"style": "SUBTLE", "text": 'Having issues? <a href="https://example.com">Portal</a>'}}},
            {"type": "TEXT", "name": "summary", "text": {}},
        ]}

        shown = present.present_modal(modal)

        assert shown["title"] == "📝 New request"
        assert shown["fields"][0]["display"]["text"]["text"] == "Having issues? [Portal](https://example.com)"
        assert shown["fields"][1] == modal["fields"][1]

    def test_citation_links_must_be_web_links(self):
        message = {"citations": [
            {"url": "javascript:alert(1)", "display": {"title": {"type": "PLAIN_TEXT", "plain_text": {"text": "Bad"}}}},
            {"url": "https://example.com/kb", "display": {"title": {"type": "COMMONMARK_TEXT", "commonmark_text": {"text": "Good"}}}},
        ]}

        sources = present.present_sources(message)

        assert [(s["title"], s["url"]) for s in sources] == [("Bad", ""), ("Good", "https://example.com/kb")]

    def test_modal_images_and_downloads_must_be_web_links(self):
        modal = {"title": "Form", "fields": [
            {"type": "DISPLAY", "display": {"image": {"url": "javascript:alert(1)"}}},
            {"type": "DISPLAY", "display": {"download": {"url": "file:///etc/passwd", "text": "Log"}}},
            {"type": "DISPLAY", "display": {"download": {"url": "https://example.com/log.txt", "text": "Log"}}},
        ]}

        fields = present.present_modal(modal)["fields"]

        assert fields[0]["display"]["image"]["url"] == ""
        assert fields[1]["display"]["download"] == {"url": "", "text": "Log"}
        assert fields[2]["display"]["download"]["url"] == "https://example.com/log.txt"


class TestHistory:
    def test_messages_are_presented_oldest_first(self, local, monkeypatch):
        server.session.client = cast(Any, object())
        newest_first = {"messages": [
            {"message_id": "m2", "actor": "ASSISTANT", "content": {"type": "MARKDOWN_TEXT", "markdown_text": {"text": "Hi!"}}},
            {"message_id": "m1", "actor": "USER", "content": {"type": "PLAIN_TEXT", "plain_text": {"text": "hello"}}},
        ]}
        monkeypatch.setattr(server, "list_messages", lambda client, cid, limit, cursor: newest_first)

        body = local.get("/api/conversations/conv_1/messages").json()

        assert [(m["message_id"], m["actor"], m["text"]) for m in body["messages"]] == [
            ("m1", "USER", "hello"),
            ("m2", "ASSISTANT", "Hi!"),
        ]
        assert body["next_cursor"] is None

    def test_older_pages_pass_the_cursor_through(self, local, monkeypatch):
        server.session.client = cast(Any, object())
        seen = {}

        def fake(client, cid, limit, cursor):
            seen.update(limit=limit, cursor=cursor)
            return {"messages": [], "metadata": {"next_cursor": "cur_2"}}

        monkeypatch.setattr(server, "list_messages", fake)

        body = local.get("/api/conversations/conv_1/messages?cursor=cur_1").json()

        assert seen == {"limit": 50, "cursor": "cur_1"}
        assert body["next_cursor"] == "cur_2"


class TestConversationsSidebar:
    def test_archived_view_and_cursor_reach_the_api(self, local, monkeypatch):
        server.session.client = cast(Any, object())
        seen = {}
        monkeypatch.setattr(server, "list_conversations", lambda client, **kw: seen.update(kw) or {"conversations": []})

        local.get("/api/conversations?limit=20&archived=true&cursor=abc")

        assert seen == {"limit": 20, "cursor": "abc", "archived": True}

    def test_rename_and_archive_patch_the_conversation(self, local, monkeypatch):
        server.session.client = cast(Any, object())
        calls = []
        monkeypatch.setattr(server, "update_conversation", lambda client, cid, **kw: calls.append((cid, kw)) or {"conversation_id": cid})

        assert local.patch("/api/conversations/conv_1", json={"title": "  VPN trouble "}).status_code == 200
        assert local.patch("/api/conversations/conv_1", json={"archived": True}).status_code == 200

        assert calls == [
            ("conv_1", {"title": "VPN trouble", "archived": None}),
            ("conv_1", {"title": None, "archived": True}),
        ]

    def test_empty_patch_and_blank_title_are_rejected(self, local):
        server.session.client = cast(Any, object())
        assert local.patch("/api/conversations/conv_1", json={}).status_code == 400
        assert local.patch("/api/conversations/conv_1", json={"title": "   "}).status_code == 400

    def test_cross_origin_patch_is_refused(self, local):
        resp = local.patch("/api/conversations/conv_1", json={"archived": True}, headers={"Origin": "https://attacker.example"})
        assert resp.status_code == 403


class TestFeedback:
    def test_comment_rides_along_as_additional_feedback(self, local):
        sent = []

        class Client:
            def post(self, path, json):
                sent.append(json)
                return type("R", (), {"json": lambda self: {"status": "OK"}})()

        server.session.client = cast(Any, Client())
        ref = {"conversation_id": "c", "response_id": "r", "message_id": "m", "callback_id": "cb"}

        local.post("/api/feedback", json={**ref, "additional_feedback": "  wrong form  "})
        local.post("/api/feedback", json={**ref, "additional_feedback": "   "})
        local.post("/api/feedback", json=ref)

        assert sent == [
            {"callback_id": "cb", "additional_feedback": "wrong form"},
            {"callback_id": "cb"},
            {"callback_id": "cb"},
        ]


class TestModalRefresh:
    def test_refresh_sends_every_value_and_presents_the_result(self, local):
        sent = []

        class Client:
            def post(self, path, json):
                sent.append((path, json))
                modal = {"modal_id": "modal_1", "title": "New laptop", "fields": [
                    {"name": "intro", "type": "DISPLAY", "display": {"text": {"text": "Pick one<br>then submit"}}}]}
                return type("R", (), {"json": lambda self: modal})()

        server.session.client = cast(Any, Client())
        ref = {"conversation_id": "c", "response_id": "r", "message_id": "m", "modal_id": "modal_1"}
        values = {"model": {"option": "mac"}, "reason": {"text": "broken"}}

        shown = local.post("/api/modal/refresh", json={**ref, "values": values}).json()

        assert sent == [("/rest/v1/conversations/c/responses/r/messages/m/modals/modal_1/refresh", {"values": values})]
        assert shown["fields"][0]["display"]["text"]["text"] == "Pick one\nthen submit"

    def test_missing_modal_explains_fresh_ids_and_availability(self, local):
        class Client:
            def post(self, path, json):
                raise RequestRejectedError("request rejected (404): 404 not found", status_code=404)

        server.session.client = cast(Any, Client())
        ref = {"conversation_id": "c", "response_id": "r", "message_id": "m", "modal_id": "modal_1"}
        detail = local.post("/api/modal/refresh", json={**ref, "values": {}}).json()["detail"]
        assert "re-read the message" in detail and "modals are available" in detail


def _fake_client() -> Any:
    return _FakeClient()


class _FakeClient:
    def __init__(self):
        self.posts = []

    def post(self, path, json=None):
        self.posts.append((path, json))
        payload = {"conversation_id": "conv_new"} if path.endswith("/conversations") else {"response_id": "resp_1"}
        return type("R", (), {"json": lambda self: payload})()


def _frames(raw: list) -> list:
    out = []
    for chunk in raw:
        event, data = chunk.decode().strip().split("\n", 1)
        out.append((event.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
    return out


class TestTurns:
    def test_submit_and_poll_turn_yields_the_same_frames_as_streaming(self, monkeypatch):
        message = json.loads((FIXTURES / "smart_handoff_message.json").read_text())
        completed = Response(
            response_id="resp_1", conversation_id="conv_new", status="COMPLETED",
            outputs=[{"type": "MESSAGE", "message": message}], raw={},
        )
        monkeypatch.setattr(turns, "wait_for_completion", lambda client, cid, rid, **kw: completed)
        client: Any = _FakeClient()

        async def collect():
            return [f async for f in turns.run_turn(client, None, "file a ticket", None, streaming=False)]

        frames = _frames(asyncio.run(collect()))

        assert [event for event, _ in frames] == ["conversation", "delta", "completed", "actions", "handoff", "feedback_ids"]
        assert client.posts[0] == ("/rest/v1/conversations", {"title": "file a ticket"})
        assert client.posts[1] == ("/rest/v1/conversations/conv_new/responses", {"input": {"chat": {"text": "file a ticket"}}})
        assert frames[1][1]["output_type"] == "MESSAGE"
        assert len(frames[3][1]["actions"]) == 3


    def test_empty_callback_response_surfaces_the_reply_filed_under_an_earlier_response(self, monkeypatch):
        # Exit Live Chat completes with no outputs; the farewell sits in history under
        # the handoff turn's response and is not an event.
        empty = Response(response_id="resp_exit", conversation_id="conv_1", status="COMPLETED", outputs=[], raw={})
        monkeypatch.setattr(turns, "wait_for_completion", lambda client, cid, rid, **kw: empty)
        history = {"messages": [
            {"message_id": "m3", "actor": "ASSISTANT", "response_id": "resp_handoff", "created_at": "2026-09-25T21:28:54Z",
             "content": {"type": "MARKDOWN_TEXT", "markdown_text": {"text": "You've ended your live agent chat session."}}},
            {"message_id": "m2", "actor": "USER", "response_id": "resp_exit", "created_at": "2026-09-25T21:28:53Z",
             "content": {"type": "CALLBACK_ACTION_CONTENT"}},
            {"message_id": "m1", "actor": "LIVE_AGENT", "response_id": "resp_agent", "created_at": "2026-09-25T21:28:11Z",
             "content": {"type": "MARKDOWN_TEXT", "markdown_text": {"text": "let me look into it"}}},
        ]}
        monkeypatch.setattr(turns, "list_messages", lambda client, cid, limit: history)

        async def collect():
            return [f async for f in turns.run_turn(_fake_client(), "conv_1", None, "cb_exit", streaming=False)]

        frames = _frames(asyncio.run(collect()))

        late = [data for event, data in frames if event == "late_message"]
        assert [m["message"]["text"] for m in late] == ["You've ended your live agent chat session."]


    def test_streamed_turn_sends_reasoning_then_the_answer_then_actions(self, monkeypatch):
        message = json.loads((FIXTURES / "smart_handoff_message.json").read_text())
        events = [
            ResponseCreated(sequence_number=0, raw={}, response_id="resp_1", conversation_id="conv_1"),
            OutputDelta(sequence_number=1, raw={}, output_type="REASONING_MESSAGE", text="→ Looking <https://example.com|here>", output={}),
            OutputDelta(sequence_number=2, raw={}, output_type="MESSAGE", text="", output={"message": message}),
            ResponseCompleted(sequence_number=3, raw={}, response_id="resp_1", status="COMPLETED"),
        ]
        completed = Response(response_id="resp_1", conversation_id="conv_1", status="COMPLETED",
                             outputs=[{"type": "MESSAGE", "message": message}], raw={})
        monkeypatch.setattr(turns, "stream_response", lambda *a, **kw: iter(events))
        monkeypatch.setattr(turns, "wait_for_completion", lambda client, cid, rid, **kw: completed)

        async def collect():
            return [f async for f in turns.run_turn(_fake_client(), "conv_1", "file a ticket", None, streaming=True)]

        frames = _frames(asyncio.run(collect()))

        assert [event for event, _ in frames] == ["delta", "delta", "completed", "actions", "handoff", "feedback_ids"]
        assert frames[0][1] == {"output_type": "REASONING_MESSAGE", "text": "→ Looking [here](https://example.com)"}
        assert frames[1][1]["text"].startswith("I understand you’d like to file a ticket")

    def test_streamed_turn_with_two_handoff_messages_keys_frames_by_message(self, monkeypatch):
        # One streamed turn can hold several messages, each with its own HANDOFF_ACTION and menu.
        first = json.loads((FIXTURES / "smart_handoff_message.json").read_text())
        second = {**json.loads((FIXTURES / "smart_handoff_message.json").read_text()), "message_id": "msg_second"}
        first["message_id"] = "msg_first"
        events = [
            ResponseCreated(sequence_number=0, raw={}, response_id="resp_1", conversation_id="conv_1"),
            OutputDelta(sequence_number=1, raw={}, output_type="MESSAGE", text="", output={"message": first}),
            OutputDelta(sequence_number=2, raw={}, output_type="MESSAGE", text="", output={"message": second}),
            ResponseCompleted(sequence_number=3, raw={}, response_id="resp_1", status="COMPLETED"),
        ]
        completed = Response(response_id="resp_1", conversation_id="conv_1", status="COMPLETED", raw={},
                             outputs=[{"type": "MESSAGE", "message": first}, {"type": "MESSAGE", "message": second}])
        monkeypatch.setattr(turns, "stream_response", lambda *a, **kw: iter(events))
        monkeypatch.setattr(turns, "wait_for_completion", lambda client, cid, rid, **kw: completed)

        async def collect():
            return [f async for f in turns.run_turn(_fake_client(), "conv_1", "file a ticket", None, streaming=True)]

        frames = _frames(asyncio.run(collect()))

        actions = [data for event, data in frames if event == "actions"]
        handoffs = [data for event, data in frames if event == "handoff"]
        assert [f["message_id"] for f in actions] == ["msg_first", "msg_second"]
        assert [f["message_id"] for f in handoffs] == ["msg_first", "msg_second"]
        assert all("HANDOFF_ACTION" in [a["type"] for a in f["actions"]] for f in actions)

    def test_stream_that_closes_before_the_answer_falls_back_to_polling(self, monkeypatch):
        # The stream closes after RESPONSE_IN_PROGRESS and the answer lands after a
        # short poll would have given up.
        message = {"message_id": "m1", "actor": "ASSISTANT",
                   "content": {"type": "MARKDOWN_TEXT", "markdown_text": {"text": "Here is how to fix your VPN."}}}
        done = Response(response_id="resp_1", conversation_id="conv_1", status="COMPLETED",
                        outputs=[{"type": "MESSAGE", "message": message}], raw={})
        timeouts = []

        async def cut_short_stream(client, cid, text, callback_id, progress):
            progress.response_id = "resp_1"
            yield turns.sse_frame("delta", {"output_type": "REASONING_MESSAGE", "text": "→ Searching"})
            raise StreamClosedError("stream closed before a terminal event", response_id="resp_1")

        def fake_wait(client, cid, rid, timeout_seconds=60.0, **kw):
            timeouts.append(timeout_seconds)
            return done

        monkeypatch.setattr(turns, "stream_turn", cut_short_stream)
        monkeypatch.setattr(turns, "wait_for_completion", fake_wait)

        async def collect():
            return [f async for f in turns.run_turn(_fake_client(), "conv_1", "vpn help", None, streaming=True)]

        frames = _frames(asyncio.run(collect()))

        assert timeouts == [turns.STREAM_FALLBACK_TIMEOUT_SECONDS]
        deltas = [d for e, d in frames if e == "delta" and d["output_type"] == "MESSAGE"]
        assert deltas and deltas[0]["text"] == "Here is how to fix your VPN."
        assert ("completed", {"status": "COMPLETED"}) in frames

    def test_stream_error_event_is_reported_without_polling(self, monkeypatch):
        polled = []

        async def failing_stream(client, cid, text, callback_id, progress):
            progress.response_id = "resp_1"
            yield turns.sse_frame("delta", {"output_type": "REASONING_MESSAGE", "text": "→ Searching"})
            raise StreamError("Something went wrong generating the answer", code="INTERNAL")

        monkeypatch.setattr(turns, "stream_turn", failing_stream)
        monkeypatch.setattr(turns, "wait_for_completion", lambda *a, **kw: polled.append(a))

        async def collect():
            return [f async for f in turns.run_turn(_fake_client(), "conv_1", "vpn help", None, streaming=True)]

        frames = _frames(asyncio.run(collect()))

        assert frames[-1] == ("error", {"message": "Something went wrong generating the answer"})
        assert polled == []

    def test_poll_timeout_is_reported_instead_of_ending_silently(self, monkeypatch):
        async def cut_short_stream(client, cid, text, callback_id, progress):
            progress.response_id = "resp_1"
            yield turns.sse_frame("delta", {"output_type": "REASONING_MESSAGE", "text": "→ Searching"})

        def fake_wait(*a, **kw):
            raise TimeoutError("still IN_PROGRESS")

        monkeypatch.setattr(turns, "stream_turn", cut_short_stream)
        monkeypatch.setattr(turns, "wait_for_completion", fake_wait)

        async def collect():
            return [f async for f in turns.run_turn(_fake_client(), "conv_1", "vpn help", None, streaming=True)]

        frames = _frames(asyncio.run(collect()))

        assert [e for e, _ in frames] == ["delta", "error"]


class _EventsClient:
    def __init__(self, pages: list):
        self.pages = pages
        self.reads: list = []

    def get(self, path, params=None):
        self.reads.append(params or {})
        body = {"events": self.pages.pop(0)}
        return type("R", (), {"json": lambda self: body, "headers": {}})()


def _event_json(event_id: str, created_at: str, conversation_id: str = "conv_1") -> dict:
    return {
        "event_id": event_id,
        "conversation_id": conversation_id,
        "created_at": created_at,
        "event_payload": {"type": "MESSAGE_EVENT", "message": {"actor": "ASSISTANT", "content": {}}},
    }


class TestNotificationPolling:
    CONNECTED_AT = 1_800_000_000.0  # 2027-01-15T08:00:00Z

    def _connect(self, monkeypatch, client: _EventsClient) -> list:
        dispatched: list = []

        def dispatch(user_id, event, client=None):
            dispatched.append(event.event_id)

        monkeypatch.setattr(server, "_dispatch_polled_event", dispatch)
        server.session.client = cast(Any, client)
        server.session.connected_at = self.CONNECTED_AT
        server.session.polling_loop = server.PollingLoop(
            client=cast(Any, client), checkpoint=server.session.checkpoint, handler=dispatch, first_run="replay_history",
        )
        return dispatched

    def _run_ticks(self, monkeypatch, client: _EventsClient, ticks: int) -> list:
        """Run the polling task for ``ticks`` polls; returns the read count seen at each sleep."""
        reads_at_sleep: list = []

        async def fake_sleep(seconds):
            reads_at_sleep.append(len(client.reads))
            if len(reads_at_sleep) > ticks:
                raise asyncio.CancelledError

        monkeypatch.setattr(server.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(server.asyncio, "to_thread", lambda fn, *a: _ready(fn(*a)))
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(server._polling_task())
        return reads_at_sleep

    def test_first_read_waits_one_interval(self, monkeypatch):
        client = _EventsClient([[]])
        self._connect(monkeypatch, client)

        assert self._run_ticks(monkeypatch, client, ticks=1) == [0, 1]

    def test_first_read_dispatches_only_events_since_connect_and_sets_the_checkpoint(self, monkeypatch):
        history = _event_json("evt_a", "2027-01-15T07:00:00Z")
        during_wait = _event_json("evt_b", "2027-01-15T08:00:03Z")
        later = _event_json("evt_c", "2027-01-15T08:00:09Z")
        client = _EventsClient([[during_wait, history], [later]])
        dispatched = self._connect(monkeypatch, client)

        self._run_ticks(monkeypatch, client, ticks=2)

        assert dispatched == ["evt_b", "evt_c"]
        assert client.reads[1] == {"starting_after": "evt_b", "limit": 20}

    def test_an_empty_first_read_delivers_every_later_event(self, monkeypatch):
        client = _EventsClient([[], [_event_json("evt_1", "2027-01-15T08:00:30Z")]])
        dispatched = self._connect(monkeypatch, client)

        self._run_ticks(monkeypatch, client, ticks=2)

        assert dispatched == ["evt_1"]
        assert server.session.checkpoint.load(server.POLL_USER) == "evt_1"


async def _ready(value):
    return value


class TestDispatch:
    def test_events_from_a_replaced_connection_are_dropped(self):
        current, previous = object(), object()
        server.session.client = cast(Any, current)
        queue: asyncio.Queue = asyncio.Queue()
        server.session.event_subscribers.append(queue)

        server._dispatch_polled_event("app-user", cast(Any, "old"), cast(Any, previous))
        server._dispatch_polled_event("app-user", cast(Any, "new"), cast(Any, current))

        assert queue.get_nowait() == "new"
        assert queue.empty()

    def test_an_event_from_the_polling_thread_wakes_the_stream_at_once(self):
        import threading

        async def scenario():
            server.session.loop = asyncio.get_running_loop()
            queue: asyncio.Queue = asyncio.Queue()
            server.session.event_subscribers.append(queue)
            threading.Thread(target=server._dispatch_polled_event, args=("app-user", "evt")).start()
            return await asyncio.wait_for(queue.get(), timeout=1.0)

        assert asyncio.run(scenario()) == "evt"


class TestNotifications:
    def test_events_without_a_message_are_left_out_of_the_list(self):
        def event(event_id: str, payload: dict) -> Event:
            return Event.from_json({
                "event_id": event_id, "conversation_id": "conv_1",
                "created_at": "2026-10-30T12:00:00Z", "event_payload": payload,
            })

        class OpenRequest:
            async def is_disconnected(self) -> bool:
                return False

        async def scenario() -> list:
            stream = cast(AsyncGenerator, server._notifications_sse(cast(Any, OpenRequest())))
            frames = [await anext(stream)]
            queue = server.session.event_subscribers[-1]
            queue.put_nowait(event("evt_1", {"type": "SOME_FUTURE_EVENT"}))
            queue.put_nowait(event("evt_2", {"type": "MESSAGE_EVENT", "message": {
                "actor": "ASSISTANT", "message_id": "msg_1",
                "content": {"type": "PLAIN_TEXT", "plain_text": {"text": "Your laptop has shipped."}},
            }}))
            frames.append(await anext(stream))
            await stream.aclose()
            return frames

        frames = _frames(asyncio.run(scenario()))

        assert [name for name, _ in frames] == ["hello", "notification"]
        assert frames[1][1]["event_id"] == "evt_2"

    def test_a_live_agent_session_end_reaches_the_browser_without_a_message(self):
        class OpenRequest:
            async def is_disconnected(self) -> bool:
                return False

        async def scenario() -> list:
            stream = cast(AsyncGenerator, server._notifications_sse(cast(Any, OpenRequest())))
            frames = [await anext(stream)]
            server.session.event_subscribers[-1].put_nowait(Event.from_json({
                "event_id": "evt_3", "conversation_id": "conv_1", "created_at": "2026-10-29T23:06:44Z",
                "category": "LIVE_AGENT", "event_payload": {"live_agent_details": {"session_state": "ENDED"}},
            }))
            frames.append(await anext(stream))
            await stream.aclose()
            return frames

        _, (name, data) = _frames(asyncio.run(scenario()))

        assert name == "notification"
        assert data["message"] is None
        assert data["category"] == "LIVE_AGENT"
        assert data["live_agent_session_state"] == "ENDED"

    def test_an_unrecognized_session_state_is_dropped_and_a_message_keeps_its_details(self):
        class OpenRequest:
            async def is_disconnected(self) -> bool:
                return False

        def event(event_id: str, payload: dict) -> Event:
            return Event.from_json({
                "event_id": event_id, "conversation_id": "conv_1", "created_at": "2026-10-29T23:08:03Z",
                "category": "LIVE_AGENT", "event_payload": payload,
            })

        async def scenario() -> list:
            stream = cast(AsyncGenerator, server._notifications_sse(cast(Any, OpenRequest())))
            frames = [await anext(stream)]
            queue = server.session.event_subscribers[-1]
            queue.put_nowait(event("evt_4", {"live_agent_details": {"session_state": "SOME_FUTURE_STATE"}}))
            queue.put_nowait(event("evt_5", {
                "type": "MESSAGE_EVENT",
                "message": {
                    "actor": "LIVE_AGENT", "message_id": "msg_5",
                    "content": {"type": "PLAIN_TEXT", "plain_text": {"text": "The agent has ended the chat."}},
                },
                "live_agent_details": {"session_state": "ENDED"},
            }))
            frames.append(await anext(stream))
            await stream.aclose()
            return frames

        _, (name, data) = _frames(asyncio.run(scenario()))

        assert name == "notification"
        assert data["event_id"] == "evt_5"
        assert data["message"]["message_id"] == "msg_5"
        assert data["live_agent_session_state"] == "ENDED"

    def test_polled_events_reach_every_subscriber(self):
        queues = [asyncio.Queue(), asyncio.Queue()]
        server.session.event_subscribers.extend(queues)
        event = object()

        server._dispatch_polled_event("app-user", event)  # type: ignore[arg-type]

        assert [q.get_nowait() for q in queues] == [event, event]

    def test_a_stream_closed_before_any_response_id_is_reported(self, monkeypatch):
        async def closed_stream(client, cid, text, callback_id, progress):
            raise StreamClosedError("stream closed before a terminal event")
            yield b""

        monkeypatch.setattr(turns, "stream_turn", closed_stream)

        async def collect():
            return [f async for f in turns.run_turn(_fake_client(), "conv_1", "hi", None, streaming=True)]

        frames = _frames(asyncio.run(collect()))

        assert frames == [("error", {"message": "stream closed before a terminal event"})]


def test_a_port_in_use_exits_with_a_message_naming_the_port_and_flag():
    with socket.socket() as taken:
        taken.bind((server.HOST, 0))
        taken.listen()
        port = taken.getsockname()[1]

        with pytest.raises(SystemExit) as exc:
            server.main(["--port", str(port)])

    assert str(exc.value) == f"Port {port} is already in use. Stop the other process, or pass --port to choose another."
