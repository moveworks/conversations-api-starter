"""Interactive OIDC bearer provider (Authorization Code + PKCE, loopback callback).

Runs the Authorization Code + PKCE flow against any OpenID Connect identity
provider, obtains the IdP's access token, and exchanges it at Moveworks
``/oauth/v1/token`` for a Moveworks bearer using the same ``jwt-bearer``
grant type used by the S2S path.

The authorize and token endpoints come from
``{issuer}/.well-known/openid-configuration``, fetched once per instance on
first use. Pass ``authorization_endpoint`` and ``token_endpoint`` to skip
discovery for an IdP that does not publish it.

See:
- https://docs.moveworks.com/api-reference/conversations-api/set-up-authentication-with-okta
- https://docs.moveworks.com/api-reference/conversations-api/set-up-authentication-with-azure-ad-entra-id
- https://datatracker.ietf.org/doc/html/rfc7636 (PKCE)

Design notes:

- The interactive flow blocks the calling thread on ``webbrowser.open`` and
  a small HTTP server on a loopback port. Suitable for CLIs and manual
  testing; not suitable for a headless service. For a service, use S2S JWT
  instead.
- The cached bearer is the Moveworks bearer, not the IdP token. Once the
  Moveworks bearer expires, the flow reruns from scratch.
- ``redirect_uri`` must match one you registered with the IdP. The class
  extracts the port from that URI and binds the loopback server to it.
- The IdP access token must be issued for an audience Moveworks can validate
  (your own app or API), never for a third-party resource such as Microsoft
  Graph. Its ``iss`` + ``aud`` pair is how Moveworks finds your credential.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import logging
import secrets
import threading
import time
import urllib.parse
import webbrowser
from typing import Optional, Tuple
from urllib.parse import urljoin

import requests

from ..errors import AuthenticationError

log = logging.getLogger(__name__)

LOGIN_TIMEOUT_SECONDS = 300
ENTRA_ISSUER_TEMPLATE = "https://login.microsoftonline.com/{tenant_id}/v2.0"


def entra_issuer(tenant_id: str) -> str:
    """The Entra ID v2.0 issuer for a tenant, as stamped in the token's ``iss``."""
    return ENTRA_ISSUER_TEMPLATE.format(tenant_id=tenant_id)


class OidcInteractiveBearer:
    """Callable that returns a cached Moveworks bearer sourced via interactive OIDC.

    Args:
        base_url: Moveworks data-center base URL, e.g. ``https://api.moveworks.ai``.
        issuer: The IdP issuer, exactly as it appears in the token's ``iss``
            claim and on the Moveworks credential.
        client_id: The client ID of the app registered with the IdP.
        redirect_uri: A redirect URI registered on that app, on the
            ``http://127.0.0.1:<port>/callback`` shape. The port is extracted
            from this URL and bound locally.
        scopes: Space-separated OAuth scopes. Must yield an access token whose
            ``aud`` matches the Audience on the Moveworks credential.
        authorization_endpoint: Overrides the discovered authorize endpoint.
        token_endpoint: Overrides the discovered token endpoint.
        idp_name: Shown in log lines and error messages.
        refresh_leeway_seconds: Seconds before expiration to refresh proactively.
    """

    def __init__(
        self,
        base_url: str,
        issuer: str,
        client_id: str,
        redirect_uri: str,
        *,
        scopes: str = "openid",
        authorization_endpoint: Optional[str] = None,
        token_endpoint: Optional[str] = None,
        idp_name: str = "IdP",
        refresh_leeway_seconds: int = 60,
    ) -> None:
        self._mw_token_url = urljoin(base_url.rstrip("/") + "/", "oauth/v1/token")
        self._issuer = issuer.rstrip("/")
        self._client_id = client_id
        self._redirect_uri = redirect_uri
        self._scopes = scopes
        self._authorization_endpoint = authorization_endpoint
        self._token_endpoint = token_endpoint
        self._idp_name = idp_name
        self._refresh_leeway = refresh_leeway_seconds
        self._lock = threading.Lock()
        self._cached_token: str = ""
        self._expires_at: float = 0.0

    def __call__(self) -> str:
        with self._lock:
            if self._cached_token and time.time() < self._expires_at - self._refresh_leeway:
                return self._cached_token
            access_token, expires_in = self.exchange(self.login())
            self._cached_token = access_token
            self._expires_at = time.time() + expires_in
            return self._cached_token

    def invalidate(self) -> None:
        """Drop the cached bearer so the next call fetches a new one.

        The client calls this after a 401, which can arrive before the cached
        bearer's own expiry (revoked credential, clock skew).
        """
        with self._lock:
            self._cached_token = ""
            self._expires_at = 0.0

    def endpoints(self) -> Tuple[str, str]:
        """``(authorization_endpoint, token_endpoint)``, discovering them if not overridden."""
        if self._authorization_endpoint is None or self._token_endpoint is None:
            discovered = self._discover()
            self._authorization_endpoint = self._authorization_endpoint or discovered[0]
            self._token_endpoint = self._token_endpoint or discovered[1]
        return self._authorization_endpoint, self._token_endpoint

    def login(self) -> str:
        """Run the browser login and return the IdP access token, without exchanging it.

        ``__call__`` uses this; call it directly to inspect the IdP token (see
        ``examples/get_idp_token.py``).
        """
        verifier = _make_code_verifier()
        state = secrets.token_urlsafe(24)
        auth_url = self.authorize_url(state=state, code_challenge=_make_code_challenge(verifier))
        received = self._await_callback(auth_url)

        if not received:
            raise AuthenticationError(
                f"{self._idp_name} login timed out after {LOGIN_TIMEOUT_SECONDS // 60} minutes"
            )
        if received.get("state") != state:
            raise AuthenticationError(f"{self._idp_name} callback state mismatch")
        if "error" in received:
            raise AuthenticationError(
                f"{self._idp_name} returned error: {received['error']}: "
                f"{received.get('error_description', '')}"
            )
        code = received.get("code")
        if not code:
            raise AuthenticationError(f"{self._idp_name} callback missing code: {received}")
        return self.redeem_code(code=code, code_verifier=verifier)

    def authorize_url(self, *, state: str, code_challenge: str) -> str:
        params = {
            "client_id": self._client_id,
            "response_type": "code",
            "scope": self._scopes,
            "redirect_uri": self._redirect_uri,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        authorization_endpoint, _ = self.endpoints()
        return f"{authorization_endpoint}?{urllib.parse.urlencode(params)}"

    def redeem_code(self, *, code: str, code_verifier: str) -> str:
        """Exchange an authorization code at the IdP token endpoint for its access token."""
        _, token_endpoint = self.endpoints()
        response = requests.post(
            token_endpoint,
            data={
                "grant_type": "authorization_code",
                "client_id": self._client_id,
                "code": code,
                "code_verifier": code_verifier,
                "redirect_uri": self._redirect_uri,
            },
            headers={"Accept": "application/json"},
            timeout=15.0,
        )
        if response.status_code >= 400:
            raise AuthenticationError(
                f"{self._idp_name} token endpoint returned {response.status_code}: {response.text[:200]}"
            )
        body = response.json()
        access_token = body.get("access_token")
        if not isinstance(access_token, str):
            raise AuthenticationError(f"{self._idp_name} token response missing access_token: {body}")
        return access_token

    def exchange(self, idp_access_token: str) -> Tuple[str, float]:
        """Exchange an IdP access token at Moveworks; returns ``(bearer, expires_in)``."""
        response = requests.post(
            self._mw_token_url,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": idp_access_token,
            },
            timeout=15.0,
        )
        if response.status_code >= 400:
            raise AuthenticationError(
                f"Moveworks /oauth/v1/token returned {response.status_code}: {response.text[:200]}"
            )
        body = response.json()
        access_token = body.get("access_token")
        expires_in = body.get("expires_in")
        if not isinstance(access_token, str) or not isinstance(expires_in, (int, float)):
            raise AuthenticationError(
                f"Moveworks token response missing access_token/expires_in: {body}"
            )
        return access_token, float(expires_in)

    def _discover(self) -> Tuple[str, str]:
        url = f"{self._issuer}/.well-known/openid-configuration"
        try:
            response = requests.get(url, headers={"Accept": "application/json"}, timeout=15.0)
        except requests.RequestException as exc:
            raise AuthenticationError(f"cannot fetch {url}: {exc}") from exc
        if response.status_code >= 400:
            raise AuthenticationError(f"{url} returned {response.status_code}: {response.text[:200]}")
        try:
            document = response.json()
        except ValueError as exc:
            raise AuthenticationError(f"{url} did not return JSON") from exc
        authorization_endpoint = document.get("authorization_endpoint") if isinstance(document, dict) else None
        token_endpoint = document.get("token_endpoint") if isinstance(document, dict) else None
        if not isinstance(authorization_endpoint, str) or not isinstance(token_endpoint, str):
            raise AuthenticationError(
                f"{url} is missing authorization_endpoint or token_endpoint; "
                "pass both to the constructor instead"
            )
        return authorization_endpoint, token_endpoint

    def _await_callback(self, auth_url: str) -> dict:
        parsed = urllib.parse.urlparse(self._redirect_uri)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port
        if port is None:
            raise ValueError(f"redirect_uri must include a port: {self._redirect_uri!r}")
        callback_path = parsed.path or "/callback"

        received: dict[str, str] = {}

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if not self.path.startswith(callback_path):
                    self.send_response(404)
                    self.end_headers()
                    return
                query = urllib.parse.urlparse(self.path).query
                received.update(dict(urllib.parse.parse_qsl(query)))
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    b"Login received. You can close this tab and return to the terminal."
                )

            def log_message(self, *_args, **_kwargs) -> None:
                return

        server = http.server.HTTPServer((host, port), _Handler)
        try:
            log.info("waiting for %s callback on %s", self._idp_name, self._redirect_uri)
            print(f"open this URL in your browser if it did not open automatically:\n  {auth_url}")
            try:
                webbrowser.open(auth_url)
            except webbrowser.Error:
                pass
            server.timeout = 0.25
            deadline = time.time() + LOGIN_TIMEOUT_SECONDS
            while not received and time.time() < deadline:
                server.handle_request()
        finally:
            server.server_close()
        return received


def _make_code_verifier() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")


def _make_code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
