"""Centralised settings.

No module outside this one reads environment variables directly.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolved from __file__ rather than the cwd: Claude Desktop launches MCP servers
# with a minimal environment, so the working directory cannot be relied upon.
ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # SecretStr so an accidental log of the settings object cannot leak the token.
    # Call .get_secret_value() only where the auth header is built.
    renile_api_token: SecretStr = Field(alias="RENILE_API_TOKEN")

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
