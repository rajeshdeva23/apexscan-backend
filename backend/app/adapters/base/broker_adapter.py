"""Broker adapter contract.

Defines the abstract interface every broker integration (Dhan, Binance,
Zerodha, …) must implement. The market engine and services depend only on
this abstraction, never on a concrete broker — this is the seam that lets
ApexScan scale to many brokers without touching core logic (Dependency
Inversion).

Phase 1 declares the contract only. No broker behaviour is implemented.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class BrokerAdapter(ABC):
    """Abstract base class for all broker integrations.

    Concrete adapters translate ApexScan's internal calls into a specific
    broker's API (auth, market data, instruments) and normalise responses
    back into internal schemas. Method bodies are intentionally undefined
    here — subclasses provide them.
    """

    #: Human-readable broker identifier, e.g. ``"dhan"``. Set by subclasses.
    name: str

    @abstractmethod
    async def connect(self) -> None:
        """Establish and authenticate a session with the broker."""
        raise NotImplementedError

    @abstractmethod
    async def disconnect(self) -> None:
        """Tear down the broker session and release resources."""
        raise NotImplementedError

    @abstractmethod
    async def is_healthy(self) -> bool:
        """Return whether the broker connection is usable right now."""
        raise NotImplementedError
