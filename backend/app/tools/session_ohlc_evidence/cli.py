"""CLI for the current-session OHLC authority evidence tool: collect (live) / evaluate (offline).

``evaluate`` is pure and network-free: it re-derives the verdict from a captured JSON record,
so a verdict is fully reproducible from evidence alone. ``collect`` runs the read-only live
collector (R4B only) and writes the JSON+Markdown artifacts. Neither mode flips any authority
capability.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from app.core.config import get_settings
from app.tools.session_ohlc_evidence.collect import run_collect
from app.tools.session_ohlc_evidence.evaluate import evaluate_record
from app.tools.session_ohlc_evidence.models import EvidenceRecord, Verdict
from app.tools.session_ohlc_evidence.report import to_json, to_markdown


def _load_record(path: Path) -> EvidenceRecord:
    """Load an EvidenceRecord from a captured JSON file (accepts a wrapped {record: …})."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    record_data = payload.get("record", payload) if isinstance(payload, dict) else payload
    return EvidenceRecord.model_validate(record_data)


def _write_outputs(out_stem: Path, record: EvidenceRecord, verdict: Verdict) -> None:
    out_stem.with_suffix(".json").write_text(to_json(record, verdict), encoding="utf-8")
    out_stem.with_suffix(".md").write_text(to_markdown(record, verdict), encoding="utf-8")


def _cmd_evaluate(args: argparse.Namespace) -> int:
    record = _load_record(Path(args.input))
    verdict = evaluate_record(record)
    if args.out:
        _write_outputs(Path(args.out), record, verdict)
    print(to_markdown(record, verdict))
    return 0


def _cmd_collect(args: argparse.Namespace) -> int:
    settings = get_settings()
    record = asyncio.run(
        run_collect(
            settings,
            windows=tuple(args.windows),
            trading_date=date.fromisoformat(args.trading_date),
            session_identity=args.session_identity,
            source_sha=args.source_sha,
            tick_size=Decimal(args.tick_size),
            per_window_seconds=args.per_window_seconds,
        )
    )
    verdict = evaluate_record(record)
    stem = Path(args.out or f"session_ohlc_evidence_{datetime.now(UTC):%Y%m%dT%H%M%SZ}")
    _write_outputs(stem, record, verdict)
    print(to_markdown(record, verdict))
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser with ``collect`` and ``evaluate`` subcommands."""
    parser = argparse.ArgumentParser(prog="session_ohlc_evidence")
    sub = parser.add_subparsers(dest="command", required=True)

    ev = sub.add_parser(
        "evaluate", help="Re-derive the verdict from a captured JSON record (offline)."
    )
    ev.add_argument("input", help="Path to a captured evidence JSON file.")
    ev.add_argument("--out", help="Output stem for .json/.md (optional).")
    ev.set_defaults(func=_cmd_evaluate)

    co = sub.add_parser("collect", help="Run the read-only live collector (R4B live session only).")
    co.add_argument("--windows", nargs="+", default=["early", "mid", "late"])
    co.add_argument("--trading-date", dest="trading_date", required=True)
    co.add_argument("--session-identity", dest="session_identity", required=True)
    co.add_argument("--source-sha", dest="source_sha", required=True)
    co.add_argument("--tick-size", dest="tick_size", default="0.05")
    co.add_argument("--per-window-seconds", dest="per_window_seconds", type=float, default=30.0)
    co.add_argument("--out", help="Output stem for .json/.md (optional).")
    co.set_defaults(func=_cmd_collect)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point: parse args and dispatch to the selected subcommand."""
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
