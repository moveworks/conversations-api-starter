import asyncio
from types import SimpleNamespace
from typing import Any, Dict

import pytest
from starlette.testclient import TestClient

from mcp_server.config import Settings
from mcp_server.identity import Unauthenticated, bearer_from_headers
from mcp_server.server import build_app, build_mcp

ISSUER = "https://acme.okta.example/oauth2/default"


def settings(**overrides):
    base: Dict[str, Any] = dict(
        mw_base_url="https://api.example",
        mw_bot_name="bot",
        auth_mode="idp_exchange",
        public_base_url="https://mcp.acme.example",
        oauth_issuer=ISSUER,
        oauth_scopes=("openid", "email", "moveworks"),
    )
    base.update(overrides)
    return Settings(**base)


class RejectAll:
    def authenticate(self, headers) -> Any:
        bearer_from_headers(headers)
        raise Unauthenticated("no")


class AcceptAll:
    def authenticate(self, headers) -> Any:
        return SimpleNamespace(user_key="alice@acme.com")


def test_discovery_names_the_identity_provider_and_scopes():
    client = TestClient(build_app(settings(), authenticator=RejectAll()))
    for path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"):
        body = client.get(path).json()
        assert body == {
            "resource": "https://mcp.acme.example/mcp",
            "authorization_servers": [ISSUER],
            "bearer_methods_supported": ["header"],
            "scopes_supported": ["openid", "email", "moveworks"],
        }


def test_unauthenticated_tool_call_points_at_the_discovery_document():
    client = TestClient(build_app(settings(), authenticator=RejectAll()))
    reply = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}},
                        headers={"accept": "application/json, text/event-stream"})
    assert reply.status_code == 401
    challenge = reply.headers["www-authenticate"]
    assert 'resource_metadata="https://mcp.acme.example/.well-known/oauth-protected-resource/mcp"' in challenge
    assert 'scope="openid email moveworks"' in challenge


def test_without_an_issuer_there_is_no_discovery_document():
    client = TestClient(build_app(settings(oauth_issuer=None, oauth_scopes=(), public_base_url=None), authenticator=AcceptAll()))
    assert client.get("/.well-known/oauth-protected-resource").status_code != 200


@pytest.mark.parametrize("overrides, message", [
    ({"auth_mode": "dev_static_map", "public_base_url": None, "s2s_issuer": "i", "s2s_audience": "a", "s2s_key_id": "k",
      "s2s_private_key": "p"}, "only to MCP_AUTH_MODE"),
    ({"oauth_issuer": "http://acme.okta.example"}, "https URL"),
    ({"public_base_url": "https://"}, "MCP_PUBLIC_BASE_URL"),
    ({"public_base_url": None}, "MCP_PUBLIC_BASE_URL"),
    ({"oauth_issuer": None}, "MCP_OAUTH_SCOPES needs MCP_OAUTH_ISSUER"),
])
def test_issuer_settings_are_validated(overrides, message):
    with pytest.raises(ValueError, match=message):
        settings(**overrides).validate()


def test_only_tools_that_change_nothing_are_annotated_read_only():
    tools = asyncio.run(build_mcp(object(), RejectAll()).list_tools())  # type: ignore[arg-type]
    read_only = {tool.name for tool in tools if tool.annotations and tool.annotations.read_only_hint}
    assert read_only == {"open_moveworks_form", "get_moveworks_result"}
