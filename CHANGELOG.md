# Changelog

All notable changes to this project are recorded here. The project is versioned 0.x, and breaking changes can land in minor versions.

## 0.5.0

- `Event` reads the event's `category` (`USER_CONSENT_AUTH`, `LIVE_AGENT`, or None when unclassified), `live_agent_session_state` (`ENDED`), and `consent_operation_type` (`AUTHORIZE` or `REVOKE`). `Event.has_message` says whether the event carries a message, since a live agent session end can arrive without one. `WebhookEnvelope.category` reads a webhook's `data.category`.
- `poll_events.py` and `receive_webhook.py` print the category and details.
- Reference app: when a live agent chat ends in the open conversation, the thread shows "Live agent chat ended", whether or not the event carries a message. Consent results read from the event's details show as "Account connected" (the first `AUTHORIZE` in a conversation since its last `REVOKE`) or "Account disconnected": a line in the open thread, or a label in the notifications list.
- README: usage note on routing events by `category`.

## 0.4.0

- Reference app: events that carry no message are left out of the notifications list.
- Reference app: while a form is open, the rest of the page is inert, so Tab and screen readers stay inside the form.
- README: Windows commands for the virtual environment and the `.env` copy, and `REQUESTS_CA_BUNDLE` for networks that inspect TLS traffic.
- Reference app: the screen-reader status line is fixed in place, so a long conversation cannot make the whole page scroll past the app.
- Reference app: a poll still running from a previous connection does not write into the new one, and a polled notification reaches the browser as soon as the poll returns, rather than at the stream's next heartbeat.
- Reference app: a conversation opened twice, or while another is loading, renders once. The custom base URL field takes part in the connect form only while it is shown. A vote from either feedback button closes an open comment box. Screen readers also hear follow-ups that arrive in the open conversation, and error lines.
- Citation titles and bodies are read from whichever text type they arrive in (`PLAIN_TEXT`, `COMMONMARK_TEXT`, or `MARKDOWN_TEXT`), so labels, snippets, and form matching work when an instance sends CommonMark. `rendering.citation_body` returns a citation's body text.
- `wait_for_completion` waits up to 120 seconds by default, the time the API allows for a response to reach a terminal state. `check_setup.py` uses the same default.
- Reference app: a `FAILED` response shows its own message when it carries one, and the generic "could not complete" banner only when it does not.
- Reference app: action buttons wait while a reply is arriving, which also stops a double click from sending a callback twice. Switching or starting a conversation, or disconnecting, stops the reply in progress so it cannot land in the next conversation, and a conversation opened while another is loading is not overwritten.
- Reference app: modal reads that name a button run one at a time across the page and are dropped when you leave the conversation, so a long history stays inside the rate limit.
- Reference app: citation links and modal images and downloads are shown only for `http` and `https` URLs.
- Reference app: writes are accepted only from the app's own origin, including its port, and the page and static files are served with `Cache-Control: no-cache`.
- Reference app: conversations and notifications open from the keyboard, screen readers hear each finished reply once, and the modal moves focus to itself and back to the button that opened it.
- README: `sender_info` also needs **Enable Agent Impersonation** in Moveworks Setup; conversations without a title get one generated from the first response; documentation links point to the Conversations API overview. Examples say Tool Studio.

## 0.3.0

- `files.upload_file` uploads a file with the Files API (alpha, `POST /rest/v1alpha1/files`) and returns an `UploadedFile`. Pass its `id` in `file_ids` to `create_response` or `stream_response`. The `purpose` field is sent before the `file` part, and the upload is resent only after a 429 or 401, like a turn. A rejected upload raises `RequestRejectedError` with the status, such as `404` when the Files API is not enabled, `413`, or `422`. Before sending, it checks the 100 MB limit and the API's filename rules and refuses empty content, raising `ValueError`. `timeout` overrides the client's per-request timeout for a large file.
- `ConversationsApiClient.request` and `post` take `files` for a `multipart/form-data` body, and `timeout` to override the per-request timeout.
- `examples/upload_file.py` uploads a file and asks the assistant about it.
- README: usage notes for the Files API.

## 0.2.0

- Reference app: a `HANDOFF_ACTION` opens the message's `Message.handoff` menu from its place in the actions row, and is omitted when the message has no handoff options. It was shown as an unsupported action.
- Reference app: live agent messages show the agent's picture from `sender_info.avatar_url` (`https` only, loaded with no referrer).
- The `Action` docstring covers `HANDOFF_ACTION`.
- README: API usage notes for `HANDOFF_ACTION`, `sender_info` and its ServiceNow prerequisites, avatar handling, and ending a live chat.
- `Action.purpose` carries a `URL_ACTION`'s `url_action.purpose`. `is_auth_link` is set when `purpose` is `USER_CONSENT_AUTH`, and falls back to the `/connections` URL check when `purpose` is absent or an unknown value.
- `responses.message_handoff` normalizes a message's `Message.handoff` into `HandoffCategory` and `HandoffItem` values. The reference app builds its Get Help menu from it.
- Reference app: after a user opens a consent link, the app says the assistant continues in the conversation. It no longer asks the user to send another message, since the original request resumes on its own.
- `examples/chat_multiturn.py` selects its auth path with `MW_AUTH`, like the other examples.
- README: the user-consent note describes `url_action.purpose` and the automatic resume.
- `HandoffCategory.category_id` and `HandoffItem.item_id` carry the spec's identifiers. `responses.extract_handoff` collects the Get Help menu from a completed response, like `extract_actions`.
- Reference app: a `URL_ACTION` whose URL is not `http` or `https`, including one inside a handoff item, is shown as an unsupported action instead of a link.
- Reference app: handoff items with no action or text, and categories left empty, are dropped. A `HANDOFF_ACTION` is omitted when nothing in the menu can be shown.
- Reference app: a consent link whose label already starts with "Connect" is not prefixed again.
- Examples that print actions also print the Get Help menu.
- Examples load `.env` from the current directory, the same place relative key paths resolve from.

## 0.1.0

First tagged release.

### Library (`moveworks_capi`)

- `ConversationsApiClient`: synchronous `/rest/v1` HTTP client that sends the `Assistant-Name` header and a `moveworks-capi-starter/0.1.0` User-Agent. Retries a 429 on every method with jittered backoff, and a 5xx or network error only on `GET`, `HEAD`, and `PATCH`, so a turn is never resent. On a 401, refreshes the bearer once through the provider's optional `invalidate()`. Usable as a context manager, with an injectable `requests.Session`. Errors carry `status_code`, `code`, and `trace_id`.
- Bearer providers for Okta OIDC with PKCE, Entra ID with PKCE, self-signed JWT (service-to-service), and HTTP Connector API Key.
- Responses: build `chat` and `action` turn input, submit, wait for completion (polls every 2 seconds and tolerates 429s), and extract text and actions. `extract_text` on a `FAILED` response raises `ResponseFailedError` carrying the `Response`.
- Streaming: server-sent events client for `/responses/stream`. A 429 at open raises `RateLimitedError`; a stream that closes before its terminal event raises `StreamClosedError` with the `response_id`. `timeout_seconds` is an idle timeout.
- Conversations: create, list, fetch, update (title, archive), and list messages.
- Notifications: `/events` polling with a durable per-user checkpoint and an explicit first-run policy (default: skip history), single-event reads, and outbound-webhook signature verification against the tenant JWKS (`JwksFetchError` when it cannot be fetched).
- `rendering`: helpers for displaying assistant messages in a UI. `conversations.replies_after_input` finds replies written to history under an earlier response.
- Includes `py.typed`.

### Examples, app, and tooling

- Runnable examples for setup checks, each auth path, multi-turn chat, polling, webhook receipt, and listener-triggered test notifications.
- Reference web app (`python -m app`): a plain chat surface over the library.
- Offline test suite, plus ruff and pyright, run in CI.
