"""Async HTTP client for the ReNile IoT platform."""

import logging
from time import perf_counter
from typing import Any

import httpx

from src.core.config import Settings

logger = logging.getLogger(__name__)


class RenileAPIError(Exception):
    """Raised when the ReNile API cannot be reached or rejects the request."""


class ReNileClient:
    """Thin wrapper over the two read endpoints this server exposes.

    The platform authenticates with `Authorization: JWT <token>` (not Bearer) and
    returns a plain-text body on 401, so status is always checked before parsing.
    """

    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    async def get_devices(self) -> list[dict[str, Any]]:
        """Return the device roster: [{"_id": ..., "name": ...}, ...]."""
        path = self._settings.renile_devices_path
        payload = await self._get(path)
        if not isinstance(payload, list):
            raise RenileAPIError(
                f"Expected a list of devices from {path}, got "
                f"{type(payload).__name__}."
            )
        return payload

    async def get_snapshot(self) -> dict[str, Any]:
        """Return the latest-readings snapshot for every project and device."""
        path = self._settings.renile_snapshot_path
        payload = await self._get(path)
        if not isinstance(payload, dict):
            raise RenileAPIError(
                f"Expected an object from {path}, got {type(payload).__name__}."
            )
        return payload

    async def _get(self, path: str) -> Any:
        """GET `path` and parse it.

        The single place that enforces "check the status before parsing JSON".
        """
        try:
            response = await self._request(path)
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

        if response.status_code in (401, 403):
            # The body here is the literal text "Unauthorized", not JSON.
            logger.warning(
                "renile_http_get_failed path=%s status_code=%s reason=auth",
                path,
                response.status_code,
            )
            raise RenileAPIError(
                "ReNile rejected the API token (check RENILE_API_TOKEN in src/.env)."
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

    async def _request(self, path: str) -> httpx.Response:
        """Issue the GET, retrying so a transient blip is not a tool failure."""
        max_attempts = self._settings.http_max_attempts
        for attempt in range(1, max_attempts + 1):
            started_at = perf_counter()
            logger.info("renile_http_get_started path=%s attempt=%s", path, attempt)
            try:
                response = await self._client.get(path)
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

    The token is read once here and lives only in this header.
    """
    http_client = httpx.AsyncClient(
        base_url=settings.renile_api_base_url,
        headers={
            "Authorization": f"JWT {settings.renile_api_token.get_secret_value()}",
            "Accept": "application/json",
        },
        timeout=settings.http_timeout_seconds,
    )
    return ReNileClient(http_client, settings)
