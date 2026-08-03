"""Middleware package.

ASGI/HTTP middleware that wraps every request (logging, and in future:
request IDs, rate limiting, auth context). Middleware is cross-cutting and
must not contain domain logic.
"""

from app.middleware.request_logging import RequestLoggingMiddleware

__all__ = ["RequestLoggingMiddleware"]
