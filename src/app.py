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
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=settings.allowed_hosts,
            allowed_origins=settings.allowed_origins,
        ),
    )


app = build_app()
