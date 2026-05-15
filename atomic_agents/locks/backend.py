"""LockBackend Protocol — the contract every lock implementation satisfies.

This is one of the open protocols in the protocol-pattern series alongside
MemoryBackend (#57, shipped), LLMBackend (#87, shipped), JudgeBackend
(#112, shipped), and the remaining Tier-2 backends LogBackend (#61),
PersonaBackend (#62), AgentProfileBackend (#63), ToolRegistryBackend
(#64), CorpusBackend (#65). Each Protocol decouples one storage / dispatch
axis so the framework's core stays small and alternate implementations
drop in without forking.

Issue #60 frames the urgency: today's ``_locks.AgentLock`` uses
``fcntl.flock`` which is per-host POSIX. Multi-process / multi-host /
serverless / containerized deployments are structurally impossible
without an abstract LockBackend. Concrete users blocked: Meridian on
Cloud Run; Bishop on gizmo running parallel agents over shared memory;
any future SaaS deployment.

Scaffolding PR (#60 PR 1): the Protocol contract + canonical types +
``FilesystemLockBackend`` reference implementation. PR 2 wires the
backend into the four existing lock sites (``agent.call()``,
``dream.start()``, two in ``memory/filesystem.py``) and converts
``_locks.py`` into a deprecation shim. PR 3 ships the distributed
reference impl. PR 4 locks ``docs/spec/21-lock-backend.md`` and
parameterizes the conformance suite across both backends.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from .types import LockCapabilities, LockHandle


@runtime_checkable
class LockBackend(Protocol):
    """Contract every lock backend implementation must satisfy.

    Implementations MUST NOT subclass this Protocol — it is structural.
    Implementations satisfy it by exposing the methods below with the
    documented behavior. The ``@runtime_checkable`` decorator enables
    ``isinstance(obj, LockBackend)`` to perform a method-presence check
    (not a signature check — signatures are static-typing's job).

    Scope is bound at backend construction, not per call. The agent
    instantiates one ``FilesystemLockBackend(agent_root)`` and uses it
    for the agent's full life. Dream / memory sub-scopes either share
    the same backend (and use distinct ``name`` arguments — e.g.,
    ``acquire("dream")``) or construct their own backend instance
    rooted at a sub-directory. This mirrors the MemoryBackend
    construction pattern (one backend per agent_root) and keeps the
    Protocol surface free of per-call path arguments that distributed
    backends (Redis, Postgres advisory) have no meaning for.

    Reentrancy contract: NON-REENTRANT by default. Re-acquiring a held
    lock from the same process MUST raise ``LockBusy``. Backends that
    implement per-(name, holder) ref-counting may advertise
    ``LockCapabilities.supports_reentrancy=True``; the conformance suite
    asserts the claim matches behavior.

    Lease / heartbeat: backends with TTL (Redis SET ... EX, Postgres
    advisory with lease) require periodic ``renew()`` calls to extend
    the lease. ``LockCapabilities.supports_lease`` advertises whether
    the backend has a meaningful lease. Backends without a lease return
    True from ``renew()`` unconditionally (no-op) so callers that
    invoke ``renew()`` on long-running operations don't have to branch
    on capability. See spec/21 §"Lease and heartbeat".
    """

    @property
    def backend_id(self) -> str:
        """Stable identifier — e.g., ``"filesystem"``, ``"redis"``,
        ``"postgres-advisory"``.

        Used by the registry for lookup (``get_lock_backend(backend_id)``)
        and by diagnostic tooling that wants to log "which backend held
        the lock?". Treat as a backwards-compatibility surface — operator
        deployments may pin against these strings.
        """
        ...

    def acquire(self, name: str = "", timeout: float = 0.0) -> LockHandle:
        """Acquire the named lock, or raise ``LockBusy`` after the timeout.

        Args:
            name: semantic identifier for the lock. Empty string is the
                default and is the most common case (the agent's main
                lock). Backends interpret the name space:
                ``FilesystemLockBackend`` maps it to ``<scope>/.lock``
                (empty) or ``<scope>/.<name>.lock`` (non-empty); a
                Redis backend uses ``<key_prefix><name>`` keys.
            timeout: max seconds to wait for the lock. ``0.0`` (default)
                fails fast — useful for cron contexts that should not
                block. Skill / interactive contexts typically pass 30s.

        Returns:
            ``LockHandle`` — pass this back to ``release()`` /
            ``renew()``. The handle is its own context manager so call
            sites can use ``with backend.acquire(name, timeout=30) as
            h: ...``.

        Raises:
            ``atomic_agents.exceptions.LockBusy``: timeout elapsed
                without the lock being granted.

        Reentrancy: non-reentrant by default. A second ``acquire()`` of
        the same name from the same process raises ``LockBusy`` unless
        the backend advertises ``supports_reentrancy=True``.
        """
        ...

    def release(self, handle: LockHandle) -> None:
        """Release a lock previously granted by ``acquire()``.

        MUST be idempotent (per CLAUDE.md §"Atomic + idempotent
        everywhere"): calling ``release()`` twice on the same handle is
        a no-op. ``LockHandle.__exit__`` calls ``release()`` for the
        context-manager case; explicit ``release()`` from a ``finally``
        block on top of a ``with`` statement must not raise.

        Args:
            handle: the handle returned by ``acquire()``. Backends use
                ``handle.backend_state`` to locate the underlying
                resource (file descriptor, lease token).
        """
        ...

    def renew(self, handle: LockHandle) -> bool:
        """Extend a lease-backed lock's deadline.

        For backends without a lease (``supports_lease=False``) this is
        a no-op that returns True unconditionally — callers wrapping
        long-running operations can call ``renew()`` periodically
        without branching on capability.

        For lease-backed backends, ``renew()`` extends the lease by the
        backend's configured lease duration. Returns True when the
        lease was successfully extended; returns False when the lease
        had already expired (caller MUST treat this as lock-lost and
        re-acquire before continuing the critical section).

        Args:
            handle: the handle returned by ``acquire()``.

        Returns:
            True on successful renewal (or unconditional True for
            non-lease backends); False when the lease has expired and
            the caller has effectively lost the lock.
        """
        ...

    def is_held(self, name: str = "") -> bool:
        """Diagnostic: is the named lock currently held?

        DIAGNOSTIC USE ONLY — racy by design. The state can change
        between the check and any subsequent decision; callers MUST
        NOT use this for control flow (use ``try: acquire(timeout=0)
        except LockBusy:`` instead).

        Used by ``atomic-agents doctor``'s ``check_locks`` to surface
        "another process holds this agent's lock right now" to the
        operator. Returns True when *any* process holds the lock;
        returns False when no holder is observable to this backend
        instance.

        Args:
            name: same semantic identifier as ``acquire()``.
        """
        ...

    def capabilities(self) -> LockCapabilities:
        """Backend capability declaration — see ``LockCapabilities``.

        Conformance tests assert claim-vs-behavior parity. Honest
        capabilities let callers fail fast against incompatible
        backends rather than discovering the mismatch mid-operation.
        """
        ...
