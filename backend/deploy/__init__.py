"""ApexScan production deployment tooling (DEPLOY-1).

Pure, dependency-injected decision logic for the manual, SHA-pinned production
deploy pipeline: promotion-target eligibility, bounded post-deploy release
verification, and immutable rollback planning. No app runtime imports, no
network/host I/O at import time, and not packaged into the application wheel
(see ``[tool.setuptools.packages.find]``). The GitHub Actions workflows call the
module CLIs; every non-trivial branch is unit tested with injected fakes.
"""
