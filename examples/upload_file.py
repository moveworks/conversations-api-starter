"""Upload a file and ask the assistant about it.

Uploads a local file with the Files API, then sends one turn that attaches
it through ``chat.files`` and prints the reply. The Files API is in alpha and
enabled per Moveworks instance; until it is enabled, the upload returns
``404``. An uploaded file expires 24 hours after it is created, so attach it
to a turn soon after uploading.

Chooses a bearer provider based on ``MW_AUTH``: ``s2s`` (default), ``okta``,
``entra``, or ``api-key``, with the same variables as the matching hello-call
example.

Usage:
    cp .env.example .env  # fill in MW_BASE_URL, MW_BOT_NAME, and your credential
    python examples/upload_file.py path/to/screenshot.png
    python examples/upload_file.py notes.txt --prompt "Summarize this file."

See: https://docs.moveworks.com/api-reference/alpha-files-api/files/upload-file
"""

from __future__ import annotations

import argparse
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
from moveworks_capi.files import upload_file
from moveworks_capi.rendering import strip_flattened_forms
from moveworks_capi.responses import create_response, wait_for_completion


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


def main() -> None:
    parser = argparse.ArgumentParser(description="Upload a file and ask the assistant about it.")
    parser.add_argument("path", type=Path, help="file to upload")
    parser.add_argument("--prompt", default="What is in this file?", help="message sent with the file")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv(find_dotenv(usecwd=True))

    base_url = os.environ.get("MW_BASE_URL", "https://api.moveworks.ai")
    bot_name = _required("MW_BOT_NAME")

    client = ConversationsApiClient(
        base_url=base_url, bot_name=bot_name, bearer_provider=_build_bearer(base_url)
    )

    uploaded = upload_file(client, args.path)
    print(f"uploaded {uploaded.name!r} as {uploaded.id} ({uploaded.content_type}, {uploaded.size_bytes} bytes)")
    print(f"expires at {uploaded.expire_time}")

    conversation_id = create_conversation(client, title=f"About {uploaded.name}")["conversation_id"]
    print(f"\n> user: {args.prompt} [{uploaded.name}]")
    response_id = create_response(client, conversation_id, args.prompt, file_ids=[uploaded.id])["response_id"]
    completed = wait_for_completion(client, conversation_id, response_id)
    print(f"< assistant [{completed.status}]:")
    # A FAILED response can still carry a message that says why, so print it either way.
    texts = [strip_flattened_forms(output["message"]) for output in completed.outputs if output.get("type") == "MESSAGE"]
    print("\n\n".join(text for text in texts if text) or "(no text content)")


if __name__ == "__main__":
    main()
