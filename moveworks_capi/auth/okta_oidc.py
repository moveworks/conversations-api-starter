"""Okta flavor of the interactive OIDC bearer.

A thin wrapper over :class:`~moveworks_capi.auth.oidc_pkce.OidcInteractiveBearer`
that keeps the Okta-named constructor arguments and Okta's default scopes.
Use a Custom Authorization Server issuer
(``https://{yourOktaDomain}/oauth2/{authServerId}``); see the Okta setup guide:
https://docs.moveworks.com/api-reference/conversations-api/set-up-authentication-with-okta
"""

from __future__ import annotations

from .oidc_pkce import OidcInteractiveBearer


class OktaInteractiveBearer(OidcInteractiveBearer):
    """Callable that returns a cached Moveworks bearer sourced via Okta OIDC.

    Args:
        base_url: Moveworks data-center base URL, e.g. ``https://api.moveworks.ai``.
        okta_issuer: Your Okta authorization server issuer, e.g.
            ``https://acme.okta.com/oauth2/default``.
        okta_client_id: The Client ID of the Okta OIDC app you configured for
            Moveworks integration.
        redirect_uri: One of the Sign-in redirect URIs registered on the Okta
            app, on the ``http://127.0.0.1:<port>/callback`` shape.
        scopes: Space-separated OAuth scopes to request. Must include
            ``openid`` and whatever custom scope the Moveworks credential
            expects (commonly ``openid profile email``).
        refresh_leeway_seconds: Seconds before expiration to refresh proactively.
    """

    def __init__(
        self,
        base_url: str,
        okta_issuer: str,
        okta_client_id: str,
        redirect_uri: str,
        *,
        scopes: str = "openid profile email",
        refresh_leeway_seconds: int = 60,
    ) -> None:
        super().__init__(
            base_url,
            okta_issuer,
            okta_client_id,
            redirect_uri,
            scopes=scopes,
            idp_name="Okta",
            refresh_leeway_seconds=refresh_leeway_seconds,
        )
