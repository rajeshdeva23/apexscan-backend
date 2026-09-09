"""Bounded at-least-once deduplication reference (PHASE A).

Redis Streams deliver at-least-once (redelivery via ``XAUTOCLAIM``), so the future consumer
must drop replays. Dedup identity is ``(producer_id, producer_epoch, producer_sequence)``.
This is the reference/test deduplicator only — it is NOT wired into the production path.
"""

from __future__ import annotations

from collections import OrderedDict

from app.market_ipc.envelope import MarketEventEnvelope, ProducerEventIdentity


class BoundedDeduplicator:
    """A fixed-capacity, insertion-ordered seen-set of producer event identities.

    ponytail: recency-window LRU, not a perfect set — it only detects a replay while the
    identity is still within the last ``max_entries`` seen. That matches Redis at-least-once
    redelivery (replays arrive close behind); raise ``max_entries`` if the claim window widens.
    """

    def __init__(self, max_entries: int) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self._max_entries = max_entries
        self._seen: OrderedDict[ProducerEventIdentity, None] = OrderedDict()

    def is_duplicate(self, identity: ProducerEventIdentity) -> bool:
        """Return whether ``identity`` was already recorded; record it if new."""
        if identity in self._seen:
            self._seen.move_to_end(identity)
            return True
        self._seen[identity] = None
        if len(self._seen) > self._max_entries:
            self._seen.popitem(last=False)
        return False

    def contains(self, identity: ProducerEventIdentity) -> bool:
        """Read-only membership test — does NOT record (used before shadow application).

        The consumer must not mark an identity seen until its shadow application succeeds;
        otherwise a redelivery after a failed apply would be discarded (silent loss). Pair
        this with :meth:`record` committed only after successful terminal handling.
        """
        return identity in self._seen

    def record(self, identity: ProducerEventIdentity) -> None:
        """Commit ``identity`` as seen (after successful application), evicting the oldest."""
        self._seen[identity] = None
        self._seen.move_to_end(identity)
        if len(self._seen) > self._max_entries:
            self._seen.popitem(last=False)

    def seen_envelope(self, envelope: MarketEventEnvelope) -> bool:
        """Convenience: dedup directly from an envelope's identity."""
        return self.is_duplicate(ProducerEventIdentity.from_envelope(envelope))

    def __len__(self) -> int:
        return len(self._seen)
