"""Request logging middleware.

Emits one structured log line per HTTP request with method, path, status, and
duration. Pure infrastructure — it observes traffic and never alters request
or response bodies.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("apexscan.access")


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Log method, path, status code, and latency for each request."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Time the request and log the outcome."""
        start = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.info(
            "request",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": round(elapsed_ms, 2),
            },
        )
        return response
