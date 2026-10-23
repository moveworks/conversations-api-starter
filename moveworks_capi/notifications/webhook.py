"""Verify Moveworks outbound webhook deliveries.

The protocol is documented at:
https://docs.moveworks.com/api-reference/conversations-api/proactive-notifications/receive-via-outbound-webhook

Signing algorithm:

    signing_string = f"{webhook_id}.{webhook_timestamp}.{raw_body}"

Signatures are base64-encoded RSA-SHA256. During a key-rotation grace period,
the ``webhook-signature`` header may contain multiple signatures separated by
spaces; accept if any one verifies.
"""

from __future__ import annotations

import base64
import binascii
import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

import jwt
import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key

from ..errors import (
    InvalidWebhookTimestampError,
    JwksFetchError,
    SignatureVerificationError,
    UnknownKidError,
)


@dataclass(frozen=True)
class WebhookEnvelope:
    """The verified webhook payload envelope."""

    id: str
    type: str
    timestamp: str
    data: Dict[str, Any]

    @classmethod
    def from_dict(cls, obj: Dict[str, Any]) -> "WebhookEnvelope":
        return cls(
            id=obj["id"],
            type=obj["type"],
            timestamp=obj["timestamp"],
            data=obj.get("data") or {},
        )


class JwksCache:
    """Fetches and caches keys from a Moveworks instance's JWKS endpoint.

    The cache serves keys by ``kid``. Any lookup for an unknown ``kid``
    triggers a refresh so a newly rotated key is picked up on demand, as
    the docs recommend.
    """

    def __init__(self, jwks_url: str, *, ttl_seconds: float = 3600.0) -> None:
        self._url = jwks_url
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._keys: Dict[str, RSAPublicKey] = {}
        self._fetched_at: float = 0.0

    def get(self, kid: str) -> RSAPublicKey:
        with self._lock:
            if kid in self._keys and time.time() - self._fetched_at < self._ttl:
                return self._keys[kid]
            self._refresh()
            if kid not in self._keys:
                raise UnknownKidError(f"kid {kid!r} not present in JWKS at {self._url}")
            return self._keys[kid]

    def _refresh(self) -> None:
        try:
            response = requests.get(self._url, timeout=10.0)
        except requests.RequestException as exc:
            raise JwksFetchError(f"cannot fetch JWKS at {self._url}: {exc}") from exc
        if response.status_code >= 400:
            raise JwksFetchError(
                f"JWKS at {self._url} returned {response.status_code}",
                status_code=response.status_code,
                trace_id=response.headers.get("x-trace-id"),
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise JwksFetchError(f"JWKS at {self._url} is not JSON") from exc
        keys_list = (body.get("keys") if isinstance(body, dict) else None) or []
        # Reset and repopulate so removed keys drop out. A missing or empty
        # ``keys`` list means no keys yet.
        self._keys = {}
        for jwk in keys_list:
            kid = jwk.get("kid")
            if not kid:
                continue
            public_key = _jwk_to_public_key(jwk)
            if public_key is not None:
                self._keys[kid] = public_key
        self._fetched_at = time.time()


class WebhookVerifier:
    """Verify an incoming outbound webhook delivery.

    Instantiate one per tenant and reuse across requests to keep the JWKS
    cache warm.
    """

    def __init__(
        self,
        jwks: JwksCache,
        *,
        timestamp_tolerance_seconds: float = 300.0,
    ) -> None:
        self._jwks = jwks
        self._tolerance = timestamp_tolerance_seconds

    def verify(
        self,
        *,
        headers: Mapping[str, str],
        raw_body: bytes,
        now: Optional[float] = None,
    ) -> WebhookEnvelope:
        webhook_id = headers.get("webhook-id")
        webhook_timestamp = headers.get("webhook-timestamp")
        webhook_signature = headers.get("webhook-signature")
        webhook_kid = headers.get("webhook-kid")
        if not (webhook_id and webhook_timestamp and webhook_signature and webhook_kid):
            raise SignatureVerificationError("missing one or more webhook headers")

        _check_timestamp(webhook_timestamp, self._tolerance, now=now)

        public_key = self._jwks.get(webhook_kid)
        signing_string = build_signing_string(webhook_id, webhook_timestamp, raw_body)

        signatures = webhook_signature.split()
        if not signatures:
            raise SignatureVerificationError("empty webhook-signature header")

        for candidate in signatures:
            if _verify_rsa_sha256(public_key, signing_string, candidate):
                break
        else:
            raise SignatureVerificationError(
                f"no signature verified against kid {webhook_kid!r}"
            )

        try:
            envelope_obj = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise SignatureVerificationError(f"body is not valid JSON: {exc}") from exc

        return WebhookEnvelope.from_dict(envelope_obj)


def build_signing_string(webhook_id: str, webhook_timestamp: str, raw_body: bytes) -> bytes:
    """Construct the exact byte sequence over which the signature was computed."""
    return f"{webhook_id}.{webhook_timestamp}.".encode("utf-8") + raw_body


def _check_timestamp(
    webhook_timestamp: str,
    tolerance_seconds: float,
    *,
    now: Optional[float] = None,
) -> None:
    try:
        parsed = _parse_iso8601(webhook_timestamp)
    except ValueError as exc:
        raise InvalidWebhookTimestampError(f"unparseable timestamp: {webhook_timestamp!r}") from exc
    current = now if now is not None else time.time()
    if abs(current - parsed.timestamp()) > tolerance_seconds:
        raise InvalidWebhookTimestampError(
            f"timestamp {webhook_timestamp!r} outside {tolerance_seconds}s tolerance"
        )


def _parse_iso8601(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00") if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _verify_rsa_sha256(public_key: RSAPublicKey, signing_string: bytes, signature_b64: str) -> bool:
    try:
        signature = base64.b64decode(signature_b64, validate=True)
    except (ValueError, binascii.Error):
        return False
    try:
        public_key.verify(
            signature,
            signing_string,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    except InvalidSignature:
        return False
    return True


def _jwk_to_public_key(jwk: Dict[str, Any]) -> Optional[RSAPublicKey]:
    kty = jwk.get("kty")
    if kty != "RSA":
        return None
    try:
        pem = jwt.algorithms.RSAAlgorithm.from_jwk(jwk)  # type: ignore[attr-defined]
    except (ValueError, KeyError):
        return None
    if isinstance(pem, RSAPublicKey):
        return pem
    if isinstance(pem, (bytes, str)):
        loaded = load_pem_public_key(pem.encode() if isinstance(pem, str) else pem)
        return loaded if isinstance(loaded, RSAPublicKey) else None
    return None
