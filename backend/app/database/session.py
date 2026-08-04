"""Async database engine and session management.

Owns the SQLAlchemy 2.0 async engine and session factory. Nothing here knows
about specific tables — models live in :mod:`app.models` and data access goes
through repositories. Sessions are provided to the request lifecycle via the
:func:`get_session` dependency so callers never construct sessions manually
or hold global connections.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings


def create_engine() -> AsyncEngine:
    """Create the async SQLAlchemy engine from application settings.

    ``pool_pre_ping`` guards against stale connections after DB restarts —
    important for a long-running scanner process.
    """
    settings = get_settings()
    return create_async_engine(
        settings.database_url,
        echo=settings.app_debug,
        pool_pre_ping=True,
        future=True,
    )


# Single engine per process. The session factory binds to it.
engine: AsyncEngine = create_engine()

async_session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_session() -> AsyncGenerator[AsyncSession]:
    """Yield a database session scoped to a single request.

    FastAPI dependency. The session is committed by the caller (service layer)
    and always closed here, even on error.
    """
    async with async_session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
