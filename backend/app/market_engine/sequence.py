"""Deterministic, injectable sequence abstraction for the Market Engine.

Ordering does not depend on arrival timing or any module-level mutable integer.
A sequence generator is owned and injected, so replaying the same ordered inputs
through a fresh generator reproduces identical sequence values — the basis of
deterministic replay (docs/06 §1.4, §12.2, §28.8; docs/09 §9.1).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class SequenceGenerator(Protocol):
    """A source of strictly increasing integer sequence values."""

    def next_value(self) -> int:
        """Return the next sequence value (strictly greater than the previous)."""
        ...


@dataclass(slots=True)
class MonotonicSequence:
    """A deterministic monotonic sequence owned by its holder (never a global).

    Values increase by exactly one per call, starting from ``start + 1``. A fresh
    instance driven by the same call order yields the same values, which is what
    makes replay reproducible.
    """

    _current: int = 0

    def next_value(self) -> int:
        """Advance and return the next sequence value."""
        self._current += 1
        return self._current

    @property
    def current(self) -> int:
        """Return the most recently issued value (0 before the first call)."""
        return self._current
