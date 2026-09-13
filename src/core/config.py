from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


def _split_csv(value: object) -> object:
    if isinstance(value, str):
        value = [item.strip() for item in value.split(",") if item.strip()]

    if not isinstance(value, list):
        return value

    return [
        expanded
        for item in value
        for expanded in (
            item,
            f"{item}:*" if isinstance(item, str) and not item.endswith(":*") else None,
        )
        if expanded is not None
    ]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    issuer_url: str = Field(alias="RENILE_ISSUER_URL")
    resource_server_url: str = Field(alias="RENILE_RESOURCE_SERVER_URL")

    token_exchange_url: str | None = Field(None, alias="TOKEN_EXCHANGE_URL")
    mcp_oauth_client_id: str | None = Field(None, alias="MCP_OAUTH_CLIENT_ID")
    mcp_oauth_client_secret: SecretStr | None = Field(
        None, alias="MCP_OAUTH_CLIENT_SECRET"
    )

    upstream_auth_scheme: str = Field("JWT", alias="RENILE_UPSTREAM_AUTH_SCHEME")
    host: str = Field("0.0.0.0", alias="HOST")

    allowed_hosts: Annotated[list[str], NoDecode] = Field(
        default_factory=list, alias="ALLOWED_HOSTS"
    )
    allowed_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=list, alias="ALLOWED_ORIGINS"
    )

    stateless_http: bool = Field(False, alias="STATELESS_HTTP")

    renile_api_base_url: str = Field(
        "https://renile-iot.com", alias="RENILE_API_BASE_URL"
    )
    http_timeout_seconds: float = Field(
        15.0, alias="HTTP_TIMEOUT_SECONDS", gt=0
    )
    log_level: str = Field("INFO", alias="LOG_LEVEL")

    renile_devices_path: str = Field(
        "/api/v1/devices/names/", alias="RENILE_DEVICES_PATH"
    )
    renile_snapshot_path: str = Field(
        "/api/v1/snapshot/", alias="RENILE_SNAPSHOT_PATH"
    )

    http_max_attempts: int = Field(2, alias="HTTP_MAX_ATTEMPTS", ge=1)
    http_max_connections: int = Field(100, alias="HTTP_MAX_CONNECTIONS", gt=0)
    error_body_preview_chars: int = Field(
        200, alias="ERROR_BODY_PREVIEW_CHARS", gt=0
    )
    stale_after_seconds: int = Field(
        3600, alias="STALE_AFTER_SECONDS", gt=0
    )

    _split_lists = field_validator(
        "allowed_hosts",
        "allowed_origins",
        mode="before",
    )(_split_csv)

    @field_validator("issuer_url", "resource_server_url")
    @classmethod
    def remove_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @model_validator(mode="after")
    def validate_token_exchange(self) -> "Settings":
        required = {
            "TOKEN_EXCHANGE_URL": self.token_exchange_url,
            "MCP_OAUTH_CLIENT_ID": self.mcp_oauth_client_id,
            "MCP_OAUTH_CLIENT_SECRET": self.mcp_oauth_client_secret,
        }

        missing = [name for name, value in required.items() if not value]

        if missing:
            raise ValueError(
                f"Missing required settings: {', '.join(missing)}"
            )

        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()