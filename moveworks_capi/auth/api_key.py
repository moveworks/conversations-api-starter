"""HTTP Connector API Key bearer provider.

Uses a long-lived opaque key minted at Moveworks Admin Portal → HTTP
Connectors → Credentials as the bearer directly. No JWT signing, no token exchange.

Scoping: the key is bound to the roster user who created it. Every call runs
as that user, and results (attribution, connected content, per-user permission
resolution) reflect that user's identity.

HTTP Connector API Keys are described on the API Credentials page
(https://docs.moveworks.com/api-reference/api-credentials).
"""

from __future__ import annotations


class ApiKeyBearer:
    """Bearer provider that always returns the same API key.

    Args:
        api_key: The raw key value minted in the Admin Portal.
    """

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise ValueError("api_key must be a non-empty string")
        self._api_key = api_key

    def __call__(self) -> str:
        return self._api_key
