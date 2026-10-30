"""Poll the Conversations API ``/events`` endpoint with a per-user checkpoint.

Implements the pattern described on the polling docs page:
https://docs.moveworks.com/api-reference/conversations-api/proactive-notifications/receive-via-polling

Three pieces to be aware of:

- ``EventsPollingClient`` calls the endpoint once and returns a page of events.
- ``Checkpoint`` is a small Protocol you implement (or use the file / memory
  reference implementations) to persist the last successfully processed
  ``event_id`` per user. The docs' "advance after processing" and
  "never regress" rules live here.
- ``PollingLoop`` ties the two together: poll on an interval, process each
  event via a caller-supplied handler, advance the checkpoint only on success.

Ordering: the loop sorts what it fetched by creation time before dispatch,
and a user with no checkpoint goes through an explicit first-run policy
instead of replaying history.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Literal, Optional, Protocol, Set, Tuple

from ..client import ConversationsApiClient

log = logging.getLogger(__name__)

ALL_CONVERSATIONS = "-"
FirstRunPolicy = Literal["skip_history", "replay_history"]
EVENTS_PATH_TEMPLATE = "/rest/v1/conversations/{conversation_id}/events"


@dataclass(frozen=True)
class Event:
    """A single event as returned by the ``/events`` endpoint."""

    event_id: str
    conversation_id: str
    created_at: str
    # Deprecated in the API, and empty on an event with no message; use has_message.
    payload_type: str
    # "ASSISTANT" for bot replies; "LIVE_AGENT" when a human agent's message
    # arrives during a live agent session; "USER" echoes are possible too.
    message_actor: Optional[str]
    content_type: Optional[str]
    content_text: Optional[str]
    raw: Dict[str, Any]
    # Where the event came from, such as "USER_CONSENT_AUTH" or "LIVE_AGENT".
    # None when unclassified; treat None or an unrecognized value as an
    # ordinary assistant message.
    category: Optional[str] = None
    # "ENDED" when a live agent session ends. Such an event may carry no message.
    live_agent_session_state: Optional[str] = None
    # "AUTHORIZE" (consent completed, also on the resumed request) or "REVOKE".
    consent_operation_type: Optional[str] = None

    @property
    def has_message(self) -> bool:
        """Whether the event carries a message, read from ``raw``. Some events carry only details."""
        return bool((self.raw.get("event_payload") or {}).get("message"))

    @classmethod
    def from_json(cls, obj: Dict[str, Any]) -> "Event":
        payload = obj.get("event_payload") or {}
        message = payload.get("message") or {}
        content = message.get("content") or {}
        content_type = content.get("type")
        content_text: Optional[str] = None
        if content_type:
            body = content.get(content_type.lower())
            if isinstance(body, dict):
                content_text = body.get("text")
        return cls(
            event_id=obj["event_id"],
            conversation_id=obj["conversation_id"],
            created_at=obj["created_at"],
            payload_type=payload.get("type", ""),
            message_actor=message.get("actor"),
            content_type=content_type,
            content_text=content_text,
            raw=obj,
            category=obj.get("category"),
            live_agent_session_state=(payload.get("live_agent_details") or {}).get("session_state"),
            consent_operation_type=(payload.get("user_consent_auth_details") or {}).get("operation_type"),
        )


class Checkpoint(Protocol):
    """Per-user checkpoint store.

    Implementations must be safe to call from a single thread; the loop takes
    care of not calling ``advance`` before ``load`` has returned. The store
    must reject any attempt to move a checkpoint backward.
    """

    def load(self, user_id: str) -> Optional[str]: ...

    def advance(self, user_id: str, event_id: str) -> None: ...


def _not_newer(candidate: str, current: str) -> bool:
    """True when ``candidate`` is not strictly newer than ``current``.

    Base-62 event IDs sort in creation order lexicographically ONLY when they
    are the same length (shared timestamp prefix). Compare by length first so
    a shorter-but-later ID is never mistaken for older.
    """
    return (len(candidate), candidate) <= (len(current), current)


def event_sort_key(event: "Event") -> Tuple[float, int, str]:
    """Oldest-first key: ``created_at``, then the event id (length first)."""
    try:
        created = datetime.fromisoformat(event.created_at.replace("Z", "+00:00")).timestamp()
    except ValueError:
        created = 0.0
    return (created, len(event.event_id), event.event_id)


class InMemoryCheckpoint:
    """Reference checkpoint that keeps state in a process-local dict."""

    def __init__(self) -> None:
        self._store: Dict[str, str] = {}
        self._lock = threading.Lock()

    def load(self, user_id: str) -> Optional[str]:
        with self._lock:
            return self._store.get(user_id)

    def advance(self, user_id: str, event_id: str) -> None:
        with self._lock:
            current = self._store.get(user_id)
            if current is not None and _not_newer(event_id, current):
                return
            self._store[user_id] = event_id


class FileCheckpoint:
    """Reference checkpoint that persists to a JSON file.

    Safe within one process: a lock serializes updates, but nothing coordinates
    across processes. For production, back this with a database or key-value
    store that supports atomic per-user updates.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> Dict[str, str]:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def _write(self, data: Dict[str, str]) -> None:
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        tmp.replace(self._path)

    def load(self, user_id: str) -> Optional[str]:
        with self._lock:
            return self._read().get(user_id)

    def advance(self, user_id: str, event_id: str) -> None:
        with self._lock:
            data = self._read()
            current = data.get(user_id)
            if current is not None and _not_newer(event_id, current):
                return
            data[user_id] = event_id
            self._write(data)


def get_event(client: ConversationsApiClient, conversation_id: str, event_id: str) -> Event:
    """Fetch one event (``GET .../conversations/{id}/events/{event_id}``).

    Needs the real conversation id; ``-`` is rejected with a 400. Useful when a
    webhook delivery names an event and you want the full payload.
    """
    path = EVENTS_PATH_TEMPLATE.format(conversation_id=conversation_id) + f"/{event_id}"
    return Event.from_json(client.get(path).json())


class EventsPollingClient:
    """Single-page reader for the ``/events`` endpoint.

    ``conversation_id`` defaults to ``-`` (every conversation the user has).
    Pass a real id to read one conversation's events; then any
    ``starting_after`` / ``ending_at`` event must belong to that conversation.
    """

    def __init__(self, client: ConversationsApiClient, *, conversation_id: str = ALL_CONVERSATIONS) -> None:
        self._client = client
        self._path = EVENTS_PATH_TEMPLATE.format(conversation_id=conversation_id)

    def fetch_page(
        self,
        *,
        starting_after: Optional[str] = None,
        ending_at: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> "EventsPage":
        params: Dict[str, Any] = {}
        if starting_after:
            params["starting_after"] = starting_after
        if ending_at:
            params["ending_at"] = ending_at
        if cursor:
            params["cursor"] = cursor
        if limit is not None:
            params["limit"] = limit

        response = self._client.get(self._path, params=params)
        body = response.json()
        events = [Event.from_json(e) for e in body.get("events", [])]
        metadata = body.get("metadata") or {}
        return EventsPage(
            events=events,
            next_cursor=metadata.get("next_cursor"),
            trace_id=response.headers.get("x-trace-id"),
        )

    def iter_events(
        self,
        *,
        starting_after: Optional[str] = None,
        ending_at: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Iterable[Event]:
        """Yield events across all pages, following ``next_cursor``."""
        cursor: Optional[str] = None
        while True:
            page = self.fetch_page(
                starting_after=starting_after,
                ending_at=ending_at,
                cursor=cursor,
                limit=limit,
            )
            yield from page.events
            if not page.next_cursor:
                return
            cursor = page.next_cursor


@dataclass(frozen=True)
class EventsPage:
    events: List[Event]
    next_cursor: Optional[str]
    trace_id: Optional[str]


EventHandler = Callable[[str, Event], None]
"""Callback signature: ``handler(user_id, event)``.

The handler must raise on failure. The loop advances the checkpoint only if
the handler returns without raising.
"""


class PollingLoop:
    """Poll ``/events`` for one user on a fixed interval.

    The loop is single-user: the events endpoint is scoped to a
    single user, so per-user polling is the right unit of work. Run multiple
    loops (one per user, or a pool) if you need to fan out.

    ``first_run`` decides what happens for a user with no checkpoint:

    - ``"skip_history"`` (default): set the checkpoint to the newest existing
      event and dispatch nothing, so only events that arrive afterwards reach
      the handler. A user with no events yet gets everything from then on.
    - ``"replay_history"``: dispatch the user's whole history, oldest first.
    """

    def __init__(
        self,
        client: ConversationsApiClient,
        checkpoint: Checkpoint,
        handler: EventHandler,
        *,
        interval_seconds: float = 15.0,
        page_limit: int = 20,
        first_run: FirstRunPolicy = "skip_history",
    ) -> None:
        if first_run not in ("skip_history", "replay_history"):
            raise ValueError(f"first_run must be 'skip_history' or 'replay_history', got {first_run!r}")
        self._events = EventsPollingClient(client)
        self._checkpoint = checkpoint
        self._handler = handler
        self._interval = interval_seconds
        self._page_limit = page_limit
        self._first_run = first_run
        # Users whose empty history was already seen, so their first event is new.
        self._started_empty: Set[str] = set()

    def poll_once(self, user_id: str) -> int:
        """Fetch, dispatch, and advance the checkpoint. Returns events dispatched."""
        starting_after = self._checkpoint.load(user_id)
        if starting_after is None and user_id not in self._started_empty:
            if self._first_run == "skip_history":
                self._skip_history(user_id)
                return 0
        events = sorted(
            self._events.iter_events(starting_after=starting_after, limit=self._page_limit),
            key=event_sort_key,
        )
        processed = 0
        for event in events:
            try:
                self._handler(user_id, event)
            except Exception:
                log.exception(
                    "handler failed for event %s (user=%s); checkpoint held at %s",
                    event.event_id,
                    user_id,
                    self._checkpoint.load(user_id),
                )
                raise
            self._checkpoint.advance(user_id, event.event_id)
            processed += 1
        return processed

    def _skip_history(self, user_id: str) -> None:
        # One read, not the whole history: an unfiltered read puts the newest
        # events on the first page. Were that ever not so, the next poll would
        # replay the rest of history rather than lose events.
        page = self._events.fetch_page(limit=self._page_limit)
        if not page.events:
            self._started_empty.add(user_id)
            return
        newest = max(page.events, key=event_sort_key)
        self._checkpoint.advance(user_id, newest.event_id)
        log.info("first poll for user=%s: checkpoint set to %s, history not dispatched", user_id, newest.event_id)

    def run_forever(self, user_id: str) -> None:
        """Poll on the configured interval until interrupted.

        Handler exceptions do not advance the checkpoint or stop the loop;
        the next poll retries from the last known-good checkpoint. This
        preserves the "never regress" rule from the docs.
        """
        while True:
            try:
                self.poll_once(user_id)
            except Exception:
                log.exception("poll_once failed; will retry in %.1fs", self._interval)
            time.sleep(self._interval)
