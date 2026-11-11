"""Every knob for tuning the voice harness against the Moveworks orchestrator harness.

The two harnesses are tuned together: the voice harness owns pacing and
delivery, and the orchestrator owns completeness and structure. This module
holds the voice side.

Prompts live as plain text in ``prompts/`` so they can be edited and diffed
without touching code (point ``VOICE_PROMPTS_DIR`` at a copy to keep your own):

- ``voice_instructions.md``: the voice model's system prompt. Persona, what it
  answers itself versus hands to the orchestrator, and how it delivers results.
- ``delegation.md``: the ``ask_orchestrator`` tool description, which the
  model reads when deciding whether to delegate.
- ``welcome.md``: how the call opens.
- ``warmup.md``: the turn sent to the orchestrator when the call connects, so
  the voice model knows who it is talking to. Empty turns warm-up off.
- ``filler.md`` and ``progress.md``: what the voice model says while the
  orchestrator works. ``{query}`` and ``{status}`` are filled in.

Timing knobs (milliseconds) cover the orchestrator's 5 to 20 second turns:
the first filler, then further updates no closer than ``progress_min_ms``
apart. An update fires early only when the orchestrator reports new activity;
otherwise a brief reassurance comes by ``progress_max_ms``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

PROMPTS_DIR = Path(__file__).parent / "prompts"
PROMPT_FILES = {
    "voice_instructions": "voice_instructions.md",
    "delegation": "delegation.md",
    "welcome": "welcome.md",
    "warmup": "warmup.md",
    "filler": "filler.md",
    "progress": "progress.md",
}


@dataclass(frozen=True)
class Tuning:
    """The voice harness's settings. See the module docstring and the README."""

    voice_instructions: str = ""
    delegation: str = ""
    welcome: str = ""
    warmup: str = ""
    filler: str = ""
    progress: str = ""

    model: str = "gpt-realtime-2.1"
    voice: str = "marin"
    speed: float = 1.0
    vad_eagerness: str = "medium"
    noise_reduction: str = "near_field"
    transcription_model: str = "gpt-4o-mini-transcribe"
    glossary: str = ""

    upfront_filler_ms: Dict[str, int] = field(default_factory=lambda: {"quick": 2500, "long": 1500})
    progress_min_ms: int = 5000
    progress_max_ms: int = 9000
    cancel_lookup_on_barge_in: bool = True
    warmup_wait_ms: int = 8000

    def client_view(self) -> Dict[str, Any]:
        """What the browser needs to run the voice harness loop. No secrets."""
        return {
            "welcome": self.welcome,
            "warmup": bool(self.warmup),
            "filler": self.filler,
            "progress": self.progress,
            "upfront_filler_ms": dict(self.upfront_filler_ms),
            "progress_min_ms": self.progress_min_ms,
            "progress_max_ms": self.progress_max_ms,
            "cancel_lookup_on_barge_in": self.cancel_lookup_on_barge_in,
            "warmup_wait_ms": self.warmup_wait_ms,
        }

    @classmethod
    def load(cls, prompts_dir: Path = PROMPTS_DIR, env: Optional[Mapping[str, str]] = None) -> "Tuning":
        """Read the prompts from ``prompts_dir`` and the knobs from ``env`` (default ``os.environ``)."""
        env = os.environ if env is None else env
        prompts_dir = Path(env.get("VOICE_PROMPTS_DIR") or prompts_dir)
        prompt = {name: (prompts_dir / filename).read_text().strip() for name, filename in PROMPT_FILES.items()}
        tuning = cls(
            voice_instructions=prompt["voice_instructions"],
            delegation=prompt["delegation"],
            welcome=prompt["welcome"],
            warmup=prompt["warmup"],
            filler=prompt["filler"],
            progress=prompt["progress"],
        )
        if env.get("VOICE_WARMUP", "").strip().lower() in ("0", "false", "no", "off"):
            tuning = replace(tuning, warmup="")
        return replace(
            tuning,
            model=env.get("OPENAI_REALTIME_MODEL") or tuning.model,
            voice=env.get("VOICE_REALTIME_VOICE") or tuning.voice,
            speed=float(env.get("VOICE_SPEED") or tuning.speed),
            vad_eagerness=env.get("VOICE_VAD_EAGERNESS") or tuning.vad_eagerness,
            glossary=env.get("VOICE_GLOSSARY", tuning.glossary),
            progress_min_ms=int(env.get("VOICE_PROGRESS_MIN_MS") or tuning.progress_min_ms),
            progress_max_ms=int(env.get("VOICE_PROGRESS_MAX_MS") or tuning.progress_max_ms),
            cancel_lookup_on_barge_in=_flag(env.get("VOICE_CANCEL_ON_BARGE_IN"), tuning.cancel_lookup_on_barge_in),
        )


def _flag(value: Optional[str], default: bool) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")
