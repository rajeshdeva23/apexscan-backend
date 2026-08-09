"""Event vocabulary and the in-process publish/subscribe seam (docs/03 §3.17).

This package defines the pipeline's event dispatch contract. It depends only on
core and schemas and knows nothing of concrete producers or consumers; domain
event payloads (e.g. MarketContext events) are defined by their owning layer.
"""

from app.events.bus import Event, EventBus, Subscription

__all__ = ["Event", "EventBus", "Subscription"]
