"""Database package.

Owns SQLAlchemy engine/session wiring and the declarative base. This is the
persistence infrastructure layer — repositories depend on it, but it depends
on nothing above it.
"""

from app.database.base import Base
from app.database.session import (
    DatabaseCleanupError,
    DatabaseConnectivityError,
    DatabaseInitializationError,
    DatabaseLifecycle,
    DatabaseLifecycleError,
    DatabaseNotInitializedError,
    database_lifecycle,
    get_session,
)

__all__ = [
    "Base",
    "DatabaseCleanupError",
    "DatabaseConnectivityError",
    "DatabaseInitializationError",
    "DatabaseLifecycle",
    "DatabaseLifecycleError",
    "DatabaseNotInitializedError",
    "database_lifecycle",
    "get_session",
]
