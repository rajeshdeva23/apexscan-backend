"""Managed async PostgreSQL engine and request-scoped session infrastructure."""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


class DatabaseLifecycleError(RuntimeError):
    """Base error for safe PostgreSQL lifecycle failures."""


class DatabaseNotInitializedError(DatabaseLifecycleError):
    """Raised when PostgreSQL resources are requested before initialization."""


class DatabaseInitializationError(DatabaseLifecycleError):
    """Raised when PostgreSQL lifecycle initialization cannot complete safely."""


class DatabaseConnectivityError(DatabaseLifecycleError):
    """Raised when PostgreSQL cannot be reached by the dependency probe."""


class DatabaseCleanupError(DatabaseLifecycleError):
    """Raised when PostgreSQL resources cannot be cleanly disposed."""


class DatabaseLifecycle:
    """Own one async PostgreSQL engine and its session factory for an app process."""

    def __init__(self) -> None:
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None

    @property
    def is_initialized(self) -> bool:
        """Return whether an engine and session factory are available for use."""
        return self._engine is not None and self._session_factory is not None

    @property
    def engine(self) -> AsyncEngine:
        """Return the initialized engine or raise a safe lifecycle error."""
        if self._engine is None:
            raise DatabaseNotInitializedError(
                "PostgreSQL has not been initialized; initialize the database lifecycle first"
            )
        return self._engine

    @property
    def _factory(self) -> async_sessionmaker[AsyncSession]:
        if self._session_factory is None:
            raise DatabaseNotInitializedError(
                "PostgreSQL has not been initialized; initialize the database lifecycle first"
            )
        return self._session_factory

    async def initialize(self, database_url: str, *, echo: bool = False) -> None:
        """Create managed engine resources without opening application connections.

        Args:
            database_url: Validated async PostgreSQL connection URL.
            echo: Whether SQLAlchemy should echo SQL for a development runtime.

        Raises:
            DatabaseInitializationError: If construction or partial cleanup fails.
        """
        if self.is_initialized:
            return

        engine: AsyncEngine | None = None
        try:
            engine = create_async_engine(
                database_url,
                echo=echo,
                pool_pre_ping=True,
                future=True,
            )
            session_factory = async_sessionmaker(
                bind=engine,
                class_=AsyncSession,
                expire_on_commit=False,
                autoflush=False,
            )
        except Exception as error:
            if engine is not None:
                try:
                    await engine.dispose()
                except Exception as cleanup_error:
                    raise DatabaseInitializationError(
                        "PostgreSQL initialization failed and partial resource cleanup failed; "
                        "retry initialization after verifying the database configuration"
                    ) from cleanup_error
            raise DatabaseInitializationError(
                "PostgreSQL initialization failed; verify the database configuration"
            ) from error

        self._engine = engine
        self._session_factory = session_factory

    async def verify_connectivity(self) -> None:
        """Verify PostgreSQL using a dependency-only ``SELECT 1`` probe.

        Raises:
            DatabaseNotInitializedError: If called before initialization.
            DatabaseConnectivityError: If PostgreSQL cannot complete the probe.
        """
        engine = self.engine
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except Exception as error:
            raise DatabaseConnectivityError(
                "PostgreSQL connectivity verification failed; verify service availability"
            ) from error

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Yield one request-scoped session, rolling back errors and always closing it.

        Successful callers retain explicit commit ownership; this provider prevents
        transaction leakage when a caller raises or is cancelled.
        """
        session = self._factory()
        try:
            yield session
        except BaseException:
            try:
                await session.rollback()
            finally:
                await session.close()
            raise
        else:
            await session.close()

    async def dispose(self) -> None:
        """Dispose PostgreSQL resources safely and idempotently.

        State is cleared before disposing so an unsuccessful cleanup cannot leave
        a stale engine cached for a later lifecycle retry.
        """
        engine = self._engine
        self._engine = None
        self._session_factory = None
        if engine is None:
            return

        try:
            await engine.dispose()
        except Exception as error:
            raise DatabaseCleanupError(
                "PostgreSQL cleanup failed; verify the database connection state"
            ) from error


database_lifecycle = DatabaseLifecycle()


async def get_session() -> AsyncGenerator[AsyncSession]:
    """Yield a managed request-scoped PostgreSQL session for dependency injection."""
    async with database_lifecycle.session() as session:
        yield session
