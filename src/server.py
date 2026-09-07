"""MCP server exposing ReNile IoT device and reading tools over stdio.

Wiring only: transport, tool declarations, and error translation. Payload shaping
lives in src/services/processing.py.

Nothing in this process may write to stdout: it carries the JSON-RPC stream.
"""

import logging
import sys
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field, ValidationError

from src.core.config import ENV_FILE, get_settings
from src.core.logging import configure_logging
from src.services.processing import build_devices_response, build_readings_response
from src.services.renile_client import ReNileClient, RenileAPIError, build_client

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

mcp = MCPServer(
    name="renile-iot",
    version="0.1.0",
    instructions=INSTRUCTIONS,
)

_client: ReNileClient | None = None


def get_client() -> ReNileClient:
    """Lazily build the shared client.

    mcp.run() drives a single event loop for the process lifetime, so one client
    instance is safe and no lifespan/Context plumbing is needed.
    """
    global _client
    if _client is None:
        _client = build_client(get_settings())
    return _client


@mcp.tool(
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
)
async def get_all_devices() -> dict[str, Any]:
    """List every ReNile IoT device on the account.

    Returns {"count": int, "devices": [{"_id": str, "name": str}, ...]}.
    Use this to discover the exact device name or id to pass to get_latest_readings.
    """
    try:
        devices = await get_client().get_devices()
    except RenileAPIError as exc:
        raise ToolError(str(exc)) from exc

    response = build_devices_response(devices)
    logger.info("get_all_devices_succeeded count=%s", response["count"])
    return response


@mcp.tool(
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
)
async def get_latest_readings(
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
    settings = get_settings()
    try:
        snapshot = await get_client().get_snapshot()
    except RenileAPIError as exc:
        raise ToolError(str(exc)) from exc

    response = build_readings_response(snapshot, device, settings.stale_after_seconds)
    logger.info(
        "get_latest_readings_succeeded device=%r matched=%s readings=%s stale=%s",
        device,
        response.get("matched", True),
        response.get("reading_count"),
        response.get("stale_count"),
    )
    return response


def main() -> None:
    """Entry point. Validates settings before speaking the protocol."""
    try:
        settings = get_settings()
    except ValidationError:
        # stderr only: stdout is the JSON-RPC channel.
        print(
            f"renile-mcp: RENILE_API_TOKEN is not set. Expected it in {ENV_FILE}",
            file=sys.stderr,
        )
        raise SystemExit(1)

    configure_logging(settings.log_level)
    logger.info("renile_mcp_starting base_url=%s", settings.renile_api_base_url)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
