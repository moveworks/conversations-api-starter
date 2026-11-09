"""MCP server (StreamableHTTP, stateless) exposing Moveworks as tools.

Run: ``python -m mcp_server`` from the repository root. Settings come from the environment and ``.env``
(see ``mcp_server/README.md``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, Dict, Iterator, Optional

import uvicorn
from dotenv import load_dotenv
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from moveworks_capi.client import ConversationsApiClient

from .config import Settings
from .discovery import OAuthRouter, build_discovery_app, resource_metadata_url
from .gate import AuthGate
from .identity import Authenticator, Unauthenticated, build_authenticator
from .service import MoveworksTools, ToolReply
from .turns import MoveworksTurns

SERVER_NAME = "Moveworks"
INSTRUCTIONS = (
    "Moveworks is the signed-in user's company help desk. It answers from the company's own "
    "knowledge base, catalog and tickets, so use it instead of general knowledge for any question about the "
    "user's work: IT (VPN, laptop, passwords, email, Wi-Fi, software or app access, printers), HR (PTO, "
    "benefits, payroll, onboarding), company policies, and their tickets or requests. General answers can be "
    "wrong for their company. Keep one Moveworks conversation per topic: pass the conversation_id from the "
    "latest result on every follow-up."
)


def build_mcp(tools: MoveworksTools, authenticator: Authenticator) -> MCPServer:
    mcp = MCPServer(SERVER_NAME, instructions=INSTRUCTIONS)

    async def identity_of(ctx: Context) -> Any:
        return await asyncio.to_thread(authenticator.authenticate, ctx.headers or {})

    # Not read-only: the assistant can act on plain text ("close my ticket"), and text injected into the
    # model's context could ask it to. Clients confirm unless the user chooses to allow this tool.
    @mcp.tool(
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True),
    )
    async def ask_moveworks(message: str, ctx: Context, conversation_id: Optional[str] = None) -> CallToolResult:
        """The user's company help desk. Use this, not general knowledge, for any workplace question: IT
        (VPN, laptop, passwords, email, Wi-Fi, software or app access, printers), HR (PTO, benefits,
        payroll), company policies, and the user's tickets or requests. It answers from the company's own
        knowledge base, checks ticket status, and starts requests. Pass the user's request in their own words, including what
        they want done (for example "file a ticket for..."); don't shorten it to just the topic.
        Always pass the conversation_id from the latest Moveworks result when the message follows up on
        it (answers a Moveworks question, refers to "that ticket", "it", or the same issue); omit it only
        for an unrelated new topic. If the result offers options, use choose_moveworks_option to pick one.
        Conversations expire after a period without use; if a conversation_id is rejected as unknown, start a
        new conversation."""
        try:
            identity = await identity_of(ctx)
        except Unauthenticated as exc:
            return _unauthenticated(exc)
        return _result(await tools.ask(identity, message, conversation_id))

    @mcp.tool(
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True),
    )
    async def choose_moveworks_option(
        conversation_id: str, option_id: str, option_label: str, ctx: Context
    ) -> CallToolResult:
        """Pick one of the options Moveworks offered in the last result, for example "File ticket".
        Pass the option_id and its exact option_label from that result. This can create or change
        records such as tickets."""
        try:
            identity = await identity_of(ctx)
        except Unauthenticated as exc:
            return _unauthenticated(exc)
        return _result(await tools.choose(identity, conversation_id, option_id, option_label))

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
    async def open_moveworks_form(conversation_id: str, option_id: str, ctx: Context) -> CallToolResult:
        """Open a form Moveworks offered (an option marked "form", e.g. "Complete this request: <item>" or
        "Edit/Add details") and see its fields and current values. Nothing is sent until
        submit_moveworks_form."""
        try:
            identity = await identity_of(ctx)
        except Unauthenticated as exc:
            return _unauthenticated(exc)
        return _result(await tools.open_form(identity, conversation_id, option_id))

    @mcp.tool(
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True),
    )
    async def submit_moveworks_form(
        conversation_id: str, ctx: Context, values: Optional[Dict[str, Any]] = None, action: str = "submit"
    ) -> CallToolResult:
        """Submit the open Moveworks form, setting any fields first: values maps field name (or label) to the
        value; for choice fields use one of the listed choices. Use action "cancel" to discard the form.
        Submitting can create requests or tickets. Ask the user for any required value you don't know."""
        try:
            identity = await identity_of(ctx)
        except Unauthenticated as exc:
            return _unauthenticated(exc)
        return _result(await tools.submit_form(identity, conversation_id, values, action))

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
    async def get_moveworks_result(conversation_id: str, ctx: Context) -> CallToolResult:
        """Get the result of a Moveworks request that came back with status "pending"."""
        try:
            identity = await identity_of(ctx)
        except Unauthenticated as exc:
            return _unauthenticated(exc)
        return _result(await tools.result(identity, conversation_id))

    return mcp


def _result(reply: ToolReply) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=reply.text)], structured_content=reply.data,
                          is_error=reply.is_error)


def _unauthenticated(exc: Exception) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=f"Not signed in: {exc}")],
                          structured_content={"status": "error", "error": "unauthenticated"}, is_error=True)


def turns_factory_for(settings: Settings):
    @contextlib.contextmanager
    def factory(identity: Any) -> Iterator[MoveworksTurns]:
        client = ConversationsApiClient(
            base_url=settings.mw_base_url, bot_name=settings.mw_bot_name, bearer_provider=identity.bearer_provider
        )
        try:
            yield MoveworksTurns(
                client,
                turn_timeout_seconds=settings.turn_timeout_seconds,
                modal_outcome_wait_seconds=settings.modal_outcome_wait_seconds,
            )
        finally:
            client.close()

    return factory


def build_app(settings: Settings, *, authenticator: Optional[Authenticator] = None, tools: Optional[MoveworksTools] = None):
    settings.validate()
    authenticator = authenticator or build_authenticator(settings)
    tools = tools or MoveworksTools(
        turns_factory_for(settings),
        deadline_seconds=settings.tool_deadline_seconds,
        idle_seconds=settings.conversation_idle_minutes * 60,
    )
    mcp = build_mcp(tools, authenticator)
    security = None
    if settings.allowed_hosts:
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=sorted(settings.allowed_hosts),
            allowed_origins=[f"https://{host}" for host in sorted(settings.allowed_hosts)],
        )
    app = mcp.streamable_http_app(stateless_http=True, transport_security=security, host=settings.host)
    if settings.auth_mode == "demo_oauth":
        from .demo_oauth import build_oauth_app

        oauth_app = build_oauth_app(settings)
    elif settings.oauth_issuer:
        oauth_app = build_discovery_app(settings)
    else:
        return AuthGate(app, authenticator, require_token_for_listing=settings.require_token_for_listing)
    gated = AuthGate(
        app,
        authenticator,
        require_token_for_listing=settings.require_token_for_listing,
        resource_metadata_url=resource_metadata_url(settings),
        scopes=settings.oauth_scopes,
    )
    return OAuthRouter(oauth_app, gated)


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    logging.getLogger("mcp_server").info("Moveworks MCP server: mode=%s bot=%s url=http://%s:%d/mcp",
                                         settings.auth_mode, settings.mw_bot_name, settings.host, settings.port)
    uvicorn.run(build_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
