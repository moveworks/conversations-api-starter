# Gemini

Connect Gemini to the [MCP server](../../README.md): the Gemini app's custom connectors, Gemini CLI, and notes for Gemini Enterprise.

## Gemini app (gemini.google.com)

The Gemini app connects to custom MCP servers over HTTPS and signs users in with OAuth, using a client ID and secret. For a demo without your identity provider:

1. Start the server behind a tunnel with the demo sign-in, as described in [Demo sign-in for OAuth-only clients](../generic/README.md#demo-sign-in-for-oauth-only-clients).
2. In the Gemini app, open **Customize > Connectors > + Custom > Connect to an MCP server**. Enter `https://<public host>/mcp`, and under additional settings the demo client ID and secret. Gemini reads the server's OAuth metadata and opens the sign-in page; enter the passphrase.

The connector shows the server's name, "Moveworks". If Gemini answers a workplace question from general knowledge, @mention the connector in the prompt. Gemini shows a progress message while a tool runs.

If the tunnel URL changes when you restart it, remove the connector and add it again with the new URL.

## Gemini CLI

Add the server to `~/.gemini/settings.json`; [`settings.example.json`](settings.example.json) shows the entry. With the server in `dev_static_map` mode, the header picks the roster user. Run Gemini CLI from a folder without code in it, or it may read files instead of calling the tools. The tools appear as `mcp_moveworks_<tool>`.

Gemini CLI asks before running tools that aren't read-only, which includes `ask_moveworks`. In non-interactive runs, those tools reach the model only with `--approval-mode yolo`, which also skips every confirmation, so test interactively.

## Gemini Enterprise

Gemini Enterprise adds custom MCP servers as data stores. Google's [setup guide](https://docs.cloud.google.com/gemini/enterprise/docs/connectors/custom-mcp-server/set-up-custom-mcp-server) covers the requirements, including StreamableHTTP, OAuth 2.0 sign-in, and an organization policy that [blocks custom MCP data stores](https://docs.cloud.google.com/gemini/enterprise/docs/connectors/custom-mcp-server/override-constraint-for-custom-mcp-data-stores) until an admin overrides it. For this server:

- Deploy it as described in [Deploying](../generic/README.md#deploying), with `MCP_AUTH_MODE=idp_exchange` (users sign in with your Okta or Entra ID) or `google_s2s` (users sign in with Google).
- The signed-in user's email must match a user in your Moveworks roster.
- Imported tools start disabled; enable all five. Gemini Enterprise asks the user to confirm tools that aren't annotated read-only, so `ask_moveworks` asks for confirmation too.
- `structuredContent` carries `sources[]` with `title` and `url`, for Google's [citation settings](https://docs.cloud.google.com/gemini/enterprise/docs/connectors/custom-mcp-server/configure-citations-for-custom-mcp-data-stores).
