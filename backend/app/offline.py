"""ASGI entry point for the offline validation harness.

Run with ``uvicorn app.offline:app`` to serve the real scanner REST surface over
a synthetic, network-free pipeline (no PostgreSQL, Redis, or Dhan required). This
is a dev/validation entrypoint only; production is served by ``app.main:app``.
"""

from __future__ import annotations

from app.services.offline_harness import create_offline_app

app = create_offline_app()
