"""Market Intelligence — reusable, read-only upstream context layers (ADR-016).

Bounded contexts here derive cross-instrument intelligence (sector strength, later
stock participation) from canonical market-engine events and publish it for strategies
and the scanner to consume as ranking/context. They never mutate MarketContext, never
open a provider connection, and never place trades. Dependency direction is one-way:

    market pipeline -> market_intelligence -> strategies / scanner

Nothing in ``app.strategies`` or ``app.strategy_manager`` may be imported here.
"""
