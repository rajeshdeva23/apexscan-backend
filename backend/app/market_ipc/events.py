"""Broker-neutral IPC event-kind discrimination (DECOUPLING PHASE A).

The wire discriminator is an explicit, stable string enum — dispatch is never derived
from Python class names. Payloads are the existing canonical ``MarketData`` values; this
module maps each supported kind to its canonical type and back, plus the correctness /
frequency priority class the frozen architecture (DESIGN-REVIEW-2 §18) requires.

No Dhan/provider protocol concepts (response codes, packet lengths, security IDs) appear
here or anywhere in the IPC contract — only broker-neutral canonical types.
"""

from __future__ import annotations

from enum import StrEnum

from app.schemas.market_data import FeedContinuityEvent, MarketReference, Quote, Tick

IpcPayload = Tick | Quote | MarketReference | FeedContinuityEvent


class EventKind(StrEnum):
    """Stable, serialization-safe discriminator for an IPC event payload.

    Values are the wire contract and must never change meaning. New kinds (depth, candle)
    are added as new members; existing members are never repurposed.
    """

    TICK = "tick"
    QUOTE = "quote"
    MARKET_REFERENCE = "market_reference"
    FEED_CONTINUITY = "feed_continuity"


class EventPriority(StrEnum):
    """Architectural delivery class (DESIGN-REVIEW-2 §18).

    Correctness-critical state must never be evicted by a high-frequency backlog; the two
    classes live in different Redis structures downstream. Phase A only classifies.
    """

    CORRECTNESS_CRITICAL = "correctness_critical"
    HIGH_FREQUENCY = "high_frequency"


_KIND_TO_TYPE: dict[EventKind, type[IpcPayload]] = {
    EventKind.TICK: Tick,
    EventKind.QUOTE: Quote,
    EventKind.MARKET_REFERENCE: MarketReference,
    EventKind.FEED_CONTINUITY: FeedContinuityEvent,
}
_TYPE_TO_KIND: dict[type[IpcPayload], EventKind] = {v: k for k, v in _KIND_TO_TYPE.items()}

_PRIORITY: dict[EventKind, EventPriority] = {
    EventKind.MARKET_REFERENCE: EventPriority.CORRECTNESS_CRITICAL,
    EventKind.FEED_CONTINUITY: EventPriority.CORRECTNESS_CRITICAL,
    EventKind.TICK: EventPriority.HIGH_FREQUENCY,
    EventKind.QUOTE: EventPriority.HIGH_FREQUENCY,
}


def event_kind_for(payload: IpcPayload) -> EventKind:
    """Return the stable discriminator for a canonical payload, failing closed."""
    try:
        return _TYPE_TO_KIND[type(payload)]
    except KeyError:
        raise ValueError(f"unsupported IPC payload type: {type(payload).__name__}") from None


def priority_for(kind: EventKind) -> EventPriority:
    """Return the delivery priority class for an event kind."""
    return _PRIORITY[kind]


def encode_payload(payload: IpcPayload) -> str:
    """Serialize a canonical payload to its lossless JSON string (Decimals as strings)."""
    return payload.model_dump_json()


def decode_payload(kind: EventKind, payload_json: str) -> IpcPayload:
    """Reconstruct a canonical payload from its JSON string, dispatching by kind.

    Uses ``model_validate_json`` (not ``model_validate``): strict canonical models accept
    Decimal only from JSON text, so this is the sole lossless round-trip path.
    """
    return _KIND_TO_TYPE[kind].model_validate_json(payload_json)
