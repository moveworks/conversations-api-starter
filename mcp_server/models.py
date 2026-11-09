"""Plain data shared by the session and the renderer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class Option:
    label: str
    kind: str
    """``callback``, ``modal``, ``handoff`` or ``url``."""
    callback_id: Optional[str] = None
    modal_id: Optional[str] = None
    url: Optional[str] = None
    response_id: Optional[str] = None
    message_id: Optional[str] = None
    handoff: Optional[Dict[str, Any]] = None
    modal: Optional[Dict[str, Any]] = None
    """The GetModal payload, when it was fetched to name the form."""

    @property
    def is_form(self) -> bool:
        return self.kind == "modal"
