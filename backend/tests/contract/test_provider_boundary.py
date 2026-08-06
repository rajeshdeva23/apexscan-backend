"""Contract and boundary tests for broker-independent provider abstractions."""

from __future__ import annotations

import inspect
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest


def _module(name: str, requirement: str) -> Any:
    """Import a P3.1 module or report the missing contract as a test failure."""
    try:
        return import_module(name)
    except ModuleNotFoundError:
        pytest.fail(requirement)


async def test_fake_adapter_satisfies_the_shared_capabilities_without_network() -> None:
    """A provider-agnostic consumer can use the fake without knowing a broker identity."""
    contracts = _module(
        "app.schemas.market_data", "P3.1 must expose canonical market-data contracts"
    )
    adapter_contracts = _module(
        "app.adapters.base.broker_adapter", "P3.1 must expose shared adapter capabilities"
    )
    contract_tests = _module(
        "tests.contract.provider_adapter_contract",
        "P3.1 must provide reusable provider adapter contract assertions",
    )
    fake_module = _module(
        "tests.fakes.fake_broker_adapter", "P3.1 must provide a test-only fake adapter"
    )
    adapter = fake_module.FakeBrokerAdapter()

    assert isinstance(adapter, adapter_contracts.BrokerAdapter)
    assert isinstance(adapter, adapter_contracts.LiveMarketDataAdapter)
    assert isinstance(adapter, adapter_contracts.HistoricalDataAdapter)
    assert isinstance(adapter, adapter_contracts.InstrumentDataAdapter)

    health, streamed = await contract_tests.assert_full_adapter_contract(
        adapter, fake_module.subscription_request()
    )

    assert health.status is contracts.ProviderStatus.HEALTHY
    assert all(isinstance(event, contracts.MarketData) for event in streamed)
    assert adapter_contracts.ProviderCapability.LIVE_MARKET_DATA in adapter.capabilities


def test_fixture_normalizer_converts_provider_shaped_input_without_leaking_fields() -> None:
    """Only canonical output crosses the normalizer boundary; unknown input is ignored."""
    contracts = _module(
        "app.schemas.market_data", "P3.1 must expose canonical market-data contracts"
    )
    fake_module = _module(
        "tests.fakes.fixture_normalizer", "P3.1 must provide a fixture normalizer"
    )

    tick = fake_module.FixtureTickNormalizer().normalize_tick(
        {
            "vendor_exchange": "NSE",
            "vendor_symbol": "APEX",
            "vendor_event_time": "2026-08-04T09:15:00+00:00",
            "vendor_last_price": "101.25",
            "vendor_volume": "10",
            "ignored_vendor_metadata": "not part of the canonical contract",
        }
    )

    assert isinstance(tick, contracts.Tick)
    assert tick.model_dump(mode="json") == {
        "instrument": {
            "exchange": "NSE",
            "market_segment": "equity",
            "symbol": "APEX",
            "instrument_class": "cash",
            "underlying": None,
            "display_name": None,
            "listing_type": None,
            "series": None,
            "expiry": None,
            "strike_price": None,
            "option_type": None,
        },
        "event_timestamp": "2026-08-04T09:15:00Z",
        "last_price": "101.25",
        "traded_quantity": 10,
    }


def test_normalization_failures_redact_provider_payload_values() -> None:
    """Malformed provider input fails safely without retaining secret-bearing values."""
    errors = _module("app.adapters.base.errors", "P3.1 must expose provider-boundary errors")
    fake_module = _module(
        "tests.fakes.fixture_normalizer", "P3.1 must provide a fixture normalizer"
    )
    secret = "never-render-provider-secret"

    with pytest.raises(errors.NormalizationError) as captured:
        fake_module.FixtureTickNormalizer().normalize_tick(
            {
                "vendor_exchange": "NSE",
                "vendor_symbol": "APEX",
                "vendor_event_time": "not-a-timestamp",
                "vendor_last_price": "101.25",
                "vendor_volume": "10",
                "authorization": f"Bearer {secret}",
            }
        )

    assert secret not in str(captured.value)
    assert "authorization" not in str(captured.value).lower()


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("vendor_event_time", "not-a-timestamp"),
        ("vendor_last_price", "not-a-number"),
        ("vendor_last_price", "0"),
        ("vendor_volume", "-1"),
    ),
)
def test_fixture_normalizer_rejects_invalid_required_values_safely(
    field_name: str, value: str
) -> None:
    """Malformed provider values fail explicitly without exposing raw input."""
    errors = _module("app.adapters.base.errors", "P3.1 must expose provider-boundary errors")
    fake_module = _module(
        "tests.fakes.fixture_normalizer", "P3.1 must provide a fixture normalizer"
    )
    payload: dict[str, object] = {
        "vendor_exchange": "NSE",
        "vendor_symbol": "APEX",
        "vendor_event_time": "2026-08-04T09:15:00+00:00",
        "vendor_last_price": "101.25",
        "vendor_volume": "10",
        "authorization": "Bearer never-render-provider-secret",
    }
    payload[field_name] = value

    with pytest.raises(errors.NormalizationError) as captured:
        fake_module.FixtureTickNormalizer().normalize_tick(payload)

    assert "never-render-provider-secret" not in str(captured.value)


def test_fixture_normalizer_rejects_missing_mandatory_values_safely() -> None:
    """A missing value is not fabricated into a market-data value."""
    errors = _module("app.adapters.base.errors", "P3.1 must expose provider-boundary errors")
    fake_module = _module(
        "tests.fakes.fixture_normalizer", "P3.1 must provide a fixture normalizer"
    )
    payload: dict[str, object] = {
        "vendor_exchange": "NSE",
        "vendor_symbol": "APEX",
        "vendor_event_time": "2026-08-04T09:15:00+00:00",
        "vendor_volume": "10",
    }

    with pytest.raises(errors.NormalizationError):
        fake_module.FixtureTickNormalizer().normalize_tick(payload)


def test_shared_contracts_and_canonical_types_do_not_import_dhan() -> None:
    """The P3.1 boundary stays broker-neutral above concrete adapter namespaces."""
    modules = (
        _module("app.schemas.market_data", "P3.1 must expose canonical market-data contracts"),
        _module("app.adapters.base.broker_adapter", "P3.1 must expose shared adapter capabilities"),
        _module("app.adapters.base.errors", "P3.1 must expose provider-boundary errors"),
        _module("app.adapters.base.normalizer", "P3.1 must expose a normalizer contract"),
    )

    for module in modules:
        assert "dhan" not in inspect.getsource(module).lower()


def test_no_market_engine_or_strategy_implementation_is_introduced() -> None:
    """P3.1 creates only the provider boundary, not later engine or strategy logic."""
    for package_name in ("app.market_engine", "app.strategies"):
        package = import_module(package_name)
        package_path = Path(package.__file__).parent
        implementation_modules = [
            path for path in package_path.glob("*.py") if path.name != "__init__.py"
        ]
        assert implementation_modules == []
