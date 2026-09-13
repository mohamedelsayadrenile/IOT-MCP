"""Async HTTP client for the ReNile IoT platform."""

import logging
from time import perf_counter
from typing import Any

import httpx

from src.core.config import Settings
from src.services.errors import (
    RenileAPIError,
    RenileAuthExpiredError,
    RenilePermissionError,
)
from src.services.token_exchange import ExchangedToken, exchange_token

logger = logging.getLogger(__name__)


class ReNileClient:
    """Thin wrapper over the two read endpoints this server exposes.

    The connection pool is shared by every caller, but credentials are not: the
    token belongs to one request and is passed in per call rather than living in
    the session's default headers.

    The platform authenticates with `Authorization: JWT <token>` (not Bearer, by
    default) and returns a plain-text body on 401, so status is always checked
    before parsing.
    """

    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    async def get_devices(self, token: str) -> list[dict[str, Any]]:
        """Return the device roster: [{"_id": ..., "name": ...}, ...]."""
        path = self._settings.renile_devices_path
        payload = await self._get(path, token)
        if not isinstance(payload, list):
            raise RenileAPIError(
                f"Expected a list of devices from {path}, got "
                f"{type(payload).__name__}."
            )
        return payload

    async def get_snapshot(self, token: str) -> dict[str, Any]:
        """Return the latest-readings snapshot for every project and device."""
        path = self._settings.renile_snapshot_path
        payload = await self._get(path, token)
        if not isinstance(payload, dict):
            raise RenileAPIError(
                f"Expected an object from {path}, got {type(payload).__name__}."
            )
        return payload

    async def exchange_token(self, oauth_token: str) -> ExchangedToken:
        """Swap an OAuth access token for a ReNile JWT. See token_exchange.py."""
        return await exchange_token(self._client, self._settings, oauth_token)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, token: str) -> Any:
        """GET `path` as the holder of `token` and parse the result.

        The single place that enforces "check the status before parsing JSON".
        """
        try:
            response = await self._request(path, token)
        except httpx.TimeoutException as exc:
            logger.warning("renile_http_get_failed path=%s reason=timeout", path)
            raise RenileAPIError(
                f"ReNile API timed out for {path}. "
                "The platform may be slow or unreachable."
            ) from exc
        except httpx.TransportError as exc:
            logger.warning(
                "renile_http_get_failed path=%s reason=transport error=%s", path, exc
            )
            raise RenileAPIError(
                f"ReNile API unreachable for {path}. Check network connectivity."
            ) from exc

        # 401 and 403 mean different things to the person on the other end and
        # must not share a branch: one is "sign in again", the other is "you are
        # signed in but not allowed to see this".
        if response.status_code == 401:
            # The body here is the literal text "Unauthorized", not JSON.
            logger.warning(
                "renile_http_get_failed path=%s status_code=401 reason=auth", path
            )
            raise RenileAuthExpiredError(
                "The ReNile platform rejected this login. The session has most "
                "likely expired -- ask the user to reconnect the ReNile connector."
            )

        if response.status_code == 403:
            logger.warning(
                "renile_http_get_failed path=%s status_code=403 reason=forbidden", path
            )
            raise RenilePermissionError(
                "This ReNile account is not allowed to read that. Signing in "
                "again will not help -- the account needs the permission granted."
            )

        if response.status_code >= 400:
            logger.warning(
                "renile_http_get_failed path=%s status_code=%s",
                path,
                response.status_code,
            )
            raise RenileAPIError(
                f"ReNile API returned {response.status_code} for {path}: "
                f"{self._preview(response)}"
            )

        try:
            return response.json()
        except ValueError as exc:
            raise RenileAPIError(
                f"ReNile API returned a non-JSON body for {path}: "
                f"{self._preview(response)}"
            ) from exc

    async def _request(self, path: str, token: str) -> httpx.Response:
        """Issue the GET, retrying so a transient blip is not a tool failure."""
        # Per-request, never a session default: the pool is shared across users.
        # Merged with the client's own headers by httpx, so Accept survives.
        headers = {
            "Authorization": f"{self._settings.upstream_auth_scheme} {token}"
        }
        max_attempts = self._settings.http_max_attempts
        for attempt in range(1, max_attempts + 1):
            started_at = perf_counter()
            logger.info("renile_http_get_started path=%s attempt=%s", path, attempt)
            try:
                response = await self._client.get(path, headers=headers)
            except httpx.TimeoutException:
                # A timeout already consumed the full budget; retrying would only
                # double the wait before failing. TimeoutException subclasses
                # TransportError, so it must be caught first.
                raise
            except httpx.TransportError as exc:
                if attempt == max_attempts:
                    raise
                logger.info(
                    "renile_http_get_retrying path=%s error=%s",
                    path,
                    type(exc).__name__,
                )
                continue

            logger.info(
                "renile_http_get_succeeded path=%s status_code=%s latency_ms=%.1f "
                "response_bytes=%s",
                path,
                response.status_code,
                (perf_counter() - started_at) * 1000,
                len(response.content),
            )
            return response

        raise AssertionError("unreachable: the final attempt returns or raises")

    def _preview(self, response: httpx.Response) -> str:
        """Truncate a body before it is quoted into an error message."""
        return response.text[: self._settings.error_body_preview_chars]


def build_client(settings: Settings) -> ReNileClient:
    """Build the client and the HTTP session it owns.

    The session carries no credentials: every caller shares this connection pool,
    so an Authorization default header here would leak one user's token to the
    next request. Tokens are supplied per call instead.
    """
    http_client = httpx.AsyncClient(
        base_url=settings.renile_api_base_url,
        headers={"Accept": "application/json"},
        timeout=settings.http_timeout_seconds,
        limits=httpx.Limits(max_connections=settings.http_max_connections),
    )
    return ReNileClient(http_client, settings)
