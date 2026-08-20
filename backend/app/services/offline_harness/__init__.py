"""Offline validation harness (dev-only).

A supported, network-free composition that serves the real scanner REST surface
over the real runtime pipeline using offline doubles (in-memory DB/Redis + a
synthetic 208-instrument provider). Used for local UI acceptance without Dhan,
PostgreSQL, or Redis. Never wired into the production composition root.
"""

from __future__ import annotations

from app.services.offline_harness.app_factory import create_offline_app
from app.services.offline_harness.fixture_provider import OfflineFixtureProvider

__all__ = ["OfflineFixtureProvider", "create_offline_app"]
