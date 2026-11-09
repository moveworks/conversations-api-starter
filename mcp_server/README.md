# MCP server example

An example [Model Context Protocol](https://modelcontextprotocol.io) server that puts the Moveworks assistant behind MCP tools. An MCP client such as Claude Code, ChatGPT, the Gemini app, or Gemini CLI calls the tools, and the server calls the Conversations API as the signed-in user, so answers, tickets, and requests are that user's own.

```
MCP client ──MCP (StreamableHTTP)──▶ mcp_server ──/rest/v1──▶ Moveworks
  signs the user in;                  resolves the user,
  their token rides on every call     runs the turn as them
```

You host and operate this server; Moveworks does not host it. Like the rest of this repo, it is sample code to adapt, built on the `moveworks_capi` library.

| Folder | For |
|---|---|
| [`clients/generic/`](clients/generic/) | Any MCP client: the endpoint, sign-in, the tool contract, a script that calls the tools, and the demo sign-in for OAuth-only clients |
| [`clients/claude-code/`](clients/claude-code/) | Claude Code |
| [`clients/chatgpt/`](clients/chatgpt/) | ChatGPT |
| [`clients/gemini/`](clients/gemini/) | The Gemini app, Gemini CLI, and notes for Gemini Enterprise |

---

## Tools

| Tool | Annotation | What it does |
|---|---|---|
| `ask_moveworks(message, conversation_id?)` | write | Sends the user's message. No `conversation_id` starts a new conversation. Returns the answer, sources, and options with ids |
| `choose_moveworks_option(conversation_id, option_id, option_label)` | write | Presses a button the assistant offered, such as "File ticket". The label is checked against the id, so a client's confirmation shows a readable choice |
| `open_moveworks_form(conversation_id, option_id)` | read-only | Opens a form the assistant offered and lists its fields, choices, and current values |
| `submit_moveworks_form(conversation_id, values?, action?)` | write | Sets fields (by name or label) and submits, or cancels |
| `get_moveworks_result(conversation_id)` | read-only | Collects a result that came back `pending` |

Every result has readable text, with option ids inline (`[opt_1] File ticket`), and `structuredContent` with `conversation_id` and `status`. A `completed` or `failed` result adds `text`, `sources[]` (`title`, `url`), `options[]` (`option_id`, `label`, and `kind`: `button` or `form`), and `notes[]`. A `pending` result adds `progress`, and a `form_open` result adds `form`. An `error` result adds `error` (a code) and `message`. Cancelling a form returns `cancelled`.

How the tools behave:

- **Writes go through the write tools.** While options are showing, `ask_moveworks` refuses a short reply such as "yes" or "yes, file it", an option label, or an option number, and tells the model to use `choose_moveworks_option`. Clients that confirm write tools then ask the user before a button is pressed.
- **`ask_moveworks` is annotated as a write tool.** The assistant can act on a plain-text message such as "close my ticket", whether the user typed it or text injected into the model's context (a web page, a document) asked for it. So clients confirm it like the other write tools. To let questions run without a prompt, allow `ask_moveworks` in your client; do that only for assistants whose plugins confirm before they change anything.
- **Options work once.** A repeated `choose_moveworks_option` or `submit_moveworks_form` (a client retry) returns the first outcome, even while it is still running, so a button is never pressed and a form never submitted twice. If Moveworks rejected the first attempt (for example with a 429), the retry sends it again. If the outcome is unknown, the result says so and asks the user to check the status instead. Options from earlier turns are rejected.
- **Forms.** Form options are named after the form. TEXT, DATE, DATETIME, BOOLEAN, and CHOICE fields can be set; read-only fields can't. This server doesn't upload files and can't fill in a required lookup field (a `CHOICE` field with no listed choices and no free input). For a form with either, the server says so when the form opens and returns the portal link. It also doesn't call the modal refresh endpoint, so on a form whose fields set `triggers_refresh`, a submit can fail validation; the error comes back as a tool result.
- **Slow turns.** A turn that runs past `MCP_TOOL_DEADLINE_SECONDS` returns `pending`. The turn keeps running, and `get_moveworks_result` collects it.
- **One turn at a time per user**, across conversations, to stay within rate limits.
- **Not offered as options by this server:** live agent chat and the Get Help menu (`Message.handoff`). The server mentions them in `notes[]`. Portal links appear as links, named after the form they open when a citation matches.
- **State is in memory.** Run one instance. Conversations expire after `MCP_CONVERSATION_IDLE_MINUTES` without use. A multi-instance deployment needs a shared store for conversation state. Turns run on a pool of 16 threads, and a user's queued turns wait on that user's lock inside the pool, so one user with many parallel conversations can delay others; add a per-user queue limit before serving many users.

The tool descriptions and server instructions in `server.py` decide when a model calls Moveworks, what it passes, and whether it keeps a conversation going. Change them for your assistant's scope, and test routing with your client after any change.

---

## Moveworks setup

The setup in the [root README](../README.md#moveworks-setup) applies: the Conversations API enabled for your Moveworks instance, a Conversations API chatbot (its Bot Name is `MW_BOT_NAME`), a credential for your sign-in mode, and roster users. Run `python examples/check_setup.py` first.

## Sign-in modes

Set `MCP_AUTH_MODE`:

| Mode | Use |
|---|---|
| `idp_exchange` (default) | The client signs users in with your Okta or Entra ID, and the server exchanges the token at Moveworks. The server holds no credential that can act as any user. The IdP token must be a JWT (Entra access tokens for a custom API are) |
| `google_s2s` | The client signs users in with Google. The server verifies the token (its audience must be in `MCP_GOOGLE_OAUTH_CLIENT_IDS`, its email in `MCP_ALLOWED_EMAIL_DOMAINS`) and acts as that email with a self-signed JWT credential. That credential can act as any roster user, so these checks are the only boundary: set the OAuth consent screen to Internal so only your Google Workspace accounts can sign in, and keep the private key in a secret store |
| `dev_static` | Local testing: every call acts as `MCP_DEV_USER_EMAIL` |
| `dev_static_map` | Local testing: `Bearer dev:<email>` acts as that roster user, so one server can serve two users |
| `demo_oauth` | Demos with clients that only accept OAuth, such as the Gemini app and ChatGPT. A built-in OAuth 2.1 server with a passphrase sign-in page; every token acts as `MCP_DEV_USER_EMAIL`. Never put it in front of a production Moveworks instance |

The `dev_*` and `demo_oauth` modes use the self-signed JWT settings (`MW_S2S_*`) and refuse to bind anything but localhost. The `dev_*` modes also refuse to start when `MCP_ALLOWED_HOSTS` or `MCP_PUBLIC_BASE_URL` is set: anyone who reaches them can act as any roster user. A tunnel makes a localhost server public, so tunnel only `demo_oauth`, and stop the tunnel when the demo ends.

A `tools/call` without a valid token gets HTTP 401 with a `WWW-Authenticate` challenge, so the client can send the user to sign in. `initialize` and `tools/list` pass without a token unless `MCP_REQUIRE_TOKEN_FOR_LISTING=true`; each request writes one log line recording what the client sent.

If the Moveworks credential lists Accepted Client IDs, add the OAuth client ID your MCP client signs users in with, and set Client ID Claim Name (`cid` for Okta, `azp` for Entra ID). A token whose client ID is missing or not listed fails the token exchange with `400 INVALID_JWT`.

### Point clients at your identity provider

MCP clients that sign users in on their own (they follow the [MCP authorization spec](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)) find the identity provider from the server. In `idp_exchange` or `google_s2s` mode, set:

| Variable | Value |
|---|---|
| `MCP_OAUTH_ISSUER` | Your identity provider's issuer. Okta: `https://{yourOktaDomain}/oauth2/{authServerId}`. Entra ID: `https://login.microsoftonline.com/{tenant_id}/v2.0`. Google: `https://accounts.google.com` |
| `MCP_OAUTH_SCOPES` | The scopes the client should request, space-separated: the scopes the Moveworks credential expects, from the [Okta](https://docs.moveworks.com/api-reference/conversations-api/set-up-authentication-with-okta) or [Entra ID](https://docs.moveworks.com/api-reference/conversations-api/set-up-authentication-with-azure-ad-entra-id) setup guide |
| `MCP_PUBLIC_BASE_URL` | The server's public https origin, such as `https://mcp.example.com` |

The server then publishes `/.well-known/oauth-protected-resource` naming that issuer, and its 401 challenge points there. The client runs the sign-in with your identity provider and sends the user's token; the server checks it as described in [Sign-in modes](#sign-in-modes). The server never sees passwords or runs the sign-in itself.

In your identity provider, register an OAuth app for each MCP client, with the redirect URI that client's documentation gives, and enter that app's client ID in the client. If the Moveworks credential lists Accepted Client IDs, add each of those client IDs. Okta, Entra ID, and Google don't register MCP clients automatically by default, so a client that relies on dynamic client registration or Client ID Metadata Documents can't sign in this way.

Entra ID's discovery document doesn't list `code_challenge_methods_supported`, which the MCP authorization spec requires clients to check, so a client that follows the spec strictly stops before sign-in. With Entra ID, test your client, or configure it with your Entra ID endpoints directly.

---

## Run it locally

From the repo root, with the virtual environment from the [Quickstart](../README.md#quickstart):

```bash
pip install -e ".[mcp]"
```

`.env.example` sets `MCP_AUTH_MODE=dev_static_map`; if your `.env` predates it, add that line next to the `MW_*` values. In this mode, `Bearer dev:<email>` acts as that roster user. Use any roster user, for example the `MW_S2S_USER_EMAIL` that `check_setup.py` passed with.

Start the server (stop it with Ctrl+C), then list its tools from another terminal:

```bash
python -m mcp_server
MOVEWORKS_MCP_TOKEN="dev:<roster email>" python mcp_server/clients/generic/mcp_chat.py list
```

It listens on `http://127.0.0.1:8791/mcp`. Then connect a client: [Claude Code](clients/claude-code/), [ChatGPT](clients/chatgpt/), [Gemini](clients/gemini/), or [any MCP client](clients/generic/).

## Settings

The server reads the environment and `.env`. Values already in the environment win.

| Variable | Default | Meaning |
|---|---|---|
| `MW_BASE_URL`, `MW_BOT_NAME` | (required) | As in `.env.example` |
| `MW_S2S_ISSUER`, `MW_S2S_AUDIENCE`, `MW_S2S_KEY_ID`, `MW_S2S_PRIVATE_KEY_PATH` | | Self-signed JWT credential, for `google_s2s`, `dev_*`, and `demo_oauth`. `MW_S2S_PRIVATE_KEY` takes the PEM itself |
| `MCP_AUTH_MODE` | `idp_exchange` | See [Sign-in modes](#sign-in-modes) |
| `MCP_DEV_USER_EMAIL` | | The roster user for `dev_static` and `demo_oauth` |
| `MCP_GOOGLE_OAUTH_CLIENT_IDS` | | `google_s2s`: accepted token audiences, comma-separated |
| `MCP_ALLOWED_EMAIL_DOMAINS` | | `google_s2s`: accepted email domains, comma-separated |
| `MCP_HOST`, `MCP_PORT` | `127.0.0.1`, `8791` | Bind address |
| `MCP_ALLOWED_HOSTS` | | Public host names the server answers to. The MCP SDK rejects other `Host` headers with HTTP 421, so set this behind a tunnel or a load balancer |
| `MCP_TOOL_DEADLINE_SECONDS` | `25` | How long a tool call waits before returning `pending` |
| `MCP_TURN_TIMEOUT_SECONDS` | `120` | How long a turn may run in the background |
| `MCP_MODAL_OUTCOME_WAIT_SECONDS` | `45` | How long to wait for the follow-up message after a form submit or an interim reply such as "Filing your ticket..." |
| `MCP_CONVERSATION_IDLE_MINUTES` | `60` | When an unused conversation is dropped |
| `MCP_REQUIRE_TOKEN_FOR_LISTING` | `false` | Require a token for `initialize` and `tools/list` too |
| `MCP_OAUTH_ISSUER`, `MCP_OAUTH_SCOPES` | | `idp_exchange` and `google_s2s`: see [Point clients at your identity provider](#point-clients-at-your-identity-provider) |
| `MCP_PUBLIC_BASE_URL` | | The server's public https origin, for OAuth discovery (`MCP_OAUTH_ISSUER` or `demo_oauth`) |
| `MCP_DEMO_OAUTH_*` | | `demo_oauth` only; see [Demo sign-in for OAuth-only clients](clients/generic/README.md#demo-sign-in-for-oauth-only-clients) |

## Layout

```
mcp_server/
├── server.py       # MCP tools, server instructions, app wiring
├── service.py      # Per-user conversation state, deadlines, consume-once options
├── turns.py        # One Conversations API turn: send text or press a button, collect the reply
├── forms.py        # Open, fill, and submit modal forms
├── models.py       # Option data shared by turns, service, and present
├── present.py      # Messages and actions to text and structuredContent
├── identity.py     # Sign-in modes: who is this, and which Moveworks bearer acts as them
├── gate.py         # 401 gate and request log in front of the MCP app
├── discovery.py    # OAuth discovery metadata naming your identity provider
├── demo_oauth.py   # Demo-only OAuth 2.1 server
├── config.py       # Settings
└── clients/        # Setup for each MCP client
```
