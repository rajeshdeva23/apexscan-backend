"""Lightweight, dependency-free errors for the market-ingestion service (DECOUPLING PHASE H3A).

Kept import-pure (no ``market_ipc`` / provider imports) so the supervisor and package ``__init__``
can reference the terminal-break signal without pulling the heavy publication stack at import time.
"""

from __future__ import annotations


class PublicationTerminalError(RuntimeError):
    """A terminal publication continuity break (overflow / worker fault) — provider must stop.

    Raised by the publishing sink on a terminal M2 submit outcome; the provider supervisor lets it
    propagate (rather than self-healing) so provider intake fails closed for the incarnation.
    """
