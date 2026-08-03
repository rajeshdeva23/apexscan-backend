"""Repositories package.

Data-access layer implementing the Repository Pattern. Each repository
encapsulates persistence for one aggregate and exposes intention-revealing
methods to the service layer, hiding SQLAlchemy details. Exposes the generic
:class:`BaseRepository`.
"""

from app.repositories.base import BaseRepository

__all__ = ["BaseRepository"]
