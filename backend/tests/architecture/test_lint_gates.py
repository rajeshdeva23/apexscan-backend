"""Prove the configured Ruff gates (C901 complexity, D docstrings) actually fire.

These tests feed synthetic source to Ruff via ``--stdin-filename`` so the real
``pyproject.toml`` configuration is exercised (max-complexity=8, google
docstrings, and the ``tests/**`` scope exemption) without writing any failing
file into the repository.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_BACKEND_ROOT = Path(__file__).parents[2]


def _ruff_executable() -> str | None:
    candidate = Path(sys.executable).with_name("ruff")
    if candidate.exists():
        return str(candidate)
    return shutil.which("ruff")


_RUFF = _ruff_executable()
pytestmark = pytest.mark.skipif(_RUFF is None, reason="ruff executable not available")

# A function whose cyclomatic complexity (11) exceeds the configured ceiling (8).
_COMPLEX_FUNCTION = (
    '"""Probe module."""\n\n\n'
    "def probe(value: int) -> int:\n"
    '    """Probe."""\n'
    "    total = 0\n"
    + "".join(f"    if value == {n}:\n        total += 1\n" for n in range(10))
    + "    return total\n"
)

# A public function that is missing its required docstring.
_UNDOCUMENTED_FUNCTION = (
    '"""Probe module."""\n\n\ndef compute(value: int) -> int:\n    return value\n'
)

# Compliant: module + function docstrings, trivial body.
_CLEAN_FUNCTION = (
    '"""Probe module."""\n\n\n'
    "def compute(value: int) -> int:\n"
    '    """Return the value."""\n'
    "    return value\n"
)


def _ruff_check(source: str, *, filename: str) -> subprocess.CompletedProcess[str]:
    assert _RUFF is not None
    return subprocess.run(
        [_RUFF, "check", "--stdin-filename", filename, "--output-format", "concise", "-"],
        input=source,
        cwd=_BACKEND_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_complexity_ceiling_is_enforced_on_engine_code() -> None:
    """A complexity-11 function under app/ is rejected by the C901 gate."""
    result = _ruff_check(_COMPLEX_FUNCTION, filename="app/market_engine/probe.py")
    assert result.returncode != 0
    assert "C901" in result.stdout


def test_public_docstring_is_enforced_on_engine_code() -> None:
    """An undocumented public function under app/ is rejected by the D gate."""
    result = _ruff_check(_UNDOCUMENTED_FUNCTION, filename="app/market_engine/probe.py")
    assert result.returncode != 0
    assert "D103" in result.stdout


def test_compliant_engine_code_passes_both_gates() -> None:
    """A simple, documented function under app/ passes cleanly."""
    result = _ruff_check(_CLEAN_FUNCTION, filename="app/market_engine/probe.py")
    assert result.returncode == 0, result.stdout


def test_tests_are_exempt_from_docstring_and_complexity_gates() -> None:
    """The same complex, undocumented code under tests/ is allowed by the scope exemption."""
    result = _ruff_check(_COMPLEX_FUNCTION, filename="tests/probe.py")
    assert result.returncode == 0, result.stdout
    undocumented = _ruff_check(_UNDOCUMENTED_FUNCTION, filename="tests/probe.py")
    assert undocumented.returncode == 0, undocumented.stdout
