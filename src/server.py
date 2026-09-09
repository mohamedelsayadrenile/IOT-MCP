"""MCP server exposing ReNile IoT device and reading tools over Streamable HTTP.

Wiring only: transport, tool declarations, and error translation. Payload shaping
lives in src/services/processing.py.

This server is an OAuth 2.1 resource server. It holds no credentials of its own:
each request carries the caller's own access token, which is forwarded to the
ReNile API unchanged. See src/core/auth.py for what is (and is not) checked.
"""

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from collections.abc import AsyncIterator
from typing import Annotated, Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import UrlElicitationRequiredError
from mcp_types import ElicitRequestURLParams, ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from src.core.auth import PassthroughTokenVerifier
from src.core.config import Settings
from src.services.processing import build_devices_response, build_readings_response
from src.services.renile_client import (
    ReNileClient,
    RenileAPIError,
    RenileAuthExpiredError,
    build_client,
)

logger = logging.getLogger(__name__)

INSTRUCTIONS = """\
Tools for the ReNile IoT platform, which monitors agricultural and water sensors \
(temperature, humidity, CO2, pH, water level, ...) grouped into devices under a project.

Call get_all_devices first when you need exact device names or IDs, then pass one to \
get_latest_readings to look at a single device instead of retrieving every reading.

Each reading carries a `status` of normal, high, low or unknown, and an `is_stale` flag. \
A stale reading is a last-known value that has not refreshed in a long time -- never \
report one as the current condition without saying how old it is.\
"""


@dataclass
class AppState:
    """What the lifespan builds once and every request borrows."""

    client: ReNileClient
    settings: Settings


ServerContext = Context[AppState, Any]


def _caller_token() -> str:
    """The access token of whoever made this request.

    RequireAuthMiddleware has already refused anonymous requests, so this is
    only ever missing if the auth wiring is wrong.
    """
    access_token = get_access_token()
    if access_token is None:  # pragma: no cover - defensive
        raise ToolError(
            "No ReNile credentials on this request. Ask the user to reconnect "
            "the ReNile connector."
        )
    return access_token.token


def _reauth(ctx: ServerContext, exc: RenileAuthExpiredError) -> Exception:
    """Turn an upstream 401 into the strongest signal this client understands.

    A tool failure cannot become an HTTP 401 in this SDK, so an expired token is
    normally caught earlier, by the bearer middleware, from the token's own `exp`.
    This path only runs when the platform rejects a token that still looks live.
    """
    capabilities = ctx.session.client_capabilities
    elicitation = capabilities.elicitation if capabilities else None
    if elicitation is not None and elicitation.url is not None:
        return UrlElicitationRequiredError(
            [
                ElicitRequestURLParams(
                    message="Your ReNile session has expired. Sign in again to continue.",
                    url=ctx.request_context.lifespan_context.settings.issuer_url,
                )
            ]
        )
    return ToolError(str(exc))


def build_server(settings: Settings) -> MCPServer[AppState]:
    """Build the server. A factory, so tests can supply their own settings."""

    @asynccontextmanager
    async def lifespan(_: MCPServer[AppState]) -> AsyncIterator[AppState]:
        client = build_client(settings)
        logger.info(
            "renile_mcp_starting base_url=%s resource=%s",
            settings.renile_api_base_url,
            settings.resource_server_url,
        )
        try:
            yield AppState(client=client, settings=settings)
        finally:
            await client.aclose()
            logger.info("renile_mcp_stopped")

    # Stage 1 leaves both of these None. That is what keeps the server silent
    # about OAuth: with no `auth`, the SDK mounts neither RequireAuthMiddleware
    # (the source of the WWW-Authenticate challenge) nor the RFC 9728
    # /.well-known/oauth-protected-resource route. The bare 401 is served by
    # StageOneUnauthorized in src/app.py instead.
    auth_wiring: dict[str, Any] = {}
    if settings.oauth_challenge_enabled:
        auth_wiring = {
            "token_verifier": PassthroughTokenVerifier(settings.issuer_url),
            "auth": AuthSettings(
                issuer_url=settings.issuer_url,
                resource_server_url=settings.resource_server_url,
                # This server enforces no scopes: the ReNile API decides what a
                # token may read.
                required_scopes=None,
            ),
        }

    mcp = MCPServer(
        name="renile-iot",
        version="0.1.0",
        instructions=INSTRUCTIONS,
        lifespan=lifespan,
        **auth_wiring,
    )

    @mcp.tool(
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    async def get_all_devices(ctx: ServerContext) -> dict[str, Any]:
        """List every ReNile IoT device on the account.

        Returns {"count": int, "devices": [{"_id": str, "name": str}, ...]}.
        Use this to discover the exact device name or id to pass to get_latest_readings.
        """
        state = ctx.request_context.lifespan_context
        try:
            devices = await state.client.get_devices(_caller_token())
        except RenileAuthExpiredError as exc:
            raise _reauth(ctx, exc) from exc
        except RenileAPIError as exc:
            raise ToolError(str(exc)) from exc

        response = build_devices_response(devices)
        logger.info("get_all_devices_succeeded count=%s", response["count"])
        return response

    @mcp.tool(
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    async def get_latest_readings(
        ctx: ServerContext,
        device: Annotated[
            str | None,
            Field(
                description=(
                    "Optional device name or device id to filter by. Matching is "
                    "case-insensitive and accepts a partial name such as 'greenhouse'. "
                    "Omit to return the readings for every device."
                )
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Get the most recent sensor readings from the ReNile IoT platform.

        Each reading has: sensor, value, unit, lower_limit, upper_limit, status
        (normal | high | low | unknown), timestamp, age_seconds, and is_stale.

        A reading with is_stale=true is a last-known value that has not refreshed
        recently -- many devices carry readings that are months old. Do not present a
        stale reading as the current condition without mentioning its age.

        With no `device`, returns every project and device plus a summary. With a
        `device`, returns that one device's readings. If the name matches nothing or is
        ambiguous, returns matched=false along with the valid device names rather than
        failing.
        """
        state = ctx.request_context.lifespan_context
        try:
            snapshot = await state.client.get_snapshot(_caller_token())
        except RenileAuthExpiredError as exc:
            raise _reauth(ctx, exc) from exc
        except RenileAPIError as exc:
            raise ToolError(str(exc)) from exc

        response = build_readings_response(
            snapshot, device, state.settings.stale_after_seconds
        )
        logger.info(
            "get_latest_readings_succeeded device=%r matched=%s readings=%s stale=%s",
            device,
            response.get("matched", True),
            response.get("reading_count"),
            response.get("stale_count"),
        )
        return response

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_: Request) -> Response:
        """Liveness only. Deliberately makes no upstream call: there is no token
        to make one with, and inventing a service credential would reintroduce
        exactly the account-wide secret this server was built to remove."""
        return JSONResponse({"status": "ok"})

    return mcp
