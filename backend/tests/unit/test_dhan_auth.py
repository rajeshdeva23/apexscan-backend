"""Behavioural tests for Dhan's documented server-side TOTP token flow."""

from __future__ import annotations

from datetime import UTC, datetime
from importlib import import_module
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

_FROZEN_NOW = datetime(1970, 1, 1, 0, 0, 59, tzinfo=UTC)
_CLIENT_ID = "fixture-client-id-must-not-leak"
_PIN = "654321"
_TOTP_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
_ACCESS_TOKEN = "fixture-runtime-access-token-must-not-leak"


def _dhan_auth() -> Any:
    """Load the proposed Dhan-local manager after each test states its boundary."""
    try:
        return import_module("app.adapters.dhan.auth")
    except ModuleNotFoundError:
        pytest.fail("P3.3 must provide a Dhan-local async TOTP authentication manager")


def _success_response(*, expiry_time: str = "2026-01-02T05:30:00.000") -> dict[str, object]:
    """Mirror the documented response, including fields the implementation must ignore."""
    return {
        "dhanClientId": "fixture-response-client-id",
        "dhanClientName": "Fixture User",
        "dhanClientUcc": "FIXTURE123",
        "givenPowerOfAttorney": False,
        "accessToken": _ACCESS_TOKEN,
        "expiryTime": expiry_time,
    }


def _manager(handler: Any, *, clock: Any = lambda: _FROZEN_NOW) -> Any:
    return _dhan_auth().DhanAuthManager(
        client_id=SecretStr(_CLIENT_ID),
        pin=SecretStr(_PIN),
        totp_secret=SecretStr(_TOTP_SECRET),
        timeout_seconds=3.0,
        transport=httpx.MockTransport(handler),
        clock=clock,
    )


async def test_generates_a_deterministic_six_digit_totp_for_the_documented_request() -> None:
    """A wrong time-based code must not silently authenticate with a live Dhan account."""
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        observed.update(
            {
                "method": request.method,
                "path": request.url.path,
                "parameter_names": frozenset(params.keys()),
                "credentials_match": (
                    params.get("dhanClientId") == _CLIENT_ID
                    and params.get("pin") == _PIN
                    and params.get("totp") == "287082"
                ),
            }
        )
        return httpx.Response(200, json=_success_response())

    manager = _manager(handler)
    try:
        token = await manager.get_access_token()
    finally:
        await manager.disconnect()

    assert token.get_secret_value() == _ACCESS_TOKEN
    assert observed == {
        "method": "POST",
        "path": "/app/generateAccessToken",
        "parameter_names": frozenset({"dhanClientId", "pin", "totp"}),
        "credentials_match": True,
    }


async def test_parses_documented_naive_expiry_as_indian_standard_time() -> None:
    """Treating the documented offset-free expiry as UTC would rotate a token incorrectly."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_success_response())

    manager = _manager(handler)
    try:
        await manager.get_access_token()
        expiry = manager.current_token_expires_at
    finally:
        await manager.disconnect()

    assert expiry == datetime(2026, 1, 2, tzinfo=UTC)


async def test_reuses_a_valid_runtime_token_without_another_authentication_request() -> None:
    """Regenerating a still-valid token would create avoidable authentication traffic."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_success_response(expiry_time="2026-01-01T06:00:00"))

    manager = _manager(handler)
    try:
        first = await manager.get_access_token()
        second = await manager.get_access_token()
    finally:
        await manager.disconnect()

    assert first.get_secret_value() == _ACCESS_TOKEN
    assert second.get_secret_value() == _ACCESS_TOKEN
    assert calls == 1


async def test_regenerates_a_runtime_token_before_its_documented_expiry() -> None:
    """Continuing to use a token inside the safety window risks a failed data request."""
    calls = 0
    times = iter(
        (
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 1, 1, 56, tzinfo=UTC),
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                **_success_response(expiry_time="2026-01-01T07:30:00"),
                "accessToken": f"token-{calls}",
            },
        )

    manager = _manager(handler, clock=lambda: next(times))
    try:
        first = await manager.get_access_token()
        second = await manager.get_access_token()
    finally:
        await manager.disconnect()

    assert first.get_secret_value() == "token-1"
    assert second.get_secret_value() == "token-2"
    assert calls == 2


@pytest.mark.parametrize(
    "response_payload",
    [
        {key: value for key, value in _success_response().items() if key != "accessToken"},
        {key: value for key, value in _success_response().items() if key != "expiryTime"},
        {**_success_response(), "expiryTime": "not-a-dhan-expiry"},
    ],
)
async def test_rejects_malformed_token_responses_without_revealing_credentials(
    response_payload: dict[str, object],
) -> None:
    """Accepting a partial Dhan token response would defer an auth defect to later requests."""
    dhan_auth = _dhan_auth()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response_payload)

    manager = _manager(handler)
    try:
        with pytest.raises(dhan_auth.NormalizationError) as captured:
            await manager.get_access_token()
    finally:
        await manager.disconnect()

    diagnostic = str(captured.value)
    for sensitive in (_CLIENT_ID, _PIN, _TOTP_SECRET, _ACCESS_TOKEN, "287082"):
        assert sensitive not in diagnostic


@pytest.mark.parametrize(
    ("failure_kind", "error_type"),
    [
        (
            "authentication",
            "ProviderAuthenticationError",
        ),
        ("timeout", "ProviderTimeoutError"),
        ("network", "ProviderNetworkError"),
    ],
)
async def test_translates_auth_failures_without_secret_leakage(
    failure_kind: str,
    error_type: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Leaking Dhan request or response details would disclose reusable authentication material."""
    dhan_auth = _dhan_auth()

    def handler(request: httpx.Request) -> httpx.Response:
        if failure_kind == "authentication":
            return httpx.Response(
                401,
                json={
                    "errorType": "Authentication Error",
                    "errorCode": "DH-901",
                    "errorMessage": f"invalid {_PIN} {_TOTP_SECRET} 287082 {_ACCESS_TOKEN}",
                },
            )
        if failure_kind == "timeout":
            raise httpx.ReadTimeout("fixture", request=request)
        raise httpx.ConnectError("fixture", request=request)

    manager = _manager(handler)
    try:
        with pytest.raises(getattr(dhan_auth, error_type)) as captured:
            await manager.get_access_token()
    finally:
        await manager.disconnect()

    output = str(captured.value) + caplog.text
    for sensitive in (_CLIENT_ID, _PIN, _TOTP_SECRET, _ACCESS_TOKEN, "287082"):
        assert sensitive not in output


async def test_runtime_authentication_material_is_not_exposed_by_manager_state() -> None:
    """A printable manager must not disclose its configured or generated secret material."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_success_response())

    manager = _manager(handler)
    try:
        await manager.get_access_token()
        representation = repr(manager)
    finally:
        await manager.disconnect()

    for sensitive in (_CLIENT_ID, _PIN, _TOTP_SECRET, _ACCESS_TOKEN, "287082"):
        assert sensitive not in representation


async def test_rest_adapter_uses_the_runtime_totp_token_for_data_api_requests() -> None:
    """Bypassing the manager would leave the adapter unable to rotate TOTP-generated tokens."""
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "auth.dhan.co":
            return httpx.Response(200, json=_success_response())
        observed.update(
            {
                "profile_path": request.url.path,
                "runtime_token_applied": request.headers.get("access-token") == _ACCESS_TOKEN,
            }
        )
        return httpx.Response(200, json={"tokenValidity": "fixture-only"})

    manager = _manager(handler)
    adapter_module = import_module("app.adapters.dhan.adapter")
    adapter = adapter_module.DhanRestAdapter(
        token_provider=manager,
        transport=httpx.MockTransport(handler),
    )
    await adapter.connect()
    try:
        health = await adapter.get_health()
    finally:
        await adapter.disconnect()

    assert health.status.value == "healthy"
    assert observed == {
        "profile_path": "/v2/profile",
        "runtime_token_applied": True,
    }
