import asyncio
import json
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from mcp_server.gate import AuthGate
from mcp_server.identity import Unauthenticated, bearer_from_headers


class TokenAuthenticator:
    def authenticate(self, headers) -> Any:
        token = bearer_from_headers(headers)
        if token != "good":
            raise Unauthenticated("bad token")
        return SimpleNamespace(user_key="alice@acme.com")


def call(gate: AuthGate, body: Any, token: Optional[str] = None):
    seen: Dict[str, Any] = {}

    async def inner(scope, receive, send):
        message = await receive()
        seen["body"] = message["body"]
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    gate._app = inner
    headers = [(b"content-type", b"application/json")]
    if token:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    raw = json.dumps(body).encode()
    sent: List[Dict[str, Any]] = []
    messages = [{"type": "http.request", "body": raw, "more_body": False}]

    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/mcp", "headers": headers}
    asyncio.run(gate(scope, receive, send))
    return sent[0]["status"], seen.get("body"), sent


def rpc(method: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": {}}


def test_tool_call_without_token_is_401_with_challenge():
    status, body, sent = call(AuthGate(None, TokenAuthenticator()), rpc("tools/call"))
    assert status == 401
    assert body is None
    assert (b"www-authenticate", b'Bearer error="invalid_token"') in sent[0]["headers"]


def test_tool_call_with_bad_token_is_401():
    status, _, _ = call(AuthGate(None, TokenAuthenticator()), rpc("tools/call"), token="nope")
    assert status == 401


def test_tool_call_with_good_token_reaches_the_app_with_the_body_intact():
    request = rpc("tools/call")
    status, body, _ = call(AuthGate(None, TokenAuthenticator()), request, token="good")
    assert status == 200
    assert body is not None
    assert json.loads(body) == request


def test_listing_without_token_passes_by_default():
    for method in ("initialize", "tools/list"):
        status, _, _ = call(AuthGate(None, TokenAuthenticator()), rpc(method))
        assert status == 200


def test_strict_mode_requires_a_token_for_listing():
    status, _, _ = call(AuthGate(None, TokenAuthenticator(), require_token_for_listing=True), rpc("tools/list"))
    assert status == 401


def test_batch_containing_a_tool_call_needs_a_token():
    status, _, _ = call(AuthGate(None, TokenAuthenticator()), [rpc("tools/list"), rpc("tools/call")])
    assert status == 401
