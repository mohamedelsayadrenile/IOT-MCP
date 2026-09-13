import os
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import parse_qs

ISSUER_URL = "https://auth.renile-iot.test"
RESOURCE_SERVER_URL = "http://testserver/mcp"
TOKEN_EXCHANGE_URL = f"{ISSUER_URL}/oauth/token"
MCP_CLIENT_ID = "renile-mcp"
MCP_CLIENT_SECRET = "test-client-secret"

os.environ.setdefault("RENILE_ISSUER_URL", ISSUER_URL)
os.environ.setdefault("RENILE_RESOURCE_SERVER_URL", RESOURCE_SERVER_URL)
os.environ.setdefault("ALLOWED_HOSTS", "testserver")
os.environ.setdefault("TOKEN_EXCHANGE_URL", TOKEN_EXCHANGE_URL)
os.environ.setdefault("MCP_OAUTH_CLIENT_ID", MCP_CLIENT_ID)
os.environ.setdefault("MCP_OAUTH_CLIENT_SECRET", MCP_CLIENT_SECRET)

import httpx  # noqa: E402
import pytest  # noqa: E402

from src.core.config import Settings  # noqa: E402


def make_settings(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "RENILE_ISSUER_URL": ISSUER_URL,
        "RENILE_RESOURCE_SERVER_URL": RESOURCE_SERVER_URL,
        "ALLOWED_HOSTS": ["testserver"],
        "TOKEN_EXCHANGE_URL": TOKEN_EXCHANGE_URL,
        "MCP_OAUTH_CLIENT_ID": MCP_CLIENT_ID,
        "MCP_OAUTH_CLIENT_SECRET": MCP_CLIENT_SECRET,
    }
    return Settings(_env_file=None, **{**defaults, **overrides})


class UpstreamRecorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.exchanges: list[httpx.Request] = []
        self.clients: list[Any] = []
        self.status_code = 200
        self.exchange_status_code: int | None = None
        self.devices: list[dict[str, Any]] = [{"_id": "d1", "name": "Greenhouse"}]
        self.snapshot: dict[str, Any] = {"generated_at": None, "projects": []}
        self._grants: dict[str, dict[str, Any]] = {}

    def grant(self, user: str = "user-1", *, expires_in: int = 900) -> str:
        oauth_token = f"oauth-{user}"
        self._grants[oauth_token] = {
            "access_token": f"renile-jwt-{user}",
            "issued_token_type": "urn:ietf:params:oauth:token-type:jwt",
            "token_type": "N_A",
            "expires_in": expires_in,
            "sub": user,
            "client_id": "https://claude.ai/oauth/mcp-oauth-client-metadata",
        }
        return oauth_token

    @property
    def tokens(self) -> list[str | None]:
        return [r.headers.get("Authorization") for r in self.requests]

    def exchange_form(self, index: int = 0) -> dict[str, str]:
        body = parse_qs(self.exchanges[index].content.decode())
        return {key: values[0] for key, values in body.items()}

    def handler(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_EXCHANGE_URL:
            return self._exchange(request)
        self.requests.append(request)
        if self.status_code != 200:
            return httpx.Response(self.status_code, text="Unauthorized")
        body = self.devices if "devices" in request.url.path else self.snapshot
        return httpx.Response(200, json=body)

    def _exchange(self, request: httpx.Request) -> httpx.Response:
        self.exchanges.append(request)
        if self.exchange_status_code is not None:
            return httpx.Response(self.exchange_status_code, json={"error": "server_error"})
        grant = self._grants.get(parse_qs(request.content.decode())["subject_token"][0])
        if grant is None:
            return httpx.Response(400, json={"error": "invalid_grant"})
        return httpx.Response(200, json=grant)


@pytest.fixture
def upstream() -> UpstreamRecorder:
    return UpstreamRecorder()


@pytest.fixture
def patched_upstream(
    monkeypatch: pytest.MonkeyPatch, upstream: UpstreamRecorder
) -> Iterator[UpstreamRecorder]:
    import src.server as server_module
    from src.services.renile_client import ReNileClient

    real_build = server_module.build_client

    def fake_build(settings: Settings) -> ReNileClient:
        client = real_build(settings)
        client._client = httpx.AsyncClient(
            base_url=settings.renile_api_base_url,
            headers={"Accept": "application/json"},
            transport=httpx.MockTransport(upstream.handler),
        )
        upstream.clients.append(client)
        return client

    monkeypatch.setattr(server_module, "build_client", fake_build)
    yield upstream


@pytest.fixture
def build_test_app(patched_upstream: UpstreamRecorder) -> Callable[..., Any]:
    from src.app import build_app

    def _build(**overrides: Any):
        return build_app(make_settings(**overrides))

    return _build
