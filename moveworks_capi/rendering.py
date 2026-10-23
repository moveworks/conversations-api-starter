"""Helpers for displaying assistant messages in a UI.

Nothing in the core client calls these helpers. Use them where your client
renders messages.

- ``normalize_markup`` converts Slack-style links (``<https://url|label>``,
  ``<https://url>``, ``<host|label>``) and ``:emoji_name:`` shortcodes in
  message, label, citation, and notification text to CommonMark, and removes
  a space left before punctuation. ``clean_label`` does the same for
  single-line button labels and titles, and drops shortcodes it cannot map.
- ``citation_label`` and ``citation_summary`` return a citation's title and
  first body line as plain text.
- ``modal_html_to_markdown`` converts the HTML in a modal ``DISPLAY`` field
  (``<br>``, ``<b>``, ``<a href>``, ``<p>``) to Markdown and drops every
  other tag.
- ``strip_flattened_forms`` removes the plain-text copy of a form citation
  from the end of a message, for clients that render the form themselves::

      ...I also found a **Corp VPN** form, which may be relevant.

      Which device are you trying to connect from?

      **Corp VPN**
      Remote access to Internal Corporate Systems

  A copy is removed only when it matches a citation's title and the first
  line of its body.
- ``layout_message`` does the same, and also reports where
  ``content_references`` first anchors a removed citation, so a client can
  place its form UI there. Several forms often share one paragraph, so the
  removed citations are reported as one group at one position. Offsets are
  treated as character (code point) indexes, matching Python ``str``
  indexing, so the split happens here rather than in a browser, where string
  indexes count UTF-16 units.
- ``form_link_citations`` pairs a form's ``URL_ACTION`` ("Complete this
  request", pointing at a ``https://app.moveworks.ai/r?...`` short link) with
  the citation for that form, by following the short link's redirect one hop
  to the form's portal URL, so a client can name each button. The links can
  arrive in a different order from ``citations[]``, so pairing by position
  does not work.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

# A form's description can wrap onto more lines, so the copy runs to the end.
_FLATTENED_TAIL = re.compile(r"\n\n\*\*(?P<title>[^*\n]+)\*\*\n(?P<body>[^\n]*(?:\n(?!\n)[^\n]*)*)\s*\Z")
_SLACK_LINK = re.compile(r"<[^|<>]+\|(?P<label>[^<>]+)>")


@dataclass(frozen=True)
class Segment:
    """A run of message text, optionally followed by a group of citations."""

    text: str
    citation_ids: Tuple[str, ...] = ()


@dataclass(frozen=True)
class MessageLayout:
    segments: Tuple[Segment, ...]
    removed_citation_ids: Tuple[str, ...]


def layout_message(message: Dict[str, Any]) -> MessageLayout:
    """Strip flattened form citations from a message and locate their anchors.

    Args:
        message: A message object (from ``list_messages``, a completed
            response's ``outputs[].message``, or a stream delta's
            ``output.message``).

    Returns:
        The text as one or two segments. When a removed citation is referenced
        in the text, the first segment ends at the paragraph holding the
        earliest such reference and carries every removed citation, in
        reference order. Otherwise the text is a single segment and the
        removed ids are only reported in ``removed_citation_ids``.
    """
    text, removed = _strip_flattened(message)
    if not removed:
        return MessageLayout(segments=(Segment(text),), removed_citation_ids=())

    anchor: Optional[int] = None
    ordered: List[str] = []
    for ref in sorted(message.get("content_references") or [], key=lambda r: r.get("offset") or 0):
        cid = ref.get("citation_id")
        offset = ref.get("offset")
        if cid not in removed or not isinstance(offset, int) or not 0 <= offset <= len(text):
            continue
        if anchor is None:
            anchor = _paragraph_end(text, offset)
        if cid not in ordered:
            ordered.append(cid)

    if anchor is None:
        return MessageLayout(segments=(Segment(text),), removed_citation_ids=tuple(removed))

    group = tuple(ordered + [cid for cid in removed if cid not in ordered])
    segments = [Segment(text[:anchor].strip("\n"), group)]
    tail = text[anchor:].strip("\n")
    if tail:
        segments.append(Segment(tail))
    return MessageLayout(segments=tuple(segments), removed_citation_ids=tuple(removed))


def strip_flattened_forms(message: Dict[str, Any]) -> str:
    """The message text without the flattened form citations at its end.

    For clients that render offered forms as actions below the answer rather
    than placing them inside it (see ``layout_message`` for that).
    """
    return _strip_flattened(message)[0]


def _strip_flattened(message: Dict[str, Any]) -> Tuple[str, List[str]]:
    text = _message_text(message)
    citations = message.get("citations") or []
    removed: List[str] = []
    while True:
        match = _FLATTENED_TAIL.search(text)
        if not match:
            break
        cid = _matching_citation(match, citations)
        if not cid or cid in removed:
            break
        removed.append(cid)
        text = text[: match.start()]
    return text, removed


def citation_label(citation: Dict[str, Any]) -> str:
    """The citation's visible title, as plain text.

    Titles arrive in Slack markup, in several shapes: ``**<url|Corp VPN>**``
    for forms and articles, ``<url|INC0010037>: connect to vpn`` for tickets,
    and article titles padded with ``\\n\\t\\t:``. Links become their labels,
    bold markers and runs of whitespace go, and the trailing colon is dropped.
    """
    title = _content_text((citation.get("display") or {}).get("title"))
    title = _SLACK_LINK.sub(lambda m: m.group("label"), title).replace("**", "")
    return " ".join(title.split()).rstrip(": ")


def citation_summary(citation: Dict[str, Any]) -> str:
    """The first non-blank line of the citation's body, as plain text."""
    return next((part.strip() for part in citation_body(citation).splitlines() if part.strip()), "")


def citation_body(citation: Dict[str, Any]) -> str:
    """The citation's body text, in whatever text type it arrived."""
    return _content_text((citation.get("display") or {}).get("body"))


@functools.lru_cache(maxsize=512)
def short_link_target(url: str, timeout_seconds: float = 3.0) -> Optional[str]:
    """Where a Moveworks short link (``https://app.moveworks.ai/r?...``)
    redirects, without following it; None for any other URL or on failure.

    Only Moveworks short links are requested, so a message cannot make this
    process fetch arbitrary URLs.
    """
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or parsed.path != "/r" or not (host == "moveworks.ai" or host.endswith(".moveworks.ai")):
        return None
    try:
        reply = requests.get(url, allow_redirects=False, timeout=timeout_seconds, stream=True)
        reply.close()
    except requests.RequestException:
        return None
    return reply.headers.get("location") if reply.is_redirect else None


def form_link_citations(
    message: Dict[str, Any],
    resolve: Optional[Callable[[str], Optional[str]]] = None,
) -> Dict[int, Dict[str, Any]]:
    """Pair ``URL_ACTION``s that open a cited form with that citation.

    Args:
        message: A message object.
        resolve: Maps a short link to its redirect target. Defaults to
            ``short_link_target``, which makes a network request.

    Returns:
        ``{index in message["actions"]: citation}`` for each paired action.
    """
    by_url = {c.get("url"): c for c in message.get("citations") or [] if c.get("url")}
    if not by_url:
        return {}
    resolve = resolve or short_link_target
    paired: Dict[int, Dict[str, Any]] = {}
    for i, action in enumerate(message.get("actions") or []):
        url = (action.get("url_action") or {}).get("url") if action.get("type") == "URL_ACTION" else None
        if not url:
            continue
        citation = by_url.get(url) or by_url.get(resolve(url) or "")
        if citation:
            paired[i] = citation
    return paired


def _matching_citation(match: "re.Match[str]", citations: Any) -> Optional[str]:
    title = match.group("title").strip()
    body = match.group("body")
    first_line = body.split("\n", 1)[0].strip()
    for citation in citations:
        if citation_label(citation) != title or citation_summary(citation) != first_line:
            continue
        if " ".join(body.split()) in " ".join(citation_body(citation).split()):
            return citation.get("citation_id")
    return None


def _paragraph_end(text: str, offset: int) -> int:
    end = text.find("\n\n", offset)
    return len(text) if end == -1 else end


def _message_text(message: Dict[str, Any]) -> str:
    return _content_text(message.get("content"))


def _content_text(content: Any) -> str:
    """Text of a Content union: plain, CommonMark, or markdown, per its ``type``.

    Citation titles and bodies often omit ``type``; those carry ``markdown_text``.
    """
    if not isinstance(content, dict):
        return ""
    body = content.get((content.get("type") or "").lower()) or content.get("markdown_text") or {}
    text = body.get("text") if isinstance(body, dict) else None
    return text if isinstance(text, str) else ""


_EMOJI = {
    "white_check_mark": "✅", "heavy_check_mark": "✔️", "x": "❌", "warning": "⚠️",
    "hourglass": "⏳", "hourglass_flowing_sand": "⏳", "thumbsup": "👍", "thumbsdown": "👎",
    "tada": "🎉", "rocket": "🚀", "point_right": "👉", "bulb": "💡", "lock": "🔒",
    "bell": "🔔", "memo": "📝", "link": "🔗", "smile": "😄", "wave": "👋", "eyes": "👀",
}
_WEB_LINK = re.compile(r"<(https?:[^|>\s]+)\|([^>]+)>")
_OTHER_LINK = re.compile(r"<[^|<>\s]+\|([^<>]+)>")
_BARE_LINK = re.compile(r"<(https?:[^>\s]+)>")
_SHORTCODE = re.compile(r":([a-z0-9_+-]+):")
_SPACE_BEFORE_PUNCTUATION = re.compile(r"[ \t]+([,.;:!?])(?=\s|$)")


def normalize_markup(text: str) -> str:
    """Convert the Slack markup found in API text fields to CommonMark."""
    text = _WEB_LINK.sub(r"[\2](\1)", text or "")
    text = _OTHER_LINK.sub(r"\1", text)
    text = _BARE_LINK.sub(r"\1", text)
    text = _SHORTCODE.sub(lambda m: _EMOJI.get(m.group(1), m.group(0)), text)
    return _SPACE_BEFORE_PUNCTUATION.sub(r"\1", text)


def clean_label(text: str) -> str:
    """``normalize_markup`` for button labels and titles: one line, and no
    unmapped shortcodes (a button must never read ``:heavy_check_mark:``)."""
    return " ".join(_SHORTCODE.sub("", normalize_markup(text)).split())


_HTML_BREAK = re.compile(r"<br\s*/?>\s*", re.IGNORECASE)
_HTML_ANCHOR = re.compile(r"<a\s+[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL)
_HTML_BOLD = re.compile(r"</?(b|strong)>", re.IGNORECASE)
_HTML_ITALIC = re.compile(r"</?(i|em)>", re.IGNORECASE)
_HTML_PARAGRAPH = re.compile(r"</?p>", re.IGNORECASE)
_HTML_ANY_TAG = re.compile(r"<[^>]+>")


def modal_html_to_markdown(text: str) -> str:
    """Convert a modal DISPLAY field's HTML to Markdown, dropping unsafe tags."""
    text = _HTML_BREAK.sub("\n", text or "")
    text = _HTML_ANCHOR.sub(r"[\2](\1)", text)
    text = _HTML_BOLD.sub("**", text)
    text = _HTML_ITALIC.sub("*", text)
    text = _HTML_PARAGRAPH.sub("\n", text)
    return _HTML_ANY_TAG.sub("", text)
