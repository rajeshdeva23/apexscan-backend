"""Entrypoint for the market-ingestion service: ``python -m app.market_ingestion`` (H1/H2/H3C).

Disabled (the default) → composes an inert service, logs its status, exits cleanly (code 0) with
NO Dhan/IPC/Redis activity. Enabled → composes the provider-owning service, starts it, and serves
until the supervisor ends or an operator termination signal (SIGTERM/SIGINT) arrives, then shuts
down deterministically (stop intake → drain M2 → finalize L1 → close the owned Redis client). A
terminal publication break fails closed with a non-zero exit. Importing this module has no side
effects; work happens only under ``if __name__ == "__main__"``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys
from collections.abc import Callable

from app.core.config import get_settings
from app.market_ingestion.composition import compose_market_ingestion_service
from app.market_ingestion.service import MarketIngestionService, ServiceStatus

logger = logging.getLogger(__name__)

_SHUTDOWN_SIGNALS = (signal.SIGTERM, signal.SIGINT)


async def _run() -> int:
    """Compose, start, and (when enabled) serve the ingestion service; return an exit code."""
    settings = get_settings()
    service = None
    try:
        service = await compose_market_ingestion_service(settings)
        await service.start()
    except Exception:  # noqa: BLE001 - a compose/startup failure is a clean non-zero exit
        logger.exception("market-ingestion service failed to start")
        if service is not None:
            await service.stop()  # unwind partial startup and close the owned Redis client
        return 1
    logger.info("market-ingestion status=%s mode=%s", service.status, service.mode)
    if service.status is ServiceStatus.RUNNING:
        try:
            await _wait_for_shutdown(service)  # supervisor end, or SIGTERM/SIGINT
        finally:
            await service.stop()  # stop intake → drain M2 → finalize L1 → close Redis (always)
        if service.terminal_failure:  # a terminal publication break fails closed at exit too
            logger.error("market-ingestion ended on a terminal publication break")
            return 1
    return 0 if service.status in (ServiceStatus.DISABLED, ServiceStatus.STOPPED) else 1


async def _wait_for_shutdown(service: MarketIngestionService) -> None:
    """Serve until the supervisor ends or an operator termination signal arrives.

    A long-lived container receives ``SIGTERM`` on ``docker stop``; handling it (and ``SIGINT``)
    lets the entrypoint drain/close gracefully instead of being killed mid-publication. On a
    platform or thread without asyncio signal support the handlers are skipped and the service
    still runs until the supervisor ends (or the task is cancelled). The actual drain/close is the
    caller's ``stop()`` — this only waits and then tears down its own wait tasks and handlers.
    """
    loop = asyncio.get_running_loop()
    shutdown = asyncio.Event()
    installed = _install_signal_handlers(loop, shutdown.set)
    supervisor = asyncio.ensure_future(service.wait())
    signalled = asyncio.ensure_future(shutdown.wait())
    try:
        await asyncio.wait({supervisor, signalled}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for pending in (supervisor, signalled):
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pending
        for sig in installed:
            with contextlib.suppress(Exception):
                loop.remove_signal_handler(sig)


def _install_signal_handlers(
    loop: asyncio.AbstractEventLoop, callback: Callable[[], None]
) -> tuple[int, ...]:
    """Install ``callback`` for SIGTERM/SIGINT; return the signals actually installed."""
    installed: list[int] = []
    for sig in _SHUTDOWN_SIGNALS:
        try:
            loop.add_signal_handler(sig, callback)
        except (NotImplementedError, RuntimeError, ValueError):
            continue  # no asyncio signal support here (e.g. Windows, or a non-main thread)
        installed.append(sig)
    return tuple(installed)


def main() -> int:
    """Configure logging and run the entrypoint."""
    logging.basicConfig(level=logging.INFO)
    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
