"""Broker adapters package.

Each sub-package integrates one broker (``dhan``, ``binance``, ``zerodha``)
behind the shared :class:`~app.adapters.base.BrokerAdapter` interface. Adding
a new broker means adding a new sub-package here — nothing in the core engine
changes. No integration logic is implemented in Phase 1.
"""
