"""Canonical dataclasses for the SecretBackend Protocol (spec/38).

Two dataclasses define the secret backend substrate contract (issue #340, spec/38):

- ``SecretRef`` -- lightweight resolution token returned by ``locate()``;
  carries source metadata, never the resolved secret value.
- ``SecretCapabilities`` -- frozen capability advertisement for a backend
  instance; conformance tests assert claim-vs-behavior parity.

No exceptions live here. SecretBackend exceptions live in
``atomic_agents/secret_backend/backend.py`` to avoid circular imports.

No Protocol definition lives here. The ``SecretBackend`` Protocol is in
``atomic_agents/secret_backend/backend.py``.
"""

from __future__ import annotations

from dataclasses import dataclass


# ──────────────────────────────────────────────────────────────────────────────
# SecretRef


@dataclass(frozen=True)
class SecretRef:
    """Resolution token returned by ``locate()``.

    Carries metadata about where a secret was found. Does NOT include the
    resolved value -- the value is returned separately by ``get()`` /
    ``get_optional()``. This separation enforces the spec/38 secrecy clauses:
    source metadata is safe to log; secret values are not.

    Fields:

    ``key``: the canonical key name (e.g., ``"ANTHROPIC_API_KEY"``).
        Must match the ``[A-Z0-9_]+`` charset.

    ``source``: a human-readable string naming the source location, e.g.:
        - ``"env:ANTHROPIC_API_KEY"`` — found in environment variable
        - ``"keychain:atomic-agents-anthropic"`` — found in macOS Keychain
        - ``"config:~/.config/atomic_agents/keys.json:anthropic"`` — found in keys.json

        MUST NOT include the resolved credential value. Names the source
        location only (spec/38 MUST 5).

    ``present``: True if the key was found at this source, False otherwise.
        Always True for refs returned by ``locate()`` when the key exists.
    """

    key: str
    source: str
    present: bool


# ──────────────────────────────────────────────────────────────────────────────
# SecretCapabilities


@dataclass(frozen=True)
class SecretCapabilities:
    """Capability advertisement for a ``SecretBackend`` instance.

    Conformance tests assert claim-vs-behavior parity (MUST 3 analog).
    Backends that misreport capabilities produce silent failures rather than
    loud refusals.

    Fields:

    ``supports_rotation``: True if the backend can serve freshly-rotated
        credentials without a process restart. ``FilesystemSecretBackend``
        reports True because each ``get()`` call re-resolves from live sources
        (no caching). GCP Secret Manager backend would report True natively.

    ``supports_audit_logging``: True if the backend records each ``get()``
        call in a durable audit log. Both v1.0 reference implementations
        return False (FilesystemSecretBackend reads process-local sources
        without an audit trail; GCP Secret Manager backend has audit logging
        but that is an operator-side configuration, not framework-side).

    ``persists_plaintext``: True if the backend stores the credential value
        in plaintext in a file or database that could travel with the vault.
        FilesystemSecretBackend reports False -- it reads from machine-scoped
        sources only (env vars, macOS Keychain, ~/.config/atomic_agents/keys.json).
        No credential is written by the framework.

    Note: ``supports_versioning`` is intentionally absent from v1.0.
    Rotation (re-keying by the secret store) and versioning (read-back of
    prior secret values) are distinct capabilities. ``supports_versioning``
    enters the Protocol when the first backend that can read prior versions
    ships (e.g., a GCP Secret Manager backend with version pinning).
    Do not add it before then.
    """

    supports_rotation: bool
    supports_audit_logging: bool
    persists_plaintext: bool
    # spec/40 addendum: Exportable Protocol composition.
    # FilesystemSecretBackend = True (wiring-map-only export, never plaintext).
    # GCPSecretBackend = False (deferred; export retrofit is out of scope for
    # PR1 — see #432).
    # Default False so existing instantiation sites without this kwarg keep working.
    supports_canonical_export: bool = False
