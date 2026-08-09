"""Canonical market-semantic validation at the Market Engine front door (docs/06 §9).

Structural and numeric validity — finite, positive prices, non-negative
quantities, and timezone-aware timestamps — is already guaranteed by the
canonical Phase-3 contracts (``Tick``/``Quote`` reject them at construction), so
this stage does not re-check them. It adds only the canonical MARKET-semantic
checks the engine owns relative to its universe and state: the instrument must
belong to the known universe, timestamps must not be implausibly in the future,
and duplicate or older-than-current events are rejected. Nothing is fabricated,
coerced, or repaired (docs/06 §9.2, §25).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum

from app.market_engine.state import InstrumentState
from app.schemas.market_data import Quote, Tick

# docs/06 §9.1: a timestamp must not be "absurdly ... future". Allow only a small
# clock skew before an event is treated as implausible and rejected.
_MAX_FUTURE_SKEW = timedelta(minutes=1)


class ValidationOutcome(StrEnum):
    """The canonical classification of a single incoming event (facts only).

    STALE covers an event whose event time is older than the current accepted
    state; under this slice's timestamp-only, no-reorder policy that also fulfils
    the out-of-order rejection rule (docs/06 §9.2, §12.5) — an event arriving
    after a newer one is never applied over fresher state.
    """

    ACCEPT = "accept"
    DUPLICATE = "duplicate"
    STALE = "stale"
    INVALID = "invalid"


def _is_duplicate(event: Tick | Quote, state: InstrumentState) -> bool:
    """Return whether the event exactly repeats the last accepted event of its type."""
    if isinstance(event, Tick):
        return event == state.latest_tick
    return event == state.latest_quote


def classify(
    event: Tick | Quote,
    *,
    known: bool,
    state: InstrumentState | None,
    now: datetime,
    max_future_skew: timedelta = _MAX_FUTURE_SKEW,
) -> ValidationOutcome:
    """Classify a canonical event against the known universe and current state.

    Args:
        event: The canonical tick or quote to classify.
        known: Whether the instrument is part of the known universe.
        state: The instrument's current state, or None if it has none yet.
        now: The current instant from the injected clock (UTC).
        max_future_skew: The tolerated look-ahead before a timestamp is implausible.

    Returns:
        The :class:`ValidationOutcome`; only ACCEPT permits a state change.
    """
    if not known:
        return ValidationOutcome.INVALID
    if event.event_timestamp > now + max_future_skew:
        return ValidationOutcome.INVALID
    if state is None:
        return ValidationOutcome.ACCEPT
    if _is_duplicate(event, state):
        return ValidationOutcome.DUPLICATE
    last = state.last_event_timestamp
    if last is not None and event.event_timestamp < last:
        return ValidationOutcome.STALE
    return ValidationOutcome.ACCEPT
