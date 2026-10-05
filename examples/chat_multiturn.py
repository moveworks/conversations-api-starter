"""Multi-turn chat example.

Creates one conversation and sends two user messages back to back, waiting
for each response to complete before submitting the next. The point is to
show the pattern most integrations need: reuse ``conversation_id`` across
turns so the assistant maintains context.

Uses the S2S JWT auth path by default. Swap the bearer provider for any of
the alternatives (Okta interactive, HTTP Connector API Key) if you prefer.

Usage:
    cp .env.example .env  # fill in MW_BASE_URL, MW_BOT_NAME, and the MW_S2S_* values
    python examples/chat_multiturn.py
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from moveworks_capi.auth import S2SJwtBearer
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


def _send_turn(client: ConversationsApiClient, conversation_id: str, text: str) -> None:
    print(f"\n> user: {text}")
    response_id = create_response(client, conversation_id, text)["response_id"]
    completed = wait_for_completion(client, conversation_id, response_id)
    print(f"< assistant [{completed.status}]:")
    print(extract_text(completed) or "(no text content)")
    _print_actions(completed)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv()

    base_url = os.environ.get("MW_BASE_URL", "https://api.moveworks.ai")
    bot_name = _required("MW_BOT_NAME")

    bearer = S2SJwtBearer(
        base_url=base_url,
        issuer=_required("MW_S2S_ISSUER"),
        audience=_required("MW_S2S_AUDIENCE"),
        key_id=_required("MW_S2S_KEY_ID"),
        private_key=Path(_required("MW_S2S_PRIVATE_KEY_PATH")),
        user_identifier=_required("MW_S2S_USER_EMAIL"),
    )
    client = ConversationsApiClient(base_url=base_url, bot_name=bot_name, bearer_provider=bearer)

    conversation_id = create_conversation(client, title="multi-turn example")["conversation_id"]
    print(f"created conversation {conversation_id}")

    _send_turn(client, conversation_id, "How many days of PTO do I have this year?")
    _send_turn(client, conversation_id, "What about next year?")


if __name__ == "__main__":
    main()
