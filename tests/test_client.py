import httpx
import pytest

from src.services.renile_client import (
    ReNileClient,
    RenileAPIError,
    RenileAuthExpiredError,
    RenilePermissionError,
    build_client,
)
from tests.conftest import make_settings

BASE_URL = "https://renile-iot.test"
TOKEN = "caller-token"


def make_client(handler, **overrides) -> ReNileClient:
    settings = make_settings(**overrides)
    return ReNileClient(
        httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"Accept": "application/json"},
            transport=httpx.MockTransport(handler),
        ),
        settings,
    )


async def test_get_devices_returns_roster():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == make_settings().renile_devices_path
        return httpx.Response(200, json=[{"_id": "abc", "name": "Greenhouse"}])

    devices = await make_client(handler).get_devices(TOKEN)
    assert devices == [{"_id": "abc", "name": "Greenhouse"}]


async def test_get_snapshot_returns_payload():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == make_settings().renile_snapshot_path
        return httpx.Response(200, json={"device_count": 7, "projects": []})

    snapshot = await make_client(handler).get_snapshot(TOKEN)
    assert snapshot["device_count"] == 7


async def test_401_plain_text_body_raises_auth_error_not_json_error():
    """The platform returns the bare string 'Unauthorized' on 401.

    Parsing before checking status would surface a JSONDecodeError instead of the
    real cause, so this is the regression that matters most.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="Unauthorized")

    with pytest.raises(RenileAuthExpiredError) as excinfo:
        await make_client(handler).get_snapshot(TOKEN)

    message = str(excinfo.value)
    assert "JSON" not in message
    # The old message named a server-side env var. There is no such thing now,
    # and an end user cannot act on it.
    assert "RENILE_API_TOKEN" not in message


async def test_403_is_a_permission_error_not_an_expiry():
    """401 and 403 lead to different advice and must not share a branch."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Forbidden")

    with pytest.raises(RenilePermissionError):
        await make_client(handler).get_devices(TOKEN)


async def test_server_error_includes_status_and_truncated_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="x" * 500)

    with pytest.raises(RenileAPIError) as excinfo:
        await make_client(handler).get_devices(TOKEN)

    assert "500" in str(excinfo.value)
    assert len(str(excinfo.value)) < 400


async def test_timeout_raises_friendly_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    with pytest.raises(RenileAPIError, match="timed out"):
        await make_client(handler).get_snapshot(TOKEN)


async def test_transport_error_is_retried_once_then_succeeds():
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        if len(attempts) == 1:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json=[])

    devices = await make_client(handler).get_devices(TOKEN)
    assert devices == []
    assert len(attempts) == 2


async def test_non_json_200_body_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>nope</html>")

    with pytest.raises(RenileAPIError, match="non-JSON"):
        await make_client(handler).get_snapshot(TOKEN)


async def test_timeout_is_not_retried():
    """Retrying a timeout would only double the wait before failing."""
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        raise httpx.ReadTimeout("too slow", request=request)

    with pytest.raises(RenileAPIError, match="timed out"):
        await make_client(handler).get_snapshot(TOKEN)

    assert len(attempts) == 1


# --- configuration is honoured ----------------------------------------------

async def test_endpoint_paths_come_from_settings():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json=[])

    client = make_client(handler, RENILE_DEVICES_PATH="/custom/devices/")
    await client.get_devices(TOKEN)
    assert seen == ["/custom/devices/"]


async def test_max_attempts_of_one_disables_retrying():
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(RenileAPIError):
        await make_client(handler, HTTP_MAX_ATTEMPTS=1).get_devices(TOKEN)

    assert len(attempts) == 1


async def test_max_attempts_of_three_retries_twice():
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        if len(attempts) < 3:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json=[])

    await make_client(handler, HTTP_MAX_ATTEMPTS=3).get_devices(TOKEN)
    assert len(attempts) == 3


async def test_error_body_preview_length_is_configurable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="y" * 500)

    with pytest.raises(RenileAPIError) as excinfo:
        await make_client(handler, ERROR_BODY_PREVIEW_CHARS=10).get_devices(TOKEN)

    assert "y" * 10 in str(excinfo.value)
    assert "y" * 11 not in str(excinfo.value)


# --- the caller's token ------------------------------------------------------

async def test_token_is_sent_as_a_per_request_authorization_header():
    """Previously untested: the header lived in the client's defaults."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.headers))
        return httpx.Response(200, json=[])

    await make_client(handler).get_devices("abc123")
    assert seen[0]["authorization"] == "JWT abc123"
    # The default header the session does carry must survive the merge.
    assert seen[0]["accept"] == "application/json"


async def test_auth_scheme_is_configurable():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json=[])

    client = make_client(handler, RENILE_UPSTREAM_AUTH_SCHEME="Bearer")
    await client.get_devices("abc123")
    assert seen == ["Bearer abc123"]


async def test_different_callers_do_not_share_a_header():
    """The pool is shared; the credential is not."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json=[])

    client = make_client(handler)
    await client.get_devices("alice-token")
    await client.get_devices("bob-token")
    assert seen == ["JWT alice-token", "JWT bob-token"]


def test_build_client_carries_no_credentials():
    """A default Authorization header here would leak one user's token to the
    next request that borrows the connection."""
    client = build_client(make_settings())
    assert "authorization" not in {k.lower() for k in client._client.headers}
