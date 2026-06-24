"""FilesystemAgentRegistryBackend — directory-walk reference implementation (spec/51).

This is the default (always-on) backend for single-host deployments. It discovers
agents by walking agents_root and applying the spec/37:314 predicate:

    A folder is a framework agent iff:
    - It is a direct subdirectory of agents_root.
    - Its name does NOT begin with '_' or '.'.
    - model.md is present AND readable without IOError.
    - Its resolved path is contained within agents_root (symlink-escape guard).

Discovery predicate notes (spec/51 MUST 4 / spec/37:314-317):
    parse_model_md() (from _model.py) TOLERATES malformed embedded YAML —
    it catches yaml.YAMLError internally and falls back to defaults. So the
    actual exclusion condition is NOT a YAML parse failure; it is:
    - model.md absent: excluded.
    - model.md unreadable (IOError/PermissionError): excluded (fail-soft, logged).
    - model.md present and readable (even if content is malformed YAML): INCLUDED.
    This matches spec/37:315-317 verbatim.

Symlink containment (part of the MUST 5 fail-soft contract):
    The list_agents() enumeration loop calls safe_resolve_under() on each
    candidate directory before applying the model.md predicate. A symlinked
    agent folder whose .resolve() lands outside agents_root is skipped (logged
    at WARNING, fail-soft), matching the OutcomeBackend/JournalBackend pattern.
    spec/51 numbers this under MUST 5 (fail-soft per agent), not a dedicated
    Implementer-Contract MUST.

Governance.md parsing:
    If governance.md is present, parse it for structured fields. Five states:
    - ABSENT: has_governance=False, governance=None.
    - PRESENT_VALID: has_governance=True, governance=GovernanceRecord.
    - PRESENT_INVALID: has_governance=True, governance=GovernanceRecord with
        non-empty parse_errors list. The entry IS returned (MUST 5: fail-soft
        and non-fabricating).
    - PRESENT_NO_BLOCK: has_governance=True, governance=None — governance.md
        exists and is readable but contains no 'governance:' YAML block, so
        there is no structured record (file presence still sets has_governance).
    - PRESENT_UNREADABLE (IOError): has_governance=False, governance=None,
        WARNING logged.

Governance.md YAML block structure:
    One embedded ```yaml ... ``` block with key 'governance:' at the root.
    Parsed the same way _model.py parses cost_guardrails blocks: re.findall
    to locate all yaml blocks, yaml.safe_load each, pick the one containing
    'governance:'. Sections outside the YAML block are prose-only (not parsed).

register_agent() / unregister_agent():
    Always raise RegistrationNotSupported — this is a discovery-only backend.
    Registration would require a separate sidecar database (future backend).

TOCTOU safety:
    get_agent() wraps all filesystem reads in try/except OSError → None.
    If model.md vanishes between list_agents() and get_agent(), returns None.

Construction is side-effect-free (no filesystem I/O in __init__).

Import boundary (circular-import safety):
    Imports only from ..exceptions, .._io, .._model, .types — no imports from
    ..agent, .._llm, .._costs, ..logs, or any module that transitively imports
    those, so it forms no import cycle with the LLM stack. NOTE: importing the
    package still triggers atomic_agents/__init__.py, which eagerly loads the
    LLM stack; the boundary here is cycle-safety, not lazy-load.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .._io import safe_resolve_under
from .._model import parse_model_md
from ..exceptions import (
    GovernanceParseError,
    PathTraversalError,
    RegistrationNotSupported,
)
from .types import (
    AgentEntry,
    AgentRef,
    AgentRegistryCapabilities,
    GovernanceRecord,
)

_logger = logging.getLogger(__name__)

# Governance YAML top-level key (governance.md embeds structured fields under this key)
_GOVERNANCE_KEY = "governance"

# Defense-in-depth size cap for governance.md (security INFO). A hostile or
# accidentally-huge governance.md must not blow up the fleet-wide discovery
# loop. A file larger than this is treated as unreadable (fail-soft skip),
# matching the PRESENT_UNREADABLE path. 64 KiB is far above any realistic
# governance.md (a few enum fields + prose sections).
_GOVERNANCE_MAX_BYTES = 64 * 1024


class FilesystemAgentRegistryBackend:
    """Filesystem reference impl for AgentRegistryBackend Protocol (spec/51).

    Fleet-scoped to agents_root — enumerates sibling agent folders.
    Construction is side-effect-free (no filesystem I/O in __init__).

    NOTE: This is a PROPERTY-based capabilities implementation, matching the
    mcp_registry/backend.py:147 convention (@property, not a method call).
    """

    def __init__(self, agents_root: Path) -> None:
        """Construct a registry backend for agents_root.

        Args:
            agents_root: the fleet root directory. Typically ~/docs/agents or
                ATOMIC_AGENTS_ROOT. May not exist yet (list_agents returns []).
        """
        # Stored UNRESOLVED: list_agents()/get_agent() pass this to
        # safe_resolve_under(), which resolves the root internally on each call
        # (_io.safe_resolve_under), so a constructor-time .resolve() would be
        # dead work. Keeping __init__ genuinely side-effect-free (no filesystem
        # I/O — resolve() touches the FS for symlink resolution) per the class
        # docstring's "side-effect-free" promise.
        self._agents_root = agents_root

    @property
    def backend_id(self) -> str:
        """Stable backend identifier for this implementation."""
        return "filesystem"

    def list_agents(self, *, include_governance: bool = True) -> list[AgentRef]:
        """Enumerate all framework-recognized agents under agents_root.

        See module docstring for the full predicate and fail-soft contract.

        Args:
            include_governance: when True (default), parse each agent's
                governance.md and populate has_governance / governance. When
                False, SKIP the governance.md read+parse — every entry carries
                has_governance=False, governance=None (progressive disclosure,
                Principle #6; used by id-only callers like discover_agents()).
                The discovery predicate, sort order, and the rest of the
                AgentRef are identical for both values.

        Returns:
            list[AgentRef] sorted lexicographically by id (MUST 6). Empty when:
            - agents_root does not exist
            - agents_root is not a directory
            - No qualifying subdirectory exists
        """
        if not self._agents_root.exists():
            return []

        if not self._agents_root.is_dir():
            _logger.warning("agents_root is not a directory: %s", self._agents_root)
            return []

        try:
            entries_iter = self._agents_root.iterdir()
        except NotADirectoryError:
            _logger.warning("agents_root is not a directory: %s", self._agents_root)
            return []
        except PermissionError as exc:
            _logger.warning(
                "PermissionError enumerating agents_root %s: %s",
                self._agents_root,
                exc,
            )
            return []
        except OSError as exc:
            _logger.warning(
                "OSError enumerating agents_root %s: %s",
                self._agents_root,
                exc,
            )
            return []

        discovered_at = datetime.now(tz=timezone.utc).isoformat()
        results: list[AgentRef] = []

        for candidate in entries_iter:
            # MUST 5 (fail-soft): a single malformed/symlinked entry MUST NOT
            # abort the fleet loop. is_dir() and resolve() (here and in the
            # AgentRef below) can raise OSError (ELOOP / permission denial on an
            # intermediate component) or RuntimeError (symlink loop) on ONE
            # pathological entry; catching them per-entry keeps the enumeration
            # alive. PathTraversalError is the in-root containment skip; the
            # OSError/RuntimeError arm is the resolution-failure skip. Both are
            # logged and `continue`, never raised.
            try:
                # Skip non-directories (files in agents_root are not agents).
                if not candidate.is_dir():
                    continue

                # Skip _- and .-prefixed dirs (e.g. _dashboard/, .git/).
                if candidate.name.startswith(("_", ".")):
                    continue

                # Symlink containment guard. Resolve the candidate and verify it
                # stays inside agents_root. A symlinked agent folder pointing
                # outside agents_root is skipped, never raised. The resolved path
                # is reused for AgentRef.location below so the candidate is
                # resolved exactly once, under this same fail-soft guard (a
                # second resolve() at the append site would be unguarded).
                resolved = safe_resolve_under(candidate.name, self._agents_root)
            except PathTraversalError:
                _logger.warning(
                    "agent folder %r resolves outside agents_root %s — skipping "
                    "(symlink escape guard, MUST 5 fail-soft)",
                    candidate.name,
                    self._agents_root,
                )
                continue
            except (OSError, RuntimeError):
                _logger.warning(
                    "agent folder %r could not be resolved (is_dir/resolve "
                    "raised) — skipping (MUST 5 fail-soft)",
                    candidate.name,
                    exc_info=True,
                )
                continue

            # spec/37:314 predicate: model.md is present AND readable without IOError.
            model_md = candidate / "model.md"
            if not model_md.exists():
                continue

            # Symlink-escape guard on model.md (MUST 5 fail-soft). A model.md
            # symlinked to an out-of-tree file would make parse_model_md() read
            # an arbitrary host file; refuse the folder as NOT a framework agent
            # rather than following the link. Mirrors OutcomeBackend's
            # is_symlink() refusal on result.json. The directory-level
            # safe_resolve_under() guard above does NOT cover a symlinked FILE
            # inside an in-root directory, so this is the file-level companion.
            if model_md.is_symlink():
                _logger.warning(
                    "model.md for agent %r is a symlink — skipping (symlink "
                    "escape guard, MUST 5 fail-soft)",
                    candidate.name,
                )
                continue

            try:
                parse_model_md(model_md)
            except IOError:
                _logger.warning(
                    "model.md at %s is unreadable — skipping agent %r",
                    model_md,
                    candidate.name,
                )
                continue
            except Exception:
                # parse_model_md() tolerates malformed YAML (returns defaults,
                # never raises on YAML errors per spec/37:315-317). Other
                # unexpected exceptions are treated as IOError-equivalent.
                _logger.warning(
                    "unexpected error reading model.md at %s — skipping agent %r",
                    model_md,
                    candidate.name,
                    exc_info=True,
                )
                continue

            # Parse governance.md (fail-soft per MUST 5). Skipped entirely when
            # the caller does not need governance (progressive disclosure).
            if include_governance:
                has_governance, governance = self._parse_governance(candidate)
            else:
                has_governance, governance = False, None

            results.append(
                AgentRef(
                    id=candidate.name,
                    location=str(resolved),
                    discovered_at=discovered_at,
                    has_governance=has_governance,
                    governance=governance,
                )
            )

        return sorted(results, key=lambda r: r.id)

    def get_agent(self, agent_id: str) -> AgentEntry | None:
        """Return the AgentEntry for agent_id, or None on miss (TOCTOU-safe).

        Args:
            agent_id: the agent folder name.

        Returns:
            AgentEntry (AgentRef) if present, None on miss (MUST 7). A `_`- or
            `.`-prefixed agent_id is a miss (returns None), agreeing with the
            MUST 3 exclusion that list_agents() applies — the registry's own
            enumeration deliberately hides those dirs, so get_agent() does not
            resurface them by id.

        Raises:
            PathTraversalError: if agent_id contains a path separator ('/' or
                '\\'), is equal to '.' or '..', or is empty (MUST 8 traversal
                guard). A name that merely *contains* '..' as a substring (e.g.
                'a..b') is a legitimate folder name and is NOT rejected here —
                safe_resolve_under() below provides containment for it.
        """
        if (
            not agent_id
            or "/" in agent_id
            or "\\" in agent_id
            or agent_id in (".", "..")
        ):
            raise PathTraversalError(
                f"agent_id {agent_id!r} is not a valid bare name",
                child=agent_id,
                root=str(self._agents_root),
            )

        # MUST 3 consistency: list_agents() excludes `_`/`.`-prefixed dirs (the
        # dashboard scratch dir, `.git`, etc. are NOT agents). get_agent() must
        # agree with that universe — otherwise get_agent('_dashboard') would
        # resurface a deliberately-hidden dir by id even though list_agents()
        # never yields it. This is a MISS (return None), not a traversal attack
        # (the dir is inside agents_root with a real model.md): an excluded name
        # is simply not a registry-recognized agent.
        if agent_id.startswith(("_", ".")):
            return None

        candidate = self._agents_root / agent_id

        # TOCTOU-safe: wrap ALL reads in try/except OSError → None.
        try:
            if not candidate.is_dir():
                return None
        except OSError:
            return None

        # MUST 5 (fail-soft): symlink containment guard. The resolved path is
        # reused for AgentRef.location below so the candidate is resolved exactly
        # once, under this guard. resolve() can raise OSError (ELOOP/permission)
        # or RuntimeError (symlink loop); both are TOCTOU-equivalent misses → None
        # (mirrors the is_dir() OSError→None guard above), never a crash.
        try:
            resolved = safe_resolve_under(agent_id, self._agents_root)
        except (PathTraversalError, OSError, RuntimeError):
            return None

        # spec/37:314 predicate check (MUST 4).
        model_md = candidate / "model.md"
        try:
            if not model_md.exists():
                return None
            # Symlink-escape guard on model.md (mirrors list_agents() and
            # OutcomeBackend's is_symlink() refusal): a model.md symlinked to an
            # out-of-tree file makes this folder NOT a framework agent → miss.
            if model_md.is_symlink():
                return None
            parse_model_md(model_md)
        except OSError:
            # TOCTOU-safe: model.md vanished or became unreadable → miss.
            return None
        except Exception:
            # parse_model_md() tolerates malformed YAML and does not raise on it;
            # an unexpected exception here is not a normal miss. Log it (with
            # exc_info so a real bug is diagnosable) and treat as a miss rather
            # than crashing the caller — mirrors the list_agents() broad-except.
            _logger.warning(
                "unexpected error reading model.md at %s for agent %r — "
                "treating as miss",
                model_md,
                agent_id,
                exc_info=True,
            )
            return None

        # Parse governance.md (fail-soft).
        has_governance, governance = self._parse_governance(candidate)

        return AgentRef(
            id=agent_id,
            location=str(resolved),
            discovered_at=datetime.now(tz=timezone.utc).isoformat(),
            has_governance=has_governance,
            governance=governance,
        )

    @property
    def capabilities(self) -> AgentRegistryCapabilities:
        """Backend capability declaration (property, not method — see Protocol note).

        FilesystemAgentRegistryBackend is:
        - Discovery-only (supports_registration=False): register_agent() and
          unregister_agent() always raise RegistrationNotSupported.
        - No canonical export in PR 1 (supports_canonical_export=False): the
          spec/40 seam is intentionally left open for a future state-owning
          backend.
        - Single-host only: filesystem atomicity does not extend across hosts.
        """
        return AgentRegistryCapabilities(
            backend_id="filesystem",
            supports_registration=False,
            supports_canonical_export=False,
            single_host_only=True,
        )

    def register_agent(self, entry: AgentRef) -> None:
        """Not supported — raises RegistrationNotSupported (spec/51 MUST 10).

        FilesystemAgentRegistryBackend is discovery-only. Registration would
        require a separate sidecar database (future backend scope).

        Raises:
            RegistrationNotSupported: always.
        """
        raise RegistrationNotSupported(
            "FilesystemAgentRegistryBackend is discovery-only and does not support "
            "register_agent(). Implement a database-backed AgentRegistryBackend "
            "(future scope) for write operations."
        )

    def unregister_agent(self, agent_id: str) -> None:
        """Not supported — raises RegistrationNotSupported (spec/51 MUST 10).

        NOTE: the canonical verb is ``unregister_agent`` (not 'deregister_agent').

        Raises:
            RegistrationNotSupported: always.
        """
        raise RegistrationNotSupported(
            f"FilesystemAgentRegistryBackend is discovery-only and does not support "
            f"unregister_agent({agent_id!r}). Implement a database-backed "
            "AgentRegistryBackend (future scope) for write operations."
        )

    # ──────────────────────────────────────────────────────────────
    # Internal helpers

    def _parse_governance(
        self, agent_dir: Path
    ) -> tuple[bool, GovernanceRecord | None]:
        """Parse governance.md for agent_dir.

        Returns (has_governance, governance_record_or_None).

        Five states:
        - ABSENT: (False, None) — governance.md does not exist.
        - PRESENT_VALID: (True, GovernanceRecord) — parsed cleanly.
        - PRESENT_INVALID: (True, GovernanceRecord with parse_errors) —
            present but had enum validation errors (fail-soft).
        - PRESENT_NO_BLOCK: (True, None) — governance.md exists and is readable
            but contains no 'governance:' YAML block (no structured record).
        - PRESENT_UNREADABLE: (False, None) — IOError/PermissionError.
        """
        gov_md = agent_dir / "governance.md"

        if not gov_md.exists():
            return False, None

        # Symlink-escape guard (MUST 5 fail-soft). A governance.md symlinked to
        # an out-of-tree file would let read_text() read an arbitrary host file;
        # refuse it and take the PRESENT_UNREADABLE path (has_governance=False).
        # Mirrors OutcomeBackend's is_symlink() refusal on result.json.
        if gov_md.is_symlink():
            _logger.warning(
                "governance.md at %s is a symlink — refusing to follow "
                "(has_governance=False, symlink escape guard, MUST 5 fail-soft)",
                gov_md,
            )
            return False, None

        # Defense-in-depth size cap (security INFO): a hostile multi-MB
        # governance.md must not blow up the fleet-wide discovery loop. Treat an
        # oversized file as unreadable (fail-soft skip), matching the
        # PRESENT_UNREADABLE path. stat() can raise (TOCTOU/permission) → also
        # fail-soft.
        try:
            if gov_md.stat().st_size > _GOVERNANCE_MAX_BYTES:
                _logger.warning(
                    "governance.md at %s exceeds %d bytes — skipping "
                    "(has_governance=False, size cap, MUST 5 fail-soft)",
                    gov_md,
                    _GOVERNANCE_MAX_BYTES,
                )
                return False, None
        except OSError as exc:
            _logger.warning(
                "governance.md at %s could not be stat()'d: %s — has_governance=False",
                gov_md,
                exc,
            )
            return False, None

        try:
            text = gov_md.read_text(encoding="utf-8")
        except (IOError, PermissionError, OSError) as exc:
            _logger.warning(
                "governance.md at %s is unreadable: %s — has_governance=False",
                gov_md,
                exc,
            )
            return False, None

        # Find the embedded YAML block containing 'governance:'.
        # Strip fenced blocks before searching for section headers (same pattern
        # as _model.py lines 104-115) to avoid false-positive header matches
        # inside YAML blocks.
        yaml_blocks = re.findall(r"```yaml\s*\n(.*?)```", text, re.DOTALL)
        gov_dict: dict | None = None
        deferred_yaml_error: str | None = None

        for block in yaml_blocks:
            try:
                parsed = yaml.safe_load(block)
            except yaml.YAMLError as exc:
                # A malformed YAML block is NOT necessarily the governance block —
                # governance.md may contain an illustrative/example fenced yaml
                # block. Defer the error: keep scanning, and only treat it as a
                # governance parse error if NO block yields the 'governance:' key.
                # (Don't short-circuit the first bad block, which could shadow a
                # valid governance block placed after it.)
                _logger.warning(
                    "governance.md at %s has an invalid YAML block (deferred — "
                    "only flagged if no valid governance block is found): %s",
                    gov_md,
                    exc,
                )
                if deferred_yaml_error is None:
                    deferred_yaml_error = f"YAML parse error: {exc}"
                continue

            if not isinstance(parsed, dict):
                continue
            # Only treat a 'governance:' block as AUTHORITATIVE when its value is
            # a dict. A null/empty first block (`governance:` with no body, or
            # `governance: null`) would otherwise set gov_dict=None and break,
            # SHADOWING a valid `governance: {...}` block placed later in the
            # file. Requiring a dict value lets the scan keep going to the real
            # block. (A non-null non-dict value — e.g. a string — is still a
            # genuine error and is caught by the post-loop isinstance check.)
            if _GOVERNANCE_KEY in parsed and isinstance(parsed[_GOVERNANCE_KEY], dict):
                gov_dict = parsed[_GOVERNANCE_KEY]
                break

        if gov_dict is None:
            if deferred_yaml_error is not None:
                # No valid governance block found AND at least one block failed
                # to parse — surface it as PRESENT_INVALID (fail-soft, MUST 5).
                return True, GovernanceRecord(
                    parse_errors=(deferred_yaml_error,),
                )
            # governance.md is present but has no governance: YAML block.
            # has_governance=True (file present), governance=None (no structured data).
            return True, None

        if not isinstance(gov_dict, dict):
            return True, GovernanceRecord(
                parse_errors=(
                    f"'governance:' YAML value is not a dict: {type(gov_dict).__name__}",
                ),
            )

        # Parse and validate the governance dict. Unknown enum values raise
        # GovernanceParseError (spec/51 §"governance.md schema": CLEAR parse
        # error, never silent misread). Catch and return a partial record
        # fail-soft (MUST 5).
        try:
            record = GovernanceRecord.from_dict(gov_dict)
        except GovernanceParseError as exc:
            _logger.warning(
                "governance.md at %s has invalid enum value: %s",
                gov_md,
                exc,
            )
            # PRESENT_INVALID: return has_governance=True + partial record.
            return True, GovernanceRecord(
                parse_errors=(str(exc),),
            )

        return True, record
