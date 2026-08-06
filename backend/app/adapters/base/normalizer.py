"""Broker-specific input to canonical market-data normalization contract."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from app.schemas.market_data import Tick


class TickNormalizer(Protocol):
    """Normalize provider-shaped tick input without exposing it to consumers."""

    def normalize_tick(self, payload: Mapping[str, object]) -> Tick:
        """Return a canonical tick or raise a safe provider-boundary error."""
