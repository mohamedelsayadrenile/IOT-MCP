import httpx
import pytest

from src.services.renile_client import (
    ReNileClient,
    RenileAPIError,
    RenileAuthExpiredError,
    RenilePermissionError,
    TokenExchangeRejectedError,
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


# --- token exchange ----------------------------------------------------------

EXCHANGE_OK = {
    "access_token": "renile-jwt",
    "issued_token_type": "urn:ietf:params:oauth:token-type:jwt",
    "token_type": "N_A",
    "expires_in": 900,
    "sub": "user-1",
    "scope": "devices:read readings:read",
}


async def test_exchange_returns_the_backend_verdict():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == make_settings().token_exchange_url
        return httpx.Response(200, json=EXCHANGE_OK)

    result = await make_client(handler).exchange_token("oauth-token")
    assert result.renile_jwt == "renile-jwt"
    assert result.subject == "user-1"
    assert result.scopes == ["devices:read", "readings:read"]
    assert result.expires_in == 900
    # A credential: kept out of reprs, and so out of logs and tracebacks.
    assert "renile-jwt" not in repr(result)


@pytest.mark.parametrize("status", [400, 401])
async def test_exchange_refusal_is_a_rejection(status):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "invalid_grant"})

    with pytest.raises(TokenExchangeRejectedError):
        await make_client(handler).exchange_token("oauth-token")


async def test_exchange_outage_is_not_a_rejection():
    """A rejection sends the client to re-authenticate; an outage must not."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    with pytest.raises(RenileAPIError) as excinfo:
        await make_client(handler).exchange_token("oauth-token")
    assert not isinstance(excinfo.value, TokenExchangeRejectedError)


async def test_malformed_exchange_response_is_never_quoted():
    """A 200 body carries a credential, so even a broken one is not echoed."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "renile-jwt", "sub": ""})

    with pytest.raises(RenileAPIError) as excinfo:
        await make_client(handler).exchange_token("oauth-token")
    assert "renile-jwt" not in str(excinfo.value)


async def test_exchange_transport_error_is_friendly():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(RenileAPIError, match="unreachable"):
        await make_client(handler).exchange_token("oauth-token")


async def test_exchange_without_audience_omits_it():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.content.decode())
        return httpx.Response(200, json=EXCHANGE_OK)

    await make_client(handler, TOKEN_EXCHANGE_AUDIENCE=None).exchange_token("t")
    assert "audience" not in seen[0]


# --- the verifier ------------------------------------------------------------


async def test_short_lived_exchange_is_not_cached():
    """A JWT inside the expiry margin must be re-exchanged, not reused dead."""
    from src.core.auth import ExchangeTokenVerifier

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={**EXCHANGE_OK, "expires_in": 10})

    verifier = ExchangeTokenVerifier(make_settings().issuer_url)
    verifier.client = make_client(handler)
    first = await verifier.verify_token("oauth-token")
    await verifier.verify_token("oauth-token")

    assert first.subject == "user-1"
    assert first.claims == {"iss": make_settings().issuer_url}
    assert "renile-jwt" not in first.model_dump_json()
    assert len(calls) == 2


async def test_forget_drops_the_cached_exchange():
    from src.core.auth import ExchangeTokenVerifier

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=EXCHANGE_OK)

    verifier = ExchangeTokenVerifier(make_settings().issuer_url)
    verifier.client = make_client(handler)
    await verifier.verify_token("oauth-token")
    await verifier.verify_token("oauth-token")
    verifier.forget("oauth-token")
    await verifier.verify_token("oauth-token")

    assert len(calls) == 2
