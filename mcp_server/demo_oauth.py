"""A minimal OAuth 2.1 authorization server for demos with clients that only accept OAuth.

The Gemini app and ChatGPT, for example, sign users in with OAuth: they discover the
endpoints from ``/.well-known`` metadata (MCP authorization spec), then ask
for a client ID and secret. There is no customer IdP in a demo, so this module
plays one: the sign-in page asks for a passphrase, and every token it issues
acts as ``MCP_DEV_USER_EMAIL``. Codes and tokens are HMAC-signed, so a restart
doesn't sign the client out. Demo only: never put it in front of a production
Moveworks instance.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import secrets
import time
from typing import Any, Dict, Mapping, Optional
from urllib.parse import urlencode

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from .config import Settings
from .discovery import protected_resource_metadata
from .identity import DevStaticAuthenticator, Unauthenticated, UserIdentity, bearer_from_headers

log = logging.getLogger("mcp_server.demo_oauth")

ACCESS_TOKEN_SECONDS = 3600
REFRESH_TOKEN_SECONDS = 30 * 24 * 3600
CODE_SECONDS = 300


class TokenSigner:
    def __init__(self, key: str) -> None:
        self._key = key.encode("utf-8")

    def issue(self, kind: str, ttl_seconds: int, **claims: Any) -> str:
        payload = {"k": kind, "exp": int(time.time()) + ttl_seconds, "n": secrets.token_hex(8), **claims}
        body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
        return f"{body}.{self._sign(body)}"

    def verify(self, token: str, kind: str) -> Dict[str, Any]:
        body, _, signature = token.partition(".")
        if not body or not hmac.compare_digest(signature, self._sign(body)):
            raise Unauthenticated("bad token signature")
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if payload.get("k") != kind:
            raise Unauthenticated(f"expected a {kind} token")
        if payload.get("exp", 0) < time.time():
            raise Unauthenticated(f"{kind} token expired")
        return payload

    def _sign(self, body: str) -> str:
        return hmac.new(self._key, body.encode(), hashlib.sha256).hexdigest()


class DemoOAuthAuthenticator:
    """Accepts only access tokens this server issued; every caller acts as MCP_DEV_USER_EMAIL."""

    def __init__(self, settings: Settings) -> None:
        self._signer = TokenSigner(settings.demo_oauth_signing_key or "")
        self._identity = DevStaticAuthenticator(settings).authenticate({})

    def authenticate(self, headers: Mapping[str, str]) -> UserIdentity:
        self._signer.verify(bearer_from_headers(headers), "access")
        return self._identity


def build_oauth_app(settings: Settings) -> Starlette:
    base = settings.public_base_url or ""
    signer = TokenSigner(settings.demo_oauth_signing_key or "")

    def protected_resource(request: Request) -> Response:
        return JSONResponse(protected_resource_metadata(settings, base))

    def authorization_server(request: Request) -> Response:
        return JSONResponse({
            "issuer": base,
            "authorization_endpoint": f"{base}/authorize",
            "token_endpoint": f"{base}/token",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
            "scopes_supported": ["moveworks"],
        })

    async def authorize(request: Request) -> Response:
        params = dict(request.query_params) if request.method == "GET" else _strings(await request.form())
        log.info(json.dumps({"authorize": request.method, "client_id": params.get("client_id"),
                             "redirect_uri": params.get("redirect_uri"), "scope": params.get("scope"),
                             "pkce": bool(params.get("code_challenge"))}))
        if params.get("client_id") != settings.demo_oauth_client_id:
            return HTMLResponse("Unknown client_id", status_code=400)
        redirect_uri = params.get("redirect_uri", "")
        if not redirect_uri.startswith("https://"):
            return HTMLResponse("redirect_uri must be https", status_code=400)
        error = ""
        if request.method == "POST":
            if hmac.compare_digest(params.get("passphrase", ""), settings.demo_oauth_passphrase or ""):
                code = signer.issue("code", CODE_SECONDS, ru=redirect_uri, cc=params.get("code_challenge", ""))
                query = {"code": code, **({"state": params["state"]} if params.get("state") else {})}
                separator = "&" if "?" in redirect_uri else "?"
                return RedirectResponse(f"{redirect_uri}{separator}{urlencode(query)}", status_code=302)
            error = "<p class='err'>Wrong passphrase.</p>"
        hidden = "".join(
            f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">'
            for k, v in params.items() if k != "passphrase"
        )
        return HTMLResponse(_SIGN_IN_PAGE.format(email=html.escape(settings.dev_user_email or ""), error=error, hidden=hidden))

    async def token(request: Request) -> Response:
        form = _strings(await request.form())
        client_id, client_secret = _client_credentials(request, form)
        log.info(json.dumps({"token": form.get("grant_type"), "client_id": client_id}))
        if client_id != settings.demo_oauth_client_id or not hmac.compare_digest(
            client_secret or "", settings.demo_oauth_client_secret or ""
        ):
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        try:
            if form.get("grant_type") == "authorization_code":
                code = signer.verify(form.get("code", ""), "code")
                if form.get("redirect_uri") and form["redirect_uri"] != code["ru"]:
                    raise Unauthenticated("redirect_uri mismatch")
                if code.get("cc") and not _pkce_ok(form.get("code_verifier", ""), code["cc"]):
                    raise Unauthenticated("PKCE check failed")
            elif form.get("grant_type") == "refresh_token":
                signer.verify(form.get("refresh_token", ""), "refresh")
            else:
                return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
        except (Unauthenticated, ValueError) as exc:
            return JSONResponse({"error": "invalid_grant", "error_description": str(exc)}, status_code=400)
        return JSONResponse({
            "access_token": signer.issue("access", ACCESS_TOKEN_SECONDS),
            "refresh_token": signer.issue("refresh", REFRESH_TOKEN_SECONDS),
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_SECONDS,
            "scope": "moveworks",
        }, headers={"Cache-Control": "no-store"})

    return Starlette(routes=[
        Route("/.well-known/oauth-protected-resource", protected_resource),
        Route("/.well-known/oauth-protected-resource/mcp", protected_resource),
        Route("/.well-known/oauth-authorization-server", authorization_server),
        Route("/.well-known/openid-configuration", authorization_server),
        Route("/authorize", authorize, methods=["GET", "POST"]),
        Route("/token", token, methods=["POST"]),
    ])


def _strings(form: Any) -> Dict[str, str]:
    return {key: value for key, value in form.items() if isinstance(value, str)}


def _client_credentials(request: Request, form: Dict[str, str]) -> "tuple[Optional[str], Optional[str]]":
    header = request.headers.get("authorization", "")
    if header.lower().startswith("basic "):
        decoded = base64.b64decode(header[6:]).decode("utf-8")
        client_id, _, client_secret = decoded.partition(":")
        return client_id, client_secret
    return form.get("client_id"), form.get("client_secret")


def _pkce_ok(verifier: str, challenge: str) -> bool:
    digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return hmac.compare_digest(digest, challenge)


_SIGN_IN_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Demo sign-in</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{{font-family:-apple-system,system-ui,sans-serif;background:#f5f5f7;display:flex;justify-content:center;padding-top:12vh}}
form{{background:#fff;padding:32px;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.08);width:340px}}
h1{{font-size:20px;margin:0 0 8px}}p{{color:#555;font-size:14px}}
input[type=password]{{width:100%;padding:10px;font-size:15px;box-sizing:border-box;margin:12px 0}}
button{{width:100%;padding:10px;font-size:15px;background:#333;color:#fff;border:0;border-radius:8px;cursor:pointer}}.err{{color:#c00}}</style></head>
<body><form method="post" action="authorize"><h1>Demo sign-in</h1>
<p>This demo server is run by your team, not by Moveworks. Allow this MCP client to act as <b>{email}</b>.</p>{error}
<input type="password" name="passphrase" placeholder="Passphrase" autofocus>{hidden}
<button type="submit">Allow</button></form></body></html>"""
