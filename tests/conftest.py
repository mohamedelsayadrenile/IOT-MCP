"""Shared fixtures.

The environment is set before any `src` import so Settings never depends on the
developer's real src/.env -- and so `src.app`, which builds an app at import
time for uvicorn, can be imported at all.
"""

import os
import time
from collections.abc import Callable, Iterator
from typing import Any

ISSUER_URL = "https://auth.renile-iot.test"
RESOURCE_SERVER_URL = "http://testserver/mcp"

os.environ.setdefault("RENILE_ISSUER_URL", ISSUER_URL)
os.environ.setdefault("RENILE_RESOURCE_SERVER_URL", RESOURCE_SERVER_URL)
os.environ.setdefault("ALLOWED_HOSTS", "testserver")

import httpx  # noqa: E402
import jwt  # noqa: E402
import pytest  # noqa: E402

from src.core.config import Settings  # noqa: E402


def make_settings(**overrides: Any) -> Settings:
    # _env_file=None so the developer's real src/.env cannot alter a test.
    defaults: dict[str, Any] = {
        "RENILE_ISSUER_URL": ISSUER_URL,
        "RENILE_RESOURCE_SERVER_URL": RESOURCE_SERVER_URL,
        "ALLOWED_HOSTS": ["testserver"],
        # The suite exercises the real resource server. Stage-1 mode is the
        # deployed default right now, so tests that want it opt in explicitly.
        "OAUTH_CHALLENGE_ENABLED": True,
    }
    return Settings(_env_file=None, **{**defaults, **overrides})


def mint_token(
    subject: str = "user-1",
    *,
    issuer: str = ISSUER_URL,
    expires_in: int | None = 600,
    **claims: Any,
) -> str:
    """A JWT for the tests. Never signed with anything meaningful -- the server
    does not check signatures, which is the whole point of the design."""
    payload: dict[str, Any] = {"iss": issuer, "sub": subject, **claims}
    if expires_in is not None:
        payload["exp"] = int(time.time()) + expires_in
    return jwt.encode(payload, "test-signing-key-not-verified-anywhere", algorithm="HS256")


class UpstreamRecorder:
    """A stand-in ReNile API that records what it was asked, and by whom."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        # Every ReNileClient the server's lifespan built, so a test can check
        # they are closed again on shutdown.
        self.clients: list[Any] = []
        self.status_code = 200
        self.devices: list[dict[str, Any]] = [{"_id": "d1", "name": "Greenhouse"}]
        self.snapshot: dict[str, Any] = {"generated_at": None, "projects": []}

    @property
    def tokens(self) -> list[str | None]:
        return [r.headers.get("Authorization") for r in self.requests]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status_code != 200:
            return httpx.Response(self.status_code, text="Unauthorized")
        body = self.devices if "devices" in request.url.path else self.snapshot
        return httpx.Response(200, json=body)


@pytest.fixture
def upstream() -> UpstreamRecorder:
    return UpstreamRecorder()


@pytest.fixture
def patched_upstream(
    monkeypatch: pytest.MonkeyPatch, upstream: UpstreamRecorder
) -> Iterator[UpstreamRecorder]:
    """Point the server's upstream client at `upstream` instead of the network.

    Patching build_client is the seam: it keeps the per-request-token plumbing in
    ReNileClient under test rather than stubbing it out.
    """
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
