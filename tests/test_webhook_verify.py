"""Signature verifier tests using a synthetic RSA keypair.

These tests generate a keypair in-process, sign a payload the same way
Moveworks does (``{id}.{timestamp}.{body}``), and confirm the verifier
accepts, rejects tampered bodies, rejects stale timestamps, and handles
space-delimited multi-signature headers during a rotation grace period.
"""

from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timezone
from typing import Tuple
from unittest.mock import patch

import pytest
import requests
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey

from moveworks_capi.errors import (
    InvalidWebhookTimestampError,
    JwksFetchError,
    SignatureVerificationError,
    UnknownKidError,
)
from moveworks_capi.notifications.webhook import (
    JwksCache,
    WebhookVerifier,
    build_signing_string,
)


def _generate_keypair() -> Tuple[RSAPrivateKey, RSAPublicKey]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


def _sign(private_key: RSAPrivateKey, signing_string: bytes) -> str:
    signature = private_key.sign(signing_string, padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(signature).decode("ascii")


def _public_key_to_jwk(public_key: RSAPublicKey, kid: str) -> dict:
    numbers = public_key.public_numbers()

    def _b64uint(n: int) -> str:
        byte_length = (n.bit_length() + 7) // 8
        return base64.urlsafe_b64encode(n.to_bytes(byte_length, "big")).rstrip(b"=").decode("ascii")

    return {"kty": "RSA", "kid": kid, "alg": "RS256", "use": "sig", "n": _b64uint(numbers.n), "e": _b64uint(numbers.e)}


class _StaticJwks(JwksCache):
    """Test double: skip HTTP and hand out a preloaded key set."""

    def __init__(self, jwks: dict) -> None:
        super().__init__("http://unused.invalid", ttl_seconds=3600.0)
        # Populate the parent's cache without hitting the network.
        for key in jwks.get("keys") or []:
            kid = key["kid"]
            n_bytes = base64.urlsafe_b64decode(key["n"] + "==")
            e_bytes = base64.urlsafe_b64decode(key["e"] + "==")
            n = int.from_bytes(n_bytes, "big")
            e = int.from_bytes(e_bytes, "big")
            public = rsa.RSAPublicNumbers(e=e, n=n).public_key()
            self._keys[kid] = public
        self._fetched_at = time.time()

    def _refresh(self) -> None:
        # Never re-fetch in tests.
        return


@pytest.fixture(name="signed_delivery")
def _signed_delivery():
    private_key, public_key = _generate_keypair()
    kid = "kid-test-1"
    jwks = {"keys": [_public_key_to_jwk(public_key, kid)]}
    jwks_cache = _StaticJwks(jwks)
    verifier = WebhookVerifier(jwks_cache, timestamp_tolerance_seconds=300)

    webhook_id = "evt_synthetic1"
    timestamp = datetime.now(timezone.utc).isoformat()
    body = json.dumps(
        {"id": webhook_id, "type": "CONVERSATION_RESPONSE_RECEIVED", "timestamp": timestamp, "data": {"user_id": "u1"}}
    ).encode("utf-8")
    signature = _sign(private_key, build_signing_string(webhook_id, timestamp, body))

    return {
        "verifier": verifier,
        "private_key": private_key,
        "kid": kid,
        "webhook_id": webhook_id,
        "timestamp": timestamp,
        "body": body,
        "signature": signature,
    }


class TestSignatureVerifier:
    def test_accepts_valid_signature(self, signed_delivery: dict) -> None:
        envelope = signed_delivery["verifier"].verify(
            headers={
                "webhook-id": signed_delivery["webhook_id"],
                "webhook-timestamp": signed_delivery["timestamp"],
                "webhook-signature": signed_delivery["signature"],
                "webhook-kid": signed_delivery["kid"],
            },
            raw_body=signed_delivery["body"],
        )
        assert envelope.id == signed_delivery["webhook_id"]
        assert envelope.data["user_id"] == "u1"

    def test_rejects_tampered_body(self, signed_delivery: dict) -> None:
        tampered = signed_delivery["body"].replace(b'"u1"', b'"attacker"')
        with pytest.raises(SignatureVerificationError):
            signed_delivery["verifier"].verify(
                headers={
                    "webhook-id": signed_delivery["webhook_id"],
                    "webhook-timestamp": signed_delivery["timestamp"],
                    "webhook-signature": signed_delivery["signature"],
                    "webhook-kid": signed_delivery["kid"],
                },
                raw_body=tampered,
            )

    def test_rejects_stale_timestamp(self, signed_delivery: dict) -> None:
        stale_ts = "2020-01-01T00:00:00+00:00"
        signature = _sign(
            signed_delivery["private_key"],
            build_signing_string(signed_delivery["webhook_id"], stale_ts, signed_delivery["body"]),
        )
        with pytest.raises(InvalidWebhookTimestampError):
            signed_delivery["verifier"].verify(
                headers={
                    "webhook-id": signed_delivery["webhook_id"],
                    "webhook-timestamp": stale_ts,
                    "webhook-signature": signature,
                    "webhook-kid": signed_delivery["kid"],
                },
                raw_body=signed_delivery["body"],
            )

    def test_rejects_missing_headers(self, signed_delivery: dict) -> None:
        with pytest.raises(SignatureVerificationError):
            signed_delivery["verifier"].verify(
                headers={"webhook-id": signed_delivery["webhook_id"]},
                raw_body=signed_delivery["body"],
            )

    def test_rejects_unknown_kid(self, signed_delivery: dict) -> None:
        with pytest.raises(UnknownKidError):
            signed_delivery["verifier"].verify(
                headers={
                    "webhook-id": signed_delivery["webhook_id"],
                    "webhook-timestamp": signed_delivery["timestamp"],
                    "webhook-signature": signed_delivery["signature"],
                    "webhook-kid": "kid-does-not-exist",
                },
                raw_body=signed_delivery["body"],
            )

    def test_accepts_multi_signature_during_rotation(self) -> None:
        old_private, old_public = _generate_keypair()
        new_private, new_public = _generate_keypair()
        jwks = {
            "keys": [
                _public_key_to_jwk(old_public, "kid-old"),
                _public_key_to_jwk(new_public, "kid-new"),
            ]
        }
        verifier = WebhookVerifier(_StaticJwks(jwks), timestamp_tolerance_seconds=300)

        webhook_id = "evt_rotation"
        timestamp = datetime.now(timezone.utc).isoformat()
        body = b'{"id":"evt_rotation","type":"CONVERSATION_RESPONSE_RECEIVED","timestamp":"' + timestamp.encode() + b'","data":{}}'
        signing_string = build_signing_string(webhook_id, timestamp, body)

        # Deliveries during a grace period carry both signatures, space-delimited.
        # The verifier is asked to verify against the new kid; the old signature
        # is stale but present. As long as one of them checks out under the
        # named kid, the request is accepted.
        good = _sign(new_private, signing_string)
        stale = _sign(old_private, signing_string)  # will fail under kid-new, ok

        envelope = verifier.verify(
            headers={
                "webhook-id": webhook_id,
                "webhook-timestamp": timestamp,
                "webhook-signature": f"{stale} {good}",
                "webhook-kid": "kid-new",
            },
            raw_body=body,
        )
        assert envelope.id == webhook_id


class TestSigningStringFormat:
    def test_uses_dot_separators(self) -> None:
        body = b'{"k":"v"}'
        assert build_signing_string("evt_1", "2026-01-01T00:00:00Z", body) == (
            b"evt_1.2026-01-01T00:00:00Z." + body
        )

    def test_body_kept_as_raw_bytes(self) -> None:
        # A body with unusual whitespace must be signed exactly as received;
        # re-serialization would change the byte stream.
        body = b'{"k": "v" ,  "x":1}'
        signing = build_signing_string("evt_1", "2026-01-01T00:00:00Z", body)
        assert signing.endswith(body)


class TestJwksFetchFailures:
    def test_network_error_is_wrapped(self) -> None:
        with patch("moveworks_capi.notifications.webhook.requests.get", side_effect=requests.ConnectionError("down")):
            with pytest.raises(JwksFetchError):
                JwksCache("https://org.example/.well-known/jwks.json").get("kid-1")

    def test_http_error_is_wrapped_with_status(self) -> None:
        class _Down:
            status_code = 503
            headers: dict = {}

        with patch("moveworks_capi.notifications.webhook.requests.get", return_value=_Down()):
            with pytest.raises(JwksFetchError) as exc:
                JwksCache("https://org.example/.well-known/jwks.json").get("kid-1")
        assert exc.value.status_code == 503
