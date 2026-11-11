"""The voice harness's model: an OpenAI Realtime speech-to-speech session.

One model hears the caller, decides when they've finished, speaks, and stops
speaking when they talk over it. No separate speech-to-text or text-to-speech
step. It answers small talk itself and calls the ``ask_orchestrator`` tool for
anything that needs the company's systems.

The browser connects to the model directly over WebRTC with a short-lived
client secret that this server mints, so the OpenAI API key never leaves the
server. See https://platform.openai.com/docs/guides/realtime.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import requests

from .tuning import Tuning

OPENAI_BASE_URL = "https://api.openai.com"
CLIENT_SECRETS_PATH = "/v1/realtime/client_secrets"
CALLS_URL = OPENAI_BASE_URL + "/v1/realtime/calls"
"""Where the browser posts its WebRTC offer, authorized with the client secret."""

TOOL_NAME = "ask_orchestrator"


class RealtimeError(Exception):
    """OpenAI refused to mint a client secret."""


def ask_orchestrator_tool(tuning: Tuning) -> Dict[str, Any]:
    """The function the voice model calls to hand a request to the orchestrator."""
    return {
        "type": "function",
        "name": TOOL_NAME,
        "description": tuning.delegation,
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "The caller's request as one self-contained, first-person sentence, "
                        "with names, dates, and pronouns resolved from the conversation."
                    ),
                },
                "choice": {
                    "type": "string",
                    "description": "When the caller picks one of the buttons in the last result, that button's exact label.",
                },
                "expected_duration": {
                    "type": "string",
                    "enum": ["quick", "long"],
                    "description": "quick for a single lookup; long for a search across systems or an action.",
                },
            },
            "required": ["query"],
        },
    }


def session_config(tuning: Tuning) -> Dict[str, Any]:
    """The Realtime session: the voice harness's prompt, voice, turn-taking, and tool."""
    transcription: Dict[str, Any] = {"model": tuning.transcription_model}
    instructions = tuning.voice_instructions
    if tuning.glossary:
        # The model has no custom vocabulary list. Bias the caller transcript with a
        # prompt, and tell the voice model how the terms are said.
        transcription["prompt"] = tuning.glossary
        instructions += f"\n\n# Company terms\nRecognize and pronounce these as written: {tuning.glossary}"
    audio_input: Dict[str, Any] = {
        "transcription": transcription,
        "turn_detection": {
            "type": "semantic_vad",
            "eagerness": tuning.vad_eagerness,
            "create_response": True,
            "interrupt_response": True,
        },
    }
    if tuning.noise_reduction:
        audio_input["noise_reduction"] = {"type": tuning.noise_reduction}
    return {
        "type": "realtime",
        "model": tuning.model,
        "instructions": instructions,
        "audio": {
            "input": audio_input,
            "output": {"voice": tuning.voice, "speed": tuning.speed},
        },
        "tools": [ask_orchestrator_tool(tuning)],
        "tool_choice": "auto",
    }


def create_client_secret(
    api_key: str,
    config: Dict[str, Any],
    *,
    expires_seconds: int = 600,
    http: Optional[requests.Session] = None,
    timeout: float = 15.0,
) -> Dict[str, Any]:
    """Mint a short-lived client secret for one browser session.

    Returns ``{"value": "ek_...", "expires_at": <unix seconds>}``. The secret
    only has to last until the browser connects; the session stays up after.
    """
    sender = http or requests
    response = sender.post(
        OPENAI_BASE_URL + CLIENT_SECRETS_PATH,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"expires_after": {"anchor": "created_at", "seconds": expires_seconds}, "session": config},
        timeout=timeout,
    )
    if response.status_code >= 400:
        raise RealtimeError(f"OpenAI returned {response.status_code} minting a client secret: {response.text[:300]}")
    body = response.json()
    value = body.get("value") if isinstance(body, dict) else None
    if not isinstance(value, str) or not value:
        raise RealtimeError("OpenAI's client secret response had no value")
    return {"value": value, "expires_at": body.get("expires_at")}
