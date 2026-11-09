"""Tool behavior, independent of the MCP transport.

Each user's conversations live in memory, keyed by (user, conversation_id).
The conversation_id is this server's own handle; the Conversations API id
behind it is created on the first turn.

Turns run on a process-level thread pool, not inside the MCP request: in
stateless mode the request is torn down when the tool returns, and a turn
that outlives the tool deadline must keep running so ``get_moveworks_result``
can pick it up.
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from moveworks_capi.errors import AuthenticationError, ConversationsApiError, RateLimitedError

from . import forms
from .models import Option
from .turns import NotAccepted, TurnOutcome

TurnsFactory = Callable[[Any], AbstractContextManager]
"""identity -> context manager yielding a MoveworksTurns bound to that user."""

AFFIRMATIVES = {
    "yes", "y", "yep", "yeah", "sure", "ok", "okay", "confirm", "go ahead", "do it", "please do",
    "yes please", "file it", "file the ticket", "submit", "submit it", "proceed",
}

SELECTION_FILLER = {
    "please", "now", "it", "that", "this", "one", "thanks", "thank", "you", "the", "ticket", "request", "form",
    "file", "submit", "send", "go", "ahead", "do", "proceed", "confirm",
}
"""Words that can follow a yes without turning the reply into a new question."""

OPTIONS_HEADING = ("Options (buttons: call choose_moveworks_option with option_id and option_label; "
                   "forms: call open_moveworks_form with option_id):")


@dataclass
class ToolReply:
    text: str
    data: Dict[str, Any]
    is_error: bool = False


@dataclass
class Job:
    future: "Future[Tuple[TurnOutcome, List[Dict[str, str]]]]"
    started: float = field(default_factory=time.monotonic)


@dataclass
class ConversationState:
    handle: str
    user_key: str
    capi_conversation_id: Optional[str] = None
    options: Dict[str, Option] = field(default_factory=dict)
    next_option: int = 1
    pressed: Dict[str, Job] = field(default_factory=dict)
    active: Optional[Job] = None
    last: Optional[Job] = None
    progress: str = ""
    form: Optional[forms.PendingForm] = None
    form_job: Optional[Job] = None
    last_submit: Optional[Job] = None
    touched: float = field(default_factory=time.monotonic)
    lock: threading.Lock = field(default_factory=threading.Lock)


class MoveworksTools:
    def __init__(
        self,
        turns_factory: TurnsFactory,
        *,
        deadline_seconds: float = 25.0,
        idle_seconds: float = 3600.0,
        max_workers: int = 16,
    ) -> None:
        self._turns_factory = turns_factory
        self._deadline = deadline_seconds
        self._idle = idle_seconds
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="mw-turn")
        self._states: Dict[Tuple[str, str], ConversationState] = {}
        self._user_locks: Dict[str, threading.Lock] = {}
        self._registry = threading.Lock()

    # Tools -----------------------------------------------------------------

    async def ask(self, identity: Any, message: str, conversation_id: Optional[str]) -> ToolReply:
        message = (message or "").strip()
        if not message:
            return _error(conversation_id, "empty_message", "Send a non-empty message.")
        if conversation_id:
            state = self._lookup(identity.user_key, conversation_id)
            if state is None:
                return _unknown_conversation(conversation_id)
        else:
            state = self._create(identity.user_key)
        with state.lock:
            running = state.active
            selection = _looks_like_selection(message, state.options)
        if running is not None:
            return _error(
                state.handle,
                "still_running",
                "The previous request in this conversation is still running. Call get_moveworks_result first, "
                "then send this message again.",
            )
        if selection:
            return _error(
                state.handle,
                "use_choose_tool",
                "That looks like a choice between the options Moveworks offered. Call choose_moveworks_option "
                "with the option_id and option_label so the user can confirm it.",
                options=self._option_views(state),
            )
        job = self._start(identity, state, lambda turns, cid, progress: turns.chat(cid, message, progress), title=message)
        return await self._wait(state, job)

    async def choose(self, identity: Any, conversation_id: str, option_id: str, option_label: str) -> ToolReply:
        state = self._lookup(identity.user_key, conversation_id)
        if state is None:
            return _unknown_conversation(conversation_id)
        with state.lock:
            existing = state.pressed.get(option_id)
            option = state.options.get(option_id)
        if existing is not None:
            # A retry of a press already sent: return its outcome, never press twice.
            return await self._wait(state, existing)
        if option is not None and option.is_form:
            return _error(state.handle, "use_open_form",
                          f"{option_id} is a form. Call open_moveworks_form with this option_id to see its fields.",
                          options=self._option_views(state))
        if option is None:
            return _error(
                state.handle,
                "stale_option",
                f"{option_id} is not one of the current options. Options from earlier turns expire.",
                options=self._option_views(state),
            )
        if _normalize(option_label) != _normalize(option.label):
            return _error(
                state.handle,
                "label_mismatch",
                f"{option_id} is '{option.label}', not '{option_label}'. Check the option and try again.",
                options=self._option_views(state),
            )
        with state.lock:
            running = state.active is not None
        if running:
            return _error(state.handle, "still_running",
                          "The previous request in this conversation is still running. Call get_moveworks_result first.")
        def remember_press(job: Job) -> None:
            state.pressed[option_id] = job

        def forget_press(job: Job) -> None:
            # Moveworks never took the press, so the button can be pressed again.
            if state.pressed.get(option_id) is job:
                del state.pressed[option_id]

        job = self._start(identity, state, lambda turns, cid, progress: turns.press(cid, option, progress),
                          register=remember_press, on_not_accepted=forget_press)
        return await self._wait(state, job)

    async def open_form(self, identity: Any, conversation_id: str, option_id: str) -> ToolReply:
        state = self._lookup(identity.user_key, conversation_id)
        if state is None:
            return _unknown_conversation(conversation_id)
        with state.lock:
            option = state.options.get(option_id)
            current = state.form if state.form and state.form.option_id == option_id else None
            running = state.active is not None
        if current is not None:
            return _form_reply(state.handle, current)
        if option is None:
            return _error(state.handle, "stale_option",
                          f"{option_id} is not one of the current options. Options from earlier turns expire.",
                          options=self._option_views(state))
        if not option.is_form:
            return _error(state.handle, "use_choose_tool",
                          f"{option_id} is a button, not a form. Call choose_moveworks_option to press it.",
                          options=self._option_views(state))
        if running:
            return _error(state.handle, "still_running",
                          "The previous request in this conversation is still running. Call get_moveworks_result first.")
        user_lock = self._user_lock(identity.user_key)

        def fetch() -> forms.PendingForm:
            with user_lock, self._turns_factory(identity) as turns:
                path = turns.modal_path(state.capi_conversation_id or "", option)
                modal = option.modal or turns.fetch_form(state.capi_conversation_id or "", option)
            return forms.open_form(option_id, path, modal, option.label)

        try:
            form = await asyncio.wait_for(asyncio.to_thread(fetch), self._deadline)
        except asyncio.TimeoutError:
            return _error(state.handle, "still_running",
                          "Another Moveworks request for this user is still running. Try opening the form again shortly.")
        except RateLimitedError as exc:
            return _rate_limited(state.handle, exc)
        except ConversationsApiError as exc:
            return _error(state.handle, "moveworks_error", f"Moveworks couldn't open the form: {exc}")
        with state.lock:
            state.form = form
            state.form_job = None
        return _form_reply(state.handle, form)

    async def submit_form(self, identity: Any, conversation_id: str, values: Optional[Dict[str, Any]], action: str) -> ToolReply:
        state = self._lookup(identity.user_key, conversation_id)
        if state is None:
            return _unknown_conversation(conversation_id)
        with state.lock:
            form = state.form
            job = state.form_job
            last = state.last_submit
            running = state.active is not None
        if form is None:
            if last is not None:
                # A retry of a submit already sent: return its outcome, never submit twice.
                return await self._wait(state, last)
            return _error(state.handle, "no_open_form", "No form is open. Call open_moveworks_form first.")
        if job is not None and not (job.future.done() and isinstance(job.future.exception(), NotAccepted)):
            # Resubmit only when Moveworks provably rejected the last submit; otherwise it may have filed it.
            return await self._wait(state, job)
        if (action or "submit").strip().lower() == "cancel":
            with state.lock:
                state.form = None
                if job is not None and state.last_submit is job:
                    state.last_submit = None  # that submit was rejected; nothing to replay
            return ToolReply(f"Cancelled **{form.name}**. Nothing was submitted.",
                             {"conversation_id": state.handle, "status": "cancelled"})
        if running:
            return _error(state.handle, "still_running",
                          "The previous request in this conversation is still running. Call get_moveworks_result first.")
        draft = forms.PendingForm(**{**form.__dict__, "values": dict(form.values)})
        try:
            forms.apply_values(draft, values or {})
        except forms.FormInputError as exc:
            return _error(state.handle, "invalid_value", str(exc), form=forms.describe(form))
        blocked = forms.blockers(draft)
        if blocked:
            where = f" Complete it on the portal instead: {form.portal_url}" if form.portal_url else ""
            return _error(state.handle, "form_unsupported",
                          "This form can't be submitted from here: " + "; ".join(blocked) + "." + where,
                          form=forms.describe(form))
        missing = forms.missing_required(draft)
        if missing:
            return _error(state.handle, "missing_required", "Still needed: " + ", ".join(missing) + ".",
                          form=forms.describe(draft))
        with state.lock:
            form.values = draft.values

        def finished(outcome: TurnOutcome) -> None:
            state.form = None

        job = self._start(identity, state, lambda turns, cid, progress: turns.submit_form(cid, form, progress),
                          on_success=finished)
        with state.lock:
            state.form_job = job
            state.last_submit = job
        return await self._wait(state, job)

    async def result(self, identity: Any, conversation_id: str) -> ToolReply:
        state = self._lookup(identity.user_key, conversation_id)
        if state is None:
            return _unknown_conversation(conversation_id)
        with state.lock:
            job = state.active or state.last
        if job is None:
            return ToolReply("Nothing has been asked in this conversation yet.", {"conversation_id": state.handle, "status": "empty"})
        return await self._wait(state, job)

    # State -----------------------------------------------------------------

    def _create(self, user_key: str) -> ConversationState:
        state = ConversationState(handle="c_" + uuid.uuid4().hex[:16], user_key=user_key)
        with self._registry:
            self._expire()
            self._states[(user_key, state.handle)] = state
        return state

    def _lookup(self, user_key: str, handle: str) -> Optional[ConversationState]:
        with self._registry:
            self._expire()
            state = self._states.get((user_key, handle))
        if state is not None:
            state.touched = time.monotonic()
        return state

    def _expire(self) -> None:
        cutoff = time.monotonic() - self._idle
        for key, state in list(self._states.items()):
            if state.touched < cutoff and state.active is None:
                del self._states[key]

    def _user_lock(self, user_key: str) -> threading.Lock:
        with self._registry:
            return self._user_locks.setdefault(user_key, threading.Lock())

    # Turns -----------------------------------------------------------------

    def _start(
        self,
        identity: Any,
        state: ConversationState,
        run: Callable[..., TurnOutcome],
        *,
        title: str = "MCP",
        on_success: Optional[Callable[[TurnOutcome], None]] = None,
        register: Optional[Callable[[Job], None]] = None,
        on_not_accepted: Optional[Callable[[Job], None]] = None,
    ) -> Job:
        user_lock = self._user_lock(identity.user_key)

        def progress(line: str) -> None:
            state.progress = line

        def work() -> Tuple[TurnOutcome, List[Dict[str, str]]]:
            try:
                # One turn at a time per user: the per-user CreateResponse limit spans conversations.
                with user_lock, self._turns_factory(identity) as turns:
                    if state.capi_conversation_id is None:
                        state.capi_conversation_id = turns.create_conversation(title)
                    outcome = run(turns, state.capi_conversation_id, progress)
                with state.lock:
                    views = []
                    for option in outcome.options:
                        option_id = f"opt_{state.next_option}"
                        state.next_option += 1
                        state.options[option_id] = option
                        views.append({"option_id": option_id, "label": option.label,
                                      "kind": "form" if option.is_form else "button"})
                    if on_success is not None:
                        on_success(outcome)
                return outcome, views
            except NotAccepted:
                with state.lock:
                    if not state.options:
                        state.options.update(offered)
                    if on_not_accepted is not None:
                        on_not_accepted(job)
                raise
            finally:
                with state.lock:
                    if state.active is job:
                        state.active = None
                    state.last = job
                    state.progress = ""

        with state.lock:
            future: Future = Future()
            job = Job(future=future)
            state.active = job
            offered = dict(state.options)
            state.options.clear()
            if register is not None:
                register(job)
        inner = self._pool.submit(work)
        inner.add_done_callback(lambda f: _copy_result(f, future))
        return job

    async def _wait(self, state: ConversationState, job: Job) -> ToolReply:
        try:
            await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(job.future)), self._deadline)
        except Exception:
            # The job's own outcome, including its exception, is read from its future below.
            pass
        if not job.future.done():
            line = state.progress or "Moveworks is still working on it."
            return ToolReply(
                f"Still working: {line}\n\nCall get_moveworks_result with conversation_id {state.handle} to get the answer.",
                {"conversation_id": state.handle, "status": "pending", "progress": line},
            )
        exc = job.future.exception()
        if isinstance(exc, NotAccepted):
            exc = exc.cause
        if exc is None:
            outcome, views = job.future.result()
            return _completed(state.handle, outcome, views)
        if isinstance(exc, RateLimitedError):
            return _rate_limited(state.handle, exc)
        if isinstance(exc, AuthenticationError):
            return _error(state.handle, "auth_expired", "The Moveworks session expired. The user needs to sign in again.")
        if isinstance(exc, ConversationsApiError):
            return _error(state.handle, "moveworks_error", f"Moveworks returned an error: {exc}")
        return _error(state.handle, "server_error",
                      "The request failed before Moveworks replied. Ask Moveworks for the status before trying again.")

    def _option_views(self, state: ConversationState) -> List[Dict[str, str]]:
        with state.lock:
            return [{"option_id": key, "label": option.label, "kind": "form" if option.is_form else "button"}
                    for key, option in state.options.items()]


def _copy_result(source: Future, target: Future) -> None:
    exc = source.exception()
    if exc is not None:
        target.set_exception(exc)
    else:
        target.set_result(source.result())


def _rate_limited(handle: str, exc: RateLimitedError) -> ToolReply:
    wait = f" Wait {int(exc.retry_after)} seconds and" if exc.retry_after else " Wait a few seconds and"
    return _error(handle, "rate_limited", f"Moveworks is rate limiting this user.{wait} try again.",
                  retry_after=exc.retry_after)


def _form_reply(handle: str, form: forms.PendingForm) -> ToolReply:
    return ToolReply(forms.render(form) + f"\n\nconversation_id: {handle}",
                     {"conversation_id": handle, "status": "form_open", "form": forms.describe(form)})


def _option_line(view: Dict[str, str]) -> str:
    return f"[{view['option_id']}] {view['label']}" + (" (form)" if view.get("kind") == "form" else "")


def _completed(handle: str, outcome: TurnOutcome, views: List[Dict[str, str]]) -> ToolReply:
    blocks = [outcome.text]
    if views:
        blocks.append(OPTIONS_HEADING + "\n" + "\n".join(_option_line(v) for v in views))
    if outcome.notes:
        blocks.append("\n".join(f"- {note}" for note in outcome.notes))
    blocks.append(f"conversation_id: {handle} (pass it back to continue this conversation)")
    data = {
        "conversation_id": handle,
        "status": "failed" if outcome.failed else "completed",
        "text": outcome.text,
        "sources": outcome.sources,
        "options": views,
        "notes": outcome.notes,
    }
    return ToolReply("\n\n".join(blocks), data, is_error=outcome.failed)


def _error(handle: Optional[str], code: str, message: str, **extra: Any) -> ToolReply:
    data: Dict[str, Any] = {"conversation_id": handle, "status": "error", "error": code, "message": message}
    data.update({k: v for k, v in extra.items() if v is not None})
    text = message
    if extra.get("options"):
        text += "\n\n" + OPTIONS_HEADING + "\n" + "\n".join(_option_line(v) for v in extra["options"])
    return ToolReply(text, data, is_error=True)


def _unknown_conversation(handle: str) -> ToolReply:
    return _error(handle, "unknown_conversation",
                  "Unknown or expired conversation_id. Omit conversation_id to start a new conversation.")


def _looks_like_selection(message: str, options: Dict[str, Option]) -> bool:
    if not options:
        return False
    normalized = _normalize(message)
    if normalized in AFFIRMATIVES:
        return True
    words = normalized.split()
    # "yes please file it now" is still a choice; "ok what about vpn" is a new question.
    for size in (2, 1):
        if len(words) > size and " ".join(words[:size]) in AFFIRMATIVES and "?" not in message:
            if all(word in SELECTION_FILLER for word in words[size:]):
                return True
            break
    if re.fullmatch(r"(?:option\s*)?#?\d+|opt_\d+", normalized):
        return True
    return any(normalized == _normalize(option.label) for option in options.values())


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s#]", "", (text or "").lower())).strip()
