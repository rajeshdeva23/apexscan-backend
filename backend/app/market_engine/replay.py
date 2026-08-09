"""Replay-enabling abstractions for the Market Engine (design only, no engine).

Deterministic replay is achieved by construction: MarketContext and every event
are immutable and carry an explicit version and sequence, so feeding identical
ordered inputs through an injected fixed clock and a fresh monotonic sequence
reproduces identical outputs (docs/06 §1.4, §26; docs/09 §9). This module names
the contracts a future replay driver will depend on; it deliberately contains no
replay driver or market logic.
"""

from __future__ import annotations

from app.market_engine.clock import Clock
from app.market_engine.sequence import SequenceGenerator

# A replay driver injects a deterministic Clock and SequenceGenerator in place of
# the production SystemClock / live sequencing. The protocols are identical to
# the engine's normal injection points, which is exactly what makes replay a
# drop-in substitution rather than a separate code path.
ReplayClock = Clock
ReplaySequence = SequenceGenerator

__all__ = ["ReplayClock", "ReplaySequence"]
