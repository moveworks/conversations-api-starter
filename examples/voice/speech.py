"""Turn an assistant reply into text a phone voice can read aloud.

Assistant replies are written for a screen: Markdown, links, bullet lists,
emoji, and forms. On a call, a link or a table read aloud is noise, and a
four-paragraph answer runs for minutes. This module:

- converts a message to plain sentences (:func:`speakable`), using
  ``moveworks_capi.rendering`` for the API's Slack-style markup first;
- splits long text into chunks of about 30 to 40 seconds of speech
  (:func:`chunk_for_speech`), so the caller hears the first part and can ask
  for more instead of waiting through all of it;
- recognizes the short phrases a caller uses to steer playback
  (:func:`wants_more`, :func:`wants_repeat`, :func:`wants_agent`) and spoken
  button labels (:func:`match_option`).
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, List, Mapping, Optional, Sequence

from moveworks_capi.rendering import normalize_markup, strip_flattened_forms

MAX_SPEECH_CHARS = 600
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
_KEEP_SYMBOLS = frozenset("°©®™")
_NOT_LETTERS = re.compile(r"[^a-z0-9' ]+")

_MORE = ("more", "continue", "keep going", "go on", "go ahead", "next", "yes", "yeah", "yes please", "sure", "tell me more")
_REPEAT = ("repeat", "repeat that", "say that again", "again", "what", "pardon", "come again", "sorry what")
_AGENT = ("agent", "person", "human", "representative", "operator", "someone", "live agent", "real person", "human agent")


def reply_text(messages: Sequence[Mapping[str, Any]]) -> str:
    """The speakable text of one or more assistant messages, in order.

    Each message is a message object (a completed response's
    ``outputs[].message`` or a history entry). The plain-text copy of offered
    forms at the end of a message is removed first: the form itself cannot be
    used on a call, so reading its title and description adds nothing.
    """
    parts = [speakable(strip_flattened_forms(dict(message))) for message in messages]
    return " ".join(part for part in parts if part)


def speakable(text: str) -> str:
    """Plain sentences from Markdown or Slack-style text, for text-to-speech.

    Links keep their label and lose the URL; a bare URL becomes "the link".
    List items and table rows become sentences. Emoji are dropped, since a
    voice reads them by name ("white heavy check mark").
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


def chunk_for_speech(text: str, max_chars: int = MAX_SPEECH_CHARS) -> List[str]:
    """Split text into chunks of at most ``max_chars``, at sentence ends.

    A single sentence longer than ``max_chars`` is split at the last space
    before the limit.
    """
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


def wants_more(utterance: str) -> bool:
    """The caller asked to hear the rest of a long answer."""
    return _normalize(utterance) in _MORE


def wants_repeat(utterance: str) -> bool:
    """The caller asked to hear the last part again."""
    return _normalize(utterance) in _REPEAT


def wants_agent(utterance: str) -> bool:
    """The caller asked for a person, as a whole utterance ("agent", "talk to a human")."""
    words = _normalize(utterance)
    for prefix in ("i want ", "i need ", "let me talk to ", "talk to ", "speak to ", "speak with ", "transfer me to "):
        if words.startswith(prefix):
            words = words[len(prefix):]
            break
    for article in ("a ", "an ", "the "):
        if words.startswith(article):
            words = words[len(article):]
            break
    return words in _AGENT


def match_option(utterance: str, options: Mapping[str, str]) -> Optional[str]:
    """The value of the option whose label the caller said, if exactly one matches.

    ``options`` maps a button label to its value, such as a ``callback_id``.
    A label matches when the caller said it as whole words: "approve it"
    matches "Approve", and "I don't know" does not match "No".
    """
    said = f" {_normalize(utterance)} "
    hits: Dict[str, str] = {}
    for label, value in options.items():
        words = _normalize(label)
        if words and f" {words} " in said:
            hits[label] = value
    return next(iter(hits.values())) if len(hits) == 1 else None


def _normalize(text: str) -> str:
    return " ".join(_NOT_LETTERS.sub(" ", (text or "").lower()).split())


def _drop_emoji(text: str) -> str:
    return "".join(
        ch for ch in text
        if ch in _KEEP_SYMBOLS or (unicodedata.category(ch) not in ("So", "Cs") and ch not in "️‍")
    )
