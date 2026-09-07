import httpx
import pytest

from src.core.config import Settings
from src.services.renile_client import ReNileClient, RenileAPIError

BASE_URL = "https://renile-iot.test"


def make_settings(**overrides) -> Settings:
    # _env_file=None so the developer's real src/.env cannot alter a test.
    return Settings(_env_file=None, RENILE_API_TOKEN="test-token", **overrides)


def make_client(handler, **overrides) -> ReNileClient:
    settings = make_settings(**overrides)
    return ReNileClient(
        httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(handler)),
        settings,
    )


async def test_get_devices_returns_roster():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == make_settings().renile_devices_path
        return httpx.Response(200, json=[{"_id": "abc", "name": "Greenhouse"}])

    devices = await make_client(handler).get_devices()
    assert devices == [{"_id": "abc", "name": "Greenhouse"}]


async def test_get_snapshot_returns_payload():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == make_settings().renile_snapshot_path
        return httpx.Response(200, json={"device_count": 7, "projects": []})

    snapshot = await make_client(handler).get_snapshot()
    assert snapshot["device_count"] == 7


async def test_401_plain_text_body_raises_token_error_not_json_error():
    """The platform returns the bare string 'Unauthorized' on 401.

    Parsing before checking status would surface a JSONDecodeError instead of the
    real cause, so this is the regression that matters most.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="Unauthorized")

    with pytest.raises(RenileAPIError) as excinfo:
        await make_client(handler).get_snapshot()

    message = str(excinfo.value)
    assert "RENILE_API_TOKEN" in message
    assert "JSON" not in message


async def test_server_error_includes_status_and_truncated_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="x" * 500)

    with pytest.raises(RenileAPIError) as excinfo:
        await make_client(handler).get_devices()

    assert "500" in str(excinfo.value)
    assert len(str(excinfo.value)) < 400


async def test_timeout_raises_friendly_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    with pytest.raises(RenileAPIError, match="timed out"):
        await make_client(handler).get_snapshot()


async def test_transport_error_is_retried_once_then_succeeds():
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        if len(attempts) == 1:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json=[])

    devices = await make_client(handler).get_devices()
    assert devices == []
    assert len(attempts) == 2


async def test_non_json_200_body_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>nope</html>")

    with pytest.raises(RenileAPIError, match="non-JSON"):
        await make_client(handler).get_snapshot()


async def test_timeout_is_not_retried():
    """Retrying a timeout would only double the wait before failing."""
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        raise httpx.ReadTimeout("too slow", request=request)

    with pytest.raises(RenileAPIError, match="timed out"):
        await make_client(handler).get_snapshot()

    assert len(attempts) == 1


# --- configuration is honoured ----------------------------------------------

async def test_endpoint_paths_come_from_settings():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json=[])

    client = make_client(handler, RENILE_DEVICES_PATH="/custom/devices/")
    await client.get_devices()
    assert seen == ["/custom/devices/"]


async def test_max_attempts_of_one_disables_retrying():
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(RenileAPIError):
        await make_client(handler, HTTP_MAX_ATTEMPTS=1).get_devices()

    assert len(attempts) == 1


async def test_max_attempts_of_three_retries_twice():
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        if len(attempts) < 3:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json=[])

    await make_client(handler, HTTP_MAX_ATTEMPTS=3).get_devices()
    assert len(attempts) == 3


async def test_error_body_preview_length_is_configurable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="y" * 500)

    with pytest.raises(RenileAPIError) as excinfo:
        await make_client(handler, ERROR_BODY_PREVIEW_CHARS=10).get_devices()

    assert "y" * 10 in str(excinfo.value)
    assert "y" * 11 not in str(excinfo.value)
