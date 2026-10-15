"""Interactive Entra ID (Azure AD) hello call, following the Entra setup guide.

Usage:
    cp .env.example .env  # fill in MW_BOT_NAME and MW_ENTRA_* values
    python examples/entra_call.py

Same flow as okta_call.py against Entra: browser sign-in with Authorization
Code + PKCE, a loopback callback, the Entra access token exchanged at
Moveworks for a bearer, then a hello message, the reply, and its actions.

Entra side (app registration):
- Add a "Mobile and desktop applications" platform with the loopback
  redirect URI from MW_ENTRA_REDIRECT_URI, and set "Allow public client
  flows" to Yes. A script redeems the code without a browser Origin header,
  which an SPA redirect requires, and holds no client secret, which a Web
  redirect requires.
- Expose an API scope (for example access_as_user) and grant it to the app.
  The script requests {client_id}/.default, as the setup guide does. Never
  request a Microsoft Graph scope: that token's audience is Graph, which no
  one but Graph can validate.
- Set accessTokenAcceptedVersion to 2 in the manifest so `iss` is the v2.0
  issuer, https://login.microsoftonline.com/{tenant_id}/v2.0.

Moveworks side (JWT OAuth credential): Issuer is that v2.0 issuer, Audience
is the token's `aud` (the client ID for v2.0 tokens), Identifier Claim is
`oid`, Identifier Type is IdP, and Integration ID is your Moveworks instance's MS Graph
identity integration. `oid` is a GUID, so Identifier Type Email never matches.

See: https://docs.moveworks.com/api-reference/conversations-api/set-up-authentication-with-azure-ad-entra-id
"""

from __future__ import annotations

import logging
import os

from dotenv import find_dotenv, load_dotenv

from moveworks_capi.auth import OidcInteractiveBearer, entra_issuer
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
    client_id = _required("MW_ENTRA_CLIENT_ID")
    bearer = OidcInteractiveBearer(
        base_url=base_url,
        issuer=entra_issuer(_required("MW_ENTRA_TENANT_ID")),
        client_id=client_id,
        redirect_uri=_required("MW_ENTRA_REDIRECT_URI"),
        scopes=os.environ.get("MW_ENTRA_SCOPES") or f"{client_id}/.default",
        idp_name="Entra ID",
    )
    client = ConversationsApiClient(base_url=base_url, bot_name=_required("MW_BOT_NAME"), bearer_provider=bearer)

    conversation_id = create_conversation(client, title="entra hello")["conversation_id"]
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
