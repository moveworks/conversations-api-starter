#!/usr/bin/env bash
# Start the MCP server in demo_oauth mode behind a public tunnel URL, for the Gemini app.
#   mcp_server/clients/gemini/demo_up.sh https://<public-host>
# Reads .env from the repository root; see mcp_server/clients/gemini/README.md.
set -euo pipefail
cd "$(dirname "$0")/../../.."
url="${1:?usage: mcp_server/clients/gemini/demo_up.sh https://<public-host>}"
url="${url%/}"
export MCP_AUTH_MODE=demo_oauth MCP_PUBLIC_BASE_URL="$url" MCP_ALLOWED_HOSTS="${url#https://}"
exec python -m mcp_server
