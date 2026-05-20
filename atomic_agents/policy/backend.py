"""PolicyBackend Protocol — the contract every policy backend satisfies.

This is the ninth open Protocol in the protocol-pattern series alongside
MemoryBackend (#57, shipped), LLMBackend (#87, shipped), JudgeBackend (#112,
shipped), LockBackend (#60, shipped), LogBackend (#61, shipped),
AgentProfileBackend (#63, shipped), ToolRegistryBackend (#64, shipped), and
MandateBackend (#124, shipped). Each Protocol decouples one storage / dispatch
axis so the framework's core stays small and alternate implementations drop in
without forking.

Issue #89 frames the policy primitive. Today, per-agent cost caps and tool
allowlists are partially enforced at construction and call time, but there is
no unified policy surface — no single place where an operator declares "agent
X may use tools A and B, must stay under model Y, and is capped at Z tokens
per day." The ``PolicyBackend`` Protocol seals the policy-resolution layer so
``AtomicAgent.__init__`` (PR 2 of #89) can talk to policy through a stable
interface, and future backends (SaaS policy engines, git-backed declarative
configs, RBAC databases) register via ``register_policy_backend(id, cls)``
without forking core.

Scaffolding PR (#89 PR 1 of 4): the Protocol contract + registry primitives +
default factory + ``FilesystemPolicyBackend`` bootstrap registration. **Zero
behavior change** — no call site routes through the Protocol yet;
``AtomicAgent.__init__`` is unchanged. PR 2 wires the bootstrap path.

The seven normative MUSTs from spec/32 §"Implementer contract for policy
backends" govern this module:

- MUST #1: ``agent_name`` validation at the API boundary — path-traversal
  tokens, control characters, newlines, or empty strings raise
  ``ValueError`` BEFORE any dict access or I/O. Pattern: ``[a-zA-Z0-9_-]+``.
  ``tool_name`` and ``mcp_server_name`` validation is lighter — rejects only
  control characters and newlines (allows dots, dashes, colons for the
  ``mcp:server:tool.name`` shape that is standard in MCP ecosystems).

- MUST #2: per-agent isolation — per-agent policy state must never bleed
  across agents. ``agent_name="caldwell"`` queries MUST NOT be contaminated
  by ``agent_name="aria"`` state in any backend.

- MUST #3: fresh re-read with ``cache_ttl_s``-bounded staleness — backends
  that cache parsed ``policy.md`` state MUST honour the advertised
  ``cache_ttl_s`` upper bound declared in ``capabilities()``. A backend
  claiming ``cache_ttl_s=0`` must reparse on every call. A backend claiming
  ``cache_ttl_s=30`` must reparse no less often than every 30 seconds so
  operator edits take effect within that window.

- MUST #4: construction is side-effect-free — ``PolicyBackend.__init__`` MUST
  NOT perform I/O, validate env vars, or resolve paths. Those actions happen
  at ``AtomicAgent.__init__`` time (PR 2 wires this). The principle mirrors
  spec/29 MUST #4 for MandateBackend and keeps test-fixture construction cheap.

- MUST #5: capability honesty — ``capabilities()`` claims MUST match observed
  backend behavior. A declared capability MUST pass the parametrized
  conformance suite's capability-gated tests. Backends that lie about
  capabilities produce silent failures rather than loud refusals.

- MUST #6: URL credential redaction in factory ``ValueError`` sites — when
  ``ATOMIC_AGENTS_POLICY_BACKEND`` contains a URL-shaped value (heuristic:
  contains ``://``), the raw value MUST NOT appear in error messages. Mirrors
  the credential-redaction helper pattern in LogBackend, ToolRegistryBackend,
  and MandateBackend factories.

- MUST #7: ``PolicyDecision`` event schema compliance — backends that emit
  log events for policy decisions MUST conform to the ``PolicyDecision`` event
  schema (schema_version 1) as documented in spec/32 §"Event schema". This
  MUST is reserved in PR 1 and enforced by the conformance suite in PR 4.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..exceptions import BackendNotRegistered
from .types import CostCaps, PolicyCapabilities

_logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
# Protocol definition


@runtime_checkable
class PolicyBackend(Protocol):
    """Contract every policy backend implementation must satisfy.

    Implementations MUST NOT subclass this Protocol — it is structural.
    Implementations satisfy it by exposing the methods below with the
    documented behavior. The ``@runtime_checkable`` decorator enables
    ``isinstance(obj, PolicyBackend)`` to perform a method-presence check
    (not a signature check — signatures are static-typing's job).

    The agent name is the unit of policy isolation. Every method that
    resolves policy accepts an ``agent_name`` parameter identifying which
    agent's effective policy to compute. Backends resolve per-agent
    overrides on top of fleet defaults as documented per method.

    Agent names are validated at the API boundary: ``[a-zA-Z0-9_-]+``.
    Path-traversal characters, control characters, newlines, or empty
    strings are refused BEFORE any I/O (spec/32 MUST #1).

    **Relationship to the JudgeBackend layer** (spec/28): ``PolicyBackend``
    owns *operator configuration resolution* — reading ``policy.md`` files
    and computing effective caps, allowlists, and model overrides for a named
    agent. The JudgeBackend (when a ``PolicyJudge`` is configured) may
    enforce the resolved policy at proposal time. The two compose:

    .. code-block:: text

        backend.get_effective_caps(agent_name) → CostCaps   # resolution
        PolicyJudge.evaluate(proposal, ctx)   → Judgment    # enforcement

    Replacing the in-memory enforcement logic would collapse the two layers
    and make alternate backends impossible. The resolution / enforcement split
    mirrors the ToolRegistryBackend (catalog / dispatch) and MandateBackend
    (discovery / validation) patterns.

    Capability-gated behavior is declared via ``capabilities()``; the
    conformance suite gates tests on the flags. Backends that lie about
    capabilities produce silent failures rather than loud refusals —
    spec/32 MUST #5.
    """

    @property
    def backend_id(self) -> str:
        """Stable identifier — e.g., ``"filesystem"``, ``"postgres"``,
        ``"saas"``.

        Used by the registry for lookup
        (``get_policy_backend(backend_id)``) and by diagnostic tooling
        that wants to record "which backend resolved this agent's policy?".
        Treat as a backwards-compatibility surface — operator deployments
        may pin against these strings in env vars and config.
        """
        ...

    # ────────────────────────────────────────────────────────────────
    # Query methods — always implemented

    def get_effective_caps(self, agent_name: str) -> CostCaps:
        """Return per-dimension cost caps for ``agent_name``.

        Semantics:

        * Returns ``CostCaps()`` (all-None, meaning no caps) when
          ``policy.md`` is absent OR when the agent is unmentioned AND
          no fleet-default ``cost_caps`` block exists (spec/32 Premise 5
          revised per plan-subagent D7).
        * When ``policy.md`` has a fleet-default ``cost_caps`` block AND
          the agent is not in the ``agents:`` section: returns the fleet
          defaults as-is.
        * When the agent appears in the ``agents:`` section: returns MIN
          per dimension of (fleet_cost_caps, agent_override_cost_caps) per
          plan-subagent D2 — the tighter of fleet and agent caps wins on
          each dimension independently.
        * Per-call ceiling (``agent.call(cost_cap=...)``) is composed at
          the consumption site (PR 3 of #89); this method returns ONLY the
          Policy-resolved caps, not the final effective ceiling.
        * ``agent_name`` MUST be validated against ``[a-zA-Z0-9_-]+`` at
          the API boundary BEFORE any dict access. Path-traversal tokens,
          control chars, newlines, or empty string raise ``ValueError``
          (spec/32 MUST #1).

        Args:
            agent_name: the agent whose effective cost caps to return.
                Validated at the API boundary.

        Returns:
            ``CostCaps`` instance. All fields may be ``None`` (no cap on
            that dimension). Callers treat ``None`` as "no limit from
            Policy" and compose it with per-call args in PR 3.

        Raises:
            ValueError: ``agent_name`` fails path-traversal / format
                validation.
        """
        ...

    def is_tool_allowed(self, agent_name: str, tool_name: str) -> bool:
        """Return whether ``agent_name`` may invoke ``tool_name`` per Policy.

        Composition (per plan-subagent F7 resolution):

        * ``effective_allow = MERGE(fleet.tools.allow, agent.tools.allow)``
          — union of fleet and agent allowlists.
        * ``effective_deny  = UNION(fleet.tools.deny, agent.tools.deny)``
          — union of fleet and agent denylists.
        * ``result = (tool_name in effective_allow OR effective_allow is
          empty) AND NOT (tool_name in effective_deny)``
        * Returns ``True`` when ``policy.md`` is absent OR no ``tools``
          rules apply to the agent.

        Validation (spec/32 MUST #1):

        * ``agent_name`` validated against ``[a-zA-Z0-9_-]+`` — full
          path-traversal + format check.
        * ``tool_name`` validated for control characters and newlines
          only (allows dots, dashes, colons for the
          ``mcp:server:tool.name`` convention).

        Args:
            agent_name: the agent invoking the tool. Validated at the
                API boundary.
            tool_name: the tool name to check. Validated for control
                chars and newlines only.

        Returns:
            ``True`` if Policy permits the tool; ``False`` if denied.

        Raises:
            ValueError: ``agent_name`` or ``tool_name`` fails validation.
        """
        ...

    def is_mcp_server_allowed(self, agent_name: str, server_name: str) -> bool:
        """Return whether ``agent_name`` may connect to MCP ``server_name``.

        Same composition shape as ``is_tool_allowed`` but for MCP server
        names (from the ``mcp_servers`` section of ``policy.md``):

        * ``effective_allow = MERGE(fleet.mcp_servers.allow,
          agent.mcp_servers.allow)``
        * ``effective_deny  = UNION(fleet.mcp_servers.deny,
          agent.mcp_servers.deny)``
        * ``result = (server_name in effective_allow OR effective_allow is
          empty) AND NOT (server_name in effective_deny)``
        * Returns ``True`` when ``policy.md`` is absent OR no
          ``mcp_servers`` rules apply to the agent.

        Validation follows ``is_tool_allowed`` semantics:
        ``agent_name`` validated against ``[a-zA-Z0-9_-]+``;
        ``server_name`` validated for control characters and newlines only
        (allows dots, dashes, colons for the standard MCP server-name
        shape).

        Args:
            agent_name: the agent requesting the MCP server connection.
                Validated at the API boundary.
            server_name: the MCP server name to check. Validated for
                control chars and newlines only.

        Returns:
            ``True`` if Policy permits the server; ``False`` if denied.

        Raises:
            ValueError: ``agent_name`` or ``server_name`` fails validation.
        """
        ...

    def get_effective_model(self, agent_name: str) -> str | None:
        """Return the Policy model override for ``agent_name``, or ``None``.

        Per plan-subagent D9 fold #2 (operator-owned compatibility):

        * ``None`` — Policy has no opinion on model; ``model.md``'s choice
          wins at the consumption site (PR 2 of #89 wires the composition).
        * Non-``None`` string — Policy override applies as-is; the framework
          does NOT enforce compatibility families between the override model
          and the agent's prompt or tool format. The operator is responsible
          for verifying the override is compatible. This is consistent with
          how ``model.md``'s ``model:`` field is treated — it names any
          registered model string without framework-level family enforcement.

        Model selection is REPLACE, not MERGE, because there is only one
        active model. Per-agent override wins over fleet default.

        Args:
            agent_name: the agent whose model override to resolve.
                Validated against ``[a-zA-Z0-9_-]+`` at the API boundary.

        Returns:
            The model override string (e.g., ``"claude-opus-4-5"``), or
            ``None`` if Policy has no opinion.

        Raises:
            ValueError: ``agent_name`` fails path-traversal / format
                validation.
        """
        ...

    # ────────────────────────────────────────────────────────────────
    # Capabilities

    def capabilities(self) -> PolicyCapabilities:
        """Backend capability declaration — see ``PolicyCapabilities``.

        Conformance tests assert claim-vs-behavior parity. Honest
        capabilities let callers fail fast against incompatible backends
        rather than discovering the mismatch mid-operation (spec/32
        MUST #5).
        """
        ...


# ────────────────────────────────────────────────────────────────────────────
# Process-local registry


# Registry: backend_id → backend class.  Classes (not instances) because
# policy backends carry per-construction args — the framework instantiates
# ``FilesystemPolicyBackend(scope_root)`` at agent-construction time; the
# registry's job is the operator-pin lookup that maps ``"filesystem"`` →
# ``FilesystemPolicyBackend``.
_registry: dict[str, type] = {}


def register_policy_backend(backend_id: str, cls: type[PolicyBackend]) -> None:
    """Register a ``PolicyBackend`` implementation under ``backend_id``.

    Typically called once at module-import time from each backend module.
    The default ``"filesystem"`` registration happens at the bottom of this
    file via a deferred import to avoid circular-dependency at import time.

    Silent replace on collision — matches the Lock / Log / Profile / LLM /
    Judge pattern. The ``_bootstrap_filesystem()`` call at module bottom is
    idempotent (guards against double-registration via presence check), so
    silent replace here is safe for user-registered backends and operator
    pin-overrides via test fixtures or alternative reference implementations.

    Args:
        backend_id: short stable identifier, e.g. ``"filesystem"``,
            ``"sqlite"``, ``"saas"``.
        cls: a class satisfying the ``PolicyBackend`` Protocol.
    """
    if backend_id in _registry:
        _logger.debug(
            "replacing policy backend %r (was %s, now %s)",
            backend_id,
            _registry[backend_id].__qualname__,
            cls.__qualname__,
        )
    _registry[backend_id] = cls
    _logger.debug("registered policy backend %r → %s", backend_id, cls.__qualname__)


def unregister_policy_backend(backend_id: str) -> None:
    """Remove ``backend_id`` from the registry.

    Used by conformance fixtures: register ``MockPolicyBackend`` under
    ``"mock"`` in setup; unregister in teardown to keep test isolation
    clean. Idempotent — silently no-ops if ``backend_id`` is not present.

    Spec/32 §"Implementer contract" notes register / unregister as part of
    the standard registry surface. Not Policy-novel — Lock, Log, Profile,
    LLM, and Judge already ship it.
    """
    _registry.pop(backend_id, None)


def get_policy_backend(backend_id: str) -> type[PolicyBackend]:
    """Return the ``PolicyBackend`` class registered under ``backend_id``.

    Raises ``BackendNotRegistered`` when the id is not in the registry.
    The caller instantiates the returned class with its scope-specific
    constructor arguments (e.g., ``cls(scope_root)`` for the filesystem
    backend).

    Args:
        backend_id: backend identifier to look up.

    Returns:
        The registered backend class.

    Raises:
        BackendNotRegistered: ``backend_id`` is not in the registry.
    """
    if backend_id not in _registry:
        raise BackendNotRegistered(
            f"No PolicyBackend registered under {backend_id!r}. "
            f"Available: {sorted(_registry.keys())}"
        )
    return _registry[backend_id]  # type: ignore[return-value]


def list_policy_backends() -> list[str]:
    """Return registered backend ids in lexicographic order."""
    return sorted(_registry.keys())


# ────────────────────────────────────────────────────────────────────────────
# Default factory


def get_default_policy_backend(scope_root: Path) -> PolicyBackend:
    """Return the operator-pinned ``PolicyBackend`` instance for ``scope_root``.

    Reads ``ATOMIC_AGENTS_POLICY_BACKEND`` from the environment (default
    ``"filesystem"``). The env var name follows the established pattern —
    ``ATOMIC_AGENTS_<PRIMITIVE>_BACKEND`` — so operators who already pin
    the log or mandate backend use the same vocabulary.

    The ``scope_root`` parameter is the directory under which per-agent and
    project-root ``policy.md`` files live. For the filesystem backend:

    - ``<scope_root>/policy.md`` — fleet-level defaults applied to all agents.
    - ``<scope_root>/<agent_name>/policy.md`` — per-agent overrides composed
      with fleet defaults.

    For programmatic operators who want to construct the backend themselves
    (custom database, alternative path structure), the future
    ``AtomicAgent(..., policy_backend=...)`` constructor kwarg (wired in
    PR 2 of #89) bypasses this factory entirely.

    Per spec/32 MUST #4: this function MUST NOT be called from
    ``FilesystemPolicyBackend.__init__`` — construction is side-effect-free;
    env-var resolution happens at ``AtomicAgent.__init__`` time (PR 2 wires).

    Per spec/32 MUST #6: raw env var values are redacted in error messages
    (heuristic strips after ``"://"`` + truncates to 32 chars) to avoid
    leaking accidentally-pasted credentials.

    See spec/32 §"Implementer contract for policy backends" for the full
    env-var reference.

    Args:
        scope_root: root directory from which per-agent and fleet-level
            ``policy.md`` paths are resolved.

    Returns:
        A ready-to-use ``PolicyBackend`` instance.

    Raises:
        BackendNotRegistered: the env var names an unknown backend.
    """
    from .filesystem import FilesystemPolicyBackend  # deferred — avoids circular

    raw_backend_id = (
        os.environ.get("ATOMIC_AGENTS_POLICY_BACKEND", "filesystem").strip().lower()
    )

    if raw_backend_id == "filesystem":
        return FilesystemPolicyBackend(scope_root)

    # Unknown backend — surface a fail-fast error with the full id list.
    # Credential-safety: raw_backend_id is redacted in case the operator
    # accidentally pasted a URL into ATOMIC_AGENTS_POLICY_BACKEND.
    safe_id = _redact_for_error_message(raw_backend_id)
    raise BackendNotRegistered(
        f"ATOMIC_AGENTS_POLICY_BACKEND={safe_id!r} is not a known backend. "
        f"Available: {list_policy_backends()}. "
        f"Unset the env var to use the filesystem default."
    )


def _redact_for_error_message(value: str, max_len: int = 32) -> str:
    """Sanitize a possibly-sensitive env var value for error-message echo.

    Strips anything after ``://`` (URL credential heuristic) and truncates
    at ``max_len``. Mirrors the identical helper in
    ``atomic_agents.mandate.backend`` and ``atomic_agents.logs.__init__``.
    A future refactor MAY hoist to a shared location once another Protocol
    needs it; until then duplicating the 5-line function is cheaper than the
    cross-module dependency.
    """
    if "://" in value:
        scheme = value.split("://", 1)[0]
        return f"{scheme}://..."
    if len(value) > max_len:
        return value[:max_len] + "..."
    return value


# ────────────────────────────────────────────────────────────────────────────
# Bootstrap registration


# Register the built-in filesystem backend at import time.  Matches the
# Profile / Log / Lock / ToolRegistry / Mandate registry pattern — the default
# is always available without an extra resolution step.  Deferred import avoids
# a circular dependency (filesystem.py imports from this module's types; this
# module imports from filesystem.py only at the bottom after the Protocol is
# fully defined).
def _bootstrap_filesystem() -> None:
    """Register the filesystem backend at import time if not already registered.

    Called unconditionally at module bottom. Guards against double-import
    (e.g., test isolation that re-imports the module) by checking presence
    before registering — this is the ONLY caller permitted to skip the
    silent-replace log line by checking first. The idempotent guard also
    preserves any operator-registered class that was pinned under
    ``"filesystem"`` before a module reload.
    """
    if "filesystem" not in _registry:
        from .filesystem import FilesystemPolicyBackend

        _registry["filesystem"] = FilesystemPolicyBackend
        _logger.debug(
            "registered policy backend 'filesystem' → FilesystemPolicyBackend"
        )


_bootstrap_filesystem()
