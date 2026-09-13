"""ASGI entrypoint.

    uv run uvicorn src.app:app --host 0.0.0.0 --port 8000 \
        --proxy-headers --forwarded-allow-ips '10.0.0.0/8'

Run uvicorn directly rather than via MCPServer.run("streamable-http"): that
helper builds its own uvicorn config with no proxy-header handling, which is not
enough behind a load balancer.
"""

from starlette.applications import Starlette

from mcp.server.transport_security import TransportSecuritySettings

from src.core.config import Settings, get_settings
from src.core.logging import configure_logging
from src.server import build_server


def build_app(settings: Settings | None = None) -> Starlette:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    return build_server(settings).streamable_http_app(
        streamable_http_path="/mcp",
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


app = build_app()
