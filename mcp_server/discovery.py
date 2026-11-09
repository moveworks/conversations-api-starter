"""OAuth discovery metadata (RFC 9728) that points MCP clients at your identity provider.

Clients that follow the MCP authorization spec read this document after a 401, then sign
the user in with the authorization server it names and send the resulting token on every
request. This server never runs the sign-in flow for ``idp_exchange`` or ``google_s2s``:
it only names the issuer, and checks tokens as before.
"""

from __future__ import annotations

from typing import Any, Dict

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from .config import Settings


def resource_metadata_url(settings: Settings) -> str:
    # RFC 9728: the metadata for a resource at /mcp lives at the path-suffixed well-known URL.
    return f"{settings.public_base_url}/.well-known/oauth-protected-resource/mcp"


def protected_resource_metadata(settings: Settings, authorization_server: str) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {
        "resource": f"{settings.public_base_url}/mcp",
        "authorization_servers": [authorization_server],
        "bearer_methods_supported": ["header"],
    }
    if settings.oauth_scopes:
        metadata["scopes_supported"] = list(settings.oauth_scopes)
    return metadata


def build_discovery_app(settings: Settings) -> Starlette:
    """Serves only the protected resource metadata, naming ``MCP_OAUTH_ISSUER``."""

    def protected_resource(request: Request) -> Response:
        return JSONResponse(protected_resource_metadata(settings, settings.oauth_issuer or ""))

    return Starlette(routes=[
        Route("/.well-known/oauth-protected-resource", protected_resource),
        Route("/.well-known/oauth-protected-resource/mcp", protected_resource),
    ])


class OAuthRouter:
    """Sends OAuth paths to the OAuth app and everything else to the MCP app."""

    _PATHS = ("/.well-known/", "/authorize", "/token")

    def __init__(self, oauth_app: Any, mcp_app: Any) -> None:
        self._oauth = oauth_app
        self._mcp = mcp_app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path", "").startswith(self._PATHS):
            await self._oauth(scope, receive, send)
        else:
            await self._mcp(scope, receive, send)
