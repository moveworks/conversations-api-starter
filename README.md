# Moveworks Conversations API Starter

Python sample code for the [Moveworks Conversations API](https://docs.moveworks.com/api-reference/conversations-api/overview). It includes a client library, runnable examples for each authentication path, and a reference chat app.

> [!NOTE]
> The Conversations API is in **Controlled Availability**. Endpoints, behaviors, and configuration surfaces may change during CA. Contact your Moveworks account team to participate.

Use it to build:

- **A custom chat surface** where employees sign in through your identity provider and talk to the Moveworks assistant from your portal, app, or other channel.
- **A backend or workflow integration** where a trusted service acts on behalf of specific users.
- **An evaluation harness** that runs test queries through the assistant.

---

## What's included

| Directory | Contents |
|---|---|
| [`moveworks_capi/`](moveworks_capi/) | A client library: HTTP client, auth providers, responses, streaming, file upload, conversations, event polling, webhook verification, and helpers for displaying messages |
| [`examples/`](examples/) | Runnable scripts, one pattern each. [`examples/README.md`](examples/README.md) lists the environment variables, expected output, and common failures for each. |
| [`app/`](app/) | A reference web app: a chat surface built on the library. See [Reference web app](#reference-web-app). |
| [`tests/`](tests/) | An offline test suite |

```
moveworks_capi/
├── client.py            # HTTP client, Assistant-Name header, retries, backoff
├── errors.py            # Named exceptions
├── responses.py         # Build turn input, submit, poll to completion; extract text and actions
├── streaming.py         # SSE client for /responses/stream
├── files.py             # Files API (alpha) upload, for files attached to a turn or a modal FILE field
├── conversations.py     # Create, list, fetch, and update conversations; list messages
├── rendering.py         # Helpers for displaying assistant messages in a UI
├── auth/                # Okta and Entra ID OIDC (PKCE), self-signed JWT (S2S), HTTP Connector API Key
└── notifications/       # /events polling with a checkpoint, single-event reads; webhook signature verification
```

Each library module can be used on its own. Two small interfaces connect them:

- `BearerProvider = Callable[[], str]`. The client asks its provider for a bearer on every request, and the provider handles caching.
- `Checkpoint`, a Protocol (`load(user_id)`, `advance(user_id, event_id)`) that the polling loop calls. `InMemoryCheckpoint` and `FileCheckpoint` are included; in production, back it with a database.

---

## Quickstart

Requires Python 3.10 or later and a Moveworks instance set up for the Conversations API (see [Moveworks setup](#moveworks-setup)).

1. Clone the repo and change into it.

   ```bash
   git clone https://github.com/moveworks/conversations-api-starter.git
   cd conversations-api-starter
   ```

2. Confirm your Python version, then create a virtual environment and activate it.

   ```bash
   python3 --version
   python3 -m venv .venv
   source .venv/bin/activate
   ```

   On an older Python, the install in the next step fails with `File "setup.py" or "setup.cfg" not found`. Install Python 3.10 or later and create the virtual environment with it.

3. Install the library and the examples. This is an editable install, so changes you make to `moveworks_capi/` take effect without reinstalling.

   ```bash
   pip install -e .
   ```

4. To run the reference web app or `receive_webhook.py`, also install the `app` extras. This adds FastAPI and uvicorn.

   ```bash
   pip install -e ".[app]"
   ```

5. Copy the environment template, then fill in your base URL, Bot Name, and the credential for your [auth path](#choose-an-auth-path).

   ```bash
   cp .env.example .env
   ```

6. Check your setup. The script confirms your environment variables, authentication, and a full round trip, and names the likely cause of any failure.

   ```bash
   python examples/check_setup.py
   ```

7. Make a first call. Each hello-call script creates a conversation, sends a message, and prints the reply and its actions. Use the one for your auth path: `s2s_jwt_call.py`, `okta_call.py`, `entra_call.py`, or `api_key_call.py`.

   ```bash
   python examples/s2s_jwt_call.py
   ```

Run scripts from the repo root so `.env` and relative key paths resolve.

---

## Use the library

A first call with the S2S credential: create a conversation, send one turn, wait for the reply, then print its text, actions, and Get Help menu. Replace the placeholder strings with the values from your `.env`.

```python
from moveworks_capi.auth import S2SJwtBearer
from moveworks_capi.client import ConversationsApiClient
from moveworks_capi.conversations import create_conversation
from moveworks_capi.responses import create_response, extract_actions, extract_handoff, extract_text, wait_for_completion

base_url = "https://api.moveworks.ai"
bearer = S2SJwtBearer(base_url, issuer="your-issuer", audience="your-audience", key_id="your-key-id",
                      private_key="keys/private_key.pem", user_identifier="user@example.com")
client = ConversationsApiClient(base_url, bot_name="your-bot-name", bearer_provider=bearer)

conversation_id = create_conversation(client, title="First call")["conversation_id"]
response_id = create_response(client, conversation_id, "How do I reset my password?")["response_id"]
response = wait_for_completion(client, conversation_id, response_id)
print(extract_text(response))
for action in extract_actions(response):
    print(action.type, action.label)
for category in extract_handoff(response):
    print(category.name, [item.title for item in category.items])
```

To attach a file to a turn, upload it with `files.upload_file`, then pass its `id` in `file_ids`. A path uploads under its file name; bytes or a stream need a `filename`. A turn carries up to five files.

```python
from moveworks_capi.files import upload_file

uploaded = upload_file(client, "screenshot.png")
response_id = create_response(client, conversation_id, "What does this error mean?", file_ids=[uploaded.id])["response_id"]
```

When the reply offers forms, its text ends with a plain-text copy of each one. A surface that shows forms as buttons displays `rendering.strip_flattened_forms(output["message"])` for each `MESSAGE` output in `response.outputs` instead of `extract_text(response)`. For other auth paths, swap the bearer provider; see the hello-call scripts in [`examples/`](examples/).

---

## Choose a starting point

| You are building | Start with | Auth path |
|---|---|---|
| A custom chat surface, users sign in with Okta | [`okta_call.py`](examples/okta_call.py), then the [reference web app](#reference-web-app) | Okta OIDC + PKCE |
| A custom chat surface, users sign in with Entra ID | [`entra_call.py`](examples/entra_call.py) | Entra OIDC + PKCE |
| A backend service that acts on behalf of specific users | [`s2s_jwt_call.py`](examples/s2s_jwt_call.py) | Self-signed JWT |
| An evaluation harness that runs queries as many users | [`s2s_jwt_call.py`](examples/s2s_jwt_call.py) | Self-signed JWT |
| A backend that acts as a single service-account user | [`api_key_call.py`](examples/api_key_call.py) | HTTP Connector API Key |
| Any of the above, plus proactive notifications | [`poll_events.py`](examples/poll_events.py) or [`receive_webhook.py`](examples/receive_webhook.py) | Any backend path |

### Choose an auth path

| Path | Credential | Use for | Setup |
|---|---|---|---|
| **Okta OIDC (PKCE)** | The user signs in through your Okta tenant. | User-facing chat surfaces | [Okta setup](https://docs.moveworks.com/api-reference/conversations-api/set-up-authentication-with-okta) |
| **Entra ID (PKCE)** | The user signs in through your Entra tenant. | User-facing chat surfaces | [Entra setup](https://docs.moveworks.com/api-reference/conversations-api/set-up-authentication-with-azure-ad-entra-id) |
| **Self-signed JWT (S2S)** | You hold a private key and sign a JWT with the user in `sub` for each call. | Backend services and evaluation harnesses that act on behalf of users. One key can act as any provisioned user. | [S2S setup](https://docs.moveworks.com/api-reference/conversations-api/set-up-service-to-service-authentication) |
| **HTTP Connector API Key** | A long-lived bearer bound to one user. | Backends that always act as one service-account user | [API Credentials](https://docs.moveworks.com/api-reference/api-credentials). Confirm with your Moveworks account team that the Conversations API accepts an API Key for your Moveworks instance. |

### Choose a notification delivery model

| Pattern | Use when | Example |
|---|---|---|
| **Outbound webhook** | You can host a public HTTPS endpoint. This is the recommended default: lower latency and less load on Moveworks. | [`receive_webhook.py`](examples/receive_webhook.py) |
| **Polling** | You cannot host a public receiver, or you want to reconcile missed webhook deliveries. | [`poll_events.py`](examples/poll_events.py) |

Both use the same per-user checkpoint. See [Receive via polling](https://docs.moveworks.com/api-reference/conversations-api/proactive-notifications/receive-via-polling) and [Receive via outbound webhook](https://docs.moveworks.com/api-reference/conversations-api/proactive-notifications/receive-via-outbound-webhook). To produce a test notification, use [`trigger_listener.py`](examples/trigger_listener.py).

---

## Reference web app

A chat surface over the Conversations API, built on the library, with FastAPI on the server and plain ES modules in the browser. There is no build step.

```bash
python -m app                # serve on http://127.0.0.1:8090
python -m app --no-stream    # submit and poll instead of streaming
python -m app --port 9000    # serve on another port
```

The app serves on port 8090 unless you pass `--port`. If that port is in use, the app exits with a message naming the port.

The connect screen accepts either credential:

- **API key.** Paste an HTTP Connector API key. The server holds it in memory only, never on disk or in browser storage.
- **S2S credential from `.env`.** The server reads the `MW_S2S_*` values (see [`.env.example`](.env.example)), signs the JWT, and exchanges it for a bearer. The browser never receives the key. The screen shows which user the app acts as.

Base URL and Bot Name default to `MW_BASE_URL` and `MW_BOT_NAME`. The app makes one `ListConversations` call to confirm the credential before it opens the chat.

The app handles:

- Streamed replies over server-sent events, with the reasoning trail. A stream that closes before the answer falls back to polling.
- Action buttons for links, callbacks, and modal forms, including user-consent (OBO/UCA) links. Once one action on a message is taken, the others are disabled.
- Offered forms. The plain-text copy of each form is removed from the answer (`rendering.strip_flattened_forms`), and buttons that share the label "Complete this request" are named after their form.
- Native modal forms (`GetModal`, `RefreshModal`, `SubmitModal`), including conditional fields that refresh when a field flagged `triggers_refresh` changes.
- Smart-handoff actions, and the Get Help menu from `Message.handoff`. A `HANDOFF_ACTION` opens the menu from its place in the actions row. Otherwise the menu sits behind one button on the newest turn.
- Citations, as a collapsed Sources list.
- Feedback, with an optional comment on "not helpful" (`additional_feedback`).
- Proactive notifications from the events API. Follow-ups for the open conversation join the thread, and notifications for other conversations appear in a drawer.
- Live agent replies (`actor: LIVE_AGENT`) as they arrive, under the agent's name and picture from `sender_info` when present, or "Live agent" otherwise, and the farewell after the user exits the chat.
- A conversation list with titles, paging (`next_cursor`), rename, archive, and an archived view.
- Conversation history, with older pages loaded on demand and attached file names shown.

The app runs locally with one connected Moveworks instance per process. It binds to `127.0.0.1`, has no login, and rejects requests with a non-local `Host`, cross-origin writes, and non-JSON writes, so other websites cannot drive it through your browser. Do not expose it on a network.

The [demo app](https://github.com/moveworks/conversations-api-demo-app) is a styled build of the same surface, with file attachments, inline form cards, a protocol inspector, motion, dark mode, and a test notification form. To build your own surface, fork this repo.

---

## Moveworks setup

Before the code in this repo can make calls, your Moveworks instance must be set up for the Conversations API, per the [Conversations API overview](https://docs.moveworks.com/api-reference/conversations-api/overview):

- Your Moveworks account team enables the Conversations API for your Moveworks instance. Requests return `404` until it is enabled.
- You [create the Conversations API chatbot](https://docs.moveworks.com/api-reference/conversations-api/create-the-chatbot) in Moveworks Setup. Its Bot Name is the `Assistant-Name` header value on every request.
- You create a credential for your auth path, following that path's setup guide. The users you act for must resolve against your Moveworks roster, by email by default.

To receive proactive notifications, the user must have sent a message through your Conversations API integration. Moveworks routes proactive messages to the user's most recent channel; see the [proactive notifications overview](https://docs.moveworks.com/api-reference/conversations-api/proactive-notifications/overview).

---

## Not included

- **A production chat UI.** The reference web app runs on localhost for one Moveworks instance, with no login or multi-user sessions. Hosting and hardening are up to your integration.
- **A batch evaluation harness.** Wrap the S2S JWT provider in your own parallelization and output.
- **Live agent session state.** The app renders agent replies, handoff actions, and the action that ends the chat, but does not track queue position or whether the session is still open.
- **File upload in the reference app.** The library uploads files (`files.upload_file`), but the app has no attach button and shows modal `FILE` fields as unsupported.
- **`ListResponseMessages`.** Use `conversations.list_messages`, which returns the messages for the whole conversation.
- **An async client.** The library uses `requests`. In async code, call it from a worker thread with `asyncio.to_thread`, as the reference app does.

This repo is sample code, not an official Moveworks SDK. The API is standard REST and works with any HTTP client.

---

## API usage notes

- **Send turns as `chat` or `action` input.** A typed turn is `{"input": {"chat": {"text": ...}}}`, with text up to 20,000 characters and up to five files in `files`. A button press is `{"input": {"action": {"callback_id": ...}}}`. The flat `{"text"}` and `{"callback_id"}` inputs are deprecated. `responses.user_input` builds either.
- **Upload files before the turn that attaches them.** The [Files API](https://docs.moveworks.com/api-reference/alpha-files-api/files/upload-file) is in alpha and enabled per Moveworks instance; until it is enabled, uploads return `404`. `files.upload_file` sends `POST /rest/v1alpha1/files` as `multipart/form-data`, with the `purpose` field before the `file` part as the API requires. A file can be up to 100 MB; `upload_file` checks the size and the filename rules before it sends anything. A large file can need more than the client's per-request timeout, so pass `timeout` to `upload_file`. Like a turn, an upload is not resent after a timeout, and a timed-out upload may have been stored with no `id` returned. Moveworks detects the content type from the bytes and returns it as `content_type`. A file expires 24 hours after it is created, and a turn that references an expired id, or a file another user uploaded, is rejected. There is no operation to read, list, or delete a file.
- **Use the `/rest/v1` base path.** `/assistant/v1` is deprecated, and modals are available only on `/rest/v1`. To correlate a request with Moveworks logs, read the `x-trace-id` response header.
- **Render both `actions` and `Message.handoff`.** Assistant replies can carry an `actions` array (`URL_ACTION`, `CALLBACK_ACTION`, `MODAL_ACTION`, `HANDOFF_ACTION`), which a text-only client drops. `responses.message_actions` normalizes it. `Message.handoff` carries the Get Help menu and can appear on any turn, including during a handoff. Unless the message carries a `HANDOFF_ACTION`, offer it on the newest turn.
- **Open the handoff menu from a `HANDOFF_ACTION`.** Render its label in its place in the actions row. Pressing it opens a menu built from the same message's `handoff` options and sends nothing. Omit it when the message has no handoff options. The menu is `handoff.handoff_categories[]`, each with a `name`, an optional `description`, and `handoff_items[]`. An `ACTION` item carries a regular `action`; a `TEXT` item carries `text` to display. `responses.message_handoff` normalizes it.
- **Attribute live agent messages by `actor`.** A `LIVE_AGENT` message is from a human agent. Agent replies arrive on responses of their own, through the events API or a webhook like proactive notifications, and in conversation history. `sender_info.display_name` and the optional `sender_info.avatar_url` give the agent's name and picture for live chats through the ServiceNow integration, when both of these are on: **Agent names and avatars** in your ServiceNow instance (Conversational Interfaces > Settings > Agent Chat > User Chat Settings), and **Enable Agent Impersonation** in Moveworks Setup (Ticketing Automation > Handoff > Message Brokering). Without `sender_info`, show a generic label such as "Live agent". The agent's name, if any, is in the message text.
- **Treat `avatar_url` as external content.** The picture is hosted by the live chat provider, outside the Moveworks API. The app accepts only `https` URLs, loads the image with no referrer, and drops it if it fails to load.
- **End a live chat with the agent message's action.** The action that ends the chat is the `CALLBACK_ACTION` on a `LIVE_AGENT` message. Identify it by type and actor, since its label can vary and its `callback_id` is opaque.
- **Detect the end of a live chat from the messages.** The API has no session resource. A `LIVE_AGENT` message without the end-chat action, after earlier ones that carried it, means the agent ended the chat. When the user ends it, the response to the end-chat action completes with no output; see "Re-read history after an empty response" below.
- **Open user-consent links in a new tab.** When a plugin's connector needs per-user authorization, the assistant sends a `URL_ACTION` whose `url_action.purpose` is `USER_CONSENT_AUTH`. When `purpose` is absent, the link is unclassified and can still be a consent link, so `is_auth_link` also flags an `https://{org}.moveworks.com/connections?...` URL. Treat an unknown `purpose` value as unclassified. After the user consents, the original request resumes automatically. Listen on the events endpoint or webhook for the resumed message in the same conversation, and do not resend the request.
- **Disable a message's other actions once one is taken.** This keeps a user from pressing a second action after answering.
- **Merge follow-up events into the open thread.** A response can complete before the work it started does, such as filing a ticket. The follow-up message arrives later as an event on the same conversation, through the events API or a webhook.
- **Convert message text to Markdown in one place.** Message text, labels, citation titles, and notification text can contain Slack-style links (`<https://url|label>`, `<host|label>`) and `:emoji_name:` shortcodes, and modal `DISPLAY` fields can contain HTML. `moveworks_capi.rendering` converts these to CommonMark (`normalize_markup`, `clean_label`, `citation_label`, `modal_html_to_markdown`).
- **Remove form text your client renders itself.** When the assistant offers catalog forms, the message text can end with a plain-text copy of each form's citation. `rendering.strip_flattened_forms` removes it, and `rendering.layout_message` also reports where `content_references` places the forms.
- **Name buttons that share a label.** Several form actions can carry the label "Complete this request". For a `MODAL_ACTION`, the form's name is the modal's first `DISPLAY` field with `style: HEADER`. For a `URL_ACTION`, the link redirects to the form's portal URL, which matches a citation's `url`; `rendering.form_link_citations` pairs them. Pair by URL, not by position.
- **Show a `CHOICE` field's `prefill`.** A `CHOICE` field can have no `options` and a server-resolved `prefill`, such as the requesting user.
- **Treat modal ids as single-use.** Re-read the message for a fresh id rather than reusing one.
- **Re-read history after an empty response.** Some replies, such as the farewell after "Exit Live Chat", are written to history under an earlier response. When a response completes with no outputs, re-read recent history and use `conversations.replies_after_input` to find them.
- **Conversation titles come with the list.** `ListConversations` returns each `title`, so a conversation list renders from one request. Without a title, the API assigns one generated from the first response; set one at creation or with `PATCH` to control the label.
- **Serialize reads.** `ListConversations`, history reads, and `/events` polls all count against the same rate limit. Read one at a time and poll at a conservative interval.
- **Back off on 429.** The shared client backs off with jitter, with or without a `Retry-After` header. For batch work, follow the concurrency guidance in the overview's [practical considerations](https://docs.moveworks.com/api-reference/conversations-api/overview#practical-considerations), test in a small batch first, and ask your Moveworks account team about your limits before sizing a rollout.
- **Decide what the first poll does.** Polling `/events` without `starting_after` returns events from the user's history. `PollingLoop` takes a `first_run` policy. The default, `"skip_history"`, sets the checkpoint to the newest event and dispatches nothing; `"replay_history"` dispatches the history, oldest first.
- **Poll `-` for new events.** `EventsPollingClient(client, conversation_id=...)` lists one conversation's events, and `notifications.get_event` fetches one event by id with its real conversation id. Listing one conversation is eventually consistent, so poll `-` for new events, as the [List events reference](https://docs.moveworks.com/api-reference/conversations-api/conversations-api/conversations/list-events) describes.
- **Branch on error fields.** Every API error carries `status_code`, the body's `error.code`, and `trace_id`. A 401 raises `AuthenticationError`, a 429 raises `RateLimitedError`, and other 4xx responses raise `RequestRejectedError`, including an upload over the size limit (`413`) or one whose content failed validation (`422`). `extract_text` on a `FAILED` response raises `ResponseFailedError`. A mid-stream `ERROR` event raises `StreamError`. A stream that closes before its terminal event raises `StreamClosedError` with the `response_id`, so you can finish with `wait_for_completion`. A stream's `timeout_seconds` is an idle timeout per read.
- **Never resend a turn.** The client retries a 429 on every method, and a 5xx or network error only on `GET`, `HEAD`, and `PATCH`. `create_response`, streaming, and `upload_file` are not retried after a timeout or 5xx, because the server may already have accepted the turn or the file. After a 401, the S2S and OIDC providers fetch a new bearer and the client retries once; an API Key 401 raises immediately. `max_retries` (default 3) caps retries, and `0` turns them off.
- **Check the Bot Name first if every call fails.** `MW_BOT_NAME` must match the Bot Name of the chatbot in Moveworks Setup. `check_setup.py` checks for this.

---

## Testing

```bash
pip install -e ".[app,test]"
python -m pytest
```

The suite runs offline, using synthetic keys, mocked endpoints, and recorded message fixtures. It covers:

- The HTTP client: headers, retries, and backoff
- The S2S JWT and OIDC (PKCE) auth providers
- Conversations, responses, streaming, and file upload
- Event polling and the checkpoint
- Webhook signature verification
- The rendering helpers
- The reference app's server

---

## Versioning

This repo is versioned 0.x, and breaking changes can land in minor versions. [CHANGELOG.md](CHANGELOG.md) records each release. To depend on the library, pin a tag.

---

## Support

See [SUPPORT.md](SUPPORT.md). Questions about your Moveworks instance's Conversations API setup go to your Moveworks account team or [support@moveworks.ai](mailto:support@moveworks.ai). Bugs in this code go to a GitHub issue on this repo. Report security issues as described in [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE.txt). This repository is sample code, provided as-is. It is not a supported Moveworks product and carries no SLA. Verify against the [Conversations API documentation](https://docs.moveworks.com/api-reference/conversations-api/overview) before using it in production.
