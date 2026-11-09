"""Turn the bearer an MCP client forwards into a Moveworks bearer for the same user.

A remote MCP client that signs users in with OAuth sends the end user's token as
``Authorization: Bearer`` on every request (a hosting platform's own headers, such as
Cloud Run's ``X-Serverless-Authorization``, are checked by the platform, not here). Every Conversations API call must run as a roster user,
so each authenticator below answers one question: who is this, and what
Moveworks bearer acts as them?

- ``GoogleS2SAuthenticator``: the client's OAuth points at Google. The token is
  verified with Google's tokeninfo endpoint (audience must be your OAuth
  client), and the server signs a self-signed JWT with ``sub`` set to the
  verified email. The server holds a credential that can act as any user, so the audience
  and email-domain checks are the security boundary.
- ``IdpExchangeAuthenticator``: the client's OAuth points at your Okta or
  Entra. The token is exchanged at Moveworks ``/oauth/v1/token``; Moveworks
  validates it against the credential registered for that IdP.
- ``DevStaticAuthenticator``: local testing only. Ignores the header and acts
  as one configured user.
- ``DevStaticMapAuthenticator``: local testing only. ``Bearer dev:<email>`` acts
  as that roster user, so one process can serve two users and the 401 path is
  exercised over HTTP. Never deploy it: anyone can claim any email.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Mapping, Optional, Protocol, Tuple

import requests

from moveworks_capi.auth import S2SJwtBearer
from moveworks_capi.errors import AuthenticationError

from .config import Settings

GOOGLE_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"


class Unauthenticated(Exception):
    """The caller did not present a usable end-user token."""


@dataclass(frozen=True)
class UserIdentity:
    user_key: str
    """Stable per-user key; conversation state is scoped to it."""
    bearer_provider: Callable[[], str]


class Authenticator(Protocol):
    def authenticate(self, headers: Mapping[str, str]) -> UserIdentity: ...


def bearer_from_headers(headers: Mapping[str, str]) -> str:
    lowered = {key.lower(): value for key, value in headers.items()}
    value = lowered.get("authorization", "")
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise Unauthenticated("missing end-user bearer token")
    return token.strip()


class GoogleS2SAuthenticator:
    def __init__(self, settings: Settings, *, http: Optional[requests.Session] = None) -> None:
        self._settings = settings
        self._http = http or requests.Session()
        self._lock = threading.Lock()
        self._verified: Dict[str, Tuple[str, float]] = {}
        self._bearers: Dict[str, S2SJwtBearer] = {}

    def authenticate(self, headers: Mapping[str, str]) -> UserIdentity:
        token = bearer_from_headers(headers)
        email = self._verified_email(token)
        with self._lock:
            bearer = self._bearers.get(email)
            if bearer is None:
                bearer = S2SJwtBearer(
                    self._settings.mw_base_url,
                    self._settings.s2s_issuer or "",
                    self._settings.s2s_audience or "",
                    self._settings.s2s_key_id or "",
                    self._settings.s2s_private_key or "",
                    email,
                )
                self._bearers[email] = bearer
        return UserIdentity(user_key=email, bearer_provider=bearer)

    def _verified_email(self, token: str) -> str:
        key = _digest(token)
        now = time.time()
        with self._lock:
            cached = self._verified.get(key)
            if cached and cached[1] > now:
                return cached[0]
        try:
            response = self._http.get(GOOGLE_TOKENINFO_URL, params={"access_token": token}, timeout=10.0)
        except requests.RequestException as exc:
            raise Unauthenticated(f"could not verify Google token: {exc}") from exc
        if response.status_code != 200:
            raise Unauthenticated("Google rejected the token")
        info = response.json()
        audiences = {info.get("aud"), info.get("azp")}
        if not audiences & self._settings.google_client_ids:
            raise Unauthenticated("token was not issued to this server's OAuth client")
        email = str(info.get("email") or "").lower()
        if not email or str(info.get("email_verified")).lower() != "true":
            raise Unauthenticated("token carries no verified email; request the 'email' scope")
        domains = self._settings.allowed_email_domains
        if domains and email.rpartition("@")[2] not in domains:
            raise Unauthenticated("email domain is not allowed")
        expires_in = float(info.get("expires_in") or 0)
        with self._lock:
            self._verified[key] = (email, now + min(expires_in, 300.0))
        return email


class _ExchangedBearer:
    """Bearer provider over one IdP token; refreshing needs a new IdP token from the client."""

    def __init__(self, token: str, expires_at: float) -> None:
        self._token = token
        self._expires_at = expires_at

    def __call__(self) -> str:
        if time.time() >= self._expires_at:
            raise AuthenticationError("Moveworks bearer expired; the user must re-authorize")
        return self._token

    def invalidate(self) -> None:
        self._expires_at = 0.0

    def usable(self, now: float) -> bool:
        return self._expires_at - 60 > now


class IdpExchangeAuthenticator:
    def __init__(self, settings: Settings, *, http: Optional[requests.Session] = None) -> None:
        self._token_url = settings.mw_base_url.rstrip("/") + "/oauth/v1/token"
        self._http = http or requests.Session()
        self._lock = threading.Lock()
        self._cache: Dict[str, Tuple[str, _ExchangedBearer, float]] = {}

    def authenticate(self, headers: Mapping[str, str]) -> UserIdentity:
        idp_token = bearer_from_headers(headers)
        key = _digest(idp_token)
        now = time.time()
        with self._lock:
            cached = self._cache.get(key)
            # An invalidated bearer (Moveworks answered 401) forces a fresh exchange, or a 401 to the client.
            if cached and cached[1].usable(now):
                return UserIdentity(user_key=cached[0], bearer_provider=cached[1])
        try:
            response = self._http.post(
                self._token_url,
                data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": idp_token},
                timeout=15.0,
            )
        except requests.RequestException as exc:
            raise Unauthenticated(f"Moveworks token exchange failed: {exc}") from exc
        if response.status_code >= 400:
            raise Unauthenticated(f"Moveworks token exchange returned {response.status_code}")
        body = response.json()
        expires_at = now + float(body.get("expires_in") or 0)
        bearer = _ExchangedBearer(body["access_token"], expires_at)
        # Moveworks accepted the token, so its claims are genuine; use them to keep
        # conversation state stable across the IdP token refreshing.
        user_key = _subject_claim(idp_token) or key
        with self._lock:
            self._cache[key] = (user_key, bearer, expires_at)
        return UserIdentity(user_key=user_key, bearer_provider=bearer)


class DevStaticAuthenticator:
    def __init__(self, settings: Settings) -> None:
        email = settings.dev_user_email or ""
        self._identity = UserIdentity(
            user_key=email,
            bearer_provider=S2SJwtBearer(
                settings.mw_base_url,
                settings.s2s_issuer or "",
                settings.s2s_audience or "",
                settings.s2s_key_id or "",
                settings.s2s_private_key or "",
                email,
            ),
        )

    def authenticate(self, headers: Mapping[str, str]) -> UserIdentity:
        return self._identity


class DevStaticMapAuthenticator:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = threading.Lock()
        self._bearers: Dict[str, S2SJwtBearer] = {}

    def authenticate(self, headers: Mapping[str, str]) -> UserIdentity:
        token = bearer_from_headers(headers)
        prefix, _, email = token.partition(":")
        email = email.strip().lower()
        if prefix != "dev" or "@" not in email:
            raise Unauthenticated("dev_static_map expects 'Bearer dev:<email>'")
        with self._lock:
            bearer = self._bearers.get(email)
            if bearer is None:
                bearer = S2SJwtBearer(
                    self._settings.mw_base_url,
                    self._settings.s2s_issuer or "",
                    self._settings.s2s_audience or "",
                    self._settings.s2s_key_id or "",
                    self._settings.s2s_private_key or "",
                    email,
                )
                self._bearers[email] = bearer
        return UserIdentity(user_key=email, bearer_provider=bearer)


def build_authenticator(settings: Settings) -> Authenticator:
    if settings.auth_mode == "demo_oauth":
        from .demo_oauth import DemoOAuthAuthenticator

        return DemoOAuthAuthenticator(settings)
    if settings.auth_mode == "dev_static_map":
        return DevStaticMapAuthenticator(settings)
    if settings.auth_mode == "google_s2s":
        return GoogleS2SAuthenticator(settings)
    if settings.auth_mode == "idp_exchange":
        return IdpExchangeAuthenticator(settings)
    return DevStaticAuthenticator(settings)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _subject_claim(token: str) -> Optional[str]:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, json.JSONDecodeError):
        return None
    for name in ("email", "upn", "preferred_username", "oid", "sub"):
        value = claims.get(name)
        if isinstance(value, str) and value:
            return value.lower()
    return None
