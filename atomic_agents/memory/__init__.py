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
    # Registry
    "register_backend",
    "get_backend",
    # Protocol + dataclasses
    "MemoryBackend",
    "Note",
    "NoteRef",
    "VersionRef",
    "WritePolicy",
    "MemoryStats",
    "StagedMemory",
    # Exceptions
    "BackendNotRegistered",
    "VersionNotFound",
    "StagingNotApplied",
]

# Lazy imports to avoid circular dependency at module load time.
# Callers that import from atomic_agents.memory will trigger these.
def __getattr__(name: str):
    """Lazy attribute resolution for public types and exceptions."""
    _protocol_types = {
        "MemoryBackend", "Note", "NoteRef", "VersionRef",
        "WritePolicy", "MemoryStats", "StagedMemory",
    }
    _exception_names = {"BackendNotRegistered", "VersionNotFound", "StagingNotApplied"}

    if name in _protocol_types:
        from .backend import (
            MemoryBackend, Note, NoteRef, VersionRef,
            WritePolicy, MemoryStats, StagedMemory,
        )
        _locals = {
            "MemoryBackend": MemoryBackend, "Note": Note, "NoteRef": NoteRef,
            "VersionRef": VersionRef, "WritePolicy": WritePolicy,
            "MemoryStats": MemoryStats, "StagedMemory": StagedMemory,
        }
        return _locals[name]

    if name in _exception_names:
        from ..exceptions import (
            BackendNotRegistered, VersionNotFound, StagingNotApplied,
        )
        _locals = {
            "BackendNotRegistered": BackendNotRegistered,
            "VersionNotFound": VersionNotFound,
            "StagingNotApplied": StagingNotApplied,
        }
        return _locals[name]

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
