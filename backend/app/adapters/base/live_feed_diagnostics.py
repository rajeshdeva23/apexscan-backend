"""Broker-neutral live-feed decode diagnostics (SECTOR-VIEW-1D).

Bounded, read-only counters that characterize what a provider's live WebSocket frames decode
into — used to root-cause discarded ("malformed") frames without logging binary payloads. The
value type is broker-neutral (plain counts keyed by integer response codes and short strings);
the provider-specific counting lives inside each adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class LiveFeedDecodeDiagnostics:
    """An immutable snapshot of live-frame decode outcomes (bounded cardinality).

    ``frames_by_response_code`` / ``failures_by_response_code`` are keyed by the provider's
    single-byte response code (≤256 keys). ``failures_by_length_bucket`` is keyed by a small
    fixed set of length labels. No instrument-level or unbounded history is retained.
    """

    websocket_messages_total: int = 0
    framing_failures: int = 0
    frames_total: int = 0
    decoded_total: int = 0
    failures_total: int = 0
    normalization_failures: int = 0
    unsupported_code_failures: int = 0
    unknown_reference_failures: int = 0
    non_binary_frames: int = 0
    previous_close_frames_seen: int = 0
    previous_close_frames_failed: int = 0
    frames_by_response_code: dict[int, int] = field(default_factory=dict)
    failures_by_response_code: dict[int, int] = field(default_factory=dict)
    failures_by_length_bucket: dict[str, int] = field(default_factory=dict)


@runtime_checkable
class LiveFeedDiagnosticsSource(Protocol):
    """Read-only seam exposing a provider's bounded live-feed decode diagnostics."""

    def live_feed_decode_diagnostics(self) -> LiveFeedDecodeDiagnostics | None:
        """Return the current decode-diagnostics snapshot, or ``None`` when unavailable."""
        ...
