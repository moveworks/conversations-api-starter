"""Self-signed JWT bearer provider for the Conversations API.

Signs a short-lived JWT, exchanges it at ``/oauth/v1/token`` for a Moveworks
bearer, and caches the bearer until near expiration. The whole flow is
described in the docs:

https://docs.moveworks.com/api-reference/conversations-api/set-up-service-to-service-authentication

The values of ``issuer``, ``audience``, and ``key_id`` come from the credential
you registered in the Moveworks Admin Portal. They are values you chose during
setup; the JWT you sign here must match those values exactly.
"""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from typing import Union
from urllib.parse import urljoin

import jwt
import requests

from ..errors import AuthenticationError


class S2SJwtBearer:
    """Callable that returns a cached bearer for a single user.

    Instantiate one per (credential, user) pair. The instance signs a fresh
    JWT and exchanges it whenever the cached bearer is missing or near
    expiration.

    Args:
        base_url: Data-center base URL, e.g. ``https://api.moveworks.ai``.
        issuer: Matches the ``Issuer`` field on the Moveworks credential.
        audience: Matches the ``Audience`` field on the Moveworks credential.
        key_id: Matches the ``kid`` returned when you registered your public
            key with Moveworks. Sent as the JWT header's ``kid``.
        private_key: RSA private key as a PEM string, ``bytes``, or a path to
            a PEM file.
        user_identifier: Value for the JWT's ``sub`` claim. This is who the
            bearer will act as. Typically the user's email.
        assertion_ttl_seconds: Lifetime of the signed JWT. Kept short
            because it is only used for the exchange and is not the API bearer.
        refresh_leeway_seconds: How many seconds before expiration to refresh
            proactively so long-running requests do not fail mid-flight.
    """

    def __init__(
        self,
        base_url: str,
        issuer: str,
        audience: str,
        key_id: str,
        private_key: Union[str, bytes, Path],
        user_identifier: str,
        *,
        assertion_ttl_seconds: int = 300,
        refresh_leeway_seconds: int = 60,
    ) -> None:
        self._token_url = urljoin(base_url.rstrip("/") + "/", "oauth/v1/token")
        self._issuer = issuer
        self._audience = audience
        self._key_id = key_id
        self._private_key = _load_private_key(private_key)
        self._user_identifier = user_identifier
        self._assertion_ttl = assertion_ttl_seconds
        self._refresh_leeway = refresh_leeway_seconds
        self._lock = threading.Lock()
        self._cached_token: str = ""
        self._expires_at: float = 0.0

    def __call__(self) -> str:
        with self._lock:
            if self._cached_token and time.time() < self._expires_at - self._refresh_leeway:
                return self._cached_token
            self._refresh()
            return self._cached_token

    def invalidate(self) -> None:
        """Drop the cached bearer so the next call fetches a new one.

        The client calls this after a 401, which can arrive before the cached
        bearer's own expiry (revoked credential, clock skew).
        """
        with self._lock:
            self._cached_token = ""
            self._expires_at = 0.0

    def _refresh(self) -> None:
        assertion = self._sign_assertion()
        try:
            response = requests.post(
                self._token_url,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": assertion,
                },
                timeout=15.0,
            )
        except requests.RequestException as exc:
            raise AuthenticationError(f"token exchange failed: {exc}") from exc

        if response.status_code >= 400:
            raise AuthenticationError(
                f"token exchange returned {response.status_code}: {response.text[:200]}"
            )

        body = response.json()
        access_token = body.get("access_token")
        expires_in = body.get("expires_in")
        if not isinstance(access_token, str) or not isinstance(expires_in, (int, float)):
            raise AuthenticationError(
                f"token response missing access_token/expires_in: {body}"
            )

        self._cached_token = access_token
        self._expires_at = time.time() + float(expires_in)

    def _sign_assertion(self) -> str:
        now = int(time.time())
        claims = {
            "iss": self._issuer,
            "sub": self._user_identifier,
            "aud": self._audience,
            "iat": now,
            "exp": now + self._assertion_ttl,
            "jti": str(uuid.uuid4()),
        }
        return jwt.encode(
            claims,
            self._private_key,
            algorithm="RS256",
            headers={"kid": self._key_id},
        )


def _load_private_key(key: Union[str, bytes, Path]) -> Union[str, bytes]:
    if isinstance(key, Path):
        return key.read_text()
    if isinstance(key, str) and not key.lstrip().startswith("-----BEGIN"):
        return Path(key).read_text()
    return key
