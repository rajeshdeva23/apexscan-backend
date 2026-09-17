"""Adapter-internal live-socket ownership authorization (DECOUPLING PHASE H9C-P3, Gate G/C/D).

The Dhan adapter must ask its injected ``live_connect_authorization`` before EVERY live-socket open
(the initial connect and every reconnect) and must NOT read a token or open a socket when it is
refused (a process that lost ownership must not reopen its WebSocket). It must still permit a
legitimate reconnect while ownership holds. The adapter knows only "may I connect?"; it has no Redis
or ownership dependency. Uses local fakes only — no Dhan, no network.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from app.adapters.base.errors import ProviderNotAuthorizedError
from app.adapters.dhan.adapter import DhanRestAdapter
from app.adapters.dhan.live import DhanLiveReconnectPolicy


class _FakeSocket:
    async def send(self, message: str) -> None:  # noqa: ARG002 - stub
        return None

    async def recv(self) -> bytes | str:
        return b""

    async def close(self) -> None:
        return None


class _FakeTransport:
    """Counts live-socket opens; each ``connect`` yields a fresh fake socket."""

    def __init__(self) -> None:
        self.connect_calls = 0

    async def connect(self, url: str, timeout_seconds: float) -> _FakeSocket:  # noqa: ARG002 - stub
        self.connect_calls += 1
        return _FakeSocket()


class _SpyTokenProvider:
    """Counts token accesses so a test can prove a refused connect never reads a token."""

    def __init__(self) -> None:
        self.calls = 0

    async def get_access_token(self) -> SecretStr:
        self.calls += 1
        return SecretStr("fake-token")

    async def disconnect(self) -> None:
        return None


async def _noop_sleep(_seconds: float) -> None:
    return None


def _adapter(*, authorized: bool | list[bool], transport: _FakeTransport, token: _SpyTokenProvider):
    """Build an adapter whose authorization returns the given value(s) in sequence."""
    outcomes = authorized if isinstance(authorized, list) else None

    async def _authz() -> bool:
        if outcomes is not None:
            return outcomes.pop(0)
        return bool(authorized)

    return DhanRestAdapter(
        token_provider=token,
        live_client_id=SecretStr("client-1"),
        websocket_transport=transport,
        live_reconnect_policy=DhanLiveReconnectPolicy(maximum_attempts=3),
        live_sleep=_noop_sleep,
        live_connect_authorization=_authz,
    )


async def test_initial_connect_refused_when_not_authorized_reads_no_token_opens_no_socket() -> None:
    transport, token = _FakeTransport(), _SpyTokenProvider()
    adapter = _adapter(authorized=False, transport=transport, token=token)
    with pytest.raises(ProviderNotAuthorizedError):
        await adapter._connect_live_socket()
    assert transport.connect_calls == 0  # no WS opened
    assert token.calls == 0  # authorization is checked BEFORE the token is read


async def test_connect_allowed_when_authorized() -> None:
    transport, token = _FakeTransport(), _SpyTokenProvider()
    adapter = _adapter(authorized=True, transport=transport, token=token)
    await adapter._connect_live_socket()
    assert transport.connect_calls == 1
    assert token.calls == 1


async def test_reconnect_denied_when_ownership_lost_is_terminal_no_retry() -> None:
    transport, token = _FakeTransport(), _SpyTokenProvider()
    # Authorized for the initial connect, then ownership is lost before the reconnect.
    adapter = _adapter(authorized=[True, False], transport=transport, token=token)
    await adapter._connect_live_socket()
    assert transport.connect_calls == 1 and token.calls == 1

    with pytest.raises(ProviderNotAuthorizedError):
        await adapter._reconnect_live_subscription()
    # No new socket opened and no new token read despite a 3-attempt policy: terminal on first deny.
    assert transport.connect_calls == 1
    assert token.calls == 1


async def test_valid_reconnect_is_permitted_while_ownership_holds() -> None:
    transport, token = _FakeTransport(), _SpyTokenProvider()
    adapter = _adapter(authorized=True, transport=transport, token=token)
    await adapter._connect_live_socket()
    assert transport.connect_calls == 1

    # No desired plan → reconnect just re-opens the socket and returns (still-owning path).
    await adapter._reconnect_live_subscription()
    assert transport.connect_calls == 2  # a legitimate reconnect was NOT blocked
    assert token.calls == 2
