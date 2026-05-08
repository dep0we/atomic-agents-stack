"""atomic_agents.memory — memory backend registry and built-in backends.

Usage:
    from atomic_agents.memory import get_backend, register_backend
    from atomic_agents.memory.filesystem import FilesystemBackend

The registry maps backend name strings to backend classes. External packages
register their backends at import time via register_backend().

Built-in backends:
    "filesystem"  →  FilesystemBackend (default)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Type

if TYPE_CHECKING:
    from .backend import MemoryBackend

# Registry: name → class
_REGISTRY: dict[str, type] = {}


def register_backend(name: str, cls: type) -> None:
    """Register a backend class under a name.

    Call this at import time in your backend package's __init__.py.
    The "filesystem" backend is pre-registered by this module.

    Args:
        name: short identifier (e.g., "sqlite", "postgres")
        cls:  class that implements the MemoryBackend protocol
    """
    _REGISTRY[name] = cls


def get_backend(name: str) -> type:
    """Return the registered backend class for a name.

    Raises:
        BackendNotRegistered if the name is not in the registry.
    """
    from ..exceptions import BackendNotRegistered
    if name not in _REGISTRY:
        raise BackendNotRegistered(
            f"No MemoryBackend registered under {name!r}. "
            f"Available: {list(_REGISTRY.keys())}"
        )
    return _REGISTRY[name]


# Register the built-in filesystem backend
def _register_defaults() -> None:
    from .filesystem import FilesystemBackend
    register_backend("filesystem", FilesystemBackend)


_register_defaults()

__all__ = [
    "register_backend",
    "get_backend",
]
