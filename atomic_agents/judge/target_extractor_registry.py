"""Per-agent named target extractor registry (spec/29 §"Target extraction").

The registry maps string IDs → callables that extract a ``target_canonical``
value from a tool's ``tool_arguments`` dict. The framework invokes the named
extractor at proposal-assembly time to populate
``ActionProposal.target_canonical``, which ``MandateCheck`` step 5 reads to
enforce ``constraints.allowed_targets`` / ``blocked_targets``.

Design notes:
- Registry lives on ``AtomicAgent._target_extractors`` (in-memory, NOT
  persisted on ``AgentProfile`` snapshot — ``Callable`` values cannot
  satisfy spec/25 MUST #4 Tier B lossless round-trip for structured-storage
  backends).
- Per-agent scoping mirrors spec/25 Decision 9 + spec/15 delegate isolation:
  coordinator-registered extractors do NOT leak into delegate evaluations.
  A delegate constructs its own ``TargetExtractorRegistry`` fresh.
- Built-in heuristic extractors are pre-registered at ``TargetExtractorRegistry``
  construction time, BEFORE tool_registry loading in ``AtomicAgent.__init__``,
  so ``ToolDefinition.target_extractor_id`` strings can be validated at
  ``tool_registry.register()`` time (loud early failure per spec/29
  §"Registration order discipline" and plan-subagent Risk A).

See also:
    ``atomic_agents.judge.types.ActionProposal.target_canonical`` —
        the field this registry populates at proposal-assembly time.
    ``atomic_agents.tools.ToolDefinition.target_extractor_id`` —
        the per-tool optional string that names which registered extractor
        to call.
"""

from __future__ import annotations

import logging
from typing import Callable

from ..exceptions import AtomicAgentsError

_logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────
# Built-in heuristic extractors (spec/29 line 462)
#
# Each extractor is a callable that receives the tool's ``tool_arguments``
# dict and returns a string target or ``None`` when the field is absent.
# The priority order (to/recipient/target/url/repository/customer_id/
# channel_id) is used by ``TargetExtractorRegistry.extract`` when no
# explicit ``extractor_id`` is specified — the first non-None match wins.

BUILTIN_EXTRACTORS: dict[str, Callable[[dict], str | None]] = {
    "recipient_to": lambda args: args.get("to"),
    "recipient_field": lambda args: args.get("recipient"),
    "target_field": lambda args: args.get("target"),
    "url_field": lambda args: args.get("url"),
    "repository_field": lambda args: args.get("repository"),
    "customer_id_field": lambda args: args.get("customer_id"),
    "channel_id_field": lambda args: args.get("channel_id"),
}

# Canonical priority order for the heuristic fallback pass. Kept as a
# separate tuple so the order is explicit and stable — dict insertion
# order is preserved in Python 3.7+ but naming the order separately makes
# spec alignment auditable.
_BUILTIN_PRIORITY_ORDER: tuple[str, ...] = (
    "recipient_to",
    "recipient_field",
    "target_field",
    "url_field",
    "repository_field",
    "customer_id_field",
    "channel_id_field",
)


# ──────────────────────────────────────────────────────────────────
# Exception


class UnknownTargetExtractor(AtomicAgentsError):
    """Raised when a ``ToolDefinition.target_extractor_id`` references an
    unregistered name.

    Loud failure at ``tool_registry.register()`` time per spec/29
    §"Registration order discipline" — surfaces operator misconfiguration
    BEFORE first mandate-citing dispatch so operators discover the problem
    at agent-load time, not at first side-effectful action.

    This is plan-subagent PR 3a Risk A: validating at MandateCheck
    evaluation time would be a silent fail-closed surface where operators
    discover at first mandate-citing action that a tool's extractor is
    missing — too late to be useful.
    """


# ──────────────────────────────────────────────────────────────────
# Registry


class TargetExtractorRegistry:
    """Per-agent named target extractor registry.

    Built-in heuristics (``recipient_to``, ``recipient_field``,
    ``target_field``, ``url_field``, ``repository_field``,
    ``customer_id_field``, ``channel_id_field``) are pre-registered at
    construction time, BEFORE ``tool_registry`` loading in
    ``AtomicAgent.__init__``. Operators add custom extractors via
    ``agent.register_target_extractor(name, callable)``.

    Spec/29 §"Target extraction": the registry lives on
    ``AtomicAgent._target_extractors`` in-memory, NOT persisted on the
    ``AgentProfile`` snapshot (JSON-serializable per spec/24 — cannot
    store ``Callable`` values). Per-agent scoping mirrors spec/25
    Decision 9 + spec/15 delegate isolation: coordinator-registered
    extractors do NOT leak into delegate evaluations.
    """

    def __init__(self) -> None:
        self._registry: dict[str, Callable[[dict], str | None]] = {}
        self._register_builtins()

    def _register_builtins(self) -> None:
        """Pre-register all built-in heuristic extractors.

        Called unconditionally at construction time. Built-ins are inserted
        in BUILTIN_EXTRACTORS declaration order, which matches
        ``_BUILTIN_PRIORITY_ORDER`` — the two must stay in sync.
        """
        for name, fn in BUILTIN_EXTRACTORS.items():
            self._registry[name] = fn
            _logger.debug("registered built-in target extractor %r", name)

    def register(self, name: str, callable_: Callable[[dict], str | None]) -> None:
        """Register a named extractor. Raises ``ValueError`` on name collision.

        Collision raises ``ValueError`` (not silent overwrite) per spec/29
        §"Registry collision discipline" — mirrors ``register_tool_registry_backend``
        precedent. Operators replacing a built-in or a previously-registered
        custom extractor call ``replace()`` explicitly.

        Name format requirement: lowercase alphanumeric plus underscore
        (greppable, filesystem-safe, round-trip safe through descriptors).

        Args:
            name: Unique string ID for this extractor. Must be non-empty
                lowercase alphanumeric + underscore.
            callable_: Callable ``(dict) → str | None``. Receives the
                tool's ``tool_arguments`` dict; returns the canonical
                target string or ``None`` when no target is present.

        Raises:
            ValueError: ``name`` is already registered or has an invalid
                format.
        """
        if not name or not all(c.isalnum() or c == "_" for c in name):
            raise ValueError(
                f"target_extractor name {name!r} must be non-empty and contain "
                f"only lowercase alphanumeric characters plus underscore"
            )
        if name in self._registry:
            raise ValueError(
                f"target_extractor name {name!r} is already registered. "
                f"Use replace() to overwrite explicitly."
            )
        self._registry[name] = callable_
        _logger.debug("registered target extractor %r", name)

    def replace(self, name: str, callable_: Callable[[dict], str | None]) -> None:
        """Replace a registered extractor (explicit overwrite path).

        Unlike ``register()``, ``replace()`` succeeds whether or not
        ``name`` is already registered — it is the intentional-overwrite
        primitive. Operators who want to shadow a built-in call this.

        Args:
            name: Extractor name to overwrite (need not pre-exist).
            callable_: New callable to bind under ``name``.
        """
        self._registry[name] = callable_
        _logger.debug("replaced target extractor %r", name)

    def has(self, name: str) -> bool:
        """Return ``True`` when ``name`` is registered.

        Used by ``ToolRegistry.register()`` to validate
        ``ToolDefinition.target_extractor_id`` at registration time (loud
        early failure per spec/29 §"Registration order discipline").

        Args:
            name: Extractor name to probe.
        """
        return name in self._registry

    def list_names(self) -> list[str]:
        """Return a sorted list of all registered extractor names.

        Useful for diagnostic / doctor output and for operators inspecting
        which extractors are available before registering tools.
        """
        return sorted(self._registry.keys())

    def extract(
        self,
        tool_name: str,
        tool_arguments: dict,
        extractor_id: str | None = None,
        mcp_server: str | None = None,
    ) -> str | None:
        """Extract ``target_canonical`` from a tool's arguments.

        Two modes:

        **Named extractor (``extractor_id`` is set):** The named extractor
        MUST be registered — raises ``UnknownTargetExtractor`` if not.
        This invariant is enforced early at ``tool_registry.register()``
        time (spec/29 §"Registration order discipline"), so reaching this
        code path with an unknown ``extractor_id`` indicates a programmatic
        caller that bypassed the normal registration gate.

        **Heuristic fallback (``extractor_id`` is ``None``):** Tries
        built-in extractors in priority order (``recipient_to`` →
        ``recipient_field`` → ``target_field`` → ``url_field`` →
        ``repository_field`` → ``customer_id_field`` → ``channel_id_field``),
        returning the first non-``None`` result. Returns ``None`` if no
        heuristic matches.

        If extraction raises (extractor callable throws), the exception is
        caught and the result is ``None`` — fail-closed at MandateCheck
        step 5 per spec/29. The exception is logged at DEBUG level so
        operators diagnosing a missed target can trace it.

        MCP tools prefix the extracted value with ``mcp:<server>:`` per
        spec/29 line 466.

        Args:
            tool_name: The tool name (used in exception messages only).
            tool_arguments: The tool's input dict from the LLM tool_use block.
            extractor_id: Optional registered extractor name. When set,
                MUST be registered.
            mcp_server: Optional MCP server name for the
                ``mcp:<server>:<target>`` prefix. ``None`` for non-MCP tools.

        Returns:
            Extracted ``target_canonical`` string, or ``None`` when no
            target could be extracted.

        Raises:
            UnknownTargetExtractor: ``extractor_id`` is not ``None`` and
                is not registered.
        """
        if extractor_id is not None:
            if extractor_id not in self._registry:
                raise UnknownTargetExtractor(
                    f"target_extractor_id {extractor_id!r} not registered. "
                    f"Tool {tool_name!r} references an unknown extractor. "
                    f"Registered extractors: {self.list_names()}"
                )
            try:
                target = self._registry[extractor_id](tool_arguments)
            except Exception as exc:
                # Spec/29: failed extraction returns None (fail-closed at
                # MandateCheck step 5). Log at DEBUG for operator traceability.
                _logger.debug(
                    "target extractor %r raised for tool %r: %s: %s",
                    extractor_id,
                    tool_name,
                    type(exc).__name__,
                    exc,
                )
                target = None
        else:
            # Heuristic fallback — try built-ins in priority order.
            # Only built-ins participate in heuristic mode (custom
            # extractors require explicit extractor_id to avoid
            # surprise implicit matching).
            target = None
            for name in _BUILTIN_PRIORITY_ORDER:
                fn = self._registry.get(name)
                if fn is None:
                    continue
                try:
                    candidate = fn(tool_arguments)
                    if candidate is not None:
                        target = candidate
                        break
                except Exception as exc:
                    _logger.debug(
                        "built-in target extractor %r raised for tool %r: %s: %s",
                        name,
                        tool_name,
                        type(exc).__name__,
                        exc,
                    )
                    continue

        if target is not None and mcp_server:
            target = f"mcp:{mcp_server}:{target}"

        return target
