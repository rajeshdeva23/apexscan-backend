"""Durable file-based UniverseSnapshot store (DECOUPLING PHASE E).

Snapshots are versioned, immutable reference/configuration artifacts — not high-frequency market
data — so they are persisted as JSON files, matching the existing ``reference_data/*.json``
convention (sector membership, trading calendar). This survives backend/ingestion/Redis restarts
and process replacement without a database schema change (there are no ORM tables/migrations yet),
and universe versions never reset because they are derived from the persisted files, not memory.

Governance: promotion is explicit and fails closed (via ``validate_promotable``). Version
allocation is monotonic and concurrency-safe through exclusive file creation, so two concurrent
promotions can never receive the same version. Re-promoting identical content is idempotent (no
new version). ``active_for`` selects by effective trading date, never "latest created".
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import ValidationError

from app.market_universe.resolver import ResolutionResult, validate_promotable
from app.market_universe.snapshot import UniverseSnapshot

_MAX_ALLOCATION_RETRIES = 64  # bounded retries under concurrent version allocation

logger = logging.getLogger(__name__)


class SnapshotNotFoundError(LookupError):
    """Raised when a requested snapshot version does not exist."""


@runtime_checkable
class UniverseSnapshotStore(Protocol):
    """Persistence contract for candidate/promoted snapshots (consumers depend on this)."""

    def save_candidate(self, candidate: UniverseSnapshot) -> None:
        """Persist a candidate snapshot (keyed by content hash)."""
        ...

    def get_candidate(self, content_sha256: str) -> UniverseSnapshot | None:
        """Return a persisted candidate by content hash, or None."""
        ...

    def promote(self, result: ResolutionResult, *, effective_at: datetime) -> UniverseSnapshot:
        """Validate and promote a candidate to an effective, versioned snapshot."""
        ...

    def get_by_version(self, universe_version: int) -> UniverseSnapshot:
        """Return the promoted snapshot with ``universe_version`` (raises if absent)."""
        ...

    def active_for(self, trading_date: date) -> UniverseSnapshot | None:
        """Return the promoted snapshot effective for ``trading_date``, or None."""
        ...

    def list_versions(self) -> tuple[int, ...]:
        """Return all promoted universe versions in ascending order."""
        ...


class FileUniverseSnapshotStore:
    """File-backed :class:`UniverseSnapshotStore` (durable JSON artifacts under a root dir)."""

    def __init__(self, root: Path) -> None:
        self._versions_dir = root / "versions"
        self._candidates_dir = root / "candidates"
        self._versions_dir.mkdir(parents=True, exist_ok=True)
        self._candidates_dir.mkdir(parents=True, exist_ok=True)

    def save_candidate(self, candidate: UniverseSnapshot) -> None:
        """Persist a candidate keyed by its content hash (idempotent for identical content)."""
        path = self._candidates_dir / f"{candidate.content_sha256}.json"
        _atomic_write(path, candidate.model_dump_json())

    def get_candidate(self, content_sha256: str) -> UniverseSnapshot | None:
        """Return a persisted candidate by content hash, or None."""
        path = self._candidates_dir / f"{content_sha256}.json"
        if not path.exists():
            return None
        return UniverseSnapshot.model_validate_json(path.read_text(encoding="utf-8"))

    def promote(self, result: ResolutionResult, *, effective_at: datetime) -> UniverseSnapshot:
        """Fail-closed validate, then promote with a monotonic version (idempotent by content).

        If a promoted snapshot with the same content already exists, it is returned unchanged (no
        new version). Otherwise a new version is allocated via exclusive file creation so
        concurrent promotions never collide on a version.
        """
        validate_promotable(result)
        candidate = result.candidate
        existing = self._find_by_content(candidate.content_sha256)
        if existing is not None:
            return existing
        for _attempt in range(_MAX_ALLOCATION_RETRIES):
            version = self._next_version()
            promoted = candidate.promoted_as(version, effective_at)
            if self._link_version(version, promoted):
                return promoted
        raise RuntimeError("exhausted universe-version allocation retries under contention")

    def _link_version(self, version: int, promoted: UniverseSnapshot) -> bool:
        """Atomically publish ``promoted`` as version file; return False if the version is taken.

        Writes the full JSON to a temp file, then ``os.link``s it to the final path. The link is
        an atomic exclusive create of an already-complete file, so a concurrent version scan never
        observes an empty/partial version file (and never reuses a version).
        """
        final = self._versions_dir / f"{version}.json"
        tmp = self._versions_dir / f".{version}.{promoted.content_sha256[:12]}.tmp"
        tmp.write_text(promoted.model_dump_json(), encoding="utf-8")
        try:
            os.link(tmp, final)
        except FileExistsError:
            return False  # concurrent allocation took this version; caller retries
        finally:
            tmp.unlink(missing_ok=True)
        return True

    def get_by_version(self, universe_version: int) -> UniverseSnapshot:
        """Return the promoted snapshot with ``universe_version`` (raises if absent)."""
        path = self._versions_dir / f"{universe_version}.json"
        if not path.exists():
            raise SnapshotNotFoundError(f"no promoted snapshot for version {universe_version}")
        return UniverseSnapshot.model_validate_json(path.read_text(encoding="utf-8"))

    def active_for(self, trading_date: date) -> UniverseSnapshot | None:
        """Return the promoted snapshot with the greatest effective date <= ``trading_date``.

        Never returns a snapshot whose effective trading date is in the future (a candidate
        created today but effective tomorrow is not active today), and correctly holds the prior
        snapshot across weekends/holidays where no new snapshot became effective.
        """
        best: UniverseSnapshot | None = None
        for snapshot in self._iter_promoted():
            if snapshot.trading_date > trading_date:
                continue
            if best is None or (snapshot.trading_date, snapshot.universe_version) > (
                best.trading_date,
                best.universe_version,
            ):
                best = snapshot
        return best

    def list_versions(self) -> tuple[int, ...]:
        """Return all promoted universe versions in ascending order."""
        return tuple(sorted(self._existing_versions()))

    def _existing_versions(self) -> list[int]:
        versions: list[int] = []
        for path in self._versions_dir.glob("*.json"):
            if path.stem.isdigit():  # ignore non-version files so allocation never crashes
                versions.append(int(path.stem))
        return versions

    def _next_version(self) -> int:
        versions = self._existing_versions()
        return max(versions) + 1 if versions else 1

    def _iter_promoted(self) -> list[UniverseSnapshot]:
        """Load all parseable promoted snapshots; skip (and log) any corrupt version file.

        A single corrupt/tampered artifact must not crash every ``active_for`` lookup forever —
        scans return the best valid snapshot rather than raising. An explicit ``get_by_version``
        still surfaces a corrupt file loudly.
        """
        snapshots: list[UniverseSnapshot] = []
        for path in self._versions_dir.glob("*.json"):
            try:
                snapshots.append(
                    UniverseSnapshot.model_validate_json(path.read_text(encoding="utf-8"))
                )
            except (ValidationError, OSError):
                logger.warning("skipping unreadable universe snapshot artifact: %s", path.name)
        return snapshots

    def _find_by_content(self, content_sha256: str) -> UniverseSnapshot | None:
        for snapshot in self._iter_promoted():
            if snapshot.content_sha256 == content_sha256:
                return snapshot
        return None


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (temp file + rename)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
