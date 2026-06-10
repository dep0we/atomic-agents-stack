"""SecretBackend Protocol and registry (spec/38, issue #340).

Package name is secret_backend/ (not secrets/) to avoid shadowing the Python
stdlib secrets module (cryptographic random generation). Mirrors mcp_registry/
naming convention (not mcp/).

This package establishes the secret-resolution abstraction in the
protocol-pattern series alongside MemoryBackend (#57), LLMBackend (#87),
JudgeBackend (#112), LockBackend (#60), LogBackend (#61),
AgentProfileBackend (#63), ToolRegistryBackend (#64), MandateBackend (#124),
PolicyBackend (#89), PersonaBackend (#62), CorpusBackend (#65), and
MCPServerRegistryBackend (#201).
See ``docs/spec/38-secret-backend.md`` (LOCKED at PR 2) for the prose contract.

Public surface:

    from atomic_agents.secret_backend import (
        # Protocol contract
        SecretBackend,
        # Exception classes
        SecretError,
        SecretNotFound,
        SecretBackendNotRegistered,
        # Canonical types
        SecretRef,
        SecretCapabilities,
        # Reference impl
        FilesystemSecretBackend,
        # Registry
        register_secret_backend,
        get_secret_backend,
        list_secret_backends,
        # Operator-config factory
        get_default_secret_backend,
    )

The registry is a process-local dict keyed by ``backend_id`` (a short
operator-pin string like ``"filesystem"`` or ``"gcp"``). Like the other
backend registries it stores backend *classes*, not instances -- secret
backends are stateless and constructed on demand.

Thread-safety: registration is expected at import time (one-shot); reads
are safe from any thread. No lock needed under that usage.
"""

from __future__ import annotations

import logging
import os
import re

from .backend import (
    SecretBackend,
    SecretBackendNotRegistered,
    SecretError,
    SecretNotFound,
    _validate_key,
)
from .filesystem import FilesystemSecretBackend
from .types import SecretCapabilities, SecretRef

_logger = logging.getLogger(__name__)

__all__ = [
    # Protocol
    "SecretBackend",
    # Exception classes
    "SecretError",
    "SecretNotFound",
    "SecretBackendNotRegistered",
    # Canonical types
    "SecretRef",
    "SecretCapabilities",
    # Reference implementations
    "FilesystemSecretBackend",
    # Registry
    "register_secret_backend",
    "get_secret_backend",
    "list_secret_backends",
    # Operator-config factory
    "get_default_secret_backend",
    # Internal util (re-exported so callers don't dig into backend.py)
    "_validate_key",
]


# Process-local registry: backend_id -> backend class. Backend classes
# (not instances) because the secret backend is currently stateless and
# callers may want to construct it per-call. The registry's job is the
# operator-pin lookup that maps ``"filesystem"`` -> ``FilesystemSecretBackend``.
_registry: dict[str, type] = {}


def register_secret_backend(backend_id: str, cls: type) -> None:
    """Register a ``SecretBackend`` implementation under ``backend_id``.

    Typically called once at module-import time from each backend's package.
    The default ``"filesystem"`` registration happens at the bottom of this
    file.

    Re-registering the same ``backend_id`` replaces the existing binding and
    logs at DEBUG -- intentional. Operators occasionally want to swap in a
    wrapper backend without first unregistering the original.
    """
    if backend_id in _registry:
        _logger.debug(
            "replacing registered secret backend for backend_id=%r",
            backend_id,
        )
    _registry[backend_id] = cls


def get_secret_backend(backend_id: str) -> type:
    """Return the registered backend class for ``backend_id``.

    Raises ``SecretBackendNotRegistered`` when the id is not in the registry.
    The caller instantiates the returned class with any required constructor
    args (``FilesystemSecretBackend`` takes no args).
    """
    if backend_id not in _registry:
        safe_id = _redact_for_error_message(backend_id)
        raise SecretBackendNotRegistered(
            f"No SecretBackend registered under {safe_id!r}. "
            f"Available: {list_secret_backends()}. "
            f"Unset ATOMIC_AGENTS_SECRET_BACKEND to use the filesystem default."
        )
    return _registry[backend_id]


def list_secret_backends() -> list[str]:
    """Return registered backend_ids in lexicographic order."""
    return sorted(_registry.keys())


# Register the built-in filesystem backend at import time. GCP backend
# is constructed on demand via get_default_secret_backend() -- it is NOT
# registered here because its constructor requires a URL argument.
register_secret_backend("filesystem", FilesystemSecretBackend)


def get_default_secret_backend() -> SecretBackend:
    """Return the operator-pinned SecretBackend instance.

    Reads ``ATOMIC_AGENTS_SECRET_BACKEND`` from the environment (default
    ``"filesystem"``). For non-filesystem backends, reads
    ``ATOMIC_AGENTS_SECRET_BACKEND_URL`` for the connection URL. The env var
    name is intentionally generic so future GCP / SaaS backends plug in via
    the same key without operators having to relearn the env vocabulary.

    An empty string (or whitespace-only) value for
    ``ATOMIC_AGENTS_SECRET_BACKEND`` is treated as "not set" and falls back
    to the filesystem default. This guards against shell
    ``export ATOMIC_AGENTS_SECRET_BACKEND=`` accidents.

    The ``ATOMIC_AGENTS_SECRET_BACKEND_URL`` companion env var is required when
    ``ATOMIC_AGENTS_SECRET_BACKEND=gcp``: the ``gcp`` branch constructs a
    ``GCPSecretManagerBackend`` from it via ``make_gcp_secret_backend_from_url``
    (format ``projects/<project_id>/secrets``), raising
    ``SecretBackendNotRegistered`` if the URL is unset or the ``[gcp]`` extra is
    not installed. Backend ids: ``"filesystem"`` (shipped), ``"gcp"`` (shipped).

    See spec/38 §"Operator surface" for the full env-var reference.
    """
    raw_backend_id = os.environ.get("ATOMIC_AGENTS_SECRET_BACKEND", "").strip().lower()

    # Empty string treated as "not set"; falls through to filesystem default.
    if not raw_backend_id:
        raw_backend_id = "filesystem"

    if raw_backend_id == "filesystem":
        # Consult the registry so register_secret_backend("filesystem", …)
        # overrides take effect at runtime. Falls back to FilesystemSecretBackend
        # if the registry entry has been cleared (e.g. in tests).
        cls = _registry.get("filesystem", FilesystemSecretBackend)
        return cls()

    elif raw_backend_id == "gcp":
        # Lazy import: filesystem operators do not pay the google-cloud-secret-manager
        # import cost. NOTE: the SDK itself is NOT imported at gcp module level --
        # GCPSecretManagerBackend.__init__ does the SDK presence check. So the
        # ``from .gcp import ...`` below only fails if the gcp module is itself
        # broken/absent (rare); the real missing-[gcp]-extra ImportError fires
        # from make_gcp_secret_backend_from_url() below. BOTH sites are wrapped
        # so a missing SDK surfaces as SecretBackendNotRegistered (a SecretError
        # subclass) and never escapes as a raw ImportError. This honors the
        # fail-closed contract (callers catching SecretError see a clean error)
        # and gives doctor's `except SecretError` branch the curated [gcp]-extra
        # fix-hint. Per MEMORY.md feedback_fail_closed_catches_base_error_class.
        try:
            from .gcp import make_gcp_secret_backend_from_url
        except ImportError as e:
            raise SecretBackendNotRegistered(
                "ATOMIC_AGENTS_SECRET_BACKEND=gcp requires the [gcp] extra. "
                "Install: uv add 'atomic-agents-stack[gcp]'"
            ) from e

        url = os.environ.get("ATOMIC_AGENTS_SECRET_BACKEND_URL", "").strip()
        if not url:
            raise SecretBackendNotRegistered(
                "ATOMIC_AGENTS_SECRET_BACKEND=gcp requires "
                "ATOMIC_AGENTS_SECRET_BACKEND_URL to be set. "
                "Expected format: projects/<project_id>/secrets "
                "(GCP Secret Manager prefix). "
                "Also install the [gcp] extra: uv add 'atomic-agents-stack[gcp]'"
            )
        # Wrap the REAL failure site: GCPSecretManagerBackend.__init__ raises
        # ImportError when the SDK is absent. Re-raise as SecretBackendNotRegistered
        # so the fail-closed contract holds end to end.
        try:
            return make_gcp_secret_backend_from_url(url)
        except ImportError as e:
            raise SecretBackendNotRegistered(
                "ATOMIC_AGENTS_SECRET_BACKEND=gcp requires the [gcp] extra. "
                "Install: uv add 'atomic-agents-stack[gcp]'"
            ) from e

    # Unknown backend_id. Sanitize before echoing in the error message to
    # prevent credential leaks when operators accidentally paste a URL into
    # ATOMIC_AGENTS_SECRET_BACKEND instead of the _URL variable.
    known_ids = {"filesystem", "gcp"}
    safe_backend_id = _redact_for_error_message(raw_backend_id)
    raise SecretBackendNotRegistered(
        f"ATOMIC_AGENTS_SECRET_BACKEND={safe_backend_id!r} is not a "
        f"known backend. Known: {sorted(known_ids)}. "
        f"Available registered: {list_secret_backends()}. "
        f"Unset the env var to use the filesystem default."
    )


def _redact_for_error_message(value: str, max_len: int = 32) -> str:
    """Sanitize a possibly-sensitive env var value for error-message echo.

    Strips anything after ``://`` (URL credential heuristic) and truncates at
    ``max_len`` to bound the echoed string. Returns the bare backend_id if no
    URL marker is present. The full original value is never echoed -- this
    prevents the credential-leak failure mode where an operator accidentally
    sets ``ATOMIC_AGENTS_SECRET_BACKEND=https://user:pass@host`` instead of
    ``ATOMIC_AGENTS_SECRET_BACKEND_URL``.

    Mirrors ``mcp_registry/__init__.py:_redact_for_error_message`` (including
    the DSN heuristic); the ``logs``/``profile``/``corpus`` variants share the
    ``://`` scheme-stripping but predate the DSN heuristic.
    """
    if "://" in value:
        scheme = value.split("://", 1)[0]
        return f"{scheme}://..."
    # DSN heuristic: catch user:password@host/db style without a scheme.
    if "@" in value and re.search(r":[^/]+@", value):
        return "[redacted-connection-string]"
    if len(value) > max_len:
        return value[:max_len] + "..."
    return value
