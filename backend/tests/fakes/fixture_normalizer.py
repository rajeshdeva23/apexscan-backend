"""Sanitized test-only provider-shaped fixture normalizer."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation

from pydantic import ValidationError

from app.adapters.base.errors import NormalizationError
from app.adapters.base.normalizer import TickNormalizer
from app.schemas.market_data import Instrument, Tick


class FixtureTickNormalizer(TickNormalizer):
    """Turn sanitized fixture input into canonical ticks for boundary tests."""

    def normalize_tick(self, payload: Mapping[str, object]) -> Tick:
        """Normalize required values only; unknown input remains at the boundary."""
        try:
            event_timestamp = datetime.fromisoformat(
                self._require_string(payload, "vendor_event_time")
            )
            return Tick(
                instrument=Instrument(
                    exchange=self._require_string(payload, "vendor_exchange"),
                    symbol=self._require_string(payload, "vendor_symbol"),
                ),
                event_timestamp=event_timestamp,
                last_price=Decimal(self._require_string(payload, "vendor_last_price")),
                traded_quantity=int(self._require_string(payload, "vendor_volume")),
            )
        except (InvalidOperation, KeyError, TypeError, ValidationError, ValueError):
            raise NormalizationError() from None

    @staticmethod
    def _require_string(payload: Mapping[str, object], field_name: str) -> str:
        value = payload[field_name]
        if not isinstance(value, str):
            raise TypeError(field_name)
        return value
