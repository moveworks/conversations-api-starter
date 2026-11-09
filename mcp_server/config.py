"""Settings, read once from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import FrozenSet, Optional, Tuple
from urllib.parse import urlparse

AUTH_MODES = ("google_s2s", "idp_exchange", "dev_static", "dev_static_map", "demo_oauth")
LOCAL_ONLY_AUTH_MODES = ("dev_static", "dev_static_map", "demo_oauth")


@dataclass(frozen=True)
class Settings:
    mw_base_url: str
    mw_bot_name: str
    auth_mode: str
    s2s_issuer: Optional[str] = None
    s2s_audience: Optional[str] = None
    s2s_key_id: Optional[str] = None
    s2s_private_key: Optional[str] = None
    google_client_ids: FrozenSet[str] = field(default_factory=frozenset)
    allowed_email_domains: FrozenSet[str] = field(default_factory=frozenset)
    dev_user_email: Optional[str] = None
    turn_timeout_seconds: float = 120.0
    modal_outcome_wait_seconds: float = 45.0
    tool_deadline_seconds: float = 25.0
    """How long a tool call waits before returning ``pending``."""
    conversation_idle_minutes: float = 60.0
    require_token_for_listing: bool = False
    """False lets ``initialize`` / ``tools/list`` through without a token (logged), to see what a client sends while it registers."""
    allowed_hosts: FrozenSet[str] = field(default_factory=frozenset)
    """Public Host values (e.g. ``abc.run.app``). Empty keeps the SDK's localhost-only protection."""
    public_base_url: Optional[str] = None
    """Public HTTPS origin, used in OAuth discovery metadata (``demo_oauth``, or ``MCP_OAUTH_ISSUER``)."""
    oauth_issuer: Optional[str] = None
    """Your identity provider's issuer, named in discovery metadata for ``idp_exchange`` and ``google_s2s``."""
    oauth_scopes: Tuple[str, ...] = ()
    """Scopes listed in discovery metadata, so clients request what the Moveworks credential expects."""
    demo_oauth_client_id: Optional[str] = None
    demo_oauth_client_secret: Optional[str] = None
    demo_oauth_passphrase: Optional[str] = None
    demo_oauth_signing_key: Optional[str] = None
    host: str = "127.0.0.1"
    port: int = 8791

    @classmethod
    def from_env(cls) -> "Settings":
        auth_mode = os.environ.get("MCP_AUTH_MODE", "idp_exchange")
        if auth_mode not in AUTH_MODES:
            raise ValueError(f"MCP_AUTH_MODE must be one of {AUTH_MODES}, got {auth_mode!r}")
        settings = cls(
            mw_base_url=_required("MW_BASE_URL"),
            mw_bot_name=_required("MW_BOT_NAME"),
            auth_mode=auth_mode,
            s2s_issuer=os.environ.get("MW_S2S_ISSUER"),
            s2s_audience=os.environ.get("MW_S2S_AUDIENCE"),
            s2s_key_id=os.environ.get("MW_S2S_KEY_ID"),
            s2s_private_key=os.environ.get("MW_S2S_PRIVATE_KEY") or _read(os.environ.get("MW_S2S_PRIVATE_KEY_PATH")),
            google_client_ids=_csv("MCP_GOOGLE_OAUTH_CLIENT_IDS", lower=False),
            allowed_email_domains=_csv("MCP_ALLOWED_EMAIL_DOMAINS"),
            dev_user_email=os.environ.get("MCP_DEV_USER_EMAIL"),
            turn_timeout_seconds=float(os.environ.get("MCP_TURN_TIMEOUT_SECONDS", "120")),
            modal_outcome_wait_seconds=float(os.environ.get("MCP_MODAL_OUTCOME_WAIT_SECONDS", "45")),
            tool_deadline_seconds=float(os.environ.get("MCP_TOOL_DEADLINE_SECONDS", "25")),
            conversation_idle_minutes=float(os.environ.get("MCP_CONVERSATION_IDLE_MINUTES", "60")),
            require_token_for_listing=_flag("MCP_REQUIRE_TOKEN_FOR_LISTING"),
            allowed_hosts=_csv("MCP_ALLOWED_HOSTS"),
            public_base_url=(os.environ.get("MCP_PUBLIC_BASE_URL") or "").rstrip("/") or None,
            oauth_issuer=os.environ.get("MCP_OAUTH_ISSUER") or None,  # kept exact: clients match it character for character
            oauth_scopes=tuple(os.environ.get("MCP_OAUTH_SCOPES", "").split()),
            demo_oauth_client_id=os.environ.get("MCP_DEMO_OAUTH_CLIENT_ID"),
            demo_oauth_client_secret=os.environ.get("MCP_DEMO_OAUTH_CLIENT_SECRET"),
            demo_oauth_passphrase=os.environ.get("MCP_DEMO_OAUTH_PASSPHRASE"),
            demo_oauth_signing_key=os.environ.get("MCP_DEMO_OAUTH_SIGNING_KEY"),
            host=os.environ.get("MCP_HOST", "127.0.0.1"),
            port=int(os.environ.get("MCP_PORT", "8791")),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.auth_mode in ("google_s2s", "dev_static", "dev_static_map", "demo_oauth"):
            missing = [
                name
                for name, value in (
                    ("MW_S2S_ISSUER", self.s2s_issuer),
                    ("MW_S2S_AUDIENCE", self.s2s_audience),
                    ("MW_S2S_KEY_ID", self.s2s_key_id),
                    ("MW_S2S_PRIVATE_KEY or MW_S2S_PRIVATE_KEY_PATH", self.s2s_private_key),
                )
                if not value
            ]
            if missing:
                raise ValueError(f"MCP_AUTH_MODE={self.auth_mode} needs {', '.join(missing)}")
        if self.auth_mode == "google_s2s" and not self.google_client_ids:
            # Without an audience check, any Google token minted for any app would be accepted.
            raise ValueError("MCP_AUTH_MODE=google_s2s needs MCP_GOOGLE_OAUTH_CLIENT_IDS")
        if self.auth_mode == "google_s2s" and not self.allowed_email_domains:
            # The credential can act as any roster user, so limit which Google accounts reach it.
            raise ValueError("MCP_AUTH_MODE=google_s2s needs MCP_ALLOWED_EMAIL_DOMAINS")
        if self.auth_mode in ("dev_static", "demo_oauth") and not self.dev_user_email:
            raise ValueError(f"MCP_AUTH_MODE={self.auth_mode} needs MCP_DEV_USER_EMAIL")
        if self.auth_mode in LOCAL_ONLY_AUTH_MODES and self.host not in ("127.0.0.1", "localhost"):
            raise ValueError(f"MCP_AUTH_MODE={self.auth_mode} is for local testing only; refusing to bind {self.host}")
        if self.auth_mode in ("dev_static", "dev_static_map") and (self.allowed_hosts or self.public_base_url):
            # Behind a tunnel, anyone could send "Bearer dev:<any roster email>".
            raise ValueError(f"MCP_AUTH_MODE={self.auth_mode} can't be exposed through a public host name")
        if self.oauth_issuer:
            if self.auth_mode not in ("idp_exchange", "google_s2s"):
                raise ValueError("MCP_OAUTH_ISSUER applies only to MCP_AUTH_MODE=idp_exchange or google_s2s")
            if not self.oauth_issuer.startswith("https://"):
                raise ValueError("MCP_OAUTH_ISSUER must be an https URL")
            base = urlparse(self.public_base_url or "")
            if base.scheme != "https" or not base.netloc or base.path not in ("", "/"):
                raise ValueError("MCP_OAUTH_ISSUER needs MCP_PUBLIC_BASE_URL, the server's public https origin with no path")
        if self.oauth_scopes and not self.oauth_issuer:
            raise ValueError("MCP_OAUTH_SCOPES needs MCP_OAUTH_ISSUER")
        if self.auth_mode == "demo_oauth":
            missing = [
                name
                for name, value in (
                    ("MCP_PUBLIC_BASE_URL", self.public_base_url),
                    ("MCP_DEMO_OAUTH_CLIENT_ID", self.demo_oauth_client_id),
                    ("MCP_DEMO_OAUTH_CLIENT_SECRET", self.demo_oauth_client_secret),
                    ("MCP_DEMO_OAUTH_PASSPHRASE", self.demo_oauth_passphrase),
                    ("MCP_DEMO_OAUTH_SIGNING_KEY", self.demo_oauth_signing_key),
                )
                if not value
            ]
            if missing:
                raise ValueError(f"MCP_AUTH_MODE=demo_oauth needs {', '.join(missing)}")


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def _csv(name: str, *, lower: bool = True) -> FrozenSet[str]:
    parts = (part.strip() for part in os.environ.get(name, "").split(","))
    return frozenset(part.lower() if lower else part for part in parts if part)


def _read(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    with open(path, encoding="utf-8") as handle:
        return handle.read()
