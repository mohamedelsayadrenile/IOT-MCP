import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from src.core.config import Settings
from src.services.auth import ExchangeTokenVerifier
from src.services.http import build_http_client
from src.tools import AppState, register_tools

logger = logging.getLogger(__name__)

INSTRUCTIONS = """\
The Nojo MCP server. It is currently a sign-in bridge only: the single whoami tool \
reports which Nojo account the connection is authenticated as.

Platform tools for devices and sensor readings are not exposed yet. If the user asks \
for data, say the connector is connected but no data tools are available yet rather \
than guessing at values.\
"""


def build_server(settings: Settings) -> MCPServer[AppState]:
    verifier = ExchangeTokenVerifier(settings)

    @asynccontextmanager
    async def lifespan(_: MCPServer[AppState]) -> AsyncIterator[AppState]:
        client = build_http_client(settings)
        verifier.http_client = client
        logger.info(
            "nojo_mcp_starting issuer=%s resource=%s",
            settings.issuer_url,
            settings.resource_server_url,
        )
        try:
            yield AppState(client=client, settings=settings, verifier=verifier)
        finally:
            verifier.http_client = None
            await client.aclose()
            logger.info("nojo_mcp_stopped")

    mcp = MCPServer(
        name="nojo",
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
