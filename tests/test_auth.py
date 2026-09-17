import time

import httpx
import pytest
from tests.conftest import TOKEN_EXCHANGE_URL, make_settings

from src.services.auth import ExchangeTokenVerifier, exchange_token
from src.services.errors import NojoAPIError, TokenExchangeRejectedError

GOOD_BODY = {
    "access_token": "nojo-jwt-user-1",
    "expires_in": 900,
    "sub": "user-1",
    "client_id": "https://claude.ai/oauth/mcp-oauth-client-metadata",
}


def client_returning(response: httpx.Response | Exception) -> httpx.AsyncClient:
    def handler(_: httpx.Request) -> httpx.Response:
        if isinstance(response, Exception):
            raise response
        return response

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_successful_exchange_is_parsed():
    settings = make_settings()
    async with client_returning(httpx.Response(200, json=GOOD_BODY)) as http:
        result = await exchange_token(http, settings, "oauth-token")

    assert result.nojo_jwt == "nojo-jwt-user-1"
    assert result.subject == "user-1"
    assert result.expires_in == 900


async def test_client_id_falls_back_when_the_backend_omits_it():
    settings = make_settings()
    body = {k: v for k, v in GOOD_BODY.items() if k != "client_id"}
    async with client_returning(httpx.Response(200, json=body)) as http:
        result = await exchange_token(http, settings, "oauth-token")

    assert result.client_id is None


@pytest.mark.parametrize("status_code", [400, 401])
async def test_rejections_raise_token_exchange_rejected(status_code):
    settings = make_settings()
    async with client_returning(
        httpx.Response(status_code, json={"error": "invalid_grant"})
    ) as http:
        with pytest.raises(TokenExchangeRejectedError):
            await exchange_token(http, settings, "oauth-token")


@pytest.mark.parametrize("status_code", [403, 500, 503])
async def test_other_statuses_raise_a_backend_error(status_code):
    settings = make_settings()
    async with client_returning(httpx.Response(status_code, text="nope")) as http:
        with pytest.raises(NojoAPIError) as excinfo:
            await exchange_token(http, settings, "oauth-token")

    assert not isinstance(excinfo.value, TokenExchangeRejectedError)
    assert str(status_code) in str(excinfo.value)


@pytest.mark.parametrize(
    "body",
    [
        {"expires_in": 900, "sub": "user-1"},
        {"access_token": "", "expires_in": 900, "sub": "user-1"},
        {"access_token": "jwt", "expires_in": 900},
        {"access_token": "jwt", "expires_in": "soon", "sub": "user-1"},
    ],
)
async def test_a_malformed_body_is_a_backend_error(body):
    settings = make_settings()
    async with client_returning(httpx.Response(200, json=body)) as http:
        with pytest.raises(NojoAPIError):
            await exchange_token(http, settings, "oauth-token")


async def test_a_transport_failure_is_a_backend_error():
    settings = make_settings()
    async with client_returning(httpx.ConnectError("down")) as http:
        with pytest.raises(NojoAPIError):
            await exchange_token(http, settings, "oauth-token")


async def test_the_error_message_never_quotes_the_backend_body():
    settings = make_settings()
    secret_body = "eyJhbGciOi.super-secret-jwt.signature"
    async with client_returning(httpx.Response(502, text=secret_body)) as http:
        with pytest.raises(NojoAPIError) as excinfo:
            await exchange_token(http, settings, "oauth-token")

    assert "super-secret-jwt" not in str(excinfo.value)


async def test_the_verifier_caches_a_verified_token():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=GOOD_BODY)

    verifier = ExchangeTokenVerifier(make_settings())
    verifier.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    first = await verifier.verify_token("oauth-token")
    second = await verifier.verify_token("  oauth-token  ")

    assert first is second
    assert len(calls) == 1
    assert first.nojo_jwt == "nojo-jwt-user-1"


async def test_forget_evicts_the_cache_entry():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=GOOD_BODY)

    verifier = ExchangeTokenVerifier(make_settings())
    verifier.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    await verifier.verify_token("oauth-token")
    verifier.forget("oauth-token")
    await verifier.verify_token("oauth-token")

    assert len(calls) == 2


async def test_an_expired_entry_is_exchanged_again():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={**GOOD_BODY, "expires_in": 1})

    verifier = ExchangeTokenVerifier(make_settings())
    verifier.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    await verifier.verify_token("oauth-token")
    # expires_in is below the 30s safety margin, so the entry is already stale.
    await verifier.verify_token("oauth-token")

    assert len(calls) == 2


async def test_a_rejected_token_verifies_as_none():
    verifier = ExchangeTokenVerifier(make_settings())
    verifier.http_client = client_returning(
        httpx.Response(400, json={"error": "invalid_grant"})
    )

    assert await verifier.verify_token("oauth-token") is None


async def test_a_backend_outage_propagates_rather_than_reading_as_invalid():
    verifier = ExchangeTokenVerifier(make_settings())
    verifier.http_client = client_returning(httpx.Response(503, text="down"))

    with pytest.raises(NojoAPIError):
        await verifier.verify_token("oauth-token")


async def test_an_empty_token_is_rejected_without_a_backend_call():
    verifier = ExchangeTokenVerifier(make_settings())
    verifier.http_client = client_returning(httpx.Response(200, json=GOOD_BODY))

    assert await verifier.verify_token("   ") is None


async def test_no_http_client_verifies_as_none():
    verifier = ExchangeTokenVerifier(make_settings())

    assert await verifier.verify_token("oauth-token") is None


async def test_the_exchange_posts_to_the_configured_url():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=GOOD_BODY)

    settings = make_settings()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await exchange_token(http, settings, "oauth-token")

    assert str(seen[0].url) == TOKEN_EXCHANGE_URL
    assert seen[0].method == "POST"


async def test_the_access_token_carries_the_issuer_claim():
    verifier = ExchangeTokenVerifier(make_settings())
    verifier.http_client = client_returning(httpx.Response(200, json=GOOD_BODY))

    token = await verifier.verify_token("oauth-token")

    assert token.claims["iss"] == make_settings().issuer_url
    assert token.scopes == []
    assert token.expires_at > time.time()
