import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient

from mcp_server.config import Settings
from mcp_server.demo_oauth import DemoOAuthAuthenticator, build_oauth_app
from mcp_server.identity import Unauthenticated

REDIRECT = "https://gemini.example/callback"


@pytest.fixture
def settings():
    return Settings(
        mw_base_url="https://api.example",
        mw_bot_name="bot",
        auth_mode="demo_oauth",
        s2s_issuer="i", s2s_audience="a", s2s_key_id="k", s2s_private_key="-----BEGIN PRIVATE KEY-----",
        dev_user_email="demo@acme.com",
        public_base_url="https://demo.example",
        demo_oauth_client_id="cid",
        demo_oauth_client_secret="csecret",
        demo_oauth_passphrase="open sesame",
        demo_oauth_signing_key="signing",
    )


def _challenge(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


def _code(client, passphrase="open sesame", challenge=None):
    form = {"client_id": "cid", "redirect_uri": REDIRECT, "state": "xyz", "response_type": "code", "passphrase": passphrase}
    if challenge:
        form["code_challenge"] = challenge
        form["code_challenge_method"] = "S256"
    return client.post("/authorize", data=form, follow_redirects=False)


def test_metadata_points_at_this_server(settings):
    client = TestClient(build_oauth_app(settings))
    assert client.get("/.well-known/oauth-protected-resource").json()["authorization_servers"] == ["https://demo.example"]
    assert client.get("/.well-known/oauth-authorization-server").json()["token_endpoint"] == "https://demo.example/token"


def test_wrong_passphrase_issues_no_code(settings):
    response = _code(TestClient(build_oauth_app(settings)), passphrase="nope")
    assert response.status_code == 200 and "Wrong passphrase" in response.text


def test_full_flow_with_pkce_issues_a_token_the_authenticator_accepts(settings):
    client = TestClient(build_oauth_app(settings))
    redirect = _code(client, challenge=_challenge("v" * 50))
    assert redirect.status_code == 302
    query = parse_qs(urlparse(redirect.headers["location"]).query)
    assert query["state"] == ["xyz"]
    tokens = client.post("/token", data={
        "grant_type": "authorization_code", "code": query["code"][0], "redirect_uri": REDIRECT,
        "code_verifier": "v" * 50, "client_id": "cid", "client_secret": "csecret",
    }).json()
    identity = DemoOAuthAuthenticator(settings).authenticate({"authorization": f"Bearer {tokens['access_token']}"})
    assert identity.user_key == "demo@acme.com"
    refreshed = client.post("/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]},
                            auth=("cid", "csecret"))
    assert refreshed.status_code == 200


def test_bad_secret_and_bad_verifier_are_rejected(settings):
    client = TestClient(build_oauth_app(settings))
    code = parse_qs(urlparse(_code(client, challenge=_challenge("v" * 50)).headers["location"]).query)["code"][0]
    base = {"grant_type": "authorization_code", "code": code, "client_id": "cid"}
    assert client.post("/token", data={**base, "client_secret": "wrong", "code_verifier": "v" * 50}).status_code == 401
    assert client.post("/token", data={**base, "client_secret": "csecret", "code_verifier": "w" * 50}).status_code == 400


def test_authenticator_rejects_codes_and_foreign_tokens(settings):
    authenticator = DemoOAuthAuthenticator(settings)
    with pytest.raises(Unauthenticated):
        authenticator.authenticate({"authorization": "Bearer dev:demo@acme.com"})
