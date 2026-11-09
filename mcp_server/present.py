"""Turn Conversations API messages, actions and forms into text and data for MCP results.

MCP clients show a tool's text to the user (often after their model summarizes
it) and can read ``structuredContent``, so each piece comes out both ways:
citations as a Sources list and as ``{title, url}`` records, buttons as options.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional

from moveworks_capi.rendering import (
    citation_label,
    clean_label,
    form_link_citations,
    normalize_markup,
    strip_flattened_forms,
)

from .models import Option

GET_HELP_MENTION = re.compile(r"get help", re.IGNORECASE)


def clean(text: str) -> str:
    return normalize_markup(text).strip()


def progress_line(reasoning_text: str) -> str:
    """The newest line of a reasoning trail ("⏳ Working on: ...", "→ Checking ...")."""
    lines = [line.strip() for line in clean(reasoning_text).splitlines() if line.strip()]
    return lines[-1] if lines else "Working..."


def render_messages(messages: List[Dict[str, Any]]) -> str:
    blocks: List[str] = []
    for message in messages:
        text = clean(strip_flattened_forms(message))
        if text:
            blocks.append(text)
        sources = _sources(message.get("citations") or [])
        if sources:
            blocks.append("Sources:\n" + "\n".join(sources))
    return "\n\n".join(blocks)


MAX_SOURCES = 5


def _unique(citations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One entry per source; Moveworks cites the same article once per paragraph that uses it."""
    seen = set()
    unique = []
    for citation in citations:
        key = citation.get("url") or citation_label(citation)
        if key not in seen:
            seen.add(key)
            unique.append(citation)
    return unique


def _sources(citations: List[Dict[str, Any]]) -> List[str]:
    citations = _unique(citations)
    lines: List[str] = []
    for citation in citations[:MAX_SOURCES]:
        label = citation_label(citation) or "Source"
        url = citation.get("url")
        lines.append(f"- [{label}]({url})" if url else f"- {label}")
    if len(citations) > MAX_SOURCES:
        lines.append(f"- and {len(citations) - MAX_SOURCES} more")
    return lines


def sources(messages: List[Dict[str, Any]]) -> List[Dict[str, Optional[str]]]:
    """Citations as records, for ``structuredContent`` (clients that render citations read these)."""
    citations = _unique([c for message in messages for c in message.get("citations") or []])
    return [{"title": citation_label(c) or "Source", "url": c.get("url")} for c in citations[:MAX_SOURCES]]


LIVE_AGENT_LABEL = re.compile(r"agent chat|live agent|chat with an agent", re.IGNORECASE)


def is_live_agent(option: Option) -> bool:
    return option.kind in ("callback", "modal") and bool(LIVE_AGENT_LABEL.search(option.label))


def message_options(
    messages: List[Dict[str, Any]], resolve: Optional[Callable[[str], Optional[str]]] = None
) -> List[Option]:
    """``resolve`` maps a short link to its target when pairing portal form links with citations."""
    options: List[Option] = []
    for message in messages:
        form_links = form_link_citations(message, resolve)
        response_id = message.get("response_id")
        message_id = message.get("message_id")
        handoff = message.get("handoff") or {}
        has_handoff_action = False
        for index, action in enumerate(message.get("actions") or []):
            label = clean_label(action.get("label") or "")
            if index in form_links:
                label = f"{label}: {citation_label(form_links[index])}"
            kind = action.get("type")
            if kind == "CALLBACK_ACTION":
                options.append(Option(label, "callback", callback_id=(action.get("callback_action") or {}).get("callback_id")))
            elif kind == "MODAL_ACTION":
                options.append(
                    Option(label, "modal", modal_id=(action.get("modal_action") or {}).get("modal_id"),
                           response_id=response_id, message_id=message_id)
                )
            elif kind == "HANDOFF_ACTION":
                has_handoff_action = True
                options.append(Option(label, "handoff", handoff=handoff, response_id=response_id, message_id=message_id))
            elif kind == "URL_ACTION":
                options.append(Option(label, "url", url=(action.get("url_action") or {}).get("url")))
        # The Get Help menu rides on nearly every turn, so offer it only when the
        # text tells the user to click Get Help, which no chat surface here can show.
        text = strip_flattened_forms(message)
        if handoff and not has_handoff_action and GET_HELP_MENTION.search(text):
            options.append(Option("Get help", "handoff", handoff=handoff, response_id=response_id, message_id=message_id))
    return options


INTERIM_PATTERN = re.compile(r"(\.\.\.|\u2026)\s*$|still being processed", re.IGNORECASE)


def is_interim(message: Dict[str, Any]) -> bool:
    """A placeholder whose real outcome follows ("Filing your ticket...")."""
    if message.get("actions") or message.get("citations"):
        return False
    return bool(INTERIM_PATTERN.search(clean(strip_flattened_forms(message))))
