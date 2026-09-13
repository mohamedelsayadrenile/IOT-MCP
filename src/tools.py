"""The MCP tools this server exposes.

Module-level functions rather than closures, so each can be imported, called
and breakpointed on its own. Payload shaping lives in src/services/processing.py.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any, TypeVar

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from src.core.config import Settings
from src.services.auth import ExchangeTokenVerifier, ReNileAccessToken
from src.services.errors import RenileAPIError, RenileAuthExpiredError
from src.services.processing import build_devices_response, build_readings_response
from src.services.renile_client import ReNileClient

logger = logging.getLogger(__name__)


@dataclass
class AppState:
    """What the server lifespan builds once and every request borrows."""

    client: ReNileClient
    settings: Settings
    verifier: ExchangeTokenVerifier


ServerContext = Context[AppState, Any]

T = TypeVar("T")

_READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)


def _caller() -> ReNileAccessToken:
    """The verified access token of whoever made this request.

    RequireAuthMiddleware has already refused anonymous requests, so this is
    only ever missing if the auth wiring is wrong. The user is identified by
    this token alone -- no tool takes a user id from the client.
    """
    access_token = get_access_token()
    if not isinstance(access_token, ReNileAccessToken):  # pragma: no cover
        raise ToolError(
            "No ReNile credentials on this request. Ask the user to reconnect "
            "the ReNile connector."
        )
    return access_token


async def _fetch(
    state: AppState,
    caller: ReNileAccessToken,
    tool: str,
    call: Callable[[str], Awaitable[T]],
) -> T:
    """Run one upstream call as `caller`, translating failures into ToolError.

    The single place tool errors are handled and logged.
    """
    try:
        return await call(caller.renile_jwt)
    except RenileAuthExpiredError as exc:
        # The ReNile API refused an exchanged JWT that still looked live. Drop
        # it, so the next request exchanges afresh: that either yields a
        # working JWT or a 401 challenge that sends the client to reconnect.
        state.verifier.forget(caller.token)
        logger.warning("tool_failed tool=%s sub=%s error=%s", tool, caller.subject, exc)
        raise ToolError(str(exc)) from exc
    except RenileAPIError as exc:
        logger.warning("tool_failed tool=%s sub=%s error=%s", tool, caller.subject, exc)
        raise ToolError(str(exc)) from exc


async def get_all_devices(ctx: ServerContext) -> dict[str, Any]:
    """List every ReNile IoT device on the account.

    Returns {"count": int, "devices": [{"_id": str, "name": str}, ...]}.
    Use this to discover the exact device name or id to pass to get_latest_readings.
    """
    state = ctx.request_context.lifespan_context
    caller = _caller()
    devices = await _fetch(state, caller, "get_all_devices", state.client.get_devices)

    response = build_devices_response(devices)
    logger.info(
        "get_all_devices_succeeded sub=%s count=%s", caller.subject, response["count"]
    )
    return response


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
    caller = _caller()
    snapshot = await _fetch(
        state, caller, "get_latest_readings", state.client.get_snapshot
    )

    response = build_readings_response(
        snapshot, device, state.settings.stale_after_seconds
    )
    logger.info(
        "get_latest_readings_succeeded sub=%s device=%r matched=%s readings=%s "
        "stale=%s",
        caller.subject,
        device,
        response.get("matched", True),
        response.get("reading_count"),
        response.get("stale_count"),
    )
    return response


def register_tools(mcp: MCPServer[AppState]) -> None:
    """Attach every tool in this module to `mcp`."""
    for tool in (get_all_devices, get_latest_readings):
        mcp.tool(annotations=_READ_ONLY)(tool)
