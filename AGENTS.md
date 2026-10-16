# Working in this repo (for AI coding assistants)

This repo is sample code for the Moveworks Conversations API: a client library (`moveworks_capi/`), runnable examples (`examples/`), and a reference web app (`app/`). `README.md` describes the layout. Follow these rules when helping someone build on it.

## Start here

1. **Confirm the Moveworks instance setup before writing code.** Run `python examples/check_setup.py`. It checks environment variables, reachability, auth, and a full round trip, and names the likely cause of a failure. Most failures come from setup: the API not enabled (404 on every call), no chatbot or a wrong Bot Name, a credential that does not match, or a user who does not resolve against the roster.
2. **Use the public docs for API behavior.** The [Conversations API documentation](https://docs.moveworks.com/api-reference/conversations-api) is the source of truth. When it disagrees with this repo or with anything you recall, follow the docs.
3. **Start from working code.** For a chat surface, run the reference app (`python -m app`) and adapt it. For a backend, start from the example for the auth path (README, "Choose a starting point").

## Rules

- **Use the `/rest/v1` base path.** `/assistant/v1` is deprecated, and modals are available only on `/rest/v1`.
- **Never fan out reads.** Reads, including `/events` polls, all count against the same rate limit. Title conversations at creation so a list renders from one call, never read each conversation's history to build a sidebar, serialize history reads, and poll conservatively.
- **Never resend a turn.** Rely on the shared client's backoff for 429s. Do not add a retry that resends `POST .../responses` or a stream open after a timeout or 5xx, because the server may already have accepted the turn. The client resends `POST .../responses` and `POST /rest/v1alpha1/files` only after a 429 or 401, and a stream open only after a 401.
- **Keep the default first-poll policy.** `PollingLoop` defaults to `first_run="skip_history"`, so a new user's history is not dispatched as new notifications. Keep that default in a chat surface.
- **Call the client from a thread in async code.** The client is synchronous. Use `asyncio.to_thread`, as `app/` does.
- **Render actions as well as text.** Messages carry `actions` (links, callbacks, modals, and handoff menu triggers), which a text-only client drops. Disable a message's other actions once one is taken.
- **Treat modal ids as single-use.** Re-read the message for a fresh id rather than caching one.
- **Convert message text in one place.** Use `moveworks_capi.rendering` (`normalize_markup`, `clean_label`, `citation_label`, `modal_html_to_markdown`, `strip_flattened_forms`, `layout_message`). The reference app does this on the server in `app/present.py`, so the browser receives Markdown.
- **Keep display helpers in `rendering.py`.** Code that changes how message content is displayed goes there, not in the core client.
- **Keep the reference app plain.** Visual polish belongs in a fork. A styled build of the same surface is at [github.com/moveworks/conversations-api-demo-app](https://github.com/moveworks/conversations-api-demo-app).
- **Never commit secrets.** `.env`, `*.pem`, and `checkpoints/` are ignored; keep them that way. API keys stay in server memory, never in browser storage or logs.
- **Keep tests offline.** `python -m pytest` must pass without a Moveworks instance.

## Do not promise

These are not implemented in this repo or not available in the API. Say so rather than building around a guess:

- Live agent session state, such as queue position or whether a chat is still open (the API has no session resource; the README's API usage notes describe inferring the end of a chat from messages)
- An attach control or modal `FILE` fields in the reference app (the library uploads with `files.upload_file`; a `FILE` value is `{"files": [{"file_id": ...}]}`)
- A client-initiated Get Help menu (the API offers it only when the assistant does)
- Specific rate limits for a Moveworks instance (the docs give starting guidance, and the account team provides the limits)

## When the API changes

Check the change against the [docs](https://docs.moveworks.com/api-reference/conversations-api), update the code and its tests, update the README's "API usage notes", and add a line under "Unreleased" in `CHANGELOG.md`.
