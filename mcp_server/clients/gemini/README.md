# Gemini

Connect Gemini to the [MCP server](../../README.md): the Gemini app's custom connectors, Gemini CLI, and notes for Gemini Enterprise.

## Gemini app (gemini.google.com)

The Gemini app connects to custom MCP servers over HTTPS and signs users in with OAuth, using a client ID and secret. For a demo without your identity provider, run the server in `demo_oauth` mode behind a tunnel. Every token then acts as `MCP_DEV_USER_EMAIL`, so use a test user in a non-production Moveworks instance.

1. Add the demo settings to `.env`. This mode also needs the self-signed JWT settings (`MW_S2S_*`).

   ```bash
   MCP_DEV_USER_EMAIL=<roster email>
   MCP_DEMO_OAUTH_CLIENT_ID=<any string>
   MCP_DEMO_OAUTH_CLIENT_SECRET=<a long random string>
   MCP_DEMO_OAUTH_PASSPHRASE=<what you'll type on the sign-in page>
   MCP_DEMO_OAUTH_SIGNING_KEY=<a long random string>
   ```

   `python -c "import secrets; print(secrets.token_urlsafe(32))"` makes a random string.

2. Start a tunnel to port 8791 with any tunnel tool that gives a public HTTPS URL, then start the server with that URL:

   ```bash
   mcp_server/clients/gemini/demo_up.sh https://<public host>
   ```

   The script sets `MCP_AUTH_MODE=demo_oauth`, `MCP_PUBLIC_BASE_URL`, and `MCP_ALLOWED_HOSTS`. The server still binds only to localhost; the tunnel forwards to it. Anyone with the URL, client secret, and passphrase acts as `MCP_DEV_USER_EMAIL`, and the sign-in page doesn't limit attempts, so use a long passphrase and stop the tunnel when the demo ends.

3. In the Gemini app, open **Customize > Connectors > + Custom > Connect to an MCP server**. Enter `https://<public host>/mcp`, and under additional settings the client ID and secret from step 1. Gemini reads the server's OAuth metadata and opens the sign-in page; enter the passphrase.

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
