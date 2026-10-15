"""Verify your `.env` and Moveworks instance setup are wired correctly.

Runs a sequence of checks against your configured auth path and prints
pass/fail for each. On failure, points at the most likely cause.

Usage:
    cp .env.example .env  # fill in MW_BASE_URL, MW_BOT_NAME, and your credential
    python examples/check_setup.py                # uses MW_AUTH env var, defaults to s2s
    MW_AUTH=entra python examples/check_setup.py  # or s2s | okta | api-key
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Callable, List, Tuple

import requests
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
from moveworks_capi.errors import AuthenticationError, ConversationsApiError
from moveworks_capi.responses import create_response, extract_actions, extract_text, wait_for_completion


def _step(label: str, fn: Callable[[], None]) -> bool:
    print(f"... {label}")
    try:
        fn()
    except Exception as exc:
        print(f"  FAIL: {exc}")
        return False
    print("  OK")
    return True


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"missing env var {name}")
    return value


def _build_bearer(base_url: str) -> Tuple[Callable[[], str], str]:
    mode = os.environ.get("MW_AUTH", "s2s").lower()
    if mode == "s2s":
        return (
            S2SJwtBearer(
                base_url=base_url,
                issuer=_required("MW_S2S_ISSUER"),
                audience=_required("MW_S2S_AUDIENCE"),
                key_id=_required("MW_S2S_KEY_ID"),
                private_key=Path(_required("MW_S2S_PRIVATE_KEY_PATH")),
                user_identifier=_required("MW_S2S_USER_EMAIL"),
            ),
            mode,
        )
    if mode == "okta":
        return (
            OktaInteractiveBearer(
                base_url=base_url,
                okta_issuer=_required("MW_OKTA_ISSUER"),
                okta_client_id=_required("MW_OKTA_CLIENT_ID"),
                redirect_uri=_required("MW_OKTA_REDIRECT_URI"),
                scopes=os.environ.get("MW_OKTA_SCOPES", "openid profile email"),
            ),
            mode,
        )
    if mode == "entra":
        client_id = _required("MW_ENTRA_CLIENT_ID")
        return (
            OidcInteractiveBearer(
                base_url=base_url,
                issuer=entra_issuer(_required("MW_ENTRA_TENANT_ID")),
                client_id=client_id,
                redirect_uri=_required("MW_ENTRA_REDIRECT_URI"),
                scopes=os.environ.get("MW_ENTRA_SCOPES") or f"{client_id}/.default",
                idp_name="Entra ID",
            ),
            mode,
        )
    if mode == "api-key":
        return ApiKeyBearer(_required("MW_API_KEY")), mode
    raise RuntimeError(f"MW_AUTH must be one of: s2s, okta, entra, api-key (got {mode!r})")


def _first_call_hint(exc: ConversationsApiError) -> str:
    status = exc.status_code or 0
    if status == 404:
        return (
            " Check the base URL, and confirm with your Moveworks account team that the "
            "Conversations API is enabled for your Moveworks instance; requests return 404 until it is."
        )
    # This is the first call that sends Assistant-Name.
    if status == 403 or status >= 500:
        return (
            f" Check that MW_BOT_NAME ({os.environ.get('MW_BOT_NAME', '')!r}) matches the Bot Name "
            "of your chatbot in Moveworks Setup."
        )
    return ""


def main() -> int:
    load_dotenv(find_dotenv(usecwd=True))

    checks: List[Tuple[str, Callable[[], None]]] = []
    state: dict = {}

    base_url = os.environ.get("MW_BASE_URL", "https://api.moveworks.ai")
    bot_name = os.environ.get("MW_BOT_NAME", "")
    print(f"base URL:  {base_url}")
    print(f"bot name:  {bot_name or '(unset)'}")
    print(f"auth mode: {os.environ.get('MW_AUTH', 's2s')}")
    print()

    def check_env() -> None:
        if not bot_name:
            raise RuntimeError("MW_BOT_NAME is not set")

    def check_base_url_answers() -> None:
        # Any HTTP status from the base URL proves DNS, TLS, and routing work;
        # the path does not need to exist. Only 5xx and network errors fail
        # this step.
        try:
            response = requests.get(f"{base_url.rstrip('/')}/health", timeout=10.0)
        except requests.RequestException as exc:
            raise RuntimeError(
                f"cannot reach {base_url}: {exc}. Check MW_BASE_URL against the Base URLs table in the docs."
            ) from exc
        if response.status_code >= 500:
            raise RuntimeError(f"{base_url} answered {response.status_code}")

    def check_auth_exchange() -> None:
        try:
            bearer, mode = _build_bearer(base_url)
            state["mode"] = mode
            token = bearer()
        except AuthenticationError as exc:
            raise RuntimeError(
                f"auth exchange failed: {exc}. Verify credential values match what is configured in the Admin Portal."
            ) from exc
        if not isinstance(token, str) or not token:
            raise RuntimeError("bearer provider returned an empty token")
        client = ConversationsApiClient(base_url=base_url, bot_name=bot_name, bearer_provider=bearer)
        state["client"] = client

    def check_create_conversation() -> None:
        client: ConversationsApiClient = state["client"]
        try:
            body = create_conversation(client, title="check_setup smoke test")
        except AuthenticationError as exc:
            raise RuntimeError(
                f"401 on POST /conversations: {exc}. Bearer was minted but Moveworks rejected it. "
                "Check that the Assistant-Name header matches your Bot Name and that the credential's "
                "identifier claim resolves to a provisioned roster user."
            ) from exc
        except ConversationsApiError as exc:
            raise RuntimeError(f"POST /conversations failed: {str(exc).rstrip('.')}.{_first_call_hint(exc)}") from exc
        conv_id = body.get("conversation_id")
        if not conv_id:
            raise RuntimeError(f"POST /conversations response missing conversation_id: {body}")
        state["conversation_id"] = conv_id

    def check_send_and_wait() -> None:
        client: ConversationsApiClient = state["client"]
        conv_id = state["conversation_id"]
        submitted = create_response(client, conv_id, "ping from check_setup")
        response_id = submitted.get("response_id")
        if not response_id:
            raise RuntimeError(f"POST /responses missing response_id: {submitted}")
        completed = wait_for_completion(client, conv_id, response_id, timeout_seconds=45.0)
        state["completed"] = completed
        if completed.status != "COMPLETED":
            raise RuntimeError(f"response reached terminal status {completed.status}, not COMPLETED")

    def check_response_has_text() -> None:
        text = extract_text(state["completed"])
        if not text.strip():
            raise RuntimeError("assistant response contained no text output")
        preview = text.splitlines()[0][:120]
        print(f"  reply preview: {preview!r}")
        # Buttons ride on message.actions, not in the text; a surface that
        # renders only text drops them.
        for action in extract_actions(state["completed"]):
            print(f"  action: {action.type} {action.label!r}")

    checks = [
        ("env vars present", check_env),
        ("base URL resolves and answers TLS", check_base_url_answers),
        ("auth exchange", check_auth_exchange),
        ("POST /conversations", check_create_conversation),
        ("send message and wait for reply", check_send_and_wait),
        ("assistant reply has text", check_response_has_text),
    ]

    all_ok = True
    for label, fn in checks:
        ok = _step(label, fn)
        if not ok:
            all_ok = False
            break

    print()
    if all_ok:
        print("all checks passed")
        return 0
    print("setup is not ready. Address the failure above and rerun.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
