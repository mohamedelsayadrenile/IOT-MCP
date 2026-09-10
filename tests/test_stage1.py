"""Stage 1: the endpoint is reachable and refuses everyone, silently.

The point of this stage is to verify plain connectivity to the MCP endpoint
before any auth work exists. So these tests assert the ABSENCE of the OAuth
challenge as much as the presence of the 401: a WWW-Authenticate header or a
live .well-known route would send a client on into authorization-server
discovery, which is stage 2's problem.

The rest of the suite runs with OAUTH_CHALLENGE_ENABLED=True (see conftest).
"""

import pytest

from tests.test_http import INITIALIZE, MCP_URL, raw, running


@pytest.fixture
def stage1_app(build_test_app):
    return build_test_app(OAUTH_CHALLENGE_ENABLED=False)


@pytest.mark.anyio
async def test_unauthenticated_request_gets_a_bare_401(stage1_app):
    async with running(stage1_app) as app, raw(app) as client:
        response = await client.post(MCP_URL, json=INITIALIZE)

    assert response.status_code == 401
    assert "www-authenticate" not in response.headers


@pytest.mark.anyio
async def test_401_carries_no_resource_metadata_pointer(stage1_app):
    """Nothing in the response may point a client at a discovery document."""
    async with running(stage1_app) as app, raw(app) as client:
        response = await client.post(MCP_URL, json=INITIALIZE)

    assert "resource_metadata" not in response.text
    assert "resource_metadata" not in str(response.headers).lower()
    assert ".well-known" not in response.text


@pytest.mark.anyio
@pytest.mark.parametrize(
    "path",
    [
        "/.well-known/oauth-protected-resource/mcp",
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-authorization-server",
    ],
)
async def test_discovery_endpoints_are_not_mounted(stage1_app, path):
    async with running(stage1_app) as app, raw(app) as client:
        response = await client.get(path)

    assert response.status_code == 404


@pytest.mark.anyio
async def test_a_valid_looking_token_is_still_refused(stage1_app):
    """No request reaches the transport, token or not -- this stage authenticates
    nobody, so a well-formed token must not become a working session."""
    async with running(stage1_app) as app, raw(app, "any-well-formed-token") as client:
        response = await client.post(MCP_URL, json=INITIALIZE)

    assert response.status_code == 401
    assert "mcp-session-id" not in response.headers


@pytest.mark.anyio
@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
async def test_every_method_on_mcp_is_refused(stage1_app, method):
    async with running(stage1_app) as app, raw(app) as client:
        response = await client.request(method, MCP_URL)

    assert response.status_code == 401


@pytest.mark.anyio
async def test_healthz_still_answers(stage1_app):
    """The gate is scoped to /mcp; a load balancer probe must not see a 401."""
    async with running(stage1_app) as app, raw(app) as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
