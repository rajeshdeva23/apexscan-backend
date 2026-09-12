"""Entrypoint for the market-ingestion service: ``python -m app.market_ingestion`` (H1/H2).

Disabled (the default) → composes an inert service, logs its status, exits cleanly (code 0) with
NO Dhan/IPC/Redis activity. Enabled → composes the provider-owning service, starts it, and stays
alive for the service lifetime (until the supervisor ends or the process is cancelled), then shuts
down deterministically. IPC publication is never activated here (H2). Importing this module has no
side effects; work happens only under ``if __name__ == "__main__"``.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from app.core.config import get_settings
from app.market_ingestion.composition import compose_market_ingestion_service
from app.market_ingestion.service import ServiceStatus

logger = logging.getLogger(__name__)


async def _run() -> int:
    """Compose, start, and (when enabled) serve the ingestion service; return an exit code."""
    settings = get_settings()
    service = await compose_market_ingestion_service(settings)
    try:
        await service.start()
    except Exception:  # noqa: BLE001 - a startup failure is a clean non-zero exit, not a crash
        logger.exception("market-ingestion service failed to start")
        await service.stop()
        return 1
    logger.info("market-ingestion status=%s mode=%s", service.status, service.mode)
    if service.status is ServiceStatus.RUNNING:
        await service.wait()  # serve until the supervisor ends or the process is cancelled
        await service.stop()
    return 0 if service.status in (ServiceStatus.DISABLED, ServiceStatus.STOPPED) else 1


def main() -> int:
    """Configure logging and run the entrypoint."""
    logging.basicConfig(level=logging.INFO)
    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
