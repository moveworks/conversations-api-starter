"""Prepare the orchestrator's answers for the ear.

Conversations API answers are written for a screen: Markdown, links, lists,
emoji, and forms. The voice model reads whatever the tool returns, so this
module hands it plain sentences, split into sections it can deliver one at a
time:

- :func:`speakable` turns Markdown or Slack-style text into plain sentences,
  using ``moveworks_capi.rendering`` for the API's markup first.
- :func:`sections` splits an answer at its labelled phases, steps, or
  headings, so a how-to guide can be spoken one phase at a time, and falls
  back to chunks of about 40 seconds of speech.
- :func:`answer_text` collects the text of a response's messages without the
  plain-text copy of offered forms.

The voice model decides how to pace and phrase the result; this only removes
what no voice should read and keeps every fact.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, List, Mapping, Sequence

from moveworks_capi.rendering import normalize_markup, strip_flattened_forms

MAX_SECTION_CHARS = 600
"""About 40 seconds of speech at a typical text-to-speech rate."""

_CODE_FENCE = re.compile(r"^\s*```.*$", re.MULTILINE)
_MARKDOWN_LINK = re.compile(r"!?\[([^\]]*)\]\([^)\s]*(?:\s+\"[^\"]*\")?\)")
_URL = re.compile(r"https?://[^\s)>\]]+")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+")
_LIST_MARKER = re.compile(r"^\s*(?:[-*+•]|\d{1,3}[.)])\s+")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)*\|?\s*$")
_EMPHASIS = re.compile(r"\*\*|__|~~|`+|(?<!\w)[*_](?=\S)|(?<=\S)[*_](?!\w)")
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([,.;:!?])")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_TRAILING_CONJUNCTION = re.compile(r"\b(?:and|or|but|then)$", re.IGNORECASE)
_SECTION_START = re.compile(r"^\s*(?:#{1,6}\s+|\**(?:phase|step|part|section|option)\s+\d+\b)", re.IGNORECASE)
_KEEP_SYMBOLS = frozenset("°©®™")


def answer_text(messages: Sequence[Mapping[str, Any]]) -> str:
    """The Markdown text of one or more assistant messages, in order.

    Each message is a message object (a completed response's
    ``outputs[].message``, a stream delta's ``output.message``, or a history
    entry). The plain-text copy of offered forms at the end of a message is
    removed: a form can't be filled in by voice.
    """
    parts = [strip_flattened_forms(dict(message)).strip() for message in messages]
    return "\n\n".join(part for part in parts if part)


def speakable(text: str) -> str:
    """Plain sentences from Markdown or Slack-style text, for text-to-speech.

    Links keep their label and lose the URL; a bare URL becomes "the link".
    List items and table rows become sentences. Emoji are dropped, since a
    voice reads them by name.
    """
    text = normalize_markup(text or "")
    text = _CODE_FENCE.sub("", text)
    text = _MARKDOWN_LINK.sub(lambda m: m.group(1), text)
    text = _URL.sub("the link", text)
    sentences: List[str] = []
    for line in text.splitlines():
        if _TABLE_RULE.match(line):
            continue
        line = _HEADING.sub("", line)
        line = _LIST_MARKER.sub("", line)
        if "|" in line:
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            line = ", ".join(cell for cell in cells if cell)
        line = _EMPHASIS.sub("", line)
        line = _drop_emoji(line)
        line = " ".join(line.split())
        if not line:
            continue
        if line[-1] not in ".!?:;," and not _TRAILING_CONJUNCTION.search(line):
            line += "."
        sentences.append(line)
    return _SPACE_BEFORE_PUNCTUATION.sub(r"\1", " ".join(sentences))


def sections(markdown: str, max_chars: int = MAX_SECTION_CHARS) -> List[str]:
    """Speakable sections of an answer, in order.

    An answer organized into labelled phases, steps, or headings splits at
    them, so the voice model can speak one and pause. Any section longer than
    ``max_chars`` is split further at sentence ends.
    """
    blocks: List[List[str]] = [[]]
    for line in normalize_markup(markdown or "").splitlines():
        if _SECTION_START.match(line) and any(part.strip() for part in blocks[-1]):
            blocks.append([])
        blocks[-1].append(line)
    result: List[str] = []
    for block in blocks:
        text = speakable("\n".join(block))
        if text:
            result.extend(_chunk(text, max_chars))
    return result


def _chunk(text: str, max_chars: int) -> List[str]:
    """Split text into pieces of at most ``max_chars``, at sentence ends where possible."""
    chunks: List[str] = []
    current = ""
    for sentence in _SENTENCE_END.split(text.strip()):
        while len(sentence) > max_chars:
            cut = sentence.rfind(" ", 0, max_chars)
            cut = cut if cut > 0 else max_chars
            if current:
                chunks.append(current)
                current = ""
            chunks.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if not sentence:
            continue
        if current and len(current) + 1 + len(sentence) > max_chars:
            chunks.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        chunks.append(current)
    return chunks


def _drop_emoji(text: str) -> str:
    return "".join(
        ch for ch in text
        if ch in _KEEP_SYMBOLS or (unicodedata.category(ch) not in ("So", "Cs") and ch not in "️‍")
    )
