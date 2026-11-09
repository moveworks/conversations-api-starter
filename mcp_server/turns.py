"""One Moveworks turn: send text or press a button, collect the reply.

Synchronous (the starter library is); the service runs each turn in a worker
thread. The MCP tools say exactly what the user chose, so there is no text parsing
of replies such as "yes".
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from moveworks_capi.client import ConversationsApiClient
from moveworks_capi.conversations import create_conversation, list_messages, replies_after_input
from moveworks_capi.errors import (
    AuthenticationError,
    ConversationsApiError,
    RateLimitedError,
    RequestRejectedError,
    StreamClosedError,
)
from moveworks_capi.responses import wait_for_completion
from moveworks_capi.streaming import OutputDelta, ResponseCompleted, ResponseCreated, stream_response

from . import forms, present
from .models import Option

ProgressCallback = Callable[[str], None]

LIVE_AGENT_NOTE = (
    "This MCP server doesn't connect to live agent chat. Use another Moveworks channel, such as chat or the portal, "
    "to reach an agent."
)
MAX_FORM_LOOKUPS = 3
"""Forms named per turn. Each name costs a GetModal call, and GetModal is rate limited."""
HANDOFF_NOTE = "More help options are available in your other Moveworks channels, such as chat or the portal."


class NotAccepted(Exception):
    """Moveworks rejected the request before acting on it, so sending it again is safe."""

    def __init__(self, cause: ConversationsApiError) -> None:
        super().__init__(str(cause))
        self.cause = cause


REJECTED_BEFORE_ACCEPTANCE = (RateLimitedError, AuthenticationError, RequestRejectedError)
"""Errors that prove Moveworks didn't take the request. A 5xx or network error proves nothing."""


@dataclass
class TurnOutcome:
    text: str
    sources: List[Dict[str, Optional[str]]] = field(default_factory=list)
    options: List[Option] = field(default_factory=list)
    """Buttons the user can press through ``choose_moveworks_option``."""
    notes: List[str] = field(default_factory=list)
    """Everything else Moveworks offered, as plain text or links."""
    failed: bool = False


class MoveworksTurns:
    def __init__(
        self,
        client: ConversationsApiClient,
        *,
        turn_timeout_seconds: float = 120.0,
        modal_outcome_wait_seconds: float = 45.0,
    ) -> None:
        self._client = client
        self._turn_timeout = turn_timeout_seconds
        self._modal_wait = modal_outcome_wait_seconds

    def create_conversation(self, title: str) -> str:
        return create_conversation(self._client, title=title[:80] or "MCP")["conversation_id"]

    def chat(self, conversation_id: str, text: str, progress: ProgressCallback) -> TurnOutcome:
        return self._turn(conversation_id, progress, text=text)

    def press(self, conversation_id: str, option: Option, progress: ProgressCallback) -> TurnOutcome:
        return self._turn(conversation_id, progress, callback_id=option.callback_id)

    def _turn(
        self, conversation_id: str, progress: ProgressCallback, *, text: Optional[str] = None, callback_id: Optional[str] = None
    ) -> TurnOutcome:
        events = iter(stream_response(
            self._client, conversation_id, text, callback_id=callback_id, timeout_seconds=self._turn_timeout
        ))
        try:
            first = next(events, None)
        except REJECTED_BEFORE_ACCEPTANCE as exc:
            raise NotAccepted(exc) from exc
        try:
            return self._collect(conversation_id, progress, itertools.chain([first] if first else [], events))
        except AuthenticationError:
            raise
        except (ConversationsApiError, TimeoutError, OSError):
            # Moveworks already has the request: report it, never send it again.
            return TurnOutcome("Moveworks received the request, but its reply couldn't be read. "
                               "Ask Moveworks for the status before trying again.", failed=True)

    def _collect(self, conversation_id: str, progress: ProgressCallback, events: Any) -> TurnOutcome:
        streamed: Dict[str, Dict[str, Any]] = {}
        response_id: Optional[str] = None
        try:
            for event in events:
                if isinstance(event, ResponseCreated):
                    response_id = event.response_id
                elif isinstance(event, OutputDelta):
                    if event.output_type == "REASONING_MESSAGE" and event.text:
                        progress(present.progress_line(event.text))
                    elif event.output_type == "MESSAGE":
                        message = event.output.get("message") or {}
                        streamed[message.get("message_id") or str(len(streamed))] = message
                elif isinstance(event, ResponseCompleted) and event.status == "FAILED":
                    return TurnOutcome("Moveworks couldn't complete that request. Please try again.", failed=True)
        except StreamClosedError as exc:
            response_id = exc.response_id or response_id
            if not response_id:
                raise
            completed = wait_for_completion(self._client, conversation_id, response_id, timeout_seconds=self._turn_timeout)
            streamed = {str(i): o.get("message") or {} for i, o in enumerate(completed.outputs) if o.get("type") == "MESSAGE"}
        messages = list(streamed.values())
        if not messages and response_id:
            # Some replies are stored under an earlier response (e.g. after a callback), not streamed.
            history = list_messages(self._client, conversation_id, limit=10).get("messages") or []
            messages = replies_after_input(history, response_id)
        if messages and present.is_interim(messages[-1]):
            progress(present.progress_line(present.render_messages(messages[-1:])))
            messages += self._await_follow_ups(
                conversation_id,
                known={m.get("message_id") for m in messages},
                after=max(m.get("created_at") or "" for m in messages),
            )
        outcome = outcome_from_messages(messages)
        self._name_forms(conversation_id, outcome)
        return outcome

    def modal_path(self, conversation_id: str, option: Option) -> str:
        return (f"/rest/v1/conversations/{conversation_id}/responses/{option.response_id}"
                f"/messages/{option.message_id}/modals/{option.modal_id}")

    def fetch_form(self, conversation_id: str, option: Option) -> Dict[str, Any]:
        return self._client.get(self.modal_path(conversation_id, option)).json()

    def submit_form(self, conversation_id: str, form: "forms.PendingForm", progress: ProgressCallback) -> TurnOutcome:
        try:
            history = list_messages(self._client, conversation_id, limit=5).get("messages") or []
        except ConversationsApiError as exc:
            raise NotAccepted(exc) from exc  # nothing has been sent yet
        known = {m.get("message_id") for m in history}
        after = max((m.get("created_at") or "" for m in history), default="")
        try:
            self._client.post(f"{form.path}/submit", json={"values": form.values})
        except REJECTED_BEFORE_ACCEPTANCE as exc:
            raise NotAccepted(exc) from exc
        except (ConversationsApiError, OSError):
            return TurnOutcome(f"Moveworks may have received **{form.name}**, but the submit didn't confirm. "
                               "Ask Moveworks for the status before submitting again.", failed=True)
        progress("Submitted. Waiting for Moveworks to confirm...")
        try:
            follow_ups = self._await_follow_ups(conversation_id, known=known, after=after)
        except ConversationsApiError:
            follow_ups = []
        if not follow_ups:
            return TurnOutcome(f"Moveworks accepted **{form.name}**, but hasn't confirmed the outcome yet "
                               "(for example a ticket number). Ask Moveworks for the status in a minute.")
        outcome = outcome_from_messages(follow_ups)
        outcome.text = f"Submitted **{form.name}**.\n\n{outcome.text}"
        self._name_forms(conversation_id, outcome)
        return outcome

    def _name_forms(self, conversation_id: str, outcome: TurnOutcome) -> None:
        """Label each form button with its form's name; catalog forms all say "Complete this request"."""
        for option in [o for o in outcome.options if o.is_form][:MAX_FORM_LOOKUPS]:
            try:
                option.modal = self.fetch_form(conversation_id, option)
            except ConversationsApiError:
                break  # naming is cosmetic; keep the answer
            name = forms.form_name(option.modal, option.label)
            if name and name.lower() != option.label.lower():
                option.label = f"{option.label}: {name}"

    def _await_follow_ups(self, conversation_id: str, *, known: set, after: str) -> List[Dict[str, Any]]:
        """Collect assistant messages that land after the turn ended.

        Filing a ticket replies "Filing your ticket..." and the ticket number
        follows seconds later under the same response. Poll until the newest
        new message is not an interim one, or the wait runs out. Only messages
        newer than ``after`` count: history also holds earlier turns' replies.
        """
        deadline = time.monotonic() + self._modal_wait
        collected: Dict[str, Dict[str, Any]] = {}
        while time.monotonic() < deadline:
            time.sleep(4)
            for message in list_messages(self._client, conversation_id, limit=5).get("messages") or []:
                message_id = message.get("message_id")
                newer = (message.get("created_at") or "") > after
                if message.get("actor") != "USER" and message_id not in known and newer:
                    collected[message_id] = message
            ordered = sorted(collected.values(), key=lambda m: m.get("created_at") or "")
            if ordered and not present.is_interim(ordered[-1]):
                break
        return sorted(collected.values(), key=lambda m: m.get("created_at") or "")


def outcome_from_messages(messages: List[Dict[str, Any]]) -> TurnOutcome:
    options: List[Option] = []
    notes: List[str] = []
    for option in present.message_options(messages):
        if present.is_live_agent(option):
            if LIVE_AGENT_NOTE not in notes:
                notes.append(LIVE_AGENT_NOTE)
        elif option.kind in ("callback", "modal"):
            options.append(option)
        elif option.kind == "url":
            notes.append(f"[{option.label}]({option.url})" if option.url else option.label)
        elif HANDOFF_NOTE not in notes:
            notes.append(HANDOFF_NOTE)
    text = present.render_messages(messages) or "Moveworks returned no message for that request."
    return TurnOutcome(text=text, sources=present.sources(messages), options=options, notes=notes)
