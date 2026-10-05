"""S2S JWT bearer: sign correct claims, cache, refresh, propagate errors."""

from __future__ import annotations

import time
from unittest.mock import patch

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from moveworks_capi.auth.s2s_jwt import S2SJwtBearer
from moveworks_capi.errors import AuthenticationError


@pytest.fixture(name="rsa_pem")
def _rsa_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem.decode("ascii")


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self) -> dict:
        return self._payload


def _bearer(rsa_pem: str, **overrides) -> S2SJwtBearer:
    return S2SJwtBearer(
        base_url="https://api.moveworks.ai",
        issuer=overrides.get("issuer", "iss.acme"),
        audience=overrides.get("audience", "aud.acme"),
        key_id=overrides.get("key_id", "kid-1"),
        private_key=rsa_pem,
        user_identifier=overrides.get("user_identifier", "alice@acme.com"),
    )


class TestS2SJwtBearer:
    def test_signs_assertion_with_expected_claims(self, rsa_pem: str) -> None:
        bearer = _bearer(rsa_pem)
        assertion = bearer._sign_assertion()
        # decode without verification to inspect the shape; we hold the key,
        # but we're testing claim structure here, not signature.
        unverified = jwt.decode(assertion, options={"verify_signature": False})
        assert unverified["iss"] == "iss.acme"
        assert unverified["aud"] == "aud.acme"
        assert unverified["sub"] == "alice@acme.com"
        assert unverified["exp"] > unverified["iat"]
        assert "jti" in unverified
        headers = jwt.get_unverified_header(assertion)
        assert headers["kid"] == "kid-1"
        assert headers["alg"] == "RS256"

    def test_caches_bearer_across_calls(self, rsa_pem: str) -> None:
        bearer = _bearer(rsa_pem)
        response = _FakeResponse(200, {"access_token": "tok-1", "expires_in": 3600})
        with patch("moveworks_capi.auth.s2s_jwt.requests.post", return_value=response) as post:
            first = bearer()
            second = bearer()
        assert first == "tok-1"
        assert second == "tok-1"
        assert post.call_count == 1

    def test_refreshes_when_near_expiry(self, rsa_pem: str) -> None:
        bearer = _bearer(rsa_pem)
        responses = iter(
            [
                _FakeResponse(200, {"access_token": "tok-1", "expires_in": 60}),
                _FakeResponse(200, {"access_token": "tok-2", "expires_in": 3600}),
            ]
        )

        def _fake_post(*args, **kwargs):
            return next(responses)

        with patch("moveworks_capi.auth.s2s_jwt.requests.post", side_effect=_fake_post):
            first = bearer()
            # Force the cache to look expired by rewinding the recorded expiry.
            bearer._expires_at = time.time() + 10  # inside leeway
            second = bearer()
        assert first == "tok-1"
        assert second == "tok-2"

    def test_raises_on_bad_status(self, rsa_pem: str) -> None:
        bearer = _bearer(rsa_pem)
        response = _FakeResponse(401, {"error": "invalid_signature"})
        with patch("moveworks_capi.auth.s2s_jwt.requests.post", return_value=response):
            with pytest.raises(AuthenticationError):
                bearer()

    def test_raises_on_missing_fields(self, rsa_pem: str) -> None:
        bearer = _bearer(rsa_pem)
        response = _FakeResponse(200, {"access_token": "tok-1"})  # missing expires_in
        with patch("moveworks_capi.auth.s2s_jwt.requests.post", return_value=response):
            with pytest.raises(AuthenticationError):
                bearer()

    def test_invalidate_forces_a_fresh_exchange(self, rsa_pem: str) -> None:
        bearer = _bearer(rsa_pem)
        tokens = iter(
            [
                _FakeResponse(200, {"access_token": "tok-1", "expires_in": 3600}),
                _FakeResponse(200, {"access_token": "tok-2", "expires_in": 3600}),
            ]
        )
        with patch("moveworks_capi.auth.s2s_jwt.requests.post", side_effect=lambda *a, **kw: next(tokens)):
            assert bearer() == "tok-1"
            bearer.invalidate()
            assert bearer() == "tok-2"
