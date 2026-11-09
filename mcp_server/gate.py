"""ASGI middleware in front of the MCP app: auth gate plus one log line per request.

A rejected token must be an HTTP 401, not a tool error, so the client can send
the user back through sign-in. ``tools/call`` always needs a token.
``initialize`` and ``tools/list`` pass without one unless
``require_token_for_listing`` is set, because some clients list tools before the user signs in; the log line
records what the client sent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence

from starlette.types import Receive, Scope, Send

from .identity import Authenticator, Unauthenticated

log = logging.getLogger("mcp_server.requests")


class AuthGate:
    def __init__(
        self,
        app: Any,
        authenticator: Authenticator,
        *,
        require_token_for_listing: bool = False,
        resource_metadata_url: Optional[str] = None,
        scopes: Sequence[str] = (),
    ) -> None:
        self._app = app
        self._challenge = 'Bearer error="invalid_token"'
        if resource_metadata_url:
            # Tells an OAuth-capable client (MCP authorization spec) where to discover sign-in.
            self._challenge += f', resource_metadata="{resource_metadata_url}"'
        if scopes:
            self._challenge += f', scope="{" ".join(scopes)}"'
        self._challenge_bytes = self._challenge.encode()
        self._authenticator = authenticator
        self._strict = require_token_for_listing

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        started = time.monotonic()
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        body = b""
        if scope["method"] == "POST":
            body = await _read_body(receive)
        methods = _jsonrpc_methods(body)
        token_present = headers.get("authorization", "").lower().startswith("bearer ")
        status: List[int] = [0]

        async def logging_send(message: Dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                status[0] = message["status"]
            await send(message)

        try:
            if scope["method"] == "POST" and self._needs_token(methods):
                try:
                    await asyncio.to_thread(self._authenticator.authenticate, headers)
                except Unauthenticated as exc:
                    await _unauthorized(logging_send, str(exc), self._challenge_bytes)
                    return
            await self._app(scope, _replay(body, receive), logging_send)
        finally:
            log.info(json.dumps({
                "http": f"{scope['method']} {scope.get('path')}",
                "methods": methods,
                "token": token_present,
                "status": status[0],
                "ms": round((time.monotonic() - started) * 1000),
                "protocol": headers.get("mcp-protocol-version"),
                "session": headers.get("mcp-session-id"),
                "client": _client_info(body),
            }))

    def _needs_token(self, methods: List[str]) -> bool:
        if self._strict:
            return True
        return "tools/call" in methods


async def _read_body(receive: Any) -> bytes:
    chunks = []
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunks.append(message.get("body", b""))
        if not message.get("more_body"):
            break
    return b"".join(chunks)


def _replay(body: bytes, receive: Any) -> Any:
    sent = False

    async def replay() -> Dict[str, Any]:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return replay


def _parse(body: bytes) -> List[Mapping[str, Any]]:
    if not body:
        return []
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return []
    items = payload if isinstance(payload, list) else [payload]
    return [item for item in items if isinstance(item, dict)]


def _jsonrpc_methods(body: bytes) -> List[str]:
    return [str(item.get("method")) for item in _parse(body) if item.get("method")]


def _client_info(body: bytes) -> Any:
    for item in _parse(body):
        if item.get("method") == "initialize":
            return (item.get("params") or {}).get("clientInfo")
    return None


async def _unauthorized(send: Any, reason: str, challenge: bytes) -> None:
    body = json.dumps({"error": "unauthorized", "message": reason}).encode()
    await send({
        "type": "http.response.start",
        "status": 401,
        "headers": [
            (b"content-type", b"application/json"),
            (b"www-authenticate", challenge),
        ],
    })
    await send({"type": "http.response.body", "body": body})
