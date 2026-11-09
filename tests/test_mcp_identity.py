import base64
import json
from typing import Any, Dict
from unittest import mock

import pytest

from mcp_server.config import Settings
from mcp_server.identity import (
    GoogleS2SAuthenticator,
    IdpExchangeAuthenticator,
    Unauthenticated,
    bearer_from_headers,
)


def settings(**overrides):
    base: Dict[str, Any] = dict(
        mw_base_url="https://api.moveworks.ai",
        mw_bot_name="acme-capi",
        auth_mode="google_s2s",
        s2s_issuer="iss",
        s2s_audience="aud",
        s2s_key_id="kid",
        s2s_private_key="-----BEGIN PRIVATE KEY-----",
        google_client_ids=frozenset({"client-123"}),
    )
    base.update(overrides)
    return Settings(**base)


def tokeninfo(status=200, **body):
    response = mock.Mock(status_code=status)
    response.json.return_value = body
    http = mock.Mock()
    http.get.return_value = response
    return http


def test_bearer_header_is_required():
    with pytest.raises(Unauthenticated):
        bearer_from_headers({})
    assert bearer_from_headers({"authorization": "Bearer abc"}) == "abc"


def test_google_token_maps_to_the_verified_email():
    http = tokeninfo(aud="client-123", email="Jane@Acme.com", email_verified="true", expires_in="3599")
    auth = GoogleS2SAuthenticator(settings(), http=http)

    identity = auth.authenticate({"Authorization": "Bearer tok"})

    assert identity.user_key == "jane@acme.com"
    assert identity.bearer_provider._user_identifier == "jane@acme.com"
    auth.authenticate({"Authorization": "Bearer tok"})
    assert http.get.call_count == 1


def test_google_token_for_another_client_is_rejected():
    auth = GoogleS2SAuthenticator(settings(), http=tokeninfo(aud="someone-else", email="a@acme.com", email_verified="true"))
    with pytest.raises(Unauthenticated, match="OAuth client"):
        auth.authenticate({"Authorization": "Bearer tok"})


def test_google_token_outside_allowed_domains_is_rejected():
    auth = GoogleS2SAuthenticator(
        settings(allowed_email_domains=frozenset({"acme.com"})),
        http=tokeninfo(aud="client-123", email="a@gmail.com", email_verified="true"),
    )
    with pytest.raises(Unauthenticated, match="domain"):
        auth.authenticate({"Authorization": "Bearer tok"})


def test_google_token_without_verified_email_is_rejected():
    auth = GoogleS2SAuthenticator(settings(), http=tokeninfo(aud="client-123", email="a@acme.com", email_verified="false"))
    with pytest.raises(Unauthenticated, match="verified email"):
        auth.authenticate({"Authorization": "Bearer tok"})


def jwt_with(claims):
    def encode(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{encode({'alg': 'RS256'})}.{encode(claims)}.sig"


def test_idp_token_is_exchanged_and_keyed_by_its_subject():
    response = mock.Mock(status_code=200)
    response.json.return_value = {"access_token": "mw-bearer", "expires_in": 3600}
    http = mock.Mock()
    http.post.return_value = response
    auth = IdpExchangeAuthenticator(settings(auth_mode="idp_exchange"), http=http)

    identity = auth.authenticate({"Authorization": f"Bearer {jwt_with({'email': 'Jane@Acme.com'})}"})

    assert identity.user_key == "jane@acme.com"
    assert identity.bearer_provider() == "mw-bearer"
    sent = http.post.call_args
    assert sent.args[0] == "https://api.moveworks.ai/oauth/v1/token"
    assert sent.kwargs["data"]["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"


def test_rejected_idp_token_is_unauthenticated():
    http = mock.Mock()
    http.post.return_value = mock.Mock(status_code=401)
    auth = IdpExchangeAuthenticator(settings(auth_mode="idp_exchange"), http=http)
    with pytest.raises(Unauthenticated):
        auth.authenticate({"Authorization": "Bearer bad"})


def test_google_mode_requires_client_ids():
    with pytest.raises(ValueError, match="GOOGLE_OAUTH_CLIENT_IDS"):
        settings(google_client_ids=frozenset()).validate()


def test_an_invalidated_moveworks_bearer_forces_a_fresh_exchange():
    http = mock.Mock()
    http.post.side_effect = [
        mock.Mock(status_code=200, json=lambda: {"access_token": "mw-1", "expires_in": 3600}),
        mock.Mock(status_code=200, json=lambda: {"access_token": "mw-2", "expires_in": 3600}),
    ]
    auth = IdpExchangeAuthenticator(settings(auth_mode="idp_exchange"), http=http)
    token = jwt_with({"sub": "alice"})
    first = auth.authenticate({"Authorization": f"Bearer {token}"})
    assert first.bearer_provider() == "mw-1"
    first.bearer_provider.invalidate()  # type: ignore[attr-defined]
    second = auth.authenticate({"Authorization": f"Bearer {token}"})
    assert second.bearer_provider() == "mw-2"
    assert http.post.call_count == 2


@pytest.mark.parametrize("overrides, message", [
    ({"auth_mode": "google_s2s", "allowed_email_domains": frozenset()}, "MCP_ALLOWED_EMAIL_DOMAINS"),
    ({"auth_mode": "dev_static_map", "allowed_hosts": frozenset({"abc.example"})}, "public host name"),
    ({"auth_mode": "dev_static_map", "host": "0.0.0.0"}, "refusing to bind"),
])
def test_settings_refuse_unsafe_combinations(overrides, message):
    with pytest.raises(ValueError, match=message):
        settings(**overrides).validate()
