"""SecretBackend Protocol -- the contract every secret backend satisfies (spec/38).

``SecretBackend`` abstracts credential resolution behind a Protocol so the
framework's core stays small and alternate secret substrates (GCP Secret
Manager, AWS Secrets Manager, HashiCorp Vault) drop in without forking.

## WritePolicy applicability

SecretBackend is read-only. The framework never writes secrets; secret
rotation is orchestrated outside the framework (in the secret store itself).
This Protocol does not include a WritePolicy for the same reason
PolicyBackend (spec/32) does not: a backend whose write path is out-of-scope
has no write policy to govern. The read-only contract is enforced
structurally -- there is no write method on the Protocol surface.

## Key charset constraint

All ``key`` parameters must match ``[A-Z0-9_]+`` (POSIX env-var charset --
the superset that covers all three current env-var sources). This is
validated at the API boundary before any backend access (MUST 1 analog).
Backends raise ``ValueError`` on invalid keys to prevent path-traversal via
the key name.

## Thread safety

``get()`` / ``get_optional()`` / ``has()`` / ``locate()`` are designed for
concurrent calls from ``helper_call_parallel`` worker threads. Backend
implementations MUST NOT cache resolved values in instance state (which
would require a lock) -- each call re-resolves from live sources. This
ensures rotation awareness: a key rotated at the secret store becomes
visible on the next ``get()`` call without a process restart.

## Exception classes

``SecretError`` and ``SecretNotFound`` live here (not in exceptions.py)
because they are raised only from within the secret_backend package, mirroring
MCPRegistryError's placement (mcp_registry/backend.py). Both are re-exported
from secret_backend/__init__.py so callers do ``from atomic_agents.secret_backend
import SecretError, SecretNotFound``.

## All call sites use property syntax

All call sites use property syntax: ``backend.capabilities.supports_rotation``
NOT ``backend.capabilities().supports_rotation`` (spec/36 Decision line 161
precedent -- @property declaration is load-bearing).
"""

from __future__ import annotations

import re
from typing import Protocol, runtime_checkable

from ..exceptions import AtomicAgentsError
from .types import SecretCapabilities, SecretRef

# Valid key charset: POSIX env-var names. Superset of all framework key names.
# Used to prevent path-traversal attacks via the key parameter.
_KEY_CHARSET_RE = re.compile(r"^[A-Z0-9_]+$")


def _validate_key(key: str) -> None:
    """Validate a key at the API boundary before any backend access (MUST 1 analog).

    Accepts only keys matching ``[A-Z0-9_]+`` (POSIX env-var charset).
    Raises ``ValueError`` on any character outside that set, including
    ``.``, ``/``, ``\\``, control characters, and empty string.

    Mirroring MCPServerRegistryBackend MUST 1 (charset validation before backend
    access).
    """
    if not key:
        raise ValueError(
            "SecretBackend key must not be empty. "
            "Keys must match [A-Z0-9_]+ (POSIX env-var charset)."
        )
    if not _KEY_CHARSET_RE.match(key):
        raise ValueError(
            f"SecretBackend key {key!r} contains invalid characters. "
            "Keys must match [A-Z0-9_]+ (POSIX env-var charset, e.g. "
            "ANTHROPIC_API_KEY, OPENAI_API_KEY). Path-traversal tokens "
            "(/, \\, ..), control characters, and lowercase letters are "
            "prohibited."
        )


# ──────────────────────────────────────────────────────────────────────────────
# Exception classes


class SecretError(AtomicAgentsError):
    """Base class for SecretBackend subsystem errors (spec/38).

    All SecretBackend reference implementations raise subclasses of this
    exception. Operators may ``except SecretError`` to catch the entire
    secret-backend error family. Inherits from ``AtomicAgentsError`` so
    framework-wide catch-alls (``except AtomicAgentsError``) see secret
    failures automatically.

    Callers catching errors at the ``_get_key()`` redirect boundary MUST
    catch ``SecretError`` (the base class), not ``SecretNotFound`` specifically,
    per the fail-closed wrapper lesson (mcp_registry PR 2). Re-raise via
    ``raise from`` to preserve the original exception type at the boundary.
    """


class SecretNotFound(SecretError):
    """``get(key)`` found no source with the key, or all sources returned empty/whitespace.

    The error message names the key and the sources that were searched so
    operators can triage which source to fix. The message MUST NOT include
    the resolved value under any circumstances (spec/38 secrecy MUST 4).

    Example:
        No secret found for key 'ANTHROPIC_API_KEY'. Sources tried (in order):
        env:ATOMIC_AGENTS_ANTHROPIC_KEY / ANTHROPIC_API_KEY,
        keychain:atomic-agents-anthropic,
        config:~/.config/atomic_agents/keys.json:anthropic.
        Set one of these sources to configure the credential.
    """


class SecretBackendNotRegistered(SecretError):
    """Operator-pinned ``backend_id`` is not in the registry.

    Raised by ``get_default_secret_backend()`` when the configured backend
    string is not registered. The error message includes the list of known
    ids and a credential-redacted echo of the value that was tried.
    """


# ──────────────────────────────────────────────────────────────────────────────
# Protocol definition


@runtime_checkable
class SecretBackend(Protocol):
    """Contract every secret backend implementation must satisfy (spec/38).

    Implementations MUST NOT subclass this Protocol -- it is structural.
    Implementations satisfy it by exposing the methods below with the
    documented behavior. The ``@runtime_checkable`` decorator enables
    ``isinstance(obj, SecretBackend)`` to perform a method-presence check.

    The key charset ``[A-Z0-9_]+`` is enforced at the API boundary BEFORE
    any backend access (MUST 1 analog). Keys outside this charset MUST raise
    ``ValueError`` (path-traversal prevention).

    Secrets are flat per-deployment. There is no scope() method or per-agent
    namespace. This is a deliberate design choice: credentials (API keys, etc.)
    are machine-scoped, not agent-scoped. An agent that runs on a machine has
    access to that machine's secrets, regardless of which agent it is.

    No list_secrets() method is present (spec/38 explicit deferral). Add
    list_secrets() to the Protocol only when a ``atomic-agents secrets list``
    CLI subcommand has a shipped spec that documents the disclosure model.

    No get_all() method returns plaintext values in bulk (spec/38 security).
    """

    # ─── Capability advertisement ─────────────────────────────────────────

    @property
    def capabilities(self) -> SecretCapabilities:
        """Advertise what this backend instance supports.

        Returns a frozen ``SecretCapabilities`` dataclass. The values are a
        contract, not a hint -- conformance tests assert claim-vs-behavior
        parity (MUST 3 analog).

        All call sites use property syntax: ``backend.capabilities.supports_rotation``
        NOT ``backend.capabilities().supports_rotation`` (spec/36 Decision
        precedent -- @property declaration is load-bearing).
        """
        ...

    @property
    def backend_id(self) -> str:
        """Stable identifier for this backend implementation.

        Returns a short lowercase string matching the registry key used to
        register this backend class (e.g., ``"filesystem"``, ``"gcp"``).
        Must not change across calls on the same instance. Used for logging,
        audit events, and operator-facing capability output.
        """
        ...

    # ─── Core resolution ──────────────────────────────────────────────────

    def get(self, key: str) -> str:
        """Return the resolved secret value for ``key``.

        Resolution order and source set are backend-specific. For
        FilesystemSecretBackend: env vars → macOS Keychain → keys.json.

        MUST raise ``ValueError`` if ``key`` does not match ``[A-Z0-9_]+``.
        MUST raise ``SecretNotFound`` if no source has the key or all sources
        return empty/whitespace.
        MUST NOT return empty string (empty/whitespace values are treated as
        absent -- same semantics as the original ``_get_key()`` ``if val:`` check).
        MUST NOT include the resolved value in any exception message
        (spec/38 secrecy MUST 4).
        """
        ...

    def get_optional(self, key: str) -> str | None:
        """Return the resolved secret value for ``key``, or None if absent.

        Same resolution logic as ``get()`` but returns None instead of raising
        ``SecretNotFound`` when the key is absent.

        MUST raise ``ValueError`` if ``key`` does not match ``[A-Z0-9_]+``.
        MUST return None (not empty string) when the key is absent or when
        all sources return empty/whitespace.
        MUST NOT include the resolved value in any exception message.
        """
        ...

    def has(self, key: str) -> bool:
        """Return True if ``key`` is resolvable via ``get()``, False otherwise.

        Strictly equivalent to ``get_optional(key) is not None``. Backends
        MUST implement ``has()`` as a delegation to ``get_optional()`` (not
        a separate resolution ladder) to guarantee ``has()`` and ``get()``
        never disagree on a key's presence (spec/38 MUST 8).

        MUST raise ``ValueError`` if ``key`` does not match ``[A-Z0-9_]+``.
        """
        ...

    def locate(self, key: str) -> SecretRef | None:
        """Return a ``SecretRef`` describing where ``key`` resolves, or None.

        Returns source metadata without returning the secret value. Used by
        ``atomic-agents secrets which <KEY>`` to print the source label
        (e.g., ``env:ANTHROPIC_API_KEY``) without ever printing the value.

        MUST raise ``ValueError`` if ``key`` does not match ``[A-Z0-9_]+``.
        MUST NOT include the resolved value in the returned ``SecretRef``
        (spec/38 secrecy MUST 5). The ``source`` field names the location only.
        Returns None if the key is absent (parallel to ``get_optional()``
        returning None).
        """
        ...

    def close(self) -> None:
        """Release any resources held by this backend instance.

        Idempotent. Calling ``close()`` twice must not raise. Calling it
        before any other method is a no-op.

        FilesystemSecretBackend: no-op (all sources are stateless).
        GCP Secret Manager backend: closes the gRPC channel.
        """
        ...
