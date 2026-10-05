"""Create, list, fetch, and update conversations owned by the authenticated user.

Straightforward wrappers over ``/rest/v1/conversations``. Every endpoint is
user-scoped: it only sees conversations that belong to whoever the bearer
resolves to.
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, List, Optional

from .client import ConversationsApiClient


def create_conversation(client: ConversationsApiClient, *, title: Optional[str] = None) -> Dict[str, Any]:
    """Start a conversation (``POST /rest/v1/conversations``).

    Set ``title`` up front: :func:`list_conversations` returns it, which is what
    lets a sidebar render from one request (see :func:`update_conversation`).
    """
    body: Dict[str, Any] = {}
    if title:
        body["title"] = title
    return client.post("/rest/v1/conversations", json=body).json()


def list_conversations(
    client: ConversationsApiClient,
    *,
    limit: Optional[int] = None,
    cursor: Optional[str] = None,
    archived: bool = False,
) -> Dict[str, Any]:
    """Fetch one page of the caller's conversations.

    Returns the raw JSON body, typically shaped as ``{"conversations": [...], "metadata": {"next_cursor": ...}}``.
    Use :func:`iter_conversations` to page through automatically.

    The default lists only unarchived conversations; ``archived=True`` lists
    only archived ones. An unarchived conversation omits ``archived`` rather
    than sending ``false``.
    """
    params: Dict[str, Any] = {}
    if limit is not None:
        params["limit"] = limit
    if cursor:
        params["cursor"] = cursor
    if archived:
        params["archived"] = "true"
    return client.get("/rest/v1/conversations", params=params).json()


def iter_conversations(
    client: ConversationsApiClient,
    *,
    limit: Optional[int] = None,
) -> Iterator[Dict[str, Any]]:
    """Yield conversations across all pages, following ``next_cursor``."""
    cursor: Optional[str] = None
    while True:
        page = list_conversations(client, limit=limit, cursor=cursor)
        conversations: List[Dict[str, Any]] = page.get("conversations") or []
        yield from conversations
        metadata = page.get("metadata") or {}
        cursor = metadata.get("next_cursor")
        if not cursor:
            return


def get_conversation(client: ConversationsApiClient, conversation_id: str) -> Dict[str, Any]:
    """Fetch a single conversation by ID."""
    return client.get(f"/rest/v1/conversations/{conversation_id}").json()


def update_conversation(
    client: ConversationsApiClient,
    conversation_id: str,
    *,
    title: Optional[str] = None,
    archived: Optional[bool] = None,
) -> Dict[str, Any]:
    """Rename or archive a conversation (``PATCH .../conversations/{id}``).

    Worth doing on the first turn: ``list_conversations`` returns each
    conversation's ``title``, so a chat surface that titles its conversations
    can render a full sidebar from ONE request. Leaving titles unset forces a
    per-conversation history read to synthesize a label, which multiplies
    reads by the number of rows.
    """
    body: Dict[str, Any] = {}
    if title is not None:
        body["title"] = title
    if archived is not None:
        body["archived"] = archived
    return client.request(
        "PATCH", f"/rest/v1/conversations/{conversation_id}", json=body
    ).json()


def list_messages(
    client: ConversationsApiClient,
    conversation_id: str,
    *,
    limit: Optional[int] = None,
    cursor: Optional[str] = None,
) -> Dict[str, Any]:
    """List every message in a conversation, most recent first.

    Wraps ``GET /rest/v1/conversations/{id}/messages``. Each entry is a
    full message object (actor, content, actions, feedback ids), so this is
    what a chat surface uses to rehydrate history when the user switches
    back to an earlier conversation.

    Assistant text can contain Slack-style links and ``:emoji_name:``
    shortcodes, and modal DISPLAY fields can contain HTML. To display them,
    see ``moveworks_capi.rendering.normalize_markup`` and
    ``modal_html_to_markdown``.
    """
    params: Dict[str, Any] = {}
    if limit is not None:
        params["limit"] = limit
    if cursor is not None:
        params["cursor"] = cursor
    return client.get(
        f"/rest/v1/conversations/{conversation_id}/messages", params=params
    ).json()


def replies_after_input(messages: List[Dict[str, Any]], response_id: str) -> List[Dict[str, Any]]:
    """Non-user messages that follow the user input of ``response_id``.

    A reply can be written to history under an earlier response rather than
    returned in the outputs of the response that prompted it (for example, the
    farewell after "Exit Live Chat"). When a response completes with no
    outputs, re-read recent history and pass it here to find those replies.

    Args:
        messages: A conversation's messages, in any order (``list_messages``
            returns newest first).
        response_id: The response whose user input (for example a callback
            click) got no outputs of its own.

    Returns:
        The messages between that input and the next user message, oldest
        first. Empty when the input is not in ``messages``.
    """
    ordered = sorted(messages, key=lambda m: m.get("created_at") or "")
    for i, message in enumerate(ordered):
        if message.get("actor") == "USER" and message.get("response_id") == response_id:
            replies = []
            for later in ordered[i + 1:]:
                if later.get("actor") == "USER":
                    break
                replies.append(later)
            return replies
    return []
