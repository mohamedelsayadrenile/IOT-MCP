import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from src.core.config import Settings
from src.services.auth import ExchangeTokenVerifier
from src.services.renile_client import build_client
from src.tools import AppState, register_tools

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


def build_server(settings: Settings) -> MCPServer[AppState]:
    verifier = ExchangeTokenVerifier(settings.issuer_url)

    @asynccontextmanager
    async def lifespan(_: MCPServer[AppState]) -> AsyncIterator[AppState]:
        client = build_client(settings)
        verifier.client = client
        logger.info(
            "renile_mcp_starting base_url=%s resource=%s",
            settings.renile_api_base_url,
            settings.resource_server_url,
        )
        try:
            yield AppState(client=client, settings=settings, verifier=verifier)
        finally:
            verifier.client = None
            await client.aclose()
            logger.info("renile_mcp_stopped")

    mcp = MCPServer(
        name="renile-iot",
        version="0.1.0",
        instructions=INSTRUCTIONS,
        lifespan=lifespan,
        token_verifier=verifier,
        auth=AuthSettings(
            issuer_url=settings.issuer_url,
            resource_server_url=settings.resource_server_url,
        ),
    )
    register_tools(mcp)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    return mcp
