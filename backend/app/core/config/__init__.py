"""Configuration package.

Re-exports the settings accessors so callers can simply do::

    from app.core.config import get_settings, Settings
"""

from app.core.config.settings import ConfigurationError, Settings, get_settings

__all__ = ["ConfigurationError", "Settings", "get_settings"]
