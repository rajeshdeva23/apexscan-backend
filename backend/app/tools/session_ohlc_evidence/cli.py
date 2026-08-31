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
from app.tools.session_ohlc_evidence.collect import (
    run_capture_late_start,
    run_capture_reconnect,
    run_collect,
)
from app.tools.session_ohlc_evidence.evaluate import combine_records, evaluate_record
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


def _cmd_combine(args: argparse.Namespace) -> int:
    records = [_load_record(Path(path)) for path in args.inputs]
    record = combine_records(records)
    verdict = evaluate_record(record)
    if args.out:
        _write_outputs(Path(args.out), record, verdict)
    print(to_markdown(record, verdict))
    return 0


def _cmd_capture_late_start(args: argparse.Namespace) -> int:
    settings = get_settings()
    record = asyncio.run(
        run_capture_late_start(
            settings,
            symbol=args.symbol,
            trading_date=date.fromisoformat(args.trading_date),
            session_identity=args.session_identity,
            source_sha=args.source_sha,
            deadline_seconds=args.deadline_seconds,
        )
    )
    stem = Path(args.out or f"late_start_{datetime.now(UTC):%Y%m%dT%H%M%SZ}")
    _write_outputs(stem, record, evaluate_record(record))
    print(to_markdown(record, evaluate_record(record)))
    return 0


def _cmd_capture_reconnect(args: argparse.Namespace) -> int:
    settings = get_settings()
    record = asyncio.run(
        run_capture_reconnect(
            settings,
            symbol=args.symbol,
            trading_date=date.fromisoformat(args.trading_date),
            session_identity=args.session_identity,
            source_sha=args.source_sha,
            deadline_seconds=args.deadline_seconds,
        )
    )
    stem = Path(args.out or f"reconnect_{datetime.now(UTC):%Y%m%dT%H%M%SZ}")
    _write_outputs(stem, record, evaluate_record(record))
    print(to_markdown(record, evaluate_record(record)))
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
            tick_size=Decimal(args.tick_size) if args.tick_size is not None else None,
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

    cb = sub.add_parser(
        "combine",
        help="Merge per-window partial records for one session, then re-derive the verdict.",
    )
    cb.add_argument(
        "inputs", nargs="+", help="Paths to captured evidence JSON files (one session)."
    )
    cb.add_argument("--out", help="Output stem for .json/.md (optional).")
    cb.set_defaults(func=_cmd_combine)

    for name, func, helptext in (
        (
            "capture-late-start",
            _cmd_capture_late_start,
            "Diagnostic late-start capture for one instrument (REST prior vs first WS tick).",
        ),
        (
            "capture-reconnect",
            _cmd_capture_reconnect,
            "Diagnostic reconnect capture for one instrument (pre/post across a fresh socket).",
        ),
    ):
        cap = sub.add_parser(name, help=helptext)
        cap.add_argument("--symbol", required=True, help="Instrument symbol in the live universe.")
        cap.add_argument("--trading-date", dest="trading_date", required=True)
        cap.add_argument("--session-identity", dest="session_identity", required=True)
        cap.add_argument("--source-sha", dest="source_sha", required=True)
        cap.add_argument("--deadline-seconds", dest="deadline_seconds", type=float, default=60.0)
        cap.add_argument("--out", help="Output stem for .json/.md (optional).")
        cap.set_defaults(func=func)

    co = sub.add_parser("collect", help="Run the read-only live collector (R4B live session only).")
    co.add_argument("--windows", nargs="+", default=["early", "mid", "late"])
    co.add_argument("--trading-date", dest="trading_date", required=True)
    co.add_argument("--session-identity", dest="session_identity", required=True)
    co.add_argument("--source-sha", dest="source_sha", required=True)
    co.add_argument(
        "--tick-size",
        dest="tick_size",
        default=None,
        help="Explicit per-run high/low drift tick size; omit to require exact high/low "
        "(unknown-tick differences are INDETERMINATE, never silently DRIFT).",
    )
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
