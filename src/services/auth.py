"""Bearer-token handling for the remote server.

This server is an OAuth 2.1 resource server that does **not** validate tokens
itself. Every OAuth access token a client presents is handed to the ReNile
backend's token-exchange endpoint (RFC 8693), which decides whether it is
valid, whose it is and what it may do -- and, if it is good, returns a
short-lived ReNile JWT for that user. That JWT is what this server uses
upstream. The OAuth token itself never reaches the ReNile API, and the ReNile
JWT never reaches the client.
"""

import hashlib
import logging
import time

from mcp.server.auth.provider import AccessToken, TokenVerifier
from pydantic import Field

from src.services.errors import TokenExchangeRejectedError
from src.services.renile_client import ReNileClient

logger = logging.getLogger(__name__)

# Used when the backend does not say which OAuth client a token belongs to.
# Not meaningful on its own -- `subject` is what separates one caller from another.
_FALLBACK_CLIENT_ID = "renile-mcp"

# Stop reusing an exchanged JWT this long before it expires, so a request that
# starts just before expiry does not reach the ReNile API with a dead credential.
_EXPIRY_MARGIN_SECONDS = 30


class ReNileAccessToken(AccessToken):
    """An access token plus the upstream credential the backend exchanged it for.

    `renile_jwt` is excluded from repr and serialisation: it is a ReNile platform
    credential and must stay inside this server.
    """

    renile_jwt: str = Field(repr=False, exclude=True)


def _cache_key(token: str) -> str:
    """Tokens are cached under a hash, so the cache itself holds no bearer token."""
    return hashlib.sha256(token.encode()).hexdigest()


class ExchangeTokenVerifier(TokenVerifier):
    """Resolves a bearer token by exchanging it at the ReNile backend.

    A successful exchange is cached until shortly before the ReNile JWT expires,
    so the backend is asked once per token lifetime, not once per request.

    The result feeds two SDK guards. `subject` (the backend's `sub`, the ReNile
    user id) binds each Streamable HTTP session to one user, so a session id is
    useless to anyone else. `scopes` is checked against the required scopes, and
    a token missing one is answered with 403 insufficient_scope.

    `client` is supplied by the server lifespan, which owns the connection pool.
    """

    def __init__(self, issuer_url: str) -> None:
        self._issuer_url = issuer_url.rstrip("/")
        self.client: ReNileClient | None = None
        self._cache: dict[str, tuple[ReNileAccessToken, float]] = {}

    async def verify_token(self, token: str) -> ReNileAccessToken | None:
        token = token.strip()
        if not token or self.client is None:
            return None

        key = _cache_key(token)
        now = time.time()
        cached = self._cache.get(key)
        if cached is not None and cached[1] > now:
            return cached[0]

        try:
            exchanged = await self.client.exchange_token(token)
        except TokenExchangeRejectedError:
            # Invalid, expired or revoked: the SDK answers 401 + WWW-Authenticate,
            # which sends the client to refresh or re-run the OAuth flow. Any
            # other RenileAPIError (backend down) propagates as a 5xx instead, so
            # a transient outage does not make clients throw their tokens away.
            self._cache.pop(key, None)
            return None

        expires_at = int(now) + exchanged.expires_in
        access_token = ReNileAccessToken(
            token=token,
            client_id=exchanged.client_id or _FALLBACK_CLIENT_ID,
            scopes=exchanged.scopes,
            subject=exchanged.subject,
            expires_at=expires_at,
            claims={"iss": self._issuer_url},
            renile_jwt=exchanged.renile_jwt,
        )
        self._purge(now)
        self._cache[key] = (access_token, expires_at - _EXPIRY_MARGIN_SECONDS)
        return access_token

    def forget(self, token: str) -> None:
        """Drop a cached exchange, e.g. after the ReNile API rejected its JWT."""
        self._cache.pop(_cache_key(token.strip()), None)

    def _purge(self, now: float) -> None:
        for key in [k for k, (_, until) in self._cache.items() if until <= now]:
            del self._cache[key]
