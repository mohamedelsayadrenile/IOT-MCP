import hashlib
import logging
import time

from mcp.server.auth.provider import AccessToken, TokenVerifier
from pydantic import Field

from src.services.errors import TokenExchangeRejectedError
from src.services.renile_client import ReNileClient

logger = logging.getLogger(__name__)

_FALLBACK_CLIENT_ID = "renile-mcp"

_EXPIRY_MARGIN_SECONDS = 30


class ReNileAccessToken(AccessToken):
    renile_jwt: str = Field(repr=False, exclude=True)


def _cache_key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class ExchangeTokenVerifier(TokenVerifier):
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
            self._cache.pop(key, None)
            return None

        expires_at = int(now) + exchanged.expires_in
        access_token = ReNileAccessToken(
            token=token,
            client_id=exchanged.client_id or _FALLBACK_CLIENT_ID,
            scopes=[],
            subject=exchanged.subject,
            expires_at=expires_at,
            claims={"iss": self._issuer_url},
            renile_jwt=exchanged.renile_jwt,
        )
        self._purge(now)
        self._cache[key] = (access_token, expires_at - _EXPIRY_MARGIN_SECONDS)
        return access_token

    def forget(self, token: str) -> None:
        self._cache.pop(_cache_key(token.strip()), None)

    def _purge(self, now: float) -> None:
        for key in [k for k, (_, until) in self._cache.items() if until <= now]:
            del self._cache[key]
