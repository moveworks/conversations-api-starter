"""Submit a turn, poll it to completion, and extract the assistant's reply.

:func:`user_input` builds a turn's ``input`` and :func:`create_response`
submits it. ``POST /rest/v1/conversations/{id}/responses`` returns ``202``
with ``status: IN_PROGRESS``; the reply is generated asynchronously, so
:func:`wait_for_completion` polls ``GET .../responses/{resp_id}`` until
``COMPLETED`` or ``FAILED``. :func:`extract_text` and :func:`extract_actions`
then pull the message text and the buttons out of the ``outputs`` array.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from .client import RATE_LIMIT_FLOOR_SECONDS, ConversationsApiClient
from .errors import RateLimitedError, ResponseFailedError

TERMINAL_STATUSES = {"COMPLETED", "FAILED"}
MAX_TEXT_LENGTH = 20000
MAX_FILES_PER_TURN = 5


def user_input(
    text: Optional[str] = None,
    *,
    callback_id: Optional[str] = None,
    file_ids: Sequence[str] = (),
) -> Dict[str, Any]:
    """Build the ``input`` object for one turn.

    A turn the user authored (``text``, ``file_ids``, or both) is sent as
    ``{"chat": {...}}``; pressing a button from a prior assistant message is
    ``{"action": {"callback_id": ...}}``. The older flat ``{"text": ...}`` and
    ``{"callback_id": ...}`` shapes still work but are deprecated.

    ``file_ids`` are ids returned by the Files API upload, which is in alpha
    and enabled per Moveworks instance; this starter does not upload files itself.
    """
    authored = text is not None or bool(file_ids)
    if authored == (callback_id is not None):
        raise ValueError("provide text and/or file_ids, or a callback_id, but not both")
    if callback_id is not None:
        return {"action": {"callback_id": callback_id}}
    if text is not None and len(text) > MAX_TEXT_LENGTH:
        raise ValueError(f"text exceeds {MAX_TEXT_LENGTH} characters")
    if len(file_ids) > MAX_FILES_PER_TURN:
        raise ValueError(f"at most {MAX_FILES_PER_TURN} files per turn")
    chat: Dict[str, Any] = {}
    if text is not None:
        chat["text"] = text
    if file_ids:
        chat["files"] = [{"file_id": file_id} for file_id in file_ids]
    return {"chat": chat}


def create_response(
    client: ConversationsApiClient,
    conversation_id: str,
    text: Optional[str] = None,
    *,
    callback_id: Optional[str] = None,
    file_ids: Sequence[str] = (),
) -> Dict[str, Any]:
    """Submit one turn (``POST .../responses``); returns the ``202`` body.

    The body carries ``response_id`` with ``status: IN_PROGRESS``; pass the id
    to :func:`wait_for_completion`. The client resends this ``POST`` only after
    a 429 or 401, never after a timeout or 5xx, since either can follow a turn
    the server already accepted.
    """
    return client.post(
        f"/rest/v1/conversations/{conversation_id}/responses",
        json={"input": user_input(text, callback_id=callback_id, file_ids=file_ids)},
    ).json()


@dataclass(frozen=True)
class Response:
    """Assistant response as returned by ``GET .../responses/{resp_id}``."""

    response_id: str
    conversation_id: str
    status: str
    outputs: List[Dict[str, Any]]
    raw: Dict[str, Any]

    @classmethod
    def from_json(cls, obj: Dict[str, Any]) -> "Response":
        return cls(
            response_id=obj.get("response_id", ""),
            conversation_id=obj.get("conversation_id", ""),
            status=obj.get("status", ""),
            outputs=obj.get("outputs") or [],
            raw=obj,
        )


def wait_for_completion(
    client: ConversationsApiClient,
    conversation_id: str,
    response_id: str,
    *,
    timeout_seconds: float = 60.0,
    poll_interval_seconds: float = 2.0,
    initial_delay_seconds: float = 1.0,
) -> Response:
    """Poll a response until it reaches a terminal status.

    Each poll counts against the Conversations API rate limit along with
    history and event reads, so the defaults wait before the first GET and poll every
    two seconds. A 429 during polling is not fatal: the loop waits and polls
    again until ``timeout_seconds``, even with ``max_retries=0`` on the client.

    Args:
        client: A ``ConversationsApiClient`` with a valid bearer provider.
        conversation_id: The conversation containing the response.
        response_id: The response to poll.
        timeout_seconds: Give up after this long. Raises ``TimeoutError``.
        poll_interval_seconds: Delay between polls.
        initial_delay_seconds: Delay before the first poll; a response is
            never terminal the instant it was submitted.

    Returns:
        The ``Response`` in a terminal status (``COMPLETED`` or ``FAILED``).
    """
    deadline = time.monotonic() + timeout_seconds
    path = f"/rest/v1/conversations/{conversation_id}/responses/{response_id}"
    status = "IN_PROGRESS"
    time.sleep(min(initial_delay_seconds, timeout_seconds))
    while True:
        wait = poll_interval_seconds
        try:
            response = Response.from_json(client.get(path).json())
        except RateLimitedError as exc:
            wait = max(poll_interval_seconds, exc.retry_after or RATE_LIMIT_FLOOR_SECONDS)
        else:
            if response.status in TERMINAL_STATUSES:
                return response
            status = response.status
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"response {response_id} still {status} after {timeout_seconds}s")
        time.sleep(min(wait, remaining))


def extract_text(response: Response) -> str:
    """Concatenate the assistant's message text across all outputs.

    Raises ``ResponseFailedError`` (carrying the ``Response``) for a ``FAILED``
    response.

    Walks ``response.outputs``, keeps ``MESSAGE`` items (skips ``REASONING_MESSAGE``),
    and pulls text out of whichever content type each message carries
    (``MARKDOWN_TEXT``, ``COMMONMARK_TEXT``, or ``PLAIN_TEXT``).
    """
    if response.status == "FAILED":
        raise ResponseFailedError(f"response {response.response_id} failed", response)

    parts: List[str] = []
    for item in response.outputs:
        if item.get("type") != "MESSAGE":
            continue
        message = item.get("message") or {}
        content = message.get("content") or {}
        text = _content_text(content)
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def _content_text(content: Dict[str, Any]) -> Optional[str]:
    """Pull the text out of a Content union based on the ``type`` discriminator."""
    content_type = content.get("type")
    if not content_type:
        return None
    body = content.get(content_type.lower())
    if isinstance(body, dict):
        text = body.get("text")
        if isinstance(text, str):
            return text
    return None


@dataclass(frozen=True)
class Action:
    """One entry from a message's ``actions`` array, normalized across types.

    For ``URL_ACTION``, ``CALLBACK_ACTION``, and ``MODAL_ACTION``, exactly one
    of ``url``, ``callback_id``, ``modal_id`` is set, matching ``type``. A
    ``HANDOFF_ACTION`` sets none and opens a menu built from the same
    message's ``handoff`` options.

    ``is_auth_link`` flags the user-consent (OBO/UCA) authorization links the
    assistant emits when a plugin's connector needs the user to grant access.
    They arrive as ordinary ``URL_ACTION`` entries, so detection is by URL
    shape: an
    ``https://{org}.moveworks.com/connections?...`` link. Treat the flag as a
    heuristic. Auth links deserve special handling in a chat surface (open in
    a popup or new tab; after the user completes consent, the assistant
    resumes the interrupted plugin automatically on the next turn).
    """

    label: str
    type: str
    url: Optional[str] = None
    callback_id: Optional[str] = None
    modal_id: Optional[str] = None
    is_auth_link: bool = False


def extract_actions(response: Response) -> List[Action]:
    """Collect the actions attached to the assistant's ``MESSAGE`` outputs.

    Buttons the assistant offers (open a link, file a ticket via modal,
    trigger a callback) ride on ``message.actions``. ``extract_text`` ignores
    them, so a text-only integration silently drops interactive replies -
    call this alongside it if your surface can render actions at all.
    """
    actions: List[Action] = []
    for item in response.outputs:
        if item.get("type") == "MESSAGE":
            actions.extend(message_actions(item.get("message") or {}))
    return actions


def message_actions(message: Dict[str, Any]) -> List[Action]:
    """Normalize one message's ``actions`` array.

    Works on any message object: a completed response's
    ``outputs[].message``, a stream delta's ``output.message``, or an entry
    from ``list_messages``.
    """
    actions: List[Action] = []
    for raw in message.get("actions") or []:
        action_type = raw.get("type", "")
        url = (raw.get("url_action") or {}).get("url")
        actions.append(
            Action(
                label=raw.get("label", ""),
                type=action_type,
                url=url,
                callback_id=(raw.get("callback_action") or {}).get("callback_id"),
                modal_id=(raw.get("modal_action") or {}).get("modal_id"),
                is_auth_link=bool(
                    action_type == "URL_ACTION"
                    and url
                    and ".moveworks.com/connections?" in url
                ),
            )
        )
    return actions
