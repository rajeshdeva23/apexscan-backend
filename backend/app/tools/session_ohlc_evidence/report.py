"""Deterministic JSON + Markdown rendering of an evidence record, with secret redaction.

The evidence models never carry credentials, but rendering passes every emitted string
through a defensive redactor so an accidental token/URL can never reach an artifact.
"""

from __future__ import annotations

import json
import re

from app.tools.session_ohlc_evidence.models import EvidenceRecord, Verdict

_REDACTED = "<redacted>"
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?i)\b(access[_-]?token|token|authorization|bearer|totp|otp|pin|password)\b"
        r"\s*[:=]\s*[^\s\"']+"
    ),
    re.compile(r"[?&](token|clientId|authType|pin|totp)=[^&\s\"']+"),
    re.compile(r"postgresql(?:\+\w+)?://[^:\s]+:[^@\s]+@\S+"),
    re.compile(r"\bredis://[^@\s]*:[^@\s]+@\S+"),
)


def sanitize_text(text: str) -> str:
    """Redact any credential-bearing substring from a rendered string (defence in depth)."""
    redacted = text
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(_REDACTED, redacted)
    return redacted


def to_json(record: EvidenceRecord, verdict: Verdict) -> str:
    """Render the record + verdict as deterministic, sorted, secret-safe JSON."""
    payload = {
        "record": record.model_dump(mode="json"),
        "verdict": verdict.model_dump(mode="json"),
    }
    return sanitize_text(json.dumps(payload, indent=2, sort_keys=True, default=str))


def to_markdown(record: EvidenceRecord, verdict: Verdict) -> str:
    """Render a concise human-readable summary (secret-safe) from the record + verdict."""
    lines = [
        f"# Current-Session OHLC Authority Evidence — {record.trading_date.isoformat()}",
        "",
        f"- Provider: {record.provider}",
        f"- Source SHA: {record.source_sha}",
        f"- Collector: {record.collector_version} (schema {record.schema_version})",
        f"- Session identity: {record.session_identity}",
        f"- Window: {record.collection_start.isoformat()} → {record.collection_end.isoformat()}",
        f"- Universe: {record.universe_observed}/{record.universe_expected} observed",
        f"- Sample windows: {', '.join(record.sample_windows) or '(none)'}",
        f"- CSOA16 reconnect required: {record.csoa16_required}; oracle available: "
        f"{record.oracle_available}",
        "",
        "## Verdict",
        f"**{verdict.outcome.value.upper()}**",
        "",
        "Reasons:",
        *[f"- {reason}" for reason in verdict.reasons],
        "",
        "## Counts",
        f"- Open mismatches: {verdict.open_mismatches}",
        f"- Monotonicity violations: {verdict.monotonicity_violations}",
        f"- High/Low one-tick drift: {verdict.high_low_drift}",
        f"- High/Low mismatches: {verdict.high_low_mismatch}",
    ]
    late = record.late_start
    reconnect = record.reconnect
    lines += [
        "",
        "## Continuity evidence",
        f"- Late-start observed: {late.observed if late else False}",
        f"- Reconnect observed: {reconnect.observed if reconnect else False}",
    ]
    return sanitize_text("\n".join(lines))
