"""Generic repository base.

Defines the repository pattern seam: services depend on repositories, not on
SQLAlchemy sessions directly. This generic base captures the common shape of
a data-access object bound to an async session and a model type. Concrete
repositories (one per aggregate) will subclass it once models exist.

Phase 1 provides the pattern skeleton only — no queries are implemented.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.database import Base

class BaseRepository[ModelT: Base]:
    """Base class for data-access repositories.

    Args:
        session: Async database session scoped to the current request.
        model: The ORM model type this repository manages.

    Concrete repositories add typed query methods (``get``, ``list``,
    ``add`` …). Kept deliberately minimal until models are introduced.
    """

    def __init__(self, session: AsyncSession, model: type[ModelT]) -> None:
        self._session = session
        self._model = model
