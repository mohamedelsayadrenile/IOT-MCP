"""End-to-end guards for the remote server.

These replace the old stdio smoke test. The app is driven in-process over ASGI,
so there is no uvicorn subprocess and no port to race on.

The load-bearing assertions here are the auth ones: a client only ever discovers
the ReNile authorization server through the 401 challenge, and a token is only
useful to its own owner. Both are easy to break silently.
"""

import json
from contextlib import asynccontextmanager

import httpx2
import pytest
from tests.conftest import ISSUER_URL, RESOURCE_SERVER_URL, make_settings, mint_token
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


async def test_healthz_needs_no_auth(build_test_app):
    app = build_test_app()
    async with running(app):
        response = await raw(app).get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# --- protocol ----------------------------------------------------------------


@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_initialize_reports_server_identity(build_test_app, mode):
    app = build_test_app()
    async with running(app), mcp_client(app, mint_token(), mode=mode) as client:
        info = client.server_info
        assert info.name == "renile-iot"
        assert info.version == "0.1.0"


@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_exactly_two_tools_are_registered(build_test_app, mode):
    app = build_test_app()
    async with running(app), mcp_client(app, mint_token(), mode=mode) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
    assert names == {"get_all_devices", "get_latest_readings"}


async def test_tool_schemas_are_model_ready(build_test_app):
    """The injected Context parameter must not leak into the published schema."""
    app = build_test_app()
    async with running(app), mcp_client(app, mint_token()) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}

    assert tools["get_all_devices"].input_schema.get("properties", {}) == {}

    readings = tools["get_latest_readings"].input_schema
    assert set(readings["properties"]) == {"device"}
    assert not readings.get("required")
    assert readings["properties"]["device"]["description"]


# --- the token is the caller's, and only the caller's ------------------------


async def test_token_is_forwarded_verbatim_upstream(build_test_app, upstream):
    """The passthrough design in one assertion."""
    token = mint_token("user-1")
    app = build_test_app()
    async with running(app), mcp_client(app, token) as client:
        await client.call_tool("get_all_devices", {})

    assert upstream.tokens == [f"JWT {token}"]


async def test_upstream_auth_scheme_is_configurable(build_test_app, upstream):
    token = mint_token("user-1")
    app = build_test_app(RENILE_UPSTREAM_AUTH_SCHEME="Bearer")
    async with running(app), mcp_client(app, token) as client:
        await client.call_tool("get_all_devices", {})

    assert upstream.tokens == [f"Bearer {token}"]


async def test_concurrent_callers_do_not_cross_tokens(build_test_app, upstream):
    """The regression a process-wide client would reintroduce."""
    alice, bob = mint_token("alice"), mint_token("bob")
    app = build_test_app()
    async with running(app):
        async with mcp_client(app, alice) as a, mcp_client(app, bob) as b:
            await a.call_tool("get_all_devices", {})
            await b.call_tool("get_all_devices", {})
            await a.call_tool("get_latest_readings", {})

    assert upstream.tokens == [f"JWT {alice}", f"JWT {bob}", f"JWT {alice}"]


async def test_cross_token_session_reuse_is_rejected(build_test_app):
    """One user's session id must not be a working credential for another.

    The SDK binds a session to the principal that created it. That guard is only
    real because the verifier gives every token a distinct `subject`; with a
    constant principal this request would succeed.
    """
    alice, bob = mint_token("alice"), mint_token("bob")
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


async def test_expired_token_never_reaches_the_tool(build_test_app, upstream):
    """Expiry is caught at the door, so the client gets a re-auth challenge
    rather than an error string buried in a tool result."""
    app = build_test_app()
    async with running(app):
        response = await raw(app, mint_token(expires_in=-10)).post(
            "/mcp", json=INITIALIZE, headers=JSON_RPC_HEADERS
        )

    assert response.status_code == 401
    assert "resource_metadata" in response.headers["www-authenticate"]
    assert upstream.requests == []


async def test_token_from_another_issuer_is_refused(build_test_app, upstream):
    app = build_test_app()
    async with running(app):
        response = await raw(app, mint_token(issuer="https://evil.test")).post(
            "/mcp", json=INITIALIZE, headers=JSON_RPC_HEADERS
        )

    assert response.status_code == 401
    assert upstream.requests == []


# --- upstream failures -------------------------------------------------------


async def test_upstream_401_asks_the_user_to_reconnect(build_test_app, upstream):
    upstream.status_code = 401
    app = build_test_app()
    async with running(app), mcp_client(app, mint_token()) as client:
        result = await client.call_tool("get_all_devices", {})

    assert result.is_error
    text = result.content[0].text
    assert "reconnect" in text.lower()
    assert "RENILE_API_TOKEN" not in text


async def test_upstream_403_does_not_ask_the_user_to_reconnect(build_test_app, upstream):
    """A permission failure is not an expiry; telling the user to sign in again
    would send them round a loop that cannot help."""
    upstream.status_code = 403
    app = build_test_app()
    async with running(app), mcp_client(app, mint_token()) as client:
        result = await client.call_tool("get_all_devices", {})

    assert result.is_error
    assert "reconnect" not in result.content[0].text.lower()


# --- lifecycle ---------------------------------------------------------------


async def test_lifespan_closes_the_upstream_client(build_test_app, upstream):
    """The connection pool is owned by the lifespan, not by a module global."""
    app = build_test_app()
    async with running(app):
        async with mcp_client(app, mint_token()) as client:
            await client.call_tool("get_all_devices", {})
        assert not upstream.clients[0]._client.is_closed

    assert upstream.clients[0]._client.is_closed


def test_settings_require_the_oauth_urls():
    """Starting without them would serve a discovery document pointing nowhere."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as excinfo:
        make_settings(RENILE_ISSUER_URL=None)
    assert "issuer" in str(excinfo.value).lower()


def test_reauth_uses_url_elicitation_when_the_client_supports_it():
    """A genuine upstream 401 should push a supporting client at the login page
    rather than dead-ending in an error string."""
    from mcp.shared.exceptions import UrlElicitationRequiredError
    from mcp_types import ClientCapabilities, ElicitationCapability, UrlElicitationCapability

    from src.server import AppState, _reauth
    from src.services.renile_client import RenileAuthExpiredError

    class FakeSession:
        client_capabilities = ClientCapabilities(
            elicitation=ElicitationCapability(url=UrlElicitationCapability())
        )

    class FakeRequestContext:
        lifespan_context = AppState(client=None, settings=make_settings())

    class FakeContext:
        session = FakeSession()
        request_context = FakeRequestContext()

    raised = _reauth(FakeContext(), RenileAuthExpiredError("expired"))
    assert isinstance(raised, UrlElicitationRequiredError)
    assert raised.elicitations[0].url == ISSUER_URL


def test_reauth_falls_back_to_a_tool_error_without_that_capability():
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp_types import ClientCapabilities

    from src.server import AppState, _reauth
    from src.services.renile_client import RenileAuthExpiredError

    class FakeContext:
        class session:
            client_capabilities = ClientCapabilities()

        class request_context:
            lifespan_context = AppState(client=None, settings=make_settings())

    assert isinstance(_reauth(FakeContext(), RenileAuthExpiredError("expired")), ToolError)


def test_json_rpc_payloads_are_plain_json():
    """Cheap guard that the request fixtures above stay serialisable."""
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


async def test_an_unlisted_host_is_rejected(build_test_app):
    app = build_test_app(ALLOWED_HOSTS=["testserver"])
    async with running(app):
        client = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://evil.example"
        )
        response = await client.post(
            "/mcp",
            json=INITIALIZE,
            headers={**JSON_RPC_HEADERS, "Authorization": f"Bearer {mint_token()}"},
        )

    assert response.status_code == 421
