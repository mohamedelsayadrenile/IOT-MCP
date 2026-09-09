"""ASGI entrypoint.

    uv run uvicorn src.app:app --host 0.0.0.0 --port 8000 \
        --proxy-headers --forwarded-allow-ips '10.0.0.0/8'

Run uvicorn directly rather than via MCPServer.run("streamable-http"): that
helper builds its own uvicorn config with no proxy-header handling, which is not
enough behind a load balancer.
"""

import json
import logging

from starlette.applications import Starlette
from starlette.types import ASGIApp, Receive, Scope, Send

from mcp.server.transport_security import TransportSecuritySettings

from src.core.config import Settings, get_settings
from src.core.logging import configure_logging
from src.server import build_server

logger = logging.getLogger(__name__)

_UNAUTHORIZED_BODY = json.dumps(
    {"error": "unauthorized", "error_description": "Authentication required"}
).encode()


class StageOneUnauthorized:
    """Answer every MCP request with a bare 401 and nothing else.

    Deliberately omits the `WWW-Authenticate` header. A 401 carrying that header
    is an invitation: an MCP client reads the `resource_metadata=` parameter,
    fetches the protected-resource document, and walks on into authorization
    server discovery. Without it there is nothing to follow, so the client stops
    at the 401 -- which is the whole point of this stage.

    Installed as the outermost middleware, so it short-circuits before the MCP
    transport: no session is created and no tool is reached. Non-HTTP scopes and
    other routes (`/healthz`) pass through untouched.

    Temporary. Deleting this class and setting OAUTH_CHALLENGE_ENABLED=true
    restores the real resource-server behaviour built in src/server.py.
    """

    def __init__(self, app: ASGIApp, mcp_path: str) -> None:
        self._app = app
        self._mcp_path = mcp_path.rstrip("/")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"].rstrip("/") != self._mcp_path:
            await self._app(scope, receive, send)
            return

        logger.info(
            "stage1_unauthorized method=%s path=%s client=%s",
            scope.get("method"),
            scope.get("path"),
            (scope.get("client") or ("-",))[0],
        )
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(_UNAUTHORIZED_BODY)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": _UNAUTHORIZED_BODY})


def build_app(settings: Settings | None = None) -> Starlette:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    mcp_path = "/mcp"
    app = build_server(settings).streamable_http_app(
        streamable_http_path=mcp_path,
        stateless_http=settings.stateless_http,
        host=settings.host,
        # Passed explicitly on purpose. Left to the SDK, a host of 127.0.0.1
        # auto-allows only localhost -- so every proxied request comes back 421
        # Invalid Host header -- while a host of 0.0.0.0 turns the protection off
        # altogether. Neither default is what a deployment wants.
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=settings.allowed_hosts,
            allowed_origins=settings.allowed_origins,
        ),
    )

    if not settings.oauth_challenge_enabled:
        logger.warning(
            "stage1_mode_active: %s answers a bare 401; OAuth discovery is off",
            mcp_path,
        )
        # add_middleware rather than wrapping the app object, so the returned
        # value stays a Starlette instance and its lifespan still runs.
        app.add_middleware(StageOneUnauthorized, mcp_path=mcp_path)

    return app


app = build_app()
