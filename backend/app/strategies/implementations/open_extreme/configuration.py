"""Configuration for the Open=High / Open=Low current-session strategies (DEPLOY-10).

Both strategies are parameter-free — a match is exact authoritative
open==high (or open==low), with no threshold or tolerance to configure (ADR-009
CSOA22). The configuration therefore adds no fields beyond the versioned identity
seam of :class:`StrategyConfiguration`, keeping results reproducible.
"""

from __future__ import annotations

from app.strategies.configuration import StrategyConfiguration


class OpenExtremeConfiguration(StrategyConfiguration):
    """Parameter-free configuration shared by the Open=High and Open=Low strategies."""
