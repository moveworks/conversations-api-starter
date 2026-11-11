"""TwiML responses and Twilio request signatures, without the Twilio SDK.

The bridge answers each Twilio webhook with a small TwiML document. Building
it with ``xml.etree`` escapes the assistant's text, so a reply that contains
``<`` or ``&`` cannot change the document. The official ``twilio`` package
offers the same (``twilio.twiml.voice_response.VoiceResponse`` and
``twilio.request_validator.RequestValidator``) if you prefer it.

Verbs used here: ``<Say>`` (text-to-speech), ``<Gather input="speech">``
(speech-to-text, posted to ``action`` as ``SpeechResult``), ``<Pause>``,
``<Redirect>``, ``<Dial>``, and ``<Hangup>``. See
https://www.twilio.com/docs/voice/twiml.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Mapping, Optional
from xml.etree import ElementTree as ET


class TwiML:
    """A ``<Response>`` document, built verb by verb."""

    def __init__(self, *, voice: Optional[str] = None, language: str = "en-US") -> None:
        self._root = ET.Element("Response")
        self._voice = voice
        self._language = language

    def say(self, text: str, *, parent: Optional[ET.Element] = None) -> "TwiML":
        """Read ``text`` aloud. Empty text adds nothing."""
        if text:
            element = ET.SubElement(self._root if parent is None else parent, "Say", self._say_attributes())
            element.text = text
        return self

    def gather(
        self,
        *,
        action: str,
        prompt: str = "",
        timeout_seconds: int = 5,
        hints: str = "",
    ) -> "TwiML":
        """Listen for speech and post the transcript to ``action``.

        ``prompt`` is read inside the ``<Gather>``, so Twilio is already
        listening when it finishes. ``speechTimeout="auto"`` ends the
        utterance at the caller's first pause. With no speech before
        ``timeout_seconds`` after the prompt, Twilio moves on to the next verb
        in the document, so follow this with a ``redirect`` or ``say``.
        """
        attributes = {
            "input": "speech",
            "action": action,
            "method": "POST",
            "language": self._language,
            "speechTimeout": "auto",
            "timeout": str(timeout_seconds),
        }
        if hints:
            attributes["hints"] = hints
        element = ET.SubElement(self._root, "Gather", attributes)
        self.say(prompt, parent=element)
        return self

    def pause(self, seconds: int) -> "TwiML":
        ET.SubElement(self._root, "Pause", {"length": str(seconds)})
        return self

    def redirect(self, url: str) -> "TwiML":
        """Ask Twilio to fetch the next TwiML from ``url``."""
        ET.SubElement(self._root, "Redirect", {"method": "POST"}).text = url
        return self

    def dial(self, number: str) -> "TwiML":
        """Transfer the call to ``number``."""
        ET.SubElement(self._root, "Dial").text = number
        return self

    def hangup(self) -> "TwiML":
        ET.SubElement(self._root, "Hangup")
        return self

    def to_xml(self) -> str:
        return '<?xml version="1.0" encoding="UTF-8"?>' + ET.tostring(self._root, encoding="unicode")

    def _say_attributes(self) -> dict:
        attributes = {"language": self._language}
        if self._voice:
            attributes["voice"] = self._voice
        return attributes


def twilio_signature(auth_token: str, url: str, params: Mapping[str, str]) -> str:
    """The ``X-Twilio-Signature`` Twilio sends for a form-encoded POST to ``url``.

    The full URL Twilio requested (scheme, host, path, and query string),
    followed by each POST parameter name and value, sorted by name, signed
    with HMAC-SHA1 using the account's auth token, then base64-encoded. See
    https://www.twilio.com/docs/usage/webhooks/webhooks-security.
    """
    payload = url + "".join(f"{name}{params[name]}" for name in sorted(params))
    digest = hmac.new(auth_token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def is_valid_signature(auth_token: str, url: str, params: Mapping[str, str], signature: Optional[str]) -> bool:
    """Whether ``signature`` is the one Twilio would send for this request."""
    if not signature:
        return False
    return hmac.compare_digest(twilio_signature(auth_token, url, params), signature)
