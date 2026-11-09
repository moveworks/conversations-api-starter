# Any MCP client

What a client needs to use the [MCP server](../../README.md), whichever client it is.

## Connection

| | |
|---|---|
| Transport | StreamableHTTP, stateless. No SSE endpoint |
| Endpoint | `http://127.0.0.1:8791/mcp` locally; `https://<your host>/mcp` when deployed |
| Auth | `Authorization: Bearer <token>` on every request. What the token is depends on `MCP_AUTH_MODE` (see [Sign-in modes](../../README.md#sign-in-modes)) |
| Unauthenticated `tools/call` | HTTP 401 with `WWW-Authenticate: Bearer`. With `MCP_OAUTH_ISSUER` set, or in `demo_oauth` mode, the challenge includes `resource_metadata`, and the server publishes OAuth discovery metadata for clients that sign users in on their own |

A client that only accepts a static header works with the `dev_*` modes locally. A client that signs users in with OAuth works with `idp_exchange` or `google_s2s` against your identity provider, or with `demo_oauth` for a demo. For `idp_exchange` and `google_s2s`, set `MCP_OAUTH_ISSUER` so clients can find your identity provider (see [Point clients at your identity provider](../../README.md#point-clients-at-your-identity-provider)), or configure the client with your identity provider's endpoints.

## Tool contract

Build your client's confirmation policy around the tool annotations:

- `open_moveworks_form` and `get_moveworks_result` change nothing; allowing them without a prompt is reasonable.
- `ask_moveworks` is annotated as a write tool: some plugins act on a message such as "close my ticket" without asking first, and the message may come from text injected into the model's context. Allow it without a prompt only for assistants whose plugins confirm before they change anything.
- `choose_moveworks_option` and `submit_moveworks_form` can create or change records such as tickets. Keep a confirmation step on them. If a client runs every tool without asking, a model can press the assistant's own "Yes" button without checking with the user.

Pass `conversation_id` from the latest result on every follow-up. Without it, the follow-up starts a new conversation and the assistant loses the context. The server's instructions and tool descriptions tell the model this; clients that ignore server instructions rely on the tool descriptions alone.

A Moveworks turn can take a while, depending on what the assistant does. Set the client's tool timeout above `MCP_TOOL_DEADLINE_SECONDS`. A result with `status: pending` is collected with `get_moveworks_result`.

## Call the tools from a script

`mcp_chat.py` connects the way a remote MCP client does and prints each result's text, `structuredContent`, and duration. Use it to check a deployment before you connect a client. With the server running in `dev_static_map` mode:

```bash
export MOVEWORKS_MCP_TOKEN="dev:<roster email>"
python mcp_server/clients/generic/mcp_chat.py list
python mcp_server/clients/generic/mcp_chat.py call ask_moveworks '{"message": "How do I set up VPN?"}'
python mcp_server/clients/generic/mcp_chat.py call choose_moveworks_option \
  '{"conversation_id": "<from the last result>", "option_id": "opt_1", "option_label": "File ticket"}'
```

`--url` (or `MOVEWORKS_MCP_URL`) points it at a deployed server. The script is also the starting point for your own Python client: it connects with the `mcp` 2.x SDK (`streamable_http_client` with an `httpx2.AsyncClient` that carries the `Authorization` header) and reads each result's `structured_content`.

## Demo sign-in for OAuth-only clients

Some clients, such as the Gemini app and ChatGPT, only connect to servers that sign users in with OAuth and only reach public HTTPS URLs. For a demo without your identity provider, run the server in `demo_oauth` mode behind a tunnel. Every token then acts as `MCP_DEV_USER_EMAIL`, so use a test user in a non-production Moveworks instance.

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
   mcp_server/clients/generic/demo_up.sh https://<public host>
   ```

   The script sets `MCP_AUTH_MODE=demo_oauth`, `MCP_PUBLIC_BASE_URL`, and `MCP_ALLOWED_HOSTS`. The server still binds only to localhost; the tunnel forwards to it. Anyone with the URL, client secret, and passphrase acts as `MCP_DEV_USER_EMAIL`, and the sign-in page doesn't limit attempts, so use a long passphrase and stop the tunnel when the demo ends.

## Deploying

- Serve HTTPS with a publicly trusted certificate. Most remote clients reject localhost and self-signed certificates.
- Set `MCP_HOST=0.0.0.0` and `MCP_ALLOWED_HOSTS=<public host name>`.
- Run one instance, with CPU available outside requests: a turn that passes the tool deadline keeps running in the background.
- Use `idp_exchange` or `google_s2s`. The other modes refuse to bind a public address.
