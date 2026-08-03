"""Database package.

Owns SQLAlchemy engine/session wiring and the declarative base. This is the
persistence infrastructure layer — repositories depend on it, but it depends
on nothing above it.
"""

from app.database.base import Base
from app.database.session import async_session_factory, engine, get_session

__all__ = ["Base", "engine", "async_session_factory", "get_session"]
