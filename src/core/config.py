"""Centralised settings.

No module outside this one reads environment variables directly.
"""

from functools import lru_cache
from pathlib import Path

from typing import Annotated

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# A dotenv file is a local-development convenience only. In a container the
# environment is the source of truth, and pydantic-settings already prefers real
# environment variables over the file. Resolved from __file__ rather than the cwd
# so a local run works from any directory.
ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


def _split_csv(value: object) -> object:
    """Accept `a,b,c` for list fields, since env vars cannot carry JSON comfortably.

    Each entry also gains a `:*` twin. Host and Origin headers carry a port
    whenever the server is not on 80/443, and the SDK matches them literally --
    so a bare `example.com` would reject `example.com:8000` and every request
    would come back 421. DNS-rebinding protection is about the hostname, not the
    port; the SDK's own localhost defaults are written the same way.
    """
    if isinstance(value, str):
        value = [item.strip() for item in value.split(",") if item.strip()]
    if not isinstance(value, list):
        return value

    expanded: list[str] = []
    for item in value:
        expanded.append(item)
        if isinstance(item, str) and not item.endswith(":*"):
            expanded.append(f"{item}:*")
    return expanded


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- OAuth ---------------------------------------------------------------
    # The ReNile backend's OAuth authorization server. This server issues and
    # validates nothing itself; it only advertises the issuer to clients.
    # Kept as plain strings rather than AnyHttpUrl: pydantic would append a
    # trailing slash to a path-less URL, and RFC 8414 compares issuers by exact
    # string. AuthSettings parses these itself with url_preserve_empty_path=True.
    issuer_url: str = Field(alias="RENILE_ISSUER_URL")

    # Must be the exact public URL clients connect to, including the /mcp path:
    # the SDK derives both the protected-resource metadata route and the
    # `resource_metadata=` value of the 401 challenge from it. Get it wrong and
    # the OAuth discovery chain dead-ends with no useful error.
    resource_server_url: str = Field(alias="RENILE_RESOURCE_SERVER_URL")

    # Scopes a token must carry to use /mcp at all. Advertised in the
    # protected-resource metadata, so clients request exactly these.
    required_scopes: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["devices:read", "readings:read"],
        alias="OAUTH_REQUIRED_SCOPES",
    )

    # --- Token exchange (RFC 8693) ---------------------------------------------
    # The backend endpoint that turns a client's OAuth access token into a
    # short-lived ReNile JWT for the same user. The backend does all validation.
    # The client credentials are what stop anyone else -- Claude included --
    # from swapping an OAuth token for a ReNile JWT themselves.
    token_exchange_url: str | None = Field(default=None, alias="TOKEN_EXCHANGE_URL")
    token_exchange_audience: str | None = Field(
        default=None, alias="TOKEN_EXCHANGE_AUDIENCE"
    )
    mcp_oauth_client_id: str | None = Field(default=None, alias="MCP_OAUTH_CLIENT_ID")
    mcp_oauth_client_secret: SecretStr | None = Field(
        default=None, alias="MCP_OAUTH_CLIENT_SECRET"
    )

    # The ReNile platform authenticates with `Authorization: JWT <token>`, not
    # `Bearer`. Configurable so the switch is a deploy, not a release.
    upstream_auth_scheme: str = Field(default="JWT", alias="RENILE_UPSTREAM_AUTH_SCHEME")

    # --- HTTP server ---------------------------------------------------------
    host: str = Field(default="0.0.0.0", alias="HOST")

    # DNS-rebinding protection is always on; these must list the public hostname
    # or every proxied request is rejected with 421.
    # NoDecode: without it pydantic-settings tries to JSON-parse the raw env
    # var before the comma-splitting validator below ever runs.
    allowed_hosts: Annotated[list[str], NoDecode] = Field(
        default_factory=list, alias="ALLOWED_HOSTS"
    )
    allowed_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=list, alias="ALLOWED_ORIGINS"
    )

    # Sessions are held in a per-process dict, so stateful serving requires either
    # a single worker or sticky routing. Enable this only when running several
    # replicas behind a round-robin balancer.
    stateless_http: bool = Field(default=False, alias="STATELESS_HTTP")

    # --- Upstream API --------------------------------------------------------
    renile_api_base_url: str = Field(
        default="https://renile-iot.com", alias="RENILE_API_BASE_URL"
    )
    http_timeout_seconds: float = Field(
        default=15.0, alias="HTTP_TIMEOUT_SECONDS", gt=0
    )
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # Endpoint paths, kept separate from the base URL so either can move
    # independently of the other.
    renile_devices_path: str = Field(
        default="/api/v1/devices/names/", alias="RENILE_DEVICES_PATH"
    )
    renile_snapshot_path: str = Field(
        default="/api/v1/snapshot/", alias="RENILE_SNAPSHOT_PATH"
    )

    # Total attempts per request, so 1 disables retrying. Timeouts are never
    # retried regardless -- they have already consumed the full budget.
    http_max_attempts: int = Field(default=2, alias="HTTP_MAX_ATTEMPTS", ge=1)

    # The connection pool is shared by every user of the server now, so its size
    # is a capacity decision rather than an afterthought.
    http_max_connections: int = Field(
        default=100, alias="HTTP_MAX_CONNECTIONS", gt=0
    )

    # How much of an error body to quote back. Caps what an upstream failure can
    # push into the model's context.
    error_body_preview_chars: int = Field(
        default=200, alias="ERROR_BODY_PREVIEW_CHARS", gt=0
    )

    # A reading older than this is flagged is_stale. The platform serves readings
    # up to ~500 days old alongside live ones, so this guard is load-bearing.
    stale_after_seconds: int = Field(
        default=3600, alias="STALE_AFTER_SECONDS", gt=0
    )

    _split_hosts = field_validator("allowed_hosts", "allowed_origins", mode="before")(
        _split_csv
    )

    @field_validator("required_scopes", mode="before")
    @classmethod
    def _split_scopes(cls, value: object) -> object:
        """Accept `a b` or `a,b`, the two ways a scope list is usually written."""
        if isinstance(value, str):
            return value.replace(",", " ").split()
        return value

    @field_validator("issuer_url", "resource_server_url")
    @classmethod
    def _no_trailing_slash(cls, value: str) -> str:
        """Strip a trailing slash so issuer comparison stays exact."""
        return value.rstrip("/")

    @model_validator(mode="after")
    def _exchange_configured(self) -> "Settings":
        """Every request depends on the exchange: fail at startup, not on the
        first user's first request."""
        missing = [
            alias
            for alias, value in (
                ("TOKEN_EXCHANGE_URL", self.token_exchange_url),
                ("MCP_OAUTH_CLIENT_ID", self.mcp_oauth_client_id),
                ("MCP_OAUTH_CLIENT_SECRET", self.mcp_oauth_client_secret),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"Missing required settings: {', '.join(missing)}")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
