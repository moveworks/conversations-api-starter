"""Call the MCP server's tools over StreamableHTTP, the way a remote MCP client does.

    python mcp_server/clients/generic/mcp_chat.py list
    python mcp_server/clients/generic/mcp_chat.py call ask_moveworks '{"message": "How do I set up VPN?"}'
    python mcp_server/clients/generic/mcp_chat.py call get_moveworks_result '{"conversation_id": "..."}'

The bearer comes from --token or MOVEWORKS_MCP_TOKEN (for example ``dev:alex@example.com`` with
MCP_AUTH_MODE=dev_static_map).

Prints the result text, the structuredContent and the call duration.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from typing import Any, Dict, Optional

import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent


async def session_call(url: str, token: Optional[str], action: str, tool: Optional[str], args: Dict[str, Any]) -> int:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx2.AsyncClient(headers=headers, timeout=180) as http:
        async with streamable_http_client(url, http_client=http) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                if action == "list":
                    for t in (await session.list_tools()).tools:
                        print(f"{t.name}  annotations={t.annotations.model_dump(exclude_none=True) if t.annotations else None}")
                        print(f"  input={json.dumps(t.input_schema.get('properties', {}))}")
                    return 0
                started = time.monotonic()
                result = await session.call_tool(tool or "", args)
                elapsed = time.monotonic() - started
                print("\n".join(c.text for c in result.content if isinstance(c, TextContent)))
                print("---")
                print(json.dumps({"is_error": result.is_error, "seconds": round(elapsed, 1),
                                  "structured": result.structured_content}, indent=2))
                return 1 if result.is_error else 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=os.environ.get("MOVEWORKS_MCP_URL", "http://127.0.0.1:8791/mcp"))
    parser.add_argument("--token", default=os.environ.get("MOVEWORKS_MCP_TOKEN"))
    parser.add_argument("action", choices=["list", "call"])
    parser.add_argument("tool", nargs="?")
    parser.add_argument("args", nargs="?", default="{}")
    ns = parser.parse_args()
    raise SystemExit(asyncio.run(session_call(ns.url, ns.token or None, ns.action, ns.tool, json.loads(ns.args))))


if __name__ == "__main__":
    main()
