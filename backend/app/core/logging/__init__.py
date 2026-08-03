"""Logging package. Re-exports :func:`configure_logging`."""

from app.core.logging.logger import configure_logging

__all__ = ["configure_logging"]
