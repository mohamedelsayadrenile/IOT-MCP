import os
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import parse_qs

ISSUER_URL = "https://auth.nojo.test"
RESOURCE_SERVER_URL = "http://testserver/mcp"
TOKEN_EXCHANGE_URL = f"{ISSUER_URL}/oauth/token"
MCP_CLIENT_ID = "nojo-mcp"
MCP_CLIENT_SECRET = "test-client-secret"

os.environ.setdefault("NOJO_ISSUER_URL", ISSUER_URL)
os.environ.setdefault("NOJO_RESOURCE_SERVER_URL", RESOURCE_SERVER_URL)
os.environ.setdefault("ALLOWED_HOSTS", "testserver")
os.environ.setdefault("TOKEN_EXCHANGE_URL", TOKEN_EXCHANGE_URL)
os.environ.setdefault("MCP_OAUTH_CLIENT_ID", MCP_CLIENT_ID)
os.environ.setdefault("MCP_OAUTH_CLIENT_SECRET", MCP_CLIENT_SECRET)

import httpx  # noqa: E402
import pytest  # noqa: E402

from src.core.config import Settings  # noqa: E402


def make_settings(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "NOJO_ISSUER_URL": ISSUER_URL,
        "NOJO_RESOURCE_SERVER_URL": RESOURCE_SERVER_URL,
        "ALLOWED_HOSTS": ["testserver"],
        "TOKEN_EXCHANGE_URL": TOKEN_EXCHANGE_URL,
        "MCP_OAUTH_CLIENT_ID": MCP_CLIENT_ID,
        "MCP_OAUTH_CLIENT_SECRET": MCP_CLIENT_SECRET,
    }
    return Settings(_env_file=None, **{**defaults, **overrides})


class BackendRecorder:
    """Stands in for the Nojo backend's token-exchange endpoint."""

    def __init__(self) -> None:
        self.exchanges: list[httpx.Request] = []
        self.clients: list[httpx.AsyncClient] = []
        self.exchange_status_code: int | None = None
        self.malformed = False
        self._grants: dict[str, dict[str, Any]] = {}

    def grant(self, user: str = "user-1", *, expires_in: int = 900) -> str:
        oauth_token = f"oauth-{user}"
        self._grants[oauth_token] = {
            "access_token": f"nojo-jwt-{user}",
            "expires_in": expires_in,
            "sub": user,
            "client_id": "https://claude.ai/oauth/mcp-oauth-client-metadata",
        }
        return oauth_token

    def exchange_form(self, index: int = 0) -> dict[str, str]:
        body = parse_qs(self.exchanges[index].content.decode())
        return {key: values[0] for key, values in body.items()}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.exchanges.append(request)
        if self.exchange_status_code is not None:
            return httpx.Response(
                self.exchange_status_code, json={"error": "server_error"}
            )
        if self.malformed:
            return httpx.Response(200, json={"not": "a token"})
        grant = self._grants.get(parse_qs(request.content.decode())["subject_token"][0])
        if grant is None:
            return httpx.Response(400, json={"error": "invalid_grant"})
        return httpx.Response(200, json=grant)


@pytest.fixture
def backend() -> BackendRecorder:
    return BackendRecorder()


@pytest.fixture
def patched_backend(
    monkeypatch: pytest.MonkeyPatch, backend: BackendRecorder
) -> Iterator[BackendRecorder]:
    import src.server as server_module

    def fake_build(settings: Settings) -> httpx.AsyncClient:
        client = httpx.AsyncClient(
            headers={"Accept": "application/json"},
            transport=httpx.MockTransport(backend.handler),
        )
        backend.clients.append(client)
        return client

    monkeypatch.setattr(server_module, "build_http_client", fake_build)
    yield backend


@pytest.fixture
def build_test_app(patched_backend: BackendRecorder) -> Callable[..., Any]:
    from src.app import build_app

    def _build(**overrides: Any):
        return build_app(make_settings(**overrides))

    return _build
