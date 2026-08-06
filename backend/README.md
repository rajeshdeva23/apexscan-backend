# ApexScan Backend

The ApexScan backend is an async FastAPI application. It provides the
application shell, configuration, infrastructure wiring, and health endpoints
(Phases 1–2), plus the broker-neutral Data Provider layer (Phase 3): canonical
market-data contracts, provider lifecycle coordination, and the Dhan adapter
kept behind that boundary. The Market Engine, Strategy Engine, scanner, and
trading logic remain intentionally out of scope.

For setup and validation commands, see the repository-root `README.md`.
