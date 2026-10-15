"""Sign a self-signed JWT and make a hello call.

Usage:
    cp .env.example .env  # fill in MW_BASE_URL, MW_BOT_NAME, and the MW_S2S_* values
    python examples/s2s_jwt_call.py

Creates a new conversation as the configured user and sends a single message.
Success confirms your credential setup end to end: keypair registered, issuer
and audience match, ``sub`` resolves to a roster user, ``Assistant-Name``
header is correct.

See: https://docs.moveworks.com/api-reference/conversations-api/set-up-service-to-service-authentication
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from moveworks_capi.auth import S2SJwtBearer
from moveworks_capi.client import ConversationsApiClient
from moveworks_capi.conversations import create_conversation
from moveworks_capi.responses import (
    Response,
    create_response,
    extract_actions,
    extract_handoff,
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
        if action.type == "HANDOFF_ACTION":
            print(f"  {action.type} {action.label!r}: opens the handoff menu")
            continue
        target = action.url or action.callback_id or action.modal_id
        auth_note = " (user-consent link)" if action.is_auth_link else ""
        print(f"  {action.type} {action.label!r}: {target}{auth_note}")
    for category in extract_handoff(completed):
        print(f"handoff menu: {category.name!r}")
        for item in category.items:
            if item.action:
                target = item.action.modal_id or item.action.url or item.action.callback_id
                print(f"  {item.title!r}: {item.action.type} {target}")
            else:
                print(f"  {item.title!r}: {item.text}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv(find_dotenv(usecwd=True))

    base_url = os.environ.get("MW_BASE_URL", "https://api.moveworks.ai")
    bot_name = _required("MW_BOT_NAME")
    user_email = _required("MW_S2S_USER_EMAIL")

    bearer = S2SJwtBearer(
        base_url=base_url,
        issuer=_required("MW_S2S_ISSUER"),
        audience=_required("MW_S2S_AUDIENCE"),
        key_id=_required("MW_S2S_KEY_ID"),
        private_key=Path(_required("MW_S2S_PRIVATE_KEY_PATH")),
        user_identifier=user_email,
    )
    client = ConversationsApiClient(base_url=base_url, bot_name=bot_name, bearer_provider=bearer)

    conversation_id = create_conversation(client, title="s2s hello")["conversation_id"]
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
