"""Callers, per-call sessions, and the Conversations API turn behind each utterance.

- :class:`CallerDirectory` maps the caller's phone number to the employee the
  call acts as. A phone number is not proof of identity; see the README's
  "Caller identity" section before you rely on it.
- :class:`ClientPool` keeps one ``ConversationsApiClient`` per employee, so
  bearer caching works across calls.
- :class:`CallSessions` keeps one :class:`CallSession` per Twilio ``CallSid``:
  the Conversations API ``conversation_id`` (so every utterance in a call is a
  turn in the same conversation), the turn in flight, and the rest of a long
  answer the caller has not heard yet.
- :class:`TurnRunner` sends one turn and returns a :class:`Reply` with
  speakable text. It never raises: every failure becomes a sentence the caller
  hears.

How a turn runs: it streams (``POST .../responses/stream``), because one
stream costs a single request where polling a 20-second answer every two
seconds costs ten reads against the rate limit. The stream stops at 120
seconds even when the response is still running; the response is not failed,
so the runner polls it (``wait_for_completion``) instead of reporting an
error. A stream that drops early is handled the same way. If opening the
stream is rate limited (429), the server did not accept the turn, so the runner
submits it with ``create_response``, which the shared client backs off and
resends after a 429, then polls. A turn is never resent after a timeout or
5xx.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from moveworks_capi.client import ConversationsApiClient
from moveworks_capi.conversations import create_conversation, list_messages, replies_after_input
from moveworks_capi.errors import AuthenticationError, RateLimitedError, StreamClosedError, StreamError
from moveworks_capi.rendering import clean_label
from moveworks_capi.responses import MAX_TEXT_LENGTH, Response, create_response, message_actions, wait_for_completion
from moveworks_capi.streaming import ResponseCompleted, ResponseCreated, stream_response

from .speech import reply_text

log = logging.getLogger(__name__)

BUSY = "The assistant is busy right now. Please try again in a minute."
SLOW = "That is taking longer than I can wait on a call. Please try again in a few minutes."
TROUBLE = "Sorry, I couldn't reach the assistant just now."
FAILED = "Sorry, I couldn't finish that request."
NOT_SIGNED_IN = "Sorry, I couldn't sign you in to the assistant."
EMPTY = "Done."

STREAM_TIMEOUT_SECONDS = 120.0
"""How long the server keeps a response stream open; the response keeps running after."""

DEFAULT_TURN_TIMEOUT_SECONDS = 150.0
"""Long answers approach two minutes end to end, as in the reference app."""

_PHONE_PUNCTUATION = re.compile(r"[\s().-]")


@dataclass(frozen=True)
class Reply:
    """What the caller hears for one turn.

    ``options`` maps each spoken button label to its ``callback_id``, so the
    caller can press a button by saying its label. ``has_link_or_form`` is
    set when the reply carries a link or form, which cannot be used on a call.
    """

    text: str
    options: Dict[str, str] = field(default_factory=dict)
    has_link_or_form: bool = False
    failed: bool = False


class CallerDirectory:
    """Maps a caller's phone number (E.164, such as ``+15555550100``) to an employee.

    ``default_user`` answers for every number not in the map. Use it only
    with an API key, where every call runs as the key's user anyway.
    """

    def __init__(self, numbers: Mapping[str, str], *, default_user: Optional[str] = None) -> None:
        self._numbers = {_normalize_number(number): user for number, user in numbers.items()}
        self._default_user = default_user

    def __len__(self) -> int:
        return len(self._numbers)

    def lookup(self, number: str) -> Optional[str]:
        return self._numbers.get(_normalize_number(number), self._default_user)

    @staticmethod
    def parse(spec: str) -> Dict[str, str]:
        """Parse ``"+15555550100=jane@example.com, +15555550101=raj@example.com"``."""
        numbers: Dict[str, str] = {}
        for entry in spec.split(","):
            if not entry.strip():
                continue
            number, sep, user = entry.partition("=")
            if not sep or not number.strip() or not user.strip():
                raise ValueError(f"expected +number=user, got {entry.strip()!r}")
            numbers[number.strip()] = user.strip()
        return numbers

    @staticmethod
    def load(path: Path) -> Dict[str, str]:
        """Read a JSON object of ``{"+15555550100": "jane@example.com"}``."""
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
            raise ValueError(f"{path} must be a JSON object of phone number to user")
        return data


class ClientPool:
    """One ``ConversationsApiClient`` per employee, created on first use."""

    def __init__(self, make_client: Callable[[str], ConversationsApiClient]) -> None:
        self._make_client = make_client
        self._clients: Dict[str, ConversationsApiClient] = {}
        self._lock = threading.Lock()

    def get(self, user: str) -> ConversationsApiClient:
        with self._lock:
            client = self._clients.get(user)
            if client is None:
                client = self._clients[user] = self._make_client(user)
            return client

    def close(self) -> None:
        with self._lock:
            for client in self._clients.values():
                client.close()
            self._clients.clear()


@dataclass
class CallSession:
    """State for one phone call, keyed by Twilio's ``CallSid``."""

    call_sid: str
    user: str
    client: ConversationsApiClient
    conversation_id: Optional[str] = None
    turn: Optional["Future[Reply]"] = None
    turns_sent: int = 0
    wait_cycles: int = 0
    pending: List[str] = field(default_factory=list)
    options: Dict[str, str] = field(default_factory=dict)
    link_note: bool = False
    last_spoken: str = ""
    silent_prompts: int = 0
    offered_more_help: bool = False
    updated_at: float = field(default_factory=time.monotonic)


class CallSessions:
    """In-memory call sessions. Sessions idle for ``ttl_seconds`` are dropped.

    A single process only. To run more than one instance behind a load
    balancer, keep this state in a shared store such as Redis, keyed by
    ``CallSid``.
    """

    def __init__(self, ttl_seconds: float = 2 * 3600) -> None:
        self._ttl = ttl_seconds
        self._sessions: Dict[str, CallSession] = {}
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._sessions)

    def start(self, call_sid: str, user: str, client: ConversationsApiClient) -> CallSession:
        session = CallSession(call_sid=call_sid, user=user, client=client)
        with self._lock:
            self._prune()
            self._sessions[call_sid] = session
        return session

    def get(self, call_sid: str) -> Optional[CallSession]:
        with self._lock:
            self._prune()
            session = self._sessions.get(call_sid)
            if session is not None:
                session.updated_at = time.monotonic()
            return session

    def end(self, call_sid: str) -> None:
        with self._lock:
            self._sessions.pop(call_sid, None)

    def _prune(self) -> None:
        cutoff = time.monotonic() - self._ttl
        for call_sid in [sid for sid, s in self._sessions.items() if s.updated_at < cutoff]:
            del self._sessions[call_sid]


class TurnRunner:
    """Send one Conversations API turn for a call and return what to say.

    Args:
        stream: Stream the turn (the default) or submit and poll.
        timeout_seconds: The longest a caller waits for one answer.
        context_hint: Optional text sent ahead of the caller's first
            utterance in a call, such as "I'm on a phone call, so keep
            answers short." Empty sends the caller's words as they are.
        conversation_title: Title for the conversation each call creates.
    """

    def __init__(
        self,
        *,
        stream: bool = True,
        timeout_seconds: float = DEFAULT_TURN_TIMEOUT_SECONDS,
        context_hint: str = "",
        conversation_title: str = "Phone call",
    ) -> None:
        self.stream = stream
        self.timeout_seconds = timeout_seconds
        self.context_hint = context_hint.strip()
        self.conversation_title = conversation_title

    def run(self, session: CallSession, text: Optional[str] = None, *, callback_id: Optional[str] = None) -> Reply:
        """Send the caller's ``text``, or press the button ``callback_id``."""
        try:
            client = session.client
            conversation_id = session.conversation_id
            if conversation_id is None:
                conversation_id = create_conversation(client, title=self.conversation_title)["conversation_id"]
                session.conversation_id = conversation_id
            if text is not None:
                if self.context_hint and session.turns_sent == 0:
                    text = f"{self.context_hint}\n\n{text}"
                text = text[:MAX_TEXT_LENGTH]
            session.turns_sent += 1
            response = self._complete(client, conversation_id, text, callback_id)
            return self._reply(client, conversation_id, response)
        except RateLimitedError as exc:
            log.warning("call %s: rate limited after retries (trace %s)", session.call_sid, exc.trace_id)
            return Reply(BUSY, failed=True)
        except TimeoutError as exc:
            # The response is still running; its answer lands in the conversation's history.
            log.warning("call %s: %s", session.call_sid, exc)
            return Reply(SLOW, failed=True)
        except AuthenticationError as exc:
            log.error("call %s: authentication failed for the call's user: %s", session.call_sid, exc)
            return Reply(NOT_SIGNED_IN, failed=True)
        except Exception:
            log.exception("call %s: turn failed", session.call_sid)
            return Reply(TROUBLE, failed=True)

    def _complete(
        self, client: ConversationsApiClient, conversation_id: str, text: Optional[str], callback_id: Optional[str]
    ) -> Response:
        deadline = time.monotonic() + self.timeout_seconds
        response_id = ""
        finished = False
        if self.stream:
            try:
                response_id, finished = self._stream(client, conversation_id, text, callback_id)
            except RateLimitedError:
                # A 429 means the server rejected the stream before accepting the turn.
                log.info("opening the stream was rate limited; submitting the turn without streaming")
        if not response_id:
            response_id = create_response(client, conversation_id, text, callback_id=callback_id)["response_id"]
        return wait_for_completion(
            client,
            conversation_id,
            response_id,
            timeout_seconds=max(deadline - time.monotonic(), 5.0),
            initial_delay_seconds=0.0 if finished else 1.0,
        )

    def _stream(
        self, client: ConversationsApiClient, conversation_id: str, text: Optional[str], callback_id: Optional[str]
    ) -> Tuple[str, bool]:
        """Stream the turn. Returns its ``response_id`` and whether it finished."""
        response_id = ""
        try:
            for event in stream_response(
                client, conversation_id, text, callback_id=callback_id, timeout_seconds=STREAM_TIMEOUT_SECONDS
            ):
                if isinstance(event, ResponseCreated):
                    response_id = event.response_id
                elif isinstance(event, ResponseCompleted):
                    return event.response_id or response_id, True
        except StreamError as exc:
            if not response_id:
                raise
            # At 120 s the stream ends with an error event while the response keeps running.
            log.info("stream ended before the answer (%s); polling response %s", exc.code or exc, response_id)
        except StreamClosedError as exc:
            response_id = exc.response_id or response_id
            if not response_id:
                raise
            log.info("stream closed before the answer; polling response %s", response_id)
        return response_id, False

    def _reply(self, client: ConversationsApiClient, conversation_id: str, response: Response) -> Reply:
        messages: List[Dict[str, Any]] = [
            item["message"] for item in response.outputs if item.get("type") == "MESSAGE" and item.get("message")
        ]
        if not messages and response.status == "COMPLETED":
            messages = _late_replies(client, conversation_id, response.response_id)
        text = reply_text(messages)
        if response.status == "FAILED":
            return Reply(text or FAILED, failed=True)
        actions = [action for message in messages for action in message_actions(message)]
        options = {
            clean_label(action.label): action.callback_id
            for action in actions
            if action.type == "CALLBACK_ACTION" and action.callback_id and clean_label(action.label)
        }
        has_link_or_form = any(action.type in ("URL_ACTION", "MODAL_ACTION") for action in actions)
        return Reply(text or EMPTY, options=options, has_link_or_form=has_link_or_form)


def _late_replies(client: ConversationsApiClient, conversation_id: str, response_id: str) -> List[Dict[str, Any]]:
    """Replies filed under an earlier response (see ``conversations.replies_after_input``)."""
    try:
        messages = list_messages(client, conversation_id, limit=10).get("messages") or []
    except Exception:
        log.exception("could not read history for a late reply")
        return []
    return [m for m in replies_after_input(messages, response_id) if m.get("actor") != "USER"]


def _normalize_number(number: str) -> str:
    return _PHONE_PUNCTUATION.sub("", number or "")


def mask_number(number: str) -> str:
    """A phone number for logs: only the last four digits."""
    digits = _normalize_number(number)
    return f"***{digits[-4:]}" if len(digits) > 4 else "***"
