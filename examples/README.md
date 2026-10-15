# Examples

Runnable Python scripts, one Conversations API pattern each, built on the `moveworks_capi` library. Run them from the repo root so `.env` and relative key paths resolve. To choose an example by integration type or auth path, see the [root README](../README.md#choose-a-starting-point).

---

## Setup

From the repo root:

1. Confirm your Python version, then create a virtual environment and activate it.

   ```bash
   python3 --version
   python3 -m venv .venv
   source .venv/bin/activate
   ```

   On an older Python, the install in the next step fails with `File "setup.py" or "setup.cfg" not found`. Install Python 3.10 or later and create the virtual environment with it.

2. Install the library and the examples as an editable install.

   ```bash
   pip install -e .
   ```

3. For `receive_webhook.py` or the reference web app, also install the `app` extras. This adds FastAPI and uvicorn.

   ```bash
   pip install -e ".[app]"
   ```

4. Copy the environment template, then fill in `.env` as described below.

   ```bash
   cp .env.example .env
   ```

Every example uses these two variables:

| Variable | Value |
|---|---|
| `MW_BASE_URL` | Your data-center base URL. Defaults to `https://api.moveworks.ai` (US Prod). See the [Base URLs table](https://docs.moveworks.com/api-reference/overview#base-urls) for others (Canada, EU, Australia / Asia Pacific, and others). |
| `MW_BOT_NAME` | The Bot Name of the [Conversations API chatbot](https://docs.moveworks.com/api-reference/conversations-api/create-the-chatbot) you created in Moveworks Setup. Sent as the `Assistant-Name` header on every request. |

The remaining variables depend on which example you are running (see below).

---

## Examples

Recommended order for a new integration:

1. `check_setup.py` confirms your `.env` and Moveworks instance setup work end to end. Run it first.
2. `okta_call.py`, `entra_call.py`, `s2s_jwt_call.py`, or `api_key_call.py`, for your auth path. Each creates a conversation, sends a message, waits for the reply, and prints its text and actions.
3. `chat_multiturn.py` sends two turns on the same `conversation_id`.
4. `poll_events.py` reads proactive notifications, with the same auth path.
5. `receive_webhook.py` receives notifications by webhook, once you can host a public receiver.
6. `trigger_listener.py` fires an Agent Studio listener to send a test notification for steps 4 and 5.

To see these patterns in one chat surface, run the reference web app with `python -m app` (see the [root README](../README.md#reference-web-app)).

### `check_setup.py`: End-to-end setup diagnostic

Runs six checks in sequence: required env vars present, base URL reachable, auth exchange succeeds, `POST /conversations` returns a `conversation_id`, a submitted message reaches `COMPLETED`, and the reply contains text. On failure each check prints a diagnostic hint pointing at the most likely misconfiguration.

**Env vars:** whatever the auth path you picked with `MW_AUTH` requires (defaults to `s2s`). Same variables the individual hello-call scripts use.

```bash
python examples/check_setup.py                # uses MW_AUTH from .env
MW_AUTH=okta python examples/check_setup.py   # or override
```

### `okta_call.py`: Interactive Okta OIDC (PKCE)

Opens your default browser to Okta, catches the OAuth callback on a local loopback port, exchanges the Okta access token for a Moveworks bearer at `/oauth/v1/token`, creates a conversation, and sends a hello message.

**Use when:** you are building a user-facing chat surface and your users sign in through Okta.

**Docs:** [Set up authentication with Okta](https://docs.moveworks.com/api-reference/conversations-api/set-up-authentication-with-okta)

**Env vars:**

| Variable | Where to find it |
|---|---|
| `MW_OKTA_ISSUER` | The Issuer field on the Okta OIDC credential in Moveworks Admin Portal → HTTP Connectors → Credentials. Use a Custom Authorization Server, `https://{yourDomain}.okta.com/oauth2/{authServerId}`, as the [Okta setup guide](https://docs.moveworks.com/api-reference/conversations-api/set-up-authentication-with-okta) recommends. |
| `MW_OKTA_CLIENT_ID` | The Client ID of the OIDC app you configured in Okta admin → Applications. |
| `MW_OKTA_REDIRECT_URI` | Must be one of the Sign-in redirect URIs registered on that Okta app. Must be a loopback URL with a specific port, e.g. `http://127.0.0.1:8765/callback`. |
| `MW_OKTA_SCOPES` | Optional. Space-separated OAuth scopes. Defaults to `openid profile email`. |

**Run:**

```bash
python examples/okta_call.py
```

**Expected output on success:** browser opens, you complete the Okta login, terminal shows the same output as `s2s_jwt_call.py`: ids, status, reply text, and actions.

**Common failures:**
- `Okta returned error: invalid_client` → wrong Client ID, or the redirect URI you passed is not registered on the Okta app.
- `state mismatch` → the callback URL was modified, or an unrelated request reached the local callback port first.
- `Moveworks /oauth/v1/token returned 401` with "Client credentials not found" → no Moveworks credential matches the Okta access token's issuer and `aud` (the credential is looked up by both). Check them against the Okta credential in Moveworks Setup, byte for byte; `get_idp_token.py` prints the token's claims.

### `entra_call.py`: Interactive Entra ID (PKCE)

Same flow as `okta_call.py` against Entra ID (Azure AD): browser sign-in, loopback callback, the Entra access token exchanged at `/oauth/v1/token`, then a hello message.

**Use when:** you are building a user-facing chat surface and your users sign in through Entra ID.

**Docs:** [Set up authentication with Azure AD (Entra ID)](https://docs.moveworks.com/api-reference/conversations-api/set-up-authentication-with-azure-ad-entra-id)

**Env vars:**

| Variable | Where to find it |
|---|---|
| `MW_ENTRA_TENANT_ID` | Directory (tenant) ID on the app registration's Overview page. The issuer is `https://login.microsoftonline.com/{tenant_id}/v2.0`. |
| `MW_ENTRA_CLIENT_ID` | Application (client) ID on the same page. |
| `MW_ENTRA_REDIRECT_URI` | A loopback URL with a specific port, e.g. `http://127.0.0.1:8765/callback`, registered under **Authentication → Mobile and desktop applications**, with **Allow public client flows** set to Yes. The setup guide's Single-page application redirect is for browser clients. A command-line client uses Mobile and desktop applications, because a script cannot redeem a code issued to an SPA redirect, and a Web redirect needs a client secret. |
| `MW_ENTRA_SCOPES` | Optional. Defaults to `{client_id}/.default`, the scope the setup guide uses. It needs the `access_as_user` scope exposed and granted to the app (setup guide, step 1). Never a Microsoft Graph scope. |

**Moveworks credential pairing:** Issuer is the v2.0 issuer above; Audience is the token's `aud` (the client ID for v2.0 tokens); Identifier Claim `oid`; Identifier Type **IdP**; Integration ID is your Moveworks instance's MS Graph identity integration (commonly `ms_graph`). `oid` is a GUID, so Identifier Type Email never matches it.

**Run:**

```bash
python examples/entra_call.py
```

**Expected output on success:** browser opens, you complete the Entra sign-in, terminal shows the same output as `s2s_jwt_call.py`: ids, status, reply text, and actions.

**Common failures:**
- `Moveworks /oauth/v1/token returned 401` with `INVALID_JWT` "Client credentials not found" → the token's `iss` + `aud` pair matches no credential. Usual causes: a Graph or `openid`-only scope (the token's audience is Graph, not your app), or v1.0 tokens (`iss` is `https://sts.windows.net/{tenant_id}/`; set `accessTokenAcceptedVersion` to 2 in the manifest). `get_idp_token.py` with `MW_AUTH=entra` prints `iss` and `aud`.
- `Moveworks /oauth/v1/token returned 400` with `INVALID_JWT` for a well-formed, unexpired token → the token's `oid` does not match the object ID stored under the Graph integration on the user's Moveworks profile.
- `AADSTS7000218` (client secret required) → the redirect URI is registered as Web, or public client flows are off.
- `AADSTS9002327` (cross-origin redemption only) → the redirect URI is registered as SPA.

### `s2s_jwt_call.py`: Self-signed JWT (S2S)

Signs a short-lived RS256 JWT with your private key, exchanges it at `/oauth/v1/token` for a Moveworks bearer, creates a conversation, and sends a hello message. The same credential can act as any provisioned user by changing the `sub` claim.

**Use when:** you are building a backend service or evaluation harness that acts on behalf of different users with one credential.

**Docs:** [Set up service-to-service authentication](https://docs.moveworks.com/api-reference/conversations-api/set-up-service-to-service-authentication)

**Env vars:**

| Variable | Where to find it |
|---|---|
| `MW_S2S_ISSUER` | The Issuer field on the S2S JWT credential in Moveworks Admin Portal → HTTP Connectors → Credentials. A string you chose at creation time. |
| `MW_S2S_AUDIENCE` | The Audience field on the same credential. Also a string you chose. |
| `MW_S2S_KEY_ID` | The Key ID (`kid`) returned by Moveworks when you uploaded your public key. |
| `MW_S2S_PRIVATE_KEY_PATH` | Path to the RSA private key file (PEM). The public counterpart must be uploaded on the credential. |
| `MW_S2S_USER_EMAIL` | The user to act as. Signed as the JWT `sub` claim. Must be a provisioned Moveworks roster user. |

**Generate a keypair** (if you do not already have one):

```bash
mkdir -p keys
openssl genrsa -out keys/private_key.pem 2048
openssl rsa -in keys/private_key.pem -pubout -out keys/public_key.pem
chmod 600 keys/private_key.pem
```

Upload `keys/public_key.pem` on the credential in the Admin Portal.

**Run:**

```bash
python examples/s2s_jwt_call.py
```

**Expected output on success:** the new `conversation_id`, the `response_id`, `status: COMPLETED`, the assistant's reply text, then its actions (type, label, and the URL, callback id, or modal id of each). A hello usually carries no actions; ask for something that offers a form to see one.

**Common failures:**
- `token exchange returned 401: ... INVALID_JWT` with "Client credentials not found" → no registered credential matches your JWT's issuer and `aud`. Check both against the credential in Moveworks Setup, byte for byte, and confirm the credential is Published.
- `token exchange returned 401: ... INVALID_JWT` with "JWT signature verification failed" → the `kid` does not match a registered key, or the private key does not pair with the uploaded public key.
- Post-exchange failure → the value in `sub` does not resolve to a user in your roster. Confirm the identifier matches what Moveworks expects (typically the email).

### `api_key_call.py`: HTTP Connector API Key

Uses a long-lived opaque key minted at Moveworks Admin Portal → HTTP Connectors → Credentials as the bearer directly. No signing, no exchange. The key is bound to the roster user who minted it; every call runs as that user.

**Use when:** you are building a backend that always acts as one service-account user, without managing signing keys.

**Docs:** [API Credentials](https://docs.moveworks.com/api-reference/api-credentials) describes API Keys. The Conversations API setup guides cover the OAuth paths. Confirm with your Moveworks account team that the Conversations API accepts an API Key for your Moveworks instance before you use this path in production.

**Env vars:**

| Variable | Where to find it |
|---|---|
| `MW_API_KEY` | The raw UUID string returned when you minted the credential. Store it securely; the Admin Portal shows it only once. |

**Run:**

```bash
python examples/api_key_call.py
```

**Expected output on success:** same shape as `s2s_jwt_call.py`.

### `chat_multiturn.py`: Multi-turn conversation

Creates one conversation and sends two user messages, waiting for each reply before submitting the next. Reuse `conversation_id` across turns so the assistant keeps context between messages.

**Env vars:** whatever the auth path you picked with `MW_AUTH` requires (defaults to `s2s`). Same variables the individual hello-call scripts use.

```bash
python examples/chat_multiturn.py
MW_AUTH=okta python examples/chat_multiturn.py   # or entra | api-key
```

**Expected output:** two `> user:` / `< assistant [COMPLETED]:` pairs printed as each turn completes, each followed by that reply's actions.

### `poll_events.py`: Poll for proactive notifications

Polls `/rest/v1/conversations/-/events` for one user on a fixed interval. Dispatches each new event to a handler and advances a per-user checkpoint on disk. If the handler raises, the checkpoint stays put; the next poll retries from the last known-good position.

**Use when:** you cannot host a public inbound receiver, or you need a source-of-truth reconciler that runs alongside webhook push.

**Docs:** [Receive via polling](https://docs.moveworks.com/api-reference/conversations-api/proactive-notifications/receive-via-polling)

**Env vars:**

| Variable | Purpose |
|---|---|
| `MW_AUTH` | `s2s`, `okta`, `entra`, or `api-key`. Selects which bearer provider to use. Default `s2s`. |
| `MW_S2S_*`, `MW_OKTA_*`, `MW_ENTRA_*`, or `MW_API_KEY` | Depending on `MW_AUTH`, the same variables as the matching example above. |
| `MW_POLL_USER_ID` | Required when `MW_AUTH` is `okta`, `entra`, or `api-key`. The user identifier the checkpoint should be keyed under. For `MW_AUTH=s2s`, `MW_S2S_USER_EMAIL` is used automatically. |
| `CHECKPOINT_PATH` | Where to persist the `{user_id, last_event_id}` map. Defaults to `./checkpoints/checkpoint.json`. |
| `POLL_INTERVAL_SECONDS` | Time between polls. Defaults to `15`. |

**Run:**

```bash
python examples/poll_events.py
```

**Expected output:** the script prints `polling for user=... via auth=...`, then any new events, oldest first. On the first run for a user it sets the checkpoint to the newest existing event and prints nothing for the history; pass `--replay-history` to print the history instead. Later polls return only events after the stored checkpoint. `--once` runs one pass, then prints `dispatched N event(s); checkpoint X -> Y`.

**Routing:** Moveworks routes proactive messages to the user's most recent channel. Send at least one message as that user through `s2s_jwt_call.py` or `okta_call.py`, then trigger the notification, then poll. See the [proactive notifications overview](https://docs.moveworks.com/api-reference/conversations-api/proactive-notifications/overview).

### `receive_webhook.py`: Receive outbound webhook push

Starts a FastAPI receiver. On each delivery it verifies the RSA-SHA256 signature against your Moveworks instance's JWKS, deduplicates against `webhook-id`, and prints the envelope.

**Use when:** you can host a public HTTPS endpoint. Webhooks have lower latency than polling and put less load on Moveworks.

**Docs:** [Receive via outbound webhook](https://docs.moveworks.com/api-reference/conversations-api/proactive-notifications/receive-via-outbound-webhook)

**Env vars:**

| Variable | Purpose |
|---|---|
| `MW_ORG_DOMAIN` | Your Admin Portal domain, no scheme, no trailing slash. Used to fetch `https://{MW_ORG_DOMAIN}/.well-known/jwks.json` for signature verification. |
| `WEBHOOK_HOST` | Bind host. Defaults to `127.0.0.1`. Put the receiver behind your HTTPS reverse proxy or tunnel, and set `0.0.0.0` only when the proxy needs it. |
| `WEBHOOK_PORT` | Bind port. Defaults to `8080`. |
| `WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS` | Max clock skew accepted. Defaults to `300` (5 minutes). |

**Run:**

```bash
python examples/receive_webhook.py
```

The receiver accepts deliveries at `POST /webhooks/moveworks` and answers a health check at `GET /healthz`. Expose it publicly (for testing, a tunnel such as `ngrok` or `cloudflared` works), then register your public host plus `/webhooks/moveworks`, for example `https://hooks.example.com/webhooks/moveworks`, in Moveworks Admin Portal → Setup → Core Platform → Outbound Webhooks with event type `CONVERSATION_RESPONSE_RECEIVED`.

**Signing algorithm** (the docs' concatenation of `webhook-id`, `webhook-timestamp`, and the raw body, joined with `.`):

```
signing_string = f"{webhook-id}.{webhook-timestamp}.{raw_body}"
signature      = base64(RSA-SHA256(signing_string, private_key))
```

During a key-rotation grace period, the `webhook-signature` header can carry several space-delimited signatures. Accept the delivery if any of them verifies. `webhook-kid` identifies which JWKS key to use.

**Common failures:**
- `SignatureVerificationError` → the body or headers were modified in transit, for example by a proxy that rewrites JSON. The verifier needs the raw body bytes; do not re-serialize the body.
- `InvalidWebhookTimestampError` → your server clock has drifted more than 5 minutes from Moveworks. Fix NTP.
- `JwksFetchError` → the JWKS could not be fetched or parsed. The receiver answers `503` so Moveworks retries the delivery; check `MW_ORG_DOMAIN`.
- `UnknownKidError` → the delivery's `kid` is not in the JWKS. The cache refreshes on an unknown `kid`, so a persistent error means `MW_ORG_DOMAIN` points at the wrong key set.

### `trigger_listener.py`: Proactive notification trigger

POSTs `{"email", "message"}` to an Agent Studio listener whose plugin notifies that user, so `poll_events.py`, `receive_webhook.py`, and the reference web app's notification drawer have a notification to receive.

**Env vars:** `MW_TEST_LISTENER_URL`, `MW_TEST_NOTIFY_EMAIL` (the recipient; defaults to `MW_S2S_USER_EMAIL` when unset), and `MW_TEST_LISTENER_SECRET` for a secured listener (HMAC-SHA256 of the raw body, hex, in `X-Signature`).

```bash
python examples/trigger_listener.py "optional message text"
```

#### Testing proactive notifications

To send a test notification on demand, use an Agent Studio listener and a plugin that ends in a `notify` step. Build them once per test Moveworks instance:

1. **Compound action** with inputs `email` and `message`: a `mw.get_user_by_email` step, then `notify` with `recipient_id` set to that user's id and `message` set to the `message` input.
2. **Listener** with signature verification: HMAC-SHA256 over the raw body, read from the `X-Signature` header, with a secret you generate. Unsecured listeners also work, limited to one request per 10 seconds per Moveworks instance.
3. **Plugin** with a system trigger on that listener, mapping `parsed_body.email` and `parsed_body.message` into the compound action.
4. Put the listener URL (`https://api.moveworks.ai/webhooks/v1/listeners/{url id}/notify`), the secret, and your email in `.env` as `MW_TEST_LISTENER_URL`, `MW_TEST_LISTENER_SECRET`, and `MW_TEST_NOTIFY_EMAIL`.

Delivery goes to the channel the user used most recently, so send a message through the Conversations API first (from the web app, for example), then run `trigger_listener.py`. A notification usually arrives in a new conversation titled "System Initiated Conversation", so in the web app it lands in the notifications drawer, not the open thread. It can take several seconds to appear in `/events`. A secured listener accepts the request before verifying the signature, so a rejected signature appears only in the listener's logs in Agent Studio.

### `get_idp_token.py`: Interactive auth-legs diagnostic

Runs only the two auth legs (browser login, then the Moveworks exchange) and prints the discovered IdP endpoints, the decoded IdP access token, and the exchange result. Use it when `okta_call.py` or `entra_call.py` fails to see which leg fails and what the token contains. `MW_AUTH=okta` (default) or `MW_AUTH=entra` selects the `MW_OKTA_*` or `MW_ENTRA_*` variables.

```bash
MW_AUTH=entra python examples/get_idp_token.py
```

## Troubleshooting

### Saving a credential returns a 400 error

The credential form rejects an **Integration ID** unless **Identifier Type** is `IdP`:

```
integration_id must be empty unless identifier_type is IDP
```

Set **Integration ID** only when **Identifier Type** is `IdP`. For the common case, where the `sub` claim carries the user's email address, use Identifier Type `Email`, leave Integration ID blank, and keep Identifier Claim as `sub`. See the setup guide for your auth path for the `IdP` and `Record ID` alternatives. Credential fields cannot be edited after saving, so create a new credential if a value was wrong.

### `poll_events.py` or `receive_webhook.py` produce no output when redirected or backgrounded

Python's `print()` is block-buffered when stdout is not a terminal (redirected to a file, piped, running under `nohup` or a process supervisor). The two long-running scripts in this starter call `sys.stdout.reconfigure(line_buffering=True)` at startup so their output shows up promptly regardless. If you fork this pattern into your own long-running scripts, do the same, or run with `python -u` for unbuffered stdout.

### Outbound webhook is registered but no deliveries land

**Check the user's most recent channel.** Moveworks routes proactive notifications to the channel the user used most recently. If the user last used Moveworks in Slack or Teams, the notification goes there and no webhook delivery occurs. Send a message as that user through your Conversations API integration, then trigger the notification.
