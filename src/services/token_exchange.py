import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from src.core.config import Settings
from src.services.errors import RenileAPIError, TokenExchangeRejectedError

logger = logging.getLogger(__name__)

TOKEN_EXCHANGE_GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"


@dataclass(frozen=True)
class ExchangedToken:
    renile_jwt: str = field(repr=False)
    subject: str
    scopes: list[str]
    expires_in: int
    client_id: str | None = None


async def exchange_token(
    http_client: httpx.AsyncClient, settings: Settings, oauth_token: str
) -> ExchangedToken:
    if not (
        settings.token_exchange_url
        and settings.mcp_oauth_client_id
        and settings.mcp_oauth_client_secret
    ):  # pragma: no cover - Settings refuses to start without them
        raise RenileAPIError("Token exchange is not configured.")
    form = {
        "grant_type": TOKEN_EXCHANGE_GRANT,
        "subject_token": oauth_token,
        "subject_token_type": ACCESS_TOKEN_TYPE,
    }
    if settings.token_exchange_audience:
        form["audience"] = settings.token_exchange_audience

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
        raise RenileAPIError("The ReNile sign-in service is unreachable.") from exc

    if response.status_code == 401:
        logger.error("token_exchange_failed status_code=401 reason=invalid_client")
        raise TokenExchangeRejectedError("Token exchange client rejected.")
    if response.status_code == 400:
        logger.info("token_exchange_rejected status_code=400")
        raise TokenExchangeRejectedError("OAuth token rejected.")
    if response.status_code != 200:
        logger.warning("token_exchange_failed status_code=%s", response.status_code)
        raise RenileAPIError(
            f"The ReNile sign-in service returned {response.status_code}."
        )

    try:
        payload = response.json()
        result = ExchangedToken(
            renile_jwt=_required_str(payload, "access_token"),
            subject=_required_str(payload, "sub"),
            scopes=str(payload.get("scope") or "").split(),
            expires_in=int(payload["expires_in"]),
            client_id=payload.get("client_id") or None,
        )
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        logger.warning("token_exchange_failed reason=malformed_response")
        raise RenileAPIError(
            "The ReNile sign-in service returned an unexpected response."
        ) from exc

    logger.info(
        "token_exchange_succeeded sub=%s scopes=%s expires_in=%s",
        result.subject,
        " ".join(result.scopes),
        result.expires_in,
    )
    return result


def _required_str(payload: dict[str, Any], key: str) -> str:
    value = payload[key]
    if not isinstance(value, str) or not value:
        raise ValueError(f"missing {key}")
    return value
