"""Bearer-token providers for every Conversations API auth path.

The client in ``moveworks_capi.client`` accepts any ``Callable[[], str]`` as
its bearer provider. The classes here are ready-to-use options:

- ``S2SJwtBearer``: self-signed JWT, for backends acting as any roster user.
- ``ApiKeyBearer``: HTTP Connector API Key, bound to one roster user.
- ``OidcInteractiveBearer``: a user signs in through any OIDC IdP (Entra ID,
  for example) with Authorization Code + PKCE. ``OktaInteractiveBearer`` is
  the same flow with Okta-named arguments.
"""

from .api_key import ApiKeyBearer
from .oidc_pkce import OidcInteractiveBearer, entra_issuer
from .okta_oidc import OktaInteractiveBearer
from .s2s_jwt import S2SJwtBearer

__all__ = [
    "ApiKeyBearer",
    "OidcInteractiveBearer",
    "OktaInteractiveBearer",
    "S2SJwtBearer",
    "entra_issuer",
]
