#!/usr/bin/env bash
# Start the MCP server in demo_oauth mode behind a public tunnel URL, for clients that
# only accept OAuth, such as the Gemini app or ChatGPT.
#   mcp_server/clients/generic/demo_up.sh https://<public-host>
# Reads .env from the repository root; see mcp_server/clients/generic/README.md.
set -euo pipefail
cd "$(dirname "$0")/../../.."
url="${1:?usage: mcp_server/clients/generic/demo_up.sh https://<public-host>}"
url="${url%/}"
export MCP_AUTH_MODE=demo_oauth MCP_PUBLIC_BASE_URL="$url" MCP_ALLOWED_HOSTS="${url#https://}"
exec python -m mcp_server
