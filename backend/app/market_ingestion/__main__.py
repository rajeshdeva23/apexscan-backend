"""Dedicated entrypoint for the market-ingestion service: ``python -m app.market_ingestion``.

H1 behaviour is inert by default. With ``MARKET_INGESTION_SERVICE_ENABLED=false`` (the default)
the process logs its disabled status and exits cleanly (code 0) having performed **no** Dhan auth,
WebSocket, epoch allocation, Redis, or IPC activity. With the service enabled it refuses to boot
(live boot is H2) and exits non-zero — H1 never begins live provider activity. Importing this
module has no side effects; work happens only under ``if __name__ == "__main__"``.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from app.core.config import get_settings
from app.market_ingestion.service import (
    MarketIngestionBootNotImplementedError,
    MarketIngestionService,
)

logger = logging.getLogger(__name__)


async def _run() -> int:
    """Construct the inert service and start it; return a process exit code."""
    settings = get_settings()
    service = MarketIngestionService(flags=settings.phase_h_flags())
    try:
        await service.start()
    except MarketIngestionBootNotImplementedError as error:
        logger.error("%s", error)
        return 1
    logger.info(
        "market-ingestion entrypoint complete: status=%s mode=%s", service.status, service.mode
    )
    return 0


def main() -> int:
    """Configure logging and run the inert entrypoint (no network activity in H1)."""
    logging.basicConfig(level=logging.INFO)
    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
