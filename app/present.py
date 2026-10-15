"""Turn Conversations API objects into the shapes the browser renders.

Every message, whether it arrived on a live turn or from conversation
history, goes through ``present_message``, so the browser renders both the
same way and never sees raw API markup.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict, List

from moveworks_capi.responses import message_actions, message_handoff
from moveworks_capi.rendering import (
    citation_label,
    citation_summary,
    clean_label,
    form_link_citations,
    modal_html_to_markdown,
    normalize_markup,
    strip_flattened_forms,
)


def present_message(message: Dict[str, Any], *, response_id: str = "", resolve_links: bool = True) -> Dict[str, Any]:
    """A message as the browser renders it.

    Keys: ``message_id``, ``response_id``, ``actor``, ``text`` (CommonMark),
    ``files`` (names of files the user attached), ``is_action`` (a user turn
    that pressed a button rather than typed), ``sources``, ``actions``,
    ``handoff``, ``feedback``. The text drops the API's flattened copy of
    offered forms, which the actions row already shows.

    A ``URL_ACTION`` that opens a cited form gets a ``form`` key (``title``
    and ``summary``) so the browser can name it. Pairing makes one blocking request per short link (cached), so
    call this off the event loop, and pass
    ``resolve_links=False`` where actions are not rendered, such as streamed
    snapshots.
    """
    forms = form_link_citations(message) if resolve_links else {}
    handoff = present_handoff(message)
    actions = []
    for i, action in enumerate(message_actions(message)):
        # A HANDOFF_ACTION opens the message's handoff menu, so omit it when the message has no handoff options.
        if action.type == "HANDOFF_ACTION" and not any(category["items"] for category in handoff):
            continue
        shown = present_action(asdict(action))
        if i in forms:
            shown["form"] = {"title": citation_label(forms[i]), "summary": form_summary(forms[i])}
        actions.append(shown)
    return {
        "message_id": message.get("message_id", ""),
        "response_id": message.get("response_id") or response_id,
        "actor": message.get("actor", ""),
        "sender": present_sender(message.get("sender_info")),
        "text": normalize_markup(strip_flattened_forms(message)),
        "files": message_files(message),
        "is_action": (message.get("content") or {}).get("type") == "CALLBACK_ACTION_CONTENT",
        "sources": present_sources(message),
        "actions": actions,
        "handoff": handoff,
        "feedback": {
            "helpful": ((message.get("feedback") or {}).get("helpful") or {}).get("callback_id"),
            "unhelpful": ((message.get("feedback") or {}).get("unhelpful") or {}).get("callback_id"),
        },
    }


def form_summary(citation: Dict[str, Any]) -> str:
    """The first line of a form's description, with any indented lines it wraps onto."""
    body = (((citation.get("display") or {}).get("body") or {}).get("markdown_text") or {}).get("text") or ""
    lines = body.lstrip().splitlines()
    summary = citation_summary(citation)
    for line in lines[1:]:
        if not line[:1].isspace() or not line.strip():
            break
        summary += " " + line.strip()
    return normalize_markup(summary)


def present_sender(sender_info: Dict[str, Any] | None) -> Dict[str, str] | None:
    """Who wrote a ``LIVE_AGENT`` message, or None.

    ``sender_info`` is the only name the API gives a human agent; an actor the
    client does not recognise would otherwise read as the assistant. The
    avatar is hosted by the live chat provider, so only an ``https`` URL is
    passed to the browser.
    """
    if not sender_info or not sender_info.get("display_name"):
        return None
    avatar_url = sender_info.get("avatar_url") or ""
    return {
        "display_name": sender_info["display_name"],
        "avatar_url": avatar_url if avatar_url.startswith("https://") else "",
    }


def present_action(action: Dict[str, Any]) -> Dict[str, Any]:
    return {**action, "label": clean_label(action.get("label", ""))}


def present_sources(message: Dict[str, Any]) -> List[Dict[str, str]]:
    """Citations for the Sources list.

    One entry per document: the API cites each section of an article
    separately, so the same title and URL often appear several times.
    """
    sources: List[Dict[str, str]] = []
    seen: set = set()
    for citation in message.get("citations") or []:
        display = citation.get("display") or {}
        title = citation_label(citation)
        url = citation.get("url") or ""
        if (title, url) in seen:
            continue
        seen.add((title, url))
        body = ((display.get("body") or {}).get("markdown_text") or {}).get("text") or ""
        sources.append({
            "title": title or url or "Source",
            "url": url,
            "snippet": " ".join(normalize_markup(body).split())[:240],
        })
    return sources


def present_handoff(message: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The Get Help menu from ``Message.handoff``, or an empty list."""
    categories = []
    for category in message_handoff(message):
        items = []
        for item in category.items:
            entry: Dict[str, Any] = {"title": clean_label(item.title)}
            if item.action:
                entry["action"] = present_action(asdict(item.action))
            if item.text:
                entry["text"] = normalize_markup(item.text)
            items.append(entry)
        categories.append({
            "name": clean_label(category.name),
            "description": normalize_markup(category.description or ""),
            "items": items,
        })
    return categories


def present_modal(modal: Dict[str, Any]) -> Dict[str, Any]:
    """A modal with its title and DISPLAY field text converted to Markdown."""
    fields = []
    for field in modal.get("fields") or []:
        field = dict(field)
        text = (field.get("display") or {}).get("text")
        if field.get("type") == "DISPLAY" and isinstance(text, dict) and text.get("text"):
            field["display"] = {**field["display"], "text": {**text, "text": modal_html_to_markdown(text["text"])}}
        fields.append(field)
    return {**modal, "title": clean_label(modal.get("title", "")), "fields": fields}


def message_files(message: Dict[str, Any]) -> List[str]:
    """Names of files attached to a user turn (``plain_text.files``)."""
    plain_text = (message.get("content") or {}).get("plain_text") or {}
    return [f.get("file_name") or f.get("file_id", "") for f in plain_text.get("files") or []]

