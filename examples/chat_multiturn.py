"""Multi-turn chat example.

Creates one conversation and sends two user messages back to back, waiting
for each response to complete before submitting the next. The point is to
show the pattern most integrations need: reuse ``conversation_id`` across
turns so the assistant maintains context.

Chooses a bearer provider based on ``MW_AUTH``: ``s2s`` (default), ``okta``,
``entra``, or ``api-key``, with the same variables as the matching hello-call
example.

Usage:
    cp .env.example .env  # fill in MW_BASE_URL, MW_BOT_NAME, and your credential
    python examples/chat_multiturn.py
    MW_AUTH=okta python examples/chat_multiturn.py
"""

from __future__ import annotations

import logging
import os
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


def _build_bearer(base_url: str) -> Callable[[], str]:
    mode = os.environ.get("MW_AUTH", "s2s").lower()

    if mode == "s2s":
        return S2SJwtBearer(
            base_url=base_url,
            issuer=_required("MW_S2S_ISSUER"),
            audience=_required("MW_S2S_AUDIENCE"),
            key_id=_required("MW_S2S_KEY_ID"),
            private_key=Path(_required("MW_S2S_PRIVATE_KEY_PATH")),
            user_identifier=_required("MW_S2S_USER_EMAIL"),
        )

    if mode == "okta":
        return OktaInteractiveBearer(
            base_url=base_url,
            okta_issuer=_required("MW_OKTA_ISSUER"),
            okta_client_id=_required("MW_OKTA_CLIENT_ID"),
            redirect_uri=_required("MW_OKTA_REDIRECT_URI"),
            scopes=os.environ.get("MW_OKTA_SCOPES", "openid profile email"),
        )

    if mode == "entra":
        client_id = _required("MW_ENTRA_CLIENT_ID")
        return OidcInteractiveBearer(
            base_url=base_url,
            issuer=entra_issuer(_required("MW_ENTRA_TENANT_ID")),
            client_id=client_id,
            redirect_uri=_required("MW_ENTRA_REDIRECT_URI"),
            scopes=os.environ.get("MW_ENTRA_SCOPES") or f"{client_id}/.default",
            idp_name="Entra ID",
        )

    if mode == "api-key":
        return ApiKeyBearer(_required("MW_API_KEY"))

    raise SystemExit(f"MW_AUTH must be one of: s2s, okta, entra, api-key (got {mode!r})")


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


def _send_turn(client: ConversationsApiClient, conversation_id: str, text: str) -> None:
    print(f"\n> user: {text}")
    response_id = create_response(client, conversation_id, text)["response_id"]
    completed = wait_for_completion(client, conversation_id, response_id)
    print(f"< assistant [{completed.status}]:")
    print(extract_text(completed) or "(no text content)")
    _print_actions(completed)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv(find_dotenv(usecwd=True))

    base_url = os.environ.get("MW_BASE_URL", "https://api.moveworks.ai")
    bot_name = _required("MW_BOT_NAME")

    client = ConversationsApiClient(
        base_url=base_url, bot_name=bot_name, bearer_provider=_build_bearer(base_url)
    )

    conversation_id = create_conversation(client, title="multi-turn example")["conversation_id"]
    print(f"created conversation {conversation_id}")

    _send_turn(client, conversation_id, "How many days of PTO do I have this year?")
    _send_turn(client, conversation_id, "What about next year?")


if __name__ == "__main__":
    main()
