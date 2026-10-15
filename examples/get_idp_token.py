"""Run only the interactive auth legs and show what the IdP token says.

Diagnostic companion to okta_call.py and entra_call.py: no chat call. Runs
the browser login, prints the IdP access token's decoded header and claims,
then exchanges it at Moveworks /oauth/v1/token. When the exchange fails,
compare `iss` and `aud` with the Issuer and Audience on the Moveworks
credential, and the credential's identifier claim (`oid` for Entra) with
the user's Moveworks profile.

Usage:
    MW_AUTH=okta python examples/get_idp_token.py    # MW_OKTA_* values
    MW_AUTH=entra python examples/get_idp_token.py   # MW_ENTRA_* values
"""

from __future__ import annotations

import base64
import json
import logging
import os

from dotenv import find_dotenv, load_dotenv

from moveworks_capi.auth import OidcInteractiveBearer, OktaInteractiveBearer, entra_issuer
from moveworks_capi.errors import AuthenticationError


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"missing required env var: {name}")
    return value


def _build_bearer(base_url: str) -> OidcInteractiveBearer:
    mode = os.environ.get("MW_AUTH", "okta").lower()
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
    raise SystemExit(f"MW_AUTH must be okta or entra for this script (got {mode!r})")


def _b64url_json(segment: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv(find_dotenv(usecwd=True))
    bearer = _build_bearer(os.environ.get("MW_BASE_URL", "https://api.moveworks.ai"))

    authorize, token = bearer.endpoints()
    print(f"== Step 1: browser login ==\nauthorize: {authorize}\ntoken:     {token}", flush=True)
    idp_token = bearer.login()

    print("\n== Step 2: decoded IdP access token ==")
    parts = idp_token.split(".")
    if len(parts) != 3:
        raise SystemExit(
            "the IdP returned an opaque (non-JWT) access token, which Moveworks cannot validate. "
            "Request a scope on your own app or API, not on a third-party resource such as Graph."
        )
    print("header:", json.dumps(_b64url_json(parts[0]), indent=2))
    print("claims:", json.dumps(_b64url_json(parts[1]), indent=2))

    print("\n== Step 3: Moveworks /oauth/v1/token exchange ==")
    try:
        mw_bearer, expires_in = bearer.exchange(idp_token)
    except AuthenticationError as exc:
        raise SystemExit(f"exchange failed: {exc}") from exc
    print(f"Moveworks accepted the token. MW bearer expires_in={expires_in:.0f}s")
    print(f"MW bearer (first 40 chars): {mw_bearer[:40]}...")

    print("\n== IdP access token (full JWT) ==")
    print(idp_token)


if __name__ == "__main__":
    main()
