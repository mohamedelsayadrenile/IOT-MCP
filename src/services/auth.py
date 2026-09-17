import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from mcp.server.auth.provider import AccessToken, TokenVerifier
from pydantic import Field

from src.core.config import Settings
from src.services.errors import NojoAPIError, TokenExchangeRejectedError

logger = logging.getLogger(__name__)

_FALLBACK_CLIENT_ID = "nojo-mcp"

_EXPIRY_MARGIN_SECONDS = 30
TOKEN_EXCHANGE_GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"


@dataclass(frozen=True)
class ExchangedToken:
    nojo_jwt: str = field(repr=False)
    subject: str
    expires_in: int
    client_id: str | None = None


class NojoAccessToken(AccessToken):
    nojo_jwt: str = Field(repr=False, exclude=True)


def _cache_key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class ExchangeTokenVerifier(TokenVerifier):
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.http_client: httpx.AsyncClient | None = None
        self._cache: dict[str, tuple[NojoAccessToken, float]] = {}

    async def verify_token(self, token: str) -> NojoAccessToken | None:
        token = token.strip()
        if not token or self.http_client is None:
            return None

        key = _cache_key(token)
        now = time.time()
        cached = self._cache.get(key)
        if cached is not None and cached[1] > now:
            return cached[0]

        try:
            exchanged = await exchange_token(self.http_client, self._settings, token)
        except TokenExchangeRejectedError:
            self._cache.pop(key, None)
            return None

        expires_at = int(now) + exchanged.expires_in
        access_token = NojoAccessToken(
            token=token,
            client_id=exchanged.client_id or _FALLBACK_CLIENT_ID,
            scopes=[],
            subject=exchanged.subject,
            expires_at=expires_at,
            claims={"iss": self._settings.issuer_url},
            nojo_jwt=exchanged.nojo_jwt,
        )
        self._purge(now)
        self._cache[key] = (access_token, expires_at - _EXPIRY_MARGIN_SECONDS)
        return access_token

    def forget(self, token: str) -> None:
        self._cache.pop(_cache_key(token.strip()), None)

    def _purge(self, now: float) -> None:
        for key in [k for k, (_, until) in self._cache.items() if until <= now]:
            del self._cache[key]


async def exchange_token(
    http_client: httpx.AsyncClient, settings: Settings, oauth_token: str
) -> ExchangedToken:
    form = {
        "grant_type": TOKEN_EXCHANGE_GRANT,
        "subject_token": oauth_token,
        "subject_token_type": ACCESS_TOKEN_TYPE,
    }

    try:
        response = await http_client.post(
            settings.token_exchange_url,
            data=form,
            auth=(
                settings.mcp_oauth_client_id,
                settings.mcp_oauth_client_secret.get_secret_value(),
            ),
        )
    except httpx.TransportError as exc:
        logger.warning(
            "token_exchange_failed reason=transport error=%s", type(exc).__name__
        )
        raise NojoAPIError("The Nojo sign-in service is unreachable.") from exc

    if response.status_code == 401:
        logger.error("token_exchange_failed status_code=401 reason=invalid_client")
        raise TokenExchangeRejectedError("Token exchange client rejected.")
    if response.status_code == 400:
        logger.info("token_exchange_rejected status_code=400")
        raise TokenExchangeRejectedError("OAuth token rejected.")
    if response.status_code != 200:
        logger.warning("token_exchange_failed status_code=%s", response.status_code)
        raise NojoAPIError(
            f"The Nojo sign-in service returned {response.status_code}."
        )

    try:
        payload = response.json()
        result = ExchangedToken(
            nojo_jwt=_required_str(payload, "access_token"),
            subject=_required_str(payload, "sub"),
            expires_in=int(payload["expires_in"]),
            client_id=payload.get("client_id") or None,
        )
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        logger.warning("token_exchange_failed reason=malformed_response")
        raise NojoAPIError(
            "The Nojo sign-in service returned an unexpected response."
        ) from exc

    logger.info(
        "token_exchange_succeeded sub=%s expires_in=%s",
        result.subject,
        result.expires_in,
    )
    return result


def _required_str(payload: dict[str, Any], key: str) -> str:
    value = payload[key]
    if not isinstance(value, str) or not value:
        raise ValueError(f"missing {key}")
    return value
