"""Interactive OIDC bearer: discovery, PKCE helpers, code and Moveworks exchanges."""

from __future__ import annotations

import base64
import hashlib
import urllib.parse
from unittest.mock import patch

import pytest

from moveworks_capi.auth import OidcInteractiveBearer, OktaInteractiveBearer, entra_issuer
from moveworks_capi.auth.oidc_pkce import _make_code_challenge, _make_code_verifier
from moveworks_capi.errors import AuthenticationError

OKTA_ISSUER = "https://acme.okta.com/oauth2/default"
ENTRA_TENANT = "11111111-2222-3333-4444-555555555555"
ENTRA_CLIENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


class _FakeResponse:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self) -> object:
        if isinstance(self._payload, dict):
            return self._payload
        raise ValueError("not json")


def _okta_discovery() -> dict:
    return {
        "issuer": OKTA_ISSUER,
        "authorization_endpoint": f"{OKTA_ISSUER}/v1/authorize",
        "token_endpoint": f"{OKTA_ISSUER}/v1/token",
    }


def _entra_discovery() -> dict:
    base = f"https://login.microsoftonline.com/{ENTRA_TENANT}/oauth2/v2.0"
    return {
        "issuer": entra_issuer(ENTRA_TENANT),
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
    }


def _okta(**overrides) -> OktaInteractiveBearer:
    return OktaInteractiveBearer(
        base_url="https://api.moveworks.ai",
        okta_issuer=overrides.get("okta_issuer", OKTA_ISSUER),
        okta_client_id="0oa123abc",
        redirect_uri=overrides.get("redirect_uri", "http://127.0.0.1:8765/callback"),
    )


def _entra(**overrides) -> OidcInteractiveBearer:
    return OidcInteractiveBearer(
        base_url="https://api.moveworks.ai",
        issuer=entra_issuer(ENTRA_TENANT),
        client_id=ENTRA_CLIENT,
        redirect_uri="http://localhost:8765/callback",
        scopes=f"openid api://{ENTRA_CLIENT}/access_as_user",
        idp_name="Entra ID",
        **overrides,
    )


def _query(url: str) -> dict:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))


class TestPkceHelpers:
    def test_code_verifier_within_spec_length(self) -> None:
        # RFC 7636: 43-128 characters, unreserved chars.
        assert 43 <= len(_make_code_verifier()) <= 128

    def test_challenge_is_s256_of_verifier(self) -> None:
        verifier = _make_code_verifier()
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        assert _make_code_challenge(verifier) == expected


class TestDiscovery:
    def test_reads_endpoints_from_well_known_document(self) -> None:
        bearer = _okta()
        with patch("moveworks_capi.auth.oidc_pkce.requests.get", return_value=_FakeResponse(200, _okta_discovery())) as get:
            assert bearer.endpoints() == (f"{OKTA_ISSUER}/v1/authorize", f"{OKTA_ISSUER}/v1/token")
        assert get.call_args[0][0] == f"{OKTA_ISSUER}/.well-known/openid-configuration"

    def test_fetched_once_per_instance(self) -> None:
        bearer = _okta()
        with patch("moveworks_capi.auth.oidc_pkce.requests.get", return_value=_FakeResponse(200, _okta_discovery())) as get:
            bearer.endpoints()
            bearer.authorize_url(state="s", code_challenge="c")
        assert get.call_count == 1

    def test_trailing_slash_on_issuer_is_ignored(self) -> None:
        bearer = _okta(okta_issuer=OKTA_ISSUER + "/")
        with patch("moveworks_capi.auth.oidc_pkce.requests.get", return_value=_FakeResponse(200, _okta_discovery())) as get:
            bearer.endpoints()
        assert get.call_args[0][0] == f"{OKTA_ISSUER}/.well-known/openid-configuration"

    def test_overrides_skip_discovery(self) -> None:
        bearer = _entra(authorization_endpoint="https://idp.example/authorize", token_endpoint="https://idp.example/token")
        with patch("moveworks_capi.auth.oidc_pkce.requests.get") as get:
            assert bearer.endpoints() == ("https://idp.example/authorize", "https://idp.example/token")
        get.assert_not_called()

    def test_one_override_fills_the_other_from_discovery(self) -> None:
        bearer = _entra(token_endpoint="https://idp.example/token")
        with patch("moveworks_capi.auth.oidc_pkce.requests.get", return_value=_FakeResponse(200, _entra_discovery())):
            authorize, token = bearer.endpoints()
        assert authorize == _entra_discovery()["authorization_endpoint"]
        assert token == "https://idp.example/token"

    @pytest.mark.parametrize(
        "response",
        [
            _FakeResponse(404, {"error": "not found"}),
            _FakeResponse(200, "<html>"),
            _FakeResponse(200, {"issuer": OKTA_ISSUER}),
        ],
    )
    def test_unusable_document_raises(self, response: _FakeResponse) -> None:
        with patch("moveworks_capi.auth.oidc_pkce.requests.get", return_value=response):
            with pytest.raises(AuthenticationError):
                _okta().endpoints()


class TestAuthorizeUrl:
    def test_okta_shape(self) -> None:
        with patch("moveworks_capi.auth.oidc_pkce.requests.get", return_value=_FakeResponse(200, _okta_discovery())):
            url = _okta().authorize_url(state="state-1", code_challenge="challenge-1")
        assert url.startswith(f"{OKTA_ISSUER}/v1/authorize?")
        assert _query(url) == {
            "client_id": "0oa123abc",
            "response_type": "code",
            "scope": "openid profile email",
            "redirect_uri": "http://127.0.0.1:8765/callback",
            "state": "state-1",
            "code_challenge": "challenge-1",
            "code_challenge_method": "S256",
        }

    def test_entra_shape(self) -> None:
        with patch("moveworks_capi.auth.oidc_pkce.requests.get", return_value=_FakeResponse(200, _entra_discovery())):
            url = _entra().authorize_url(state="state-1", code_challenge="challenge-1")
        assert url.startswith(f"https://login.microsoftonline.com/{ENTRA_TENANT}/oauth2/v2.0/authorize?")
        query = _query(url)
        assert query["scope"] == f"openid api://{ENTRA_CLIENT}/access_as_user"
        assert query["client_id"] == ENTRA_CLIENT
        assert query["code_challenge_method"] == "S256"


class TestEntraIssuer:
    def test_v2_issuer_for_tenant(self) -> None:
        assert entra_issuer(ENTRA_TENANT) == f"https://login.microsoftonline.com/{ENTRA_TENANT}/v2.0"


class TestRedeemCode:
    def test_posts_pkce_verifier_to_discovered_token_endpoint(self) -> None:
        bearer = _okta()
        with patch("moveworks_capi.auth.oidc_pkce.requests.get", return_value=_FakeResponse(200, _okta_discovery())), \
                patch("moveworks_capi.auth.oidc_pkce.requests.post", return_value=_FakeResponse(200, {"access_token": "okta-tok"})) as post:
            assert bearer.redeem_code(code="c1", code_verifier="v1") == "okta-tok"
        assert post.call_args[0][0] == f"{OKTA_ISSUER}/v1/token"
        assert post.call_args.kwargs["data"] == {
            "grant_type": "authorization_code",
            "client_id": "0oa123abc",
            "code": "c1",
            "code_verifier": "v1",
            "redirect_uri": "http://127.0.0.1:8765/callback",
        }

    def test_raises_on_idp_error(self) -> None:
        bearer = _entra(authorization_endpoint="https://idp.example/authorize", token_endpoint="https://idp.example/token")
        with patch("moveworks_capi.auth.oidc_pkce.requests.post", return_value=_FakeResponse(400, {"error": "invalid_grant"})):
            with pytest.raises(AuthenticationError, match="Entra ID"):
                bearer.redeem_code(code="c1", code_verifier="v1")


class TestMoveworksExchange:
    def test_returns_bearer_and_expiry(self) -> None:
        response = _FakeResponse(200, {"access_token": "mw-tok", "expires_in": 3600})
        with patch("moveworks_capi.auth.oidc_pkce.requests.post", return_value=response) as post:
            token, expires_in = _okta().exchange("okta-tok")
        assert (token, expires_in) == ("mw-tok", 3600.0)
        assert post.call_args[0][0] == "https://api.moveworks.ai/oauth/v1/token"
        assert post.call_args.kwargs["data"] == {
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": "okta-tok",
        }

    def test_raises_on_missing_expiry(self) -> None:
        with patch("moveworks_capi.auth.oidc_pkce.requests.post", return_value=_FakeResponse(200, {"access_token": "mw-tok"})):
            with pytest.raises(AuthenticationError):
                _okta().exchange("okta-tok")

    def test_raises_on_status_error(self) -> None:
        body = {"error": {"code": "INVALID_JWT", "message": "Client credentials not found"}}
        with patch("moveworks_capi.auth.oidc_pkce.requests.post", return_value=_FakeResponse(401, body)):
            with pytest.raises(AuthenticationError, match="401"):
                _entra().exchange("entra-tok")


class TestCaching:
    def test_login_and_exchange_run_once_until_expiry(self) -> None:
        bearer = _okta()
        with patch.object(bearer, "login", return_value="idp-tok") as login, \
                patch.object(bearer, "exchange", return_value=("mw-tok", 3600.0)) as exchange:
            assert bearer() == "mw-tok"
            assert bearer() == "mw-tok"
        login.assert_called_once()
        exchange.assert_called_once_with("idp-tok")


class TestRedirectUriValidation:
    def test_rejects_uri_without_port(self) -> None:
        bearer = _okta(redirect_uri="http://127.0.0.1/callback")
        with pytest.raises(ValueError, match="port"):
            bearer._await_callback("https://idp.example/authorize")
