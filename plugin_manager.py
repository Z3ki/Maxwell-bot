"""Compatibility façade for Maxwell's plugin runtime.

The implementation lives in ``maxwell_core.plugins``. Import from this
module the same way as before; new code may import the core package
directly.
"""

from maxwell_core.plugins.context import (  # noqa: F401
    ALLOWED_EVENTS,
    MIN_JOB_INTERVAL_SECONDS,
    PluginContext,
)
from maxwell_core.plugins.manager import PluginManager  # noqa: F401

__all__ = [
    "ALLOWED_EVENTS",
    "MIN_JOB_INTERVAL_SECONDS",
    "PluginContext",
    "PluginManager",
]
