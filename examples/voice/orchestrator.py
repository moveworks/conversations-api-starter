"""The orchestrator harness: the Moveworks orchestrator, reached through the Conversations API.

The voice harness hands every real request here through its
``ask_orchestrator`` tool. One voice session is one Conversations API
conversation, so a follow-up such as "and what about Tuesday?" has context.
Turns run as the employee your credential acts for, with the same assistant
configuration as chat.

:meth:`Orchestrator.ask` runs one turn and yields events for the voice
harness:

- ``{"type": "progress", "text": ...}`` each time the orchestrator reports new
  activity (its reasoning steps), so the voice model can say something true
  while it waits;
- ``{"type": "result", "output": {...}}`` last, the tool output the voice
  model reads: a ``status``, the ``answer`` as plain sentences, its
  ``sections`` for step-by-step delivery, and any ``buttons``.

How a turn runs: it streams (``POST .../responses/stream``), which costs one
request where polling a 20-second answer costs about ten reads against the
rate limit, and the answer is taken from the stream itself. A stream stops at
120 seconds even when the response is still running; that response is not
failed, so the turn switches to polling it instead of reporting an error. A
dropped stream is handled the same way. If opening the stream is rate limited
(429), the server did not accept the turn, so it is submitted with
``create_response``, which the shared client backs off and resends after a
429. A turn is never resent after a timeout or 5xx. Every failure becomes a
result the voice model can say out loud.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Generator, List, Optional, Tuple

from moveworks_capi.client import ConversationsApiClient
from moveworks_capi.conversations import create_conversation, list_messages, replies_after_input
from moveworks_capi.errors import AuthenticationError, RateLimitedError, StreamClosedError, StreamError
from moveworks_capi.rendering import clean_label
from moveworks_capi.responses import MAX_TEXT_LENGTH, create_response, message_actions, wait_for_completion
from moveworks_capi.streaming import OutputDelta, ResponseCompleted, ResponseCreated, stream_response

from .speech import answer_text, sections, speakable

log = logging.getLogger(__name__)

STREAM_TIMEOUT_SECONDS = 120.0
"""How long the server keeps a response stream open; the response keeps running after."""

DEFAULT_TURN_TIMEOUT_SECONDS = 150.0
"""Long answers approach two minutes end to end, as in the reference app."""

TURN_LOCK_WAIT_SECONDS = 30.0
MAX_STATUS_CHARS = 200

NOTES = {
    "busy": "The orchestrator is rate limited right now. Tell the caller it's busy and to try again in a minute.",
    "in_progress": "The orchestrator is still finishing the caller's previous request. Ask them to hold on a moment and try again.",
    "timed_out": (
        "The orchestrator is still working on this after a long wait. Tell the caller it's taking longer than usual "
        "and offer to check on it again in a moment."
    ),
    "not_signed_in": "The orchestrator couldn't sign the caller in. Apologize and suggest they try again later.",
    "error": "The orchestrator couldn't be reached. Apologize briefly and suggest trying again.",
}


@dataclass
class VoiceSession:
    """One voice session: its Conversations API conversation and the last result's buttons."""

    session_id: str
    conversation_id: Optional[str] = None
    buttons: Dict[str, str] = field(default_factory=dict)
    updated_at: float = field(default_factory=time.monotonic)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


class Sessions:
    """In-memory voice sessions. Sessions idle for ``ttl_seconds`` are dropped.

    A single process only. To run more than one instance, keep them in a shared
    store keyed by session id.
    """

    def __init__(self, ttl_seconds: float = 2 * 3600) -> None:
        self._ttl = ttl_seconds
        self._sessions: Dict[str, VoiceSession] = {}
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._sessions)

    def new(self) -> VoiceSession:
        session = VoiceSession(session_id=secrets.token_urlsafe(16))
        with self._lock:
            self._prune()
            self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str) -> Optional[VoiceSession]:
        with self._lock:
            self._prune()
            session = self._sessions.get(session_id)
            if session is not None:
                session.updated_at = time.monotonic()
            return session

    def end(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def _prune(self) -> None:
        cutoff = time.monotonic() - self._ttl
        for session_id in [sid for sid, s in self._sessions.items() if s.updated_at < cutoff]:
            del self._sessions[session_id]


class Orchestrator:
    """Runs ``ask_orchestrator`` calls as Conversations API turns.

    Args:
        client: A ``ConversationsApiClient`` for the employee the voice session
            acts as.
        stream: Stream each turn (the default) or submit and poll.
        timeout_seconds: The longest one turn may take before the voice model
            hears that it is still running.
        conversation_title: Title for the conversation each voice session creates.
    """

    def __init__(
        self,
        client: ConversationsApiClient,
        *,
        stream: bool = True,
        timeout_seconds: float = DEFAULT_TURN_TIMEOUT_SECONDS,
        conversation_title: str = "Voice call",
    ) -> None:
        self.client = client
        self.stream = stream
        self.timeout_seconds = timeout_seconds
        self.conversation_title = conversation_title

    def ask(
        self, session: VoiceSession, query: str, *, choice: Optional[str] = None
    ) -> Generator[Dict[str, Any], None, None]:
        """Run one turn: the caller's ``query``, or the button whose label is ``choice``."""
        if not session.lock.acquire(timeout=TURN_LOCK_WAIT_SECONDS):
            yield _result("in_progress")
            return
        try:
            conversation_id = self._conversation(session)
            callback_id = _find_button(session.buttons, choice) if choice else None
            text = None if callback_id else (query or choice or "").strip()[:MAX_TEXT_LENGTH]
            status, messages = yield from self._turn(conversation_id, text, callback_id)
            yield {"type": "result", "output": self._output(session, status, messages)}
        except RateLimitedError as exc:
            log.warning("session %s: rate limited after retries (trace %s)", session.session_id, exc.trace_id)
            yield _result("busy")
        except TimeoutError as exc:
            log.warning("session %s: %s", session.session_id, exc)
            yield _result("timed_out")
        except AuthenticationError as exc:
            log.error("session %s: authentication failed: %s", session.session_id, exc)
            yield _result("not_signed_in")
        except GeneratorExit:
            # The voice harness cancelled the lookup (the caller barged in). The
            # response may still finish; its answer stays in the conversation.
            log.info("session %s: lookup cancelled by the voice harness", session.session_id)
            raise
        except Exception:
            log.exception("session %s: turn failed", session.session_id)
            yield _result("error")
        finally:
            session.lock.release()

    def warm_up(self, session: VoiceSession, query: str) -> str:
        """Ask who the caller is and what the orchestrator can do for them, as session context."""
        for event in self.ask(session, query):
            if event["type"] == "result":
                output = event["output"]
                return output.get("answer", "") if output.get("status") == "answered" else ""
        return ""

    def _conversation(self, session: VoiceSession) -> str:
        conversation_id = session.conversation_id
        if conversation_id is None:
            conversation_id = create_conversation(self.client, title=self.conversation_title)["conversation_id"]
            session.conversation_id = conversation_id
        return conversation_id

    def _turn(
        self, conversation_id: str, text: Optional[str], callback_id: Optional[str]
    ) -> Generator[Dict[str, Any], None, Tuple[str, List[Dict[str, Any]]]]:
        """Yield progress events; return the final status and the answer's messages."""
        deadline = time.monotonic() + self.timeout_seconds
        response_id = ""
        status = ""
        messages: Dict[str, Dict[str, Any]] = {}
        if self.stream:
            last_status = ""
            try:
                for event in stream_response(
                    self.client, conversation_id, text, callback_id=callback_id, timeout_seconds=STREAM_TIMEOUT_SECONDS
                ):
                    if isinstance(event, ResponseCreated):
                        response_id = event.response_id
                    elif isinstance(event, OutputDelta) and event.output_type == "REASONING_MESSAGE":
                        latest = latest_status(event.text or "")
                        if latest and latest != last_status:
                            last_status = latest
                            yield {"type": "progress", "text": latest}
                    elif isinstance(event, OutputDelta) and event.output_type == "MESSAGE":
                        message = event.output.get("message") or {}
                        messages[message.get("message_id") or f"#{len(messages)}"] = message
                    elif isinstance(event, ResponseCompleted):
                        response_id = event.response_id or response_id
                        status = event.status
            except RateLimitedError:
                # A 429 means the server rejected the stream before accepting the turn.
                log.info("opening the stream was rate limited; submitting the turn without streaming")
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
        if status and messages:
            return status, list(messages.values())
        if not response_id:
            response_id = create_response(self.client, conversation_id, text, callback_id=callback_id)["response_id"]
        final = wait_for_completion(
            self.client,
            conversation_id,
            response_id,
            timeout_seconds=max(deadline - time.monotonic(), 5.0),
            initial_delay_seconds=0.0 if status else 1.0,
        )
        found = [item["message"] for item in final.outputs if item.get("type") == "MESSAGE" and item.get("message")]
        if not found and final.status == "COMPLETED":
            found = self._late_replies(conversation_id, final.response_id)
        return final.status, found

    def _late_replies(self, conversation_id: str, response_id: str) -> List[Dict[str, Any]]:
        """Replies filed under an earlier response (see ``conversations.replies_after_input``)."""
        try:
            history = list_messages(self.client, conversation_id, limit=10).get("messages") or []
        except Exception:
            log.exception("could not read history for a late reply")
            return []
        return [m for m in replies_after_input(history, response_id) if m.get("actor") != "USER"]

    @staticmethod
    def _output(session: VoiceSession, status: str, messages: List[Dict[str, Any]]) -> Dict[str, Any]:
        parts = sections(answer_text(messages))
        actions = [action for message in messages for action in message_actions(message)]
        session.buttons = {
            clean_label(action.label): action.callback_id
            for action in actions
            if action.type == "CALLBACK_ACTION" and action.callback_id and clean_label(action.label)
        }
        output: Dict[str, Any] = {
            "status": "failed" if status == "FAILED" else "answered",
            "answer": " ".join(parts) or ("The request failed." if status == "FAILED" else "Done. There's nothing more to report."),
        }
        if len(parts) > 1:
            output["sections"] = parts
        if session.buttons:
            output["buttons"] = list(session.buttons)
        if any(action.type in ("URL_ACTION", "MODAL_ACTION") for action in actions):
            output["has_link_or_form"] = True
        return output


def latest_status(reasoning: str) -> str:
    """The newest line of the orchestrator's reasoning trail, as a short plain sentence."""
    lines = [line for line in (reasoning or "").splitlines() if line.strip()]
    return speakable(lines[-1])[:MAX_STATUS_CHARS] if lines else ""


def _result(status: str) -> Dict[str, Any]:
    return {"type": "result", "output": {"status": status, "message": NOTES[status]}}


def _find_button(buttons: Dict[str, str], choice: Optional[str]) -> Optional[str]:
    wanted = " ".join((choice or "").lower().split())
    for label, callback_id in buttons.items():
        if " ".join(label.lower().split()) == wanted:
            return callback_id
    return None
