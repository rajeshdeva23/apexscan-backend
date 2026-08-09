"""Minimal, deterministic, in-process synchronous event bus (docs/03 §14, §3.17).

The bus is a contract, not a technology (docs/09 §15): this implementation is
purely in-process and synchronous — no threads, no asyncio, no Redis, no
persistence, no retry, no background workers. Publishing invokes each matching
subscriber inline, in subscription order, so the same publications always
produce the same, reproducible delivery order (docs/01 §9 forward event chain).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import cast


class Event:
    """Base class for immutable, replay-safe domain events published on the bus."""

    __slots__ = ()


_Handler = Callable[[Event], None]


@dataclass(frozen=True, slots=True)
class Subscription[E: Event]:
    """An opaque, immutable handle returned by :meth:`EventBus.subscribe`.

    Attributes:
        event_type: The exact event type the handler is registered for.
        handler: The callable invoked when a matching event is published.
    """

    event_type: type[E]
    handler: Callable[[E], None]


class EventBus:
    """A typed, synchronous, in-process event bus with deterministic dispatch."""

    def __init__(self) -> None:
        """Create an empty bus with no subscribers."""
        self._subscribers: dict[type[Event], list[_Handler]] = {}

    def subscribe[E: Event](
        self, event_type: type[E], handler: Callable[[E], None]
    ) -> Subscription[E]:
        """Register ``handler`` for events whose exact type is ``event_type``.

        Args:
            event_type: The concrete event class to subscribe to.
            handler: A callable invoked with each published matching event.

        Returns:
            A :class:`Subscription` handle for later :meth:`unsubscribe`.
        """
        self._subscribers.setdefault(event_type, []).append(cast("_Handler", handler))
        return Subscription(event_type=event_type, handler=handler)

    def unsubscribe[E: Event](self, subscription: Subscription[E]) -> None:
        """Remove a previously registered subscription (idempotent).

        Args:
            subscription: The handle returned by :meth:`subscribe`.
        """
        handlers = self._subscribers.get(subscription.event_type)
        if handlers is None:
            return
        target = cast("_Handler", subscription.handler)
        self._subscribers[subscription.event_type] = [h for h in handlers if h is not target]

    def publish(self, event: Event) -> None:
        """Deliver ``event`` synchronously to every subscriber of its exact type.

        Handlers are invoked in subscription order. A snapshot of the handler
        list is taken first, so a handler that subscribes or unsubscribes during
        dispatch does not affect the current publication.

        Args:
            event: The immutable event to deliver.
        """
        for handler in list(self._subscribers.get(type(event), [])):
            handler(event)
