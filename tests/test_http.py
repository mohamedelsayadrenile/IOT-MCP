"""End-to-end guards for the remote server.

These replace the old stdio smoke test. The app is driven in-process over ASGI,
so there is no uvicorn subprocess and no port to race on.

The load-bearing assertions here are the auth ones: a client only ever discovers
the ReNile authorization server through the 401 challenge, the OAuth token is
exchanged rather than forwarded, and a token is only useful to its own owner.
All are easy to break silently.
"""

import base64
from contextlib import asynccontextmanager

import httpx2
import pytest
from tests.conftest import (
    ISSUER_URL,
    MCP_CLIENT_ID,
    MCP_CLIENT_SECRET,
    RESOURCE_SERVER_URL,
    make_settings,
)
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client

MCP_URL = "http://testserver/mcp"
JSON_RPC_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "http-test", "version": "1"},
    },
}


@asynccontextmanager
async def running(app):
    """Run the app's lifespan, which is what builds and closes the upstream client."""
    async with app.router.lifespan_context(app):
        yield app


def raw(app, token: str | None = None) -> httpx2.AsyncClient:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url="http://testserver",
        headers=headers,
    )


@asynccontextmanager
async def mcp_client(app, token: str, **kwargs):
    async with Client(
        streamable_http_client(MCP_URL, http_client=raw(app, token)), **kwargs
    ) as client:
        yield client


# --- discovery ---------------------------------------------------------------


async def test_unauthenticated_request_challenges_with_resource_metadata(build_test_app):
    """The whole OAuth chain hangs off this header.

    Without `resource_metadata` in the challenge a client has no way to find the
    authorization server, and the connector silently never logs in.
    """
    app = build_test_app()
    async with running(app):
        response = await raw(app).post("/mcp", json=INITIALIZE, headers=JSON_RPC_HEADERS)

    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    assert challenge.startswith("Bearer ")
    assert (
        'resource_metadata="http://testserver/.well-known/oauth-protected-resource/mcp"'
        in challenge
    )


async def test_protected_resource_metadata_is_served(build_test_app):
    app = build_test_app()
    async with running(app):
        response = await raw(app).get("/.well-known/oauth-protected-resource/mcp")

    assert response.status_code == 200
    body = response.json()
    assert body["resource"] == RESOURCE_SERVER_URL
    assert body["authorization_servers"] == [ISSUER_URL]
    assert body["scopes_supported"] == ["devices:read", "readings:read"]


@pytest.mark.parametrize(
    "path", ["/.well-known/oauth-authorization-server", "/oauth/authorize", "/oauth/token"]
)
async def test_this_server_is_not_an_authorization_server(build_test_app, path):
    """The ReNile backend owns the whole OAuth flow; nothing here may shadow it."""
    app = build_test_app()
    async with running(app):
        response = await raw(app).get(path)

    assert response.status_code == 404


async def test_healthz_needs_no_auth(build_test_app):
    app = build_test_app()
    async with running(app):
        response = await raw(app).get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# --- protocol ----------------------------------------------------------------


@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_initialize_reports_server_identity(build_test_app, upstream, mode):
    app = build_test_app()
    async with running(app), mcp_client(app, upstream.grant(), mode=mode) as client:
        info = client.server_info
        assert info.name == "renile-iot"
        assert info.version == "0.1.0"


@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_exactly_two_tools_are_registered(build_test_app, upstream, mode):
    app = build_test_app()
    async with running(app), mcp_client(app, upstream.grant(), mode=mode) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
    assert names == {"get_all_devices", "get_latest_readings"}


async def test_tool_schemas_are_model_ready(build_test_app, upstream):
    """The injected Context parameter must not leak into the published schema."""
    app = build_test_app()
    async with running(app), mcp_client(app, upstream.grant()) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}

    assert tools["get_all_devices"].input_schema.get("properties", {}) == {}

    readings = tools["get_latest_readings"].input_schema
    assert set(readings["properties"]) == {"device"}
    assert not readings.get("required")
    assert readings["properties"]["device"]["description"]


# --- the OAuth token is exchanged, never forwarded ----------------------------


async def test_upstream_receives_the_exchanged_jwt_never_the_oauth_token(
    build_test_app, upstream
):
    """The whole design in one assertion: the ReNile API only ever sees the
    ReNile JWT the backend minted, and the OAuth token goes only to the exchange."""
    token = upstream.grant("user-1")
    app = build_test_app()
    async with running(app), mcp_client(app, token) as client:
        result = await client.call_tool("get_all_devices", {})

    assert upstream.tokens == ["JWT renile-jwt-user-1"]
    assert all(token not in str(r.headers) for r in upstream.requests)
    assert "renile-jwt" not in str(result.model_dump())


async def test_exchange_is_authenticated_as_this_server(build_test_app, upstream):
    """Without its own client credentials, anyone holding an OAuth token --
    Claude included -- could swap it for a ReNile JWT."""
    token = upstream.grant("user-1")
    app = build_test_app()
    async with running(app), mcp_client(app, token) as client:
        await client.call_tool("get_all_devices", {})

    expected = base64.b64encode(f"{MCP_CLIENT_ID}:{MCP_CLIENT_SECRET}".encode()).decode()
    assert upstream.exchanges[0].headers["authorization"] == f"Basic {expected}"
    assert upstream.exchange_form() == {
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "subject_token": token,
        "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "audience": "renile-api",
    }


async def test_exchange_is_cached_across_requests(build_test_app, upstream):
    app = build_test_app()
    async with running(app), mcp_client(app, upstream.grant()) as client:
        await client.call_tool("get_all_devices", {})
        await client.call_tool("get_latest_readings", {})

    assert len(upstream.exchanges) == 1
    assert len(upstream.requests) == 2


async def test_upstream_auth_scheme_is_configurable(build_test_app, upstream):
    token = upstream.grant("user-1")
    app = build_test_app(RENILE_UPSTREAM_AUTH_SCHEME="Bearer")
    async with running(app), mcp_client(app, token) as client:
        await client.call_tool("get_all_devices", {})

    assert upstream.tokens == ["Bearer renile-jwt-user-1"]


async def test_concurrent_callers_do_not_cross_tokens(build_test_app, upstream):
    """The regression a process-wide client or a shared cache entry would cause."""
    alice, bob = upstream.grant("alice"), upstream.grant("bob")
    app = build_test_app()
    async with running(app):
        async with mcp_client(app, alice) as a, mcp_client(app, bob) as b:
            await a.call_tool("get_all_devices", {})
            await b.call_tool("get_all_devices", {})
            await a.call_tool("get_latest_readings", {})

    assert upstream.tokens == [
        "JWT renile-jwt-alice",
        "JWT renile-jwt-bob",
        "JWT renile-jwt-alice",
    ]


async def test_cross_token_session_reuse_is_rejected(build_test_app, upstream):
    """One user's session id must not be a working credential for another.

    The SDK binds a session to the principal that created it. That guard is only
    real because the verifier gives each user a distinct `subject` -- the `sub`
    the backend returned; with a constant principal this request would succeed.
    """
    alice, bob = upstream.grant("alice"), upstream.grant("bob")
    app = build_test_app()
    async with running(app):
        opened = await raw(app, alice).post(
            "/mcp", json=INITIALIZE, headers=JSON_RPC_HEADERS
        )
        session_id = opened.headers["mcp-session-id"]

        hijacked = await raw(app, bob).post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            headers={**JSON_RPC_HEADERS, "mcp-session-id": session_id},
        )

    assert opened.status_code == 200
    assert hijacked.status_code == 404


async def test_token_the_backend_rejects_gets_the_challenge(build_test_app, upstream):
    """Invalid, expired or revoked is the backend's call. Its refusal must come
    back as the 401 challenge, so the client refreshes or re-runs OAuth rather
    than seeing an error string buried in a tool result."""
    app = build_test_app()
    async with running(app):
        response = await raw(app, "not-a-granted-token").post(
            "/mcp", json=INITIALIZE, headers=JSON_RPC_HEADERS
        )

    assert response.status_code == 401
    assert "resource_metadata" in response.headers["www-authenticate"]
    assert len(upstream.exchanges) == 1
    assert upstream.requests == []


async def test_rejected_exchange_client_gets_the_challenge(build_test_app, upstream):
    upstream.exchange_status_code = 401
    app = build_test_app()
    async with running(app):
        response = await raw(app, upstream.grant()).post(
            "/mcp", json=INITIALIZE, headers=JSON_RPC_HEADERS
        )

    assert response.status_code == 401


async def test_missing_scope_is_forbidden(build_test_app, upstream):
    token = upstream.grant("user-1", scope="devices:read")
    app = build_test_app()
    async with running(app):
        response = await raw(app, token).post(
            "/mcp", json=INITIALIZE, headers=JSON_RPC_HEADERS
        )

    assert response.status_code == 403
    assert 'error="insufficient_scope"' in response.headers["www-authenticate"]
    assert upstream.requests == []


async def test_backend_outage_is_not_a_401(build_test_app, upstream):
    """A 401 would make clients discard a perfectly good token over a blip."""
    upstream.exchange_status_code = 503
    app = build_test_app()
    async with running(app):
        client = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://testserver",
            headers={"Authorization": f"Bearer {upstream.grant()}"},
        )
        response = await client.post("/mcp", json=INITIALIZE, headers=JSON_RPC_HEADERS)

    assert response.status_code == 500
    assert "renile-jwt" not in response.text


# --- upstream failures -------------------------------------------------------


async def test_upstream_401_asks_the_user_to_reconnect(build_test_app, upstream):
    """The API refused a JWT that still looked live: the cached exchange is
    dropped, so the next call asks the backend again instead of reusing it."""
    upstream.status_code = 401
    app = build_test_app()
    async with running(app), mcp_client(app, upstream.grant()) as client:
        result = await client.call_tool("get_all_devices", {})
        await client.call_tool("get_all_devices", {})

    assert result.is_error
    text = result.content[0].text
    assert "reconnect" in text.lower()
    assert "RENILE_API_TOKEN" not in text
    assert len(upstream.exchanges) == 2


async def test_upstream_403_does_not_ask_the_user_to_reconnect(build_test_app, upstream):
    """A permission failure is not an expiry; telling the user to sign in again
    would send them round a loop that cannot help."""
    upstream.status_code = 403
    app = build_test_app()
    async with running(app), mcp_client(app, upstream.grant()) as client:
        result = await client.call_tool("get_all_devices", {})

    assert result.is_error
    assert "reconnect" not in result.content[0].text.lower()


# --- lifecycle ---------------------------------------------------------------


async def test_lifespan_closes_the_upstream_client(build_test_app, upstream):
    """The connection pool is owned by the lifespan, not by a module global."""
    app = build_test_app()
    async with running(app):
        async with mcp_client(app, upstream.grant()) as client:
            await client.call_tool("get_all_devices", {})
        assert not upstream.clients[0]._client.is_closed

    assert upstream.clients[0]._client.is_closed


def test_settings_require_the_oauth_urls():
    """Starting without them would serve a discovery document pointing nowhere."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as excinfo:
        make_settings(RENILE_ISSUER_URL=None)
    assert "issuer" in str(excinfo.value).lower()


def test_oauth_mode_requires_the_exchange_settings():
    """Every request depends on the exchange; a missing secret must stop startup."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as excinfo:
        make_settings(MCP_OAUTH_CLIENT_SECRET=None)
    assert "MCP_OAUTH_CLIENT_SECRET" in str(excinfo.value)


def test_stage_one_needs_no_exchange_settings():
    settings = make_settings(
        OAUTH_CHALLENGE_ENABLED=False,
        TOKEN_EXCHANGE_URL=None,
        MCP_OAUTH_CLIENT_ID=None,
        MCP_OAUTH_CLIENT_SECRET=None,
    )
    assert not settings.oauth_challenge_enabled


def test_required_scopes_accept_a_space_separated_string():
    settings = make_settings(OAUTH_REQUIRED_SCOPES="devices:read,readings:read x")
    assert settings.required_scopes == ["devices:read", "readings:read", "x"]


def test_client_secret_is_not_in_the_settings_repr():
    assert MCP_CLIENT_SECRET not in repr(make_settings())


def test_json_rpc_payloads_are_plain_json():
    """Cheap guard that the request fixtures above stay serialisable."""
    import json

    assert json.loads(json.dumps(INITIALIZE))["method"] == "initialize"


async def test_a_bare_allowed_host_also_matches_a_port(build_test_app):
    """Host headers carry a port off 80/443. A literal match on the bare name
    would 421 every local and container-mapped request."""
    app = build_test_app(ALLOWED_HOSTS=["testserver"])
    async with running(app):
        client = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://testserver:8931"
        )
        response = await client.post("/mcp", json=INITIALIZE, headers=JSON_RPC_HEADERS)

    # 401 (no token), not 421 (rejected host): the host check passed.
    assert response.status_code == 401


async def test_an_unlisted_host_is_rejected(build_test_app, upstream):
    app = build_test_app(ALLOWED_HOSTS=["testserver"])
    async with running(app):
        client = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://evil.example"
        )
        response = await client.post(
            "/mcp",
            json=INITIALIZE,
            headers={**JSON_RPC_HEADERS, "Authorization": f"Bearer {upstream.grant()}"},
        )

    assert response.status_code == 421
