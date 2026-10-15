"""Poll the Conversations API events endpoint for one user.

Usage:
    cp .env.example .env  # fill in MW_BASE_URL, MW_BOT_NAME, and your credential
    python examples/poll_events.py                   # poll until Ctrl-C
    python examples/poll_events.py --once            # one pass, then exit
    python examples/poll_events.py --replay-history  # first run prints past events too

Chooses a bearer provider based on ``MW_AUTH``:

- ``MW_AUTH=s2s`` (default): sign a self-signed JWT and exchange it. Recommended
  for backend integrations.
- ``MW_AUTH=okta`` or ``MW_AUTH=entra``: interactive OIDC (PKCE) through
  Okta or Entra ID. Polls as the user who signs in.
- ``MW_AUTH=api-key``: HTTP Connector API Key bearer. Per-user credential.

Once authenticated, polls ``/rest/v1/conversations/-/events`` on a fixed
interval. Each new event is printed, oldest first, and the per-user checkpoint
on disk is advanced only after successful processing.

With no checkpoint for the user yet, the first pass sets the checkpoint to the
user's newest event and prints nothing, so only events from then on appear.
``--replay-history`` prints the user's whole history instead.

See: https://docs.moveworks.com/api-reference/conversations-api/proactive-notifications/receive-via-polling
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import sys
from pathlib import Path
from typing import Callable

from dotenv import find_dotenv, load_dotenv

from moveworks_capi.auth import (
    ApiKeyBearer,
    OidcInteractiveBearer,
    OktaInteractiveBearer,
    S2SJwtBearer,
    entra_issuer,
)
from moveworks_capi.client import ConversationsApiClient
from moveworks_capi.notifications import Event, FileCheckpoint, PollingLoop


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"missing required env var: {name}")
    return value


def _build_bearer(base_url: str) -> tuple[Callable[[], str], str]:
    """Return the bearer provider and the user id to poll for."""
    mode = os.environ.get("MW_AUTH", "s2s").lower()

    if mode == "s2s":
        user_email = _required("MW_S2S_USER_EMAIL")
        bearer = S2SJwtBearer(
            base_url=base_url,
            issuer=_required("MW_S2S_ISSUER"),
            audience=_required("MW_S2S_AUDIENCE"),
            key_id=_required("MW_S2S_KEY_ID"),
            private_key=Path(_required("MW_S2S_PRIVATE_KEY_PATH")),
            user_identifier=user_email,
        )
        return bearer, user_email

    if mode == "okta":
        # The polled user is whoever signs in, so the bearer carries no
        # identifier to key the checkpoint file on. MW_POLL_USER_ID supplies
        # one that stays stable across runs.
        user_id = _required("MW_POLL_USER_ID")
        bearer = OktaInteractiveBearer(
            base_url=base_url,
            okta_issuer=_required("MW_OKTA_ISSUER"),
            okta_client_id=_required("MW_OKTA_CLIENT_ID"),
            redirect_uri=_required("MW_OKTA_REDIRECT_URI"),
            scopes=os.environ.get("MW_OKTA_SCOPES", "openid profile email"),
        )
        return bearer, user_id

    if mode == "entra":
        user_id = _required("MW_POLL_USER_ID")
        client_id = _required("MW_ENTRA_CLIENT_ID")
        bearer = OidcInteractiveBearer(
            base_url=base_url,
            issuer=entra_issuer(_required("MW_ENTRA_TENANT_ID")),
            client_id=client_id,
            redirect_uri=_required("MW_ENTRA_REDIRECT_URI"),
            scopes=os.environ.get("MW_ENTRA_SCOPES") or f"{client_id}/.default",
            idp_name="Entra ID",
        )
        return bearer, user_id

    if mode == "api-key":
        user_id = _required("MW_POLL_USER_ID")
        bearer = ApiKeyBearer(_required("MW_API_KEY"))
        return bearer, user_id

    raise SystemExit(f"MW_AUTH must be one of: s2s, okta, entra, api-key (got {mode!r})")


def _handle_event(user_id: str, event: Event) -> None:
    text = event.content_text or "<no text>"
    print(
        f"[{event.created_at}] user={user_id} conv={event.conversation_id} "
        f"actor={event.message_actor} event={event.event_id}\n  {text}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Poll the Conversations API events endpoint for one user.")
    parser.add_argument("--once", action="store_true", help="run one poll and exit")
    parser.add_argument("--replay-history", action="store_true", help="on the first run, print past events too")
    args = parser.parse_args()

    # Line-buffered stdout so `print` output shows up promptly even when the
    # script is redirected to a file or piped through a viewer.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv(find_dotenv(usecwd=True))

    base_url = os.environ.get("MW_BASE_URL", "https://api.moveworks.ai")
    bot_name = _required("MW_BOT_NAME")

    bearer, user_id = _build_bearer(base_url)
    client = ConversationsApiClient(base_url=base_url, bot_name=bot_name, bearer_provider=bearer)

    checkpoint = FileCheckpoint(Path(os.environ.get("CHECKPOINT_PATH", "./checkpoints/checkpoint.json")))
    loop = PollingLoop(
        client=client,
        checkpoint=checkpoint,
        handler=_handle_event,
        interval_seconds=float(os.environ.get("POLL_INTERVAL_SECONDS", "15")),
        first_run="replay_history" if args.replay_history else "skip_history",
    )

    mode = "one pass" if args.once else "Ctrl-C to stop"
    print(f"polling for user={user_id} via auth={os.environ.get('MW_AUTH', 's2s')} ({mode})")
    if args.once:
        before = checkpoint.load(user_id)
        count = loop.poll_once(user_id)
        print(f"dispatched {count} event(s); checkpoint {before or '(none)'} -> {checkpoint.load(user_id) or '(none)'}")
        return

    loop.run_forever(user_id)


if __name__ == "__main__":
    main()
