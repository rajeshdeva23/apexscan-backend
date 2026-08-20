"""Concrete strategy implementations (P5; ADR-007 D15).

Each concrete strategy lives in its own isolated subpackage under this namespace and
imports only its own subtree plus the read-only shared contracts and canonical
schemas (the import-boundary guard enforces this). Implementations never import one
another, a provider adapter, the Strategy Manager, or a Market-Engine mutation engine.
"""

from __future__ import annotations
