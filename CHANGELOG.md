# Changelog

All notable changes to this project are recorded here. The project is versioned 0.x, and breaking changes can land in minor versions.

## Unreleased

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
