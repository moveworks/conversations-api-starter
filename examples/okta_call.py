"""Interactive Okta OIDC hello call.

Usage:
    cp .env.example .env  # fill in MW_BOT_NAME and MW_OKTA_* values
    python examples/okta_call.py

Opens your browser to Okta, catches the callback on a local loopback port,
exchanges the resulting Okta access token for a Moveworks bearer, creates a
conversation, sends a hello message, and prints the reply and its actions.
Success confirms the interactive Okta path end to end for the user who logs in.

See: https://docs.moveworks.com/api-reference/conversations-api/set-up-authentication-with-okta
"""

from __future__ import annotations

import logging
import os

from dotenv import load_dotenv

from moveworks_capi.auth import OktaInteractiveBearer
from moveworks_capi.client import ConversationsApiClient
from moveworks_capi.conversations import create_conversation
from moveworks_capi.responses import (
    Response,
    create_response,
    extract_actions,
    extract_text,
    wait_for_completion,
)


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"missing required env var: {name}")
    return value


def _print_actions(completed: Response) -> None:
    actions = extract_actions(completed)
    print(f"actions: {len(actions)}")
    for action in actions:
        target = action.url or action.callback_id or action.modal_id
        auth_note = " (user-consent link)" if action.is_auth_link else ""
        print(f"  {action.type} {action.label!r}: {target}{auth_note}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv()

    base_url = os.environ.get("MW_BASE_URL", "https://api.moveworks.ai")
    bearer = OktaInteractiveBearer(
        base_url=base_url,
        okta_issuer=_required("MW_OKTA_ISSUER"),
        okta_client_id=_required("MW_OKTA_CLIENT_ID"),
        redirect_uri=_required("MW_OKTA_REDIRECT_URI"),
        scopes=os.environ.get("MW_OKTA_SCOPES", "openid profile email"),
    )
    client = ConversationsApiClient(base_url=base_url, bot_name=_required("MW_BOT_NAME"), bearer_provider=bearer)

    conversation_id = create_conversation(client, title="okta hello")["conversation_id"]
    print(f"created conversation {conversation_id}")

    response_id = create_response(client, conversation_id, "hello from the starter code")["response_id"]
    print(f"submitted message, waiting for reply (response_id={response_id})...")

    completed = wait_for_completion(client, conversation_id, response_id)
    print(f"status: {completed.status}")
    print("assistant reply:")
    print(extract_text(completed) or "(no text content)")
    _print_actions(completed)


if __name__ == "__main__":
    main()
