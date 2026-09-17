"""Two-service pre-activation preflight for the decoupled market path (H9C-P2, Gate F/I).

Before a governed H9C ownership/authority activation, an operator must prove that the two
application processes — ``apexscan-backend`` (``OwnerRole.BACKEND``) and
``apexscan-market-ingestion`` (``OwnerRole.INGESTION``) — will compete on ONE shared fenced
ownership domain from the SAME immutable image. Two independent failure modes make a cutover
unsafe and are impossible to see from a single service:

* **Different revisions.** If the two services run different image digests, the interlock code on
  one side may not match the other → a stale contender could bypass the lease.
* **Mismatched ownership config.** If ``redis_url`` / ``market_ownership_enabled`` / the lease
  timings differ, the two processes either target different Redis authorities or one runs with
  ownership OFF and connects to Dhan unfenced — defeating single-owner.

This module is the explicit, offline preflight for both. It is a pure comparison over two
snapshots the operator gathers from ``docker compose config`` / ``ps`` — it performs NO Redis,
Docker, AWS, or Dhan I/O, and never prints secret values (it compares a fixed set of
ownership-critical keys and reports only key names and equal/not-equal, never the credential env).
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field

# The ownership-critical env keys that MUST be identical across both processes for a safe cutover.
# Deliberately excludes every secret (Dhan creds, DB/Redis passwords): only the shared ownership
# domain + Redis endpoint + lease timings, none of which are credentials.
OWNERSHIP_PARITY_KEYS: tuple[str, ...] = (
    "REDIS_URL",
    "MARKET_OWNERSHIP_ENABLED",
    "MARKET_OWNERSHIP_LEASE_TTL_SECONDS",
    "MARKET_OWNERSHIP_RENEWAL_INTERVAL_SECONDS",
)

# Flags whose truthiness constitutes AUTHORITY ACTIVATION. A plain (pre-activation) deployment must
# leave all of them OFF; the preflight refuses to pass if any is on.
AUTHORITY_FLAG_KEYS: tuple[str, ...] = (
    "MARKET_OWNERSHIP_ENABLED",
    "IPC_AUTHORITATIVE_ENABLED",
)

# Recognized OFF tokens (mirrors pydantic's bool coercion: false/f/no/n/off/0, plus unset/empty).
# Anything NOT in this set — including pydantic's truthy spellings true/t/yes/y/on/1 AND any
# unrecognized value — is treated as ON, so the authority-off gate can never false-pass an
# activation it does not understand (fail-safe direction for a safety gate).
_FALSY = frozenset({"0", "false", "f", "no", "n", "off", ""})


@dataclass(frozen=True)
class PreflightResult:
    """Bounded, secret-free preflight verdict."""

    ok: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)

    def with_reason(self, reason: str) -> PreflightResult:
        """Return a failing result that also carries ``reason`` (never mutates in place)."""
        return PreflightResult(ok=False, reasons=(*self.reasons, reason))


def _is_truthy(value: str | None) -> bool:
    """Whether an env value activates a flag; ON for anything not an explicit OFF token."""
    return value is not None and value.strip().lower() not in _FALSY


def verify_same_revision(backend_image: str, ingestion_image: str) -> PreflightResult:
    """Both application services must run the exact same immutable image reference."""
    if not backend_image or not ingestion_image:
        return PreflightResult(False, ("a service image reference is empty",))
    if backend_image != ingestion_image:
        return PreflightResult(
            False,
            (f"image mismatch: backend={backend_image!r} != ingestion={ingestion_image!r}",),
        )
    return PreflightResult(True)


def verify_ownership_config_parity(
    backend_env: Mapping[str, str], ingestion_env: Mapping[str, str]
) -> PreflightResult:
    """Every ownership-critical key must be present and identical on both processes."""
    reasons: list[str] = []
    for key in OWNERSHIP_PARITY_KEYS:
        b = backend_env.get(key)
        i = ingestion_env.get(key)
        if b is None or i is None:
            reasons.append(f"{key} missing on {'backend' if b is None else 'ingestion'}")
        elif b != i:
            # Report the KEY only, never the value — REDIS_URL may embed a password.
            reasons.append(f"{key} differs between backend and ingestion")
    return PreflightResult(not reasons, tuple(reasons))


def verify_authority_off(env: Mapping[str, str], *, service: str) -> PreflightResult:
    """A pre-activation deployment must leave every authority-activation flag OFF."""
    on = [key for key in AUTHORITY_FLAG_KEYS if _is_truthy(env.get(key))]
    if on:
        return PreflightResult(False, tuple(f"{service}: {key} is ON (activation)" for key in on))
    return PreflightResult(True)


def two_service_preflight(
    *,
    backend_image: str,
    ingestion_image: str,
    backend_env: Mapping[str, str],
    ingestion_env: Mapping[str, str],
) -> PreflightResult:
    """Compose the three checks into one pass/fail verdict (all reasons aggregated)."""
    reasons: list[str] = []
    for result in (
        verify_same_revision(backend_image, ingestion_image),
        verify_ownership_config_parity(backend_env, ingestion_env),
        verify_authority_off(backend_env, service="backend"),
        verify_authority_off(ingestion_env, service="ingestion"),
    ):
        reasons.extend(result.reasons)
    return PreflightResult(not reasons, tuple(reasons))


def _run_cli(raw: str) -> int:
    """Read a JSON snapshot from stdin and print a secret-free verdict. Returns an exit code.

    Snapshot shape (produced by the operator from ``docker compose config`` / ``ps`` — this module
    never reads it itself):
    ``{"backend": {"image": "...", "env": {...}}, "ingestion": {"image": "...", "env": {...}}}``
    """
    try:
        snapshot = json.loads(raw)
        backend = snapshot["backend"]
        ingestion = snapshot["ingestion"]
        result = two_service_preflight(
            backend_image=backend["image"],
            ingestion_image=ingestion["image"],
            backend_env=backend["env"],
            ingestion_env=ingestion["env"],
        )
    except (ValueError, KeyError, TypeError) as error:
        print(f"PREFLIGHT ERROR: malformed snapshot ({type(error).__name__})", file=sys.stderr)
        return 2
    if result.ok:
        print("PREFLIGHT PASS: same revision, ownership config parity, authority OFF")
        return 0
    print("PREFLIGHT FAIL:", file=sys.stderr)
    for reason in result.reasons:
        print(f"  - {reason}", file=sys.stderr)
    return 1


if __name__ == "__main__":  # pragma: no cover - thin CLI shell
    sys.exit(_run_cli(sys.stdin.read()))
