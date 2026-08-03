"""Structured logging configuration.

Provides a single ``configure_logging`` entry point that sets up JSON logging
suitable for container environments (stdout, parseable by log aggregators).
Application code obtains loggers via ``logging.getLogger(__name__)`` — this
module only configures handlers and formatters. No business logic here.
"""

from __future__ import annotations

import logging
import sys

from pythonjsonlogger import json as json_logger


def configure_logging(level: str = "INFO") -> None:
    """Configure root logging for the application.

    Args:
        level: Minimum log level name (e.g. ``"INFO"``, ``"DEBUG"``).

    Idempotent: existing handlers on the root logger are replaced so repeated
    calls (e.g. during test setup) do not duplicate log lines.
    """
    root = logging.getLogger()
    root.setLevel(level.upper())

    handler = logging.StreamHandler(sys.stdout)
    formatter = json_logger.JsonFormatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        rename_fields={"asctime": "timestamp", "levelname": "level", "name": "logger"},
    )
    handler.setFormatter(formatter)

    root.handlers = [handler]
