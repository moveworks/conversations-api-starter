"""Checkpoint invariants: advance forward, never regress, hold on failure."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from moveworks_capi.notifications.polling import (
    Event,
    EventsPage,
    EventsPollingClient,
    FileCheckpoint,
    InMemoryCheckpoint,
    PollingLoop,
    event_sort_key,
    get_event,
)


def _event(event_id: str, text: str = "hello", created_at: str = "2026-01-01T00:00:00Z") -> Event:
    return Event(
        event_id=event_id,
        conversation_id="conv_1",
        created_at=created_at,
        payload_type="MESSAGE_EVENT",
        message_actor="ASSISTANT",
        content_type="MARKDOWN_TEXT",
        content_text=text,
        raw={},
    )


class TestInMemoryCheckpoint:
    def test_advance_from_empty(self) -> None:
        cp = InMemoryCheckpoint()
        assert cp.load("u1") is None
        cp.advance("u1", "evt_a")
        assert cp.load("u1") == "evt_a"

    def test_advance_forward(self) -> None:
        cp = InMemoryCheckpoint()
        cp.advance("u1", "evt_a")
        cp.advance("u1", "evt_b")
        assert cp.load("u1") == "evt_b"

    def test_never_regresses(self) -> None:
        cp = InMemoryCheckpoint()
        cp.advance("u1", "evt_b")
        cp.advance("u1", "evt_a")  # older ID, should be ignored
        assert cp.load("u1") == "evt_b"

    def test_isolates_users(self) -> None:
        cp = InMemoryCheckpoint()
        cp.advance("u1", "evt_a")
        cp.advance("u2", "evt_z")
        assert cp.load("u1") == "evt_a"
        assert cp.load("u2") == "evt_z"


class TestFileCheckpoint:
    def test_roundtrip(self, tmp_path: Path) -> None:
        cp = FileCheckpoint(tmp_path / "cp.json")
        cp.advance("u1", "evt_a")
        cp2 = FileCheckpoint(tmp_path / "cp.json")
        assert cp2.load("u1") == "evt_a"

    def test_never_regresses_across_instances(self, tmp_path: Path) -> None:
        path = tmp_path / "cp.json"
        cp = FileCheckpoint(path)
        cp.advance("u1", "evt_b")
        # Simulate a stale caller trying to move backward.
        cp2 = FileCheckpoint(path)
        cp2.advance("u1", "evt_a")
        assert FileCheckpoint(path).load("u1") == "evt_b"

    def test_persists_json(self, tmp_path: Path) -> None:
        path = tmp_path / "cp.json"
        cp = FileCheckpoint(path)
        cp.advance("u1", "evt_a")
        assert json.loads(path.read_text()) == {"u1": "evt_a"}


def _page(*events: Event) -> EventsPage:
    return EventsPage(events=list(events), next_cursor=None, trace_id=None)


class TestPollingLoopSemantics:
    def _loop(self, events_page: EventsPage, handler: Mock, checkpoint: InMemoryCheckpoint, **kwargs) -> PollingLoop:
        client = Mock()
        client.get.return_value = _JsonResponse({"events": []})
        loop = PollingLoop(client=client, checkpoint=checkpoint, handler=handler, **kwargs)
        loop._events.fetch_page = Mock(return_value=events_page)  # type: ignore[method-assign]
        return loop

    def test_advances_after_successful_processing(self) -> None:
        cp = InMemoryCheckpoint()
        cp.advance("u1", "evt_0")
        handler = Mock()
        page = EventsPage(events=[_event("evt_a"), _event("evt_b")], next_cursor=None, trace_id=None)
        loop = self._loop(page, handler, cp)

        count = loop.poll_once("u1")

        assert count == 2
        assert handler.call_count == 2
        assert cp.load("u1") == "evt_b"

    def test_holds_checkpoint_when_handler_raises(self) -> None:
        cp = InMemoryCheckpoint()
        cp.advance("u1", "evt_000")
        handler = Mock(side_effect=[None, RuntimeError("downstream boom")])
        page = EventsPage(
            events=[_event("evt_aaa"), _event("evt_bbb"), _event("evt_ccc")],
            next_cursor=None,
            trace_id=None,
        )
        loop = self._loop(page, handler, cp)

        with pytest.raises(RuntimeError):
            loop.poll_once("u1")

        # First event processed, then handler raised on second: checkpoint
        # should sit at the last successful, never at the failing one.
        assert cp.load("u1") == "evt_aaa"


class TestFirstRunPolicy:
    # Out of order on purpose: the loop sorts by creation time before dispatch.
    NEWEST_FIRST = _page(
        _event("evt_c", created_at="2026-01-03T00:00:00Z"),
        _event("evt_b", created_at="2026-01-02T00:00:00Z"),
        _event("evt_a", created_at="2026-01-01T00:00:00Z"),
    )

    def _loop(self, page: EventsPage, handler: Mock, cp: InMemoryCheckpoint, **kwargs) -> PollingLoop:
        return TestPollingLoopSemantics()._loop(page, handler, cp, **kwargs)

    def test_default_sets_the_checkpoint_to_the_newest_and_dispatches_nothing(self) -> None:
        cp, handler = InMemoryCheckpoint(), Mock()
        loop = self._loop(self.NEWEST_FIRST, handler, cp)

        assert loop.poll_once("u1") == 0
        handler.assert_not_called()
        assert cp.load("u1") == "evt_c"
        assert "starting_after" not in loop._events.fetch_page.call_args.kwargs  # type: ignore[attr-defined]

    def test_after_the_first_run_new_events_are_dispatched(self) -> None:
        cp, handler = InMemoryCheckpoint(), Mock()
        loop = self._loop(self.NEWEST_FIRST, handler, cp)
        loop.poll_once("u1")

        loop._events.fetch_page = Mock(return_value=_page(_event("evt_d", created_at="2026-01-04T00:00:00Z")))  # type: ignore[method-assign]
        assert loop.poll_once("u1") == 1
        assert loop._events.fetch_page.call_args.kwargs["starting_after"] == "evt_c"  # type: ignore[attr-defined]
        assert cp.load("u1") == "evt_d"

    def test_a_user_with_no_history_gets_every_later_event(self) -> None:
        cp, handler = InMemoryCheckpoint(), Mock()
        loop = self._loop(_page(), handler, cp)
        assert loop.poll_once("u1") == 0
        assert cp.load("u1") is None

        loop._events.fetch_page = Mock(return_value=_page(_event("evt_a")))  # type: ignore[method-assign]
        assert loop.poll_once("u1") == 1
        handler.assert_called_once()

    def test_replay_dispatches_history_oldest_first(self) -> None:
        cp, handler = InMemoryCheckpoint(), Mock()
        loop = self._loop(self.NEWEST_FIRST, handler, cp, first_run="replay_history")

        assert loop.poll_once("u1") == 3
        assert [call.args[1].event_id for call in handler.call_args_list] == ["evt_a", "evt_b", "evt_c"]
        assert cp.load("u1") == "evt_c"

    def test_a_descending_page_is_sorted_before_dispatch(self) -> None:
        cp, handler = InMemoryCheckpoint(), Mock()
        cp.advance("u1", "evt_0")
        loop = self._loop(self.NEWEST_FIRST, handler, cp)

        loop.poll_once("u1")

        assert [call.args[1].event_id for call in handler.call_args_list] == ["evt_a", "evt_b", "evt_c"]

    def test_sort_uses_time_not_string_order(self) -> None:
        whole = _event("evt_x", created_at="2026-01-01T00:00:01Z")
        fractional = _event("evt_y", created_at="2026-01-01T00:00:00.500Z")
        assert sorted([whole, fractional], key=event_sort_key) == [fractional, whole]

    def test_rejects_an_unknown_policy(self) -> None:
        with pytest.raises(ValueError):
            PollingLoop(client=Mock(), checkpoint=InMemoryCheckpoint(), handler=Mock(), first_run="everything")  # type: ignore[arg-type]


class _JsonResponse:
    def __init__(self, body: dict) -> None:
        self._body = body
        self.headers = {"x-trace-id": "t1"}

    def json(self) -> dict:
        return self._body


_EVENT_JSON = {
    "event_id": "evt_1",
    "conversation_id": "conv_1",
    "created_at": "2026-01-01T00:00:00Z",
    "event_payload": {
        "type": "MESSAGE_EVENT",
        "message": {"actor": "ASSISTANT", "content": {"type": "MARKDOWN_TEXT", "markdown_text": {"text": "hi"}}},
    },
}


class TestEventReads:
    def test_reads_every_conversation_by_default(self) -> None:
        client = Mock()
        client.get.return_value = _JsonResponse({"events": []})
        EventsPollingClient(client).fetch_page()
        assert client.get.call_args[0][0] == "/rest/v1/conversations/-/events"

    def test_reads_one_conversation(self) -> None:
        client = Mock()
        client.get.return_value = _JsonResponse({"events": [_EVENT_JSON]})
        page = EventsPollingClient(client, conversation_id="conv_1").fetch_page(starting_after="evt_0")
        assert client.get.call_args[0][0] == "/rest/v1/conversations/conv_1/events"
        assert client.get.call_args.kwargs["params"] == {"starting_after": "evt_0"}
        assert page.events[0].content_text == "hi"

    def test_get_event(self) -> None:
        client = Mock()
        client.get.return_value = _JsonResponse(_EVENT_JSON)
        event = get_event(client, "conv_1", "evt_1")
        client.get.assert_called_once_with("/rest/v1/conversations/conv_1/events/evt_1")
        assert (event.event_id, event.message_actor) == ("evt_1", "ASSISTANT")


class TestEventCategories:
    @staticmethod
    def _parse(payload: dict, category: str | None = None) -> Event:
        obj = {"event_id": "evt_1", "conversation_id": "conv_1", "created_at": "2026-10-29T23:06:44Z", "event_payload": payload}
        if category:
            obj["category"] = category
        return Event.from_json(obj)

    def test_a_live_agent_session_end_carries_only_its_state(self):
        event = self._parse({"live_agent_details": {"session_state": "ENDED"}}, "LIVE_AGENT")

        assert event.category == "LIVE_AGENT"
        assert event.live_agent_session_state == "ENDED"
        assert not event.has_message
        assert event.payload_type == ""
        assert event.content_text is None

    def test_a_consent_event_reports_its_operation(self):
        event = self._parse(
            {
                "type": "MESSAGE_EVENT",
                "message": {"actor": "ASSISTANT", "content": {"type": "MARKDOWN_TEXT", "markdown_text": {"text": "Done."}}},
                "user_consent_auth_details": {"operation_type": "REVOKE"},
            },
            "USER_CONSENT_AUTH",
        )

        assert event.category == "USER_CONSENT_AUTH"
        assert event.consent_operation_type == "REVOKE"
        assert event.has_message
        assert event.content_text == "Done."

    def test_an_unclassified_event_has_no_category_or_details(self):
        event = self._parse({"type": "MESSAGE_EVENT", "message": {"actor": "ASSISTANT"}})

        assert event.category is None
        assert event.live_agent_session_state is None
        assert event.consent_operation_type is None
        assert event.has_message
