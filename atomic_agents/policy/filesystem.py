"""FilesystemPolicyBackend — project-root policy.md reference impl (spec/32).

This is the default backend for single-host deployments.  It reads the
operator-authored ``policy.md`` file from ``<project_root>/policy.md``
and surfaces fleet + per-agent policy through the ``PolicyBackend`` Protocol
(spec/32).

Three surface promises hold across PR 1 → PR 2:

1. **No policy.md, no opinion.**  When ``policy.md`` is absent or empty,
   every method returns the no-opinion value for its return type:
   ``CostCaps()`` (all None), ``True`` for allow/deny (default-allow), and
   ``None`` for model.  Every existing agent that has no ``policy.md``
   continues to work unchanged.

2. **Path-traversal refused at the API boundary** (spec/32 MUST #1).
   Operator- or agent-controlled ``agent_name``, ``tool_name``, and
   ``server_name`` are validated BEFORE any storage or dict access.
   ``_validate_agent_name`` permits ``[a-zA-Z0-9_-]+`` and rejects path-
   traversal tokens, control characters, newlines, empty strings, and leading
   dots.  ``_validate_tool_or_server_name`` rejects only control characters,
   newlines, and empty strings (allows dots, dashes, colons because
   ``mcp:server:tool.name`` is legitimate per F9).

3. **Construction is side-effect-free** (spec/32 MUST #4).  ``__init__``
   records ``project_root`` and initializes the cache to empty.  No stat,
   no parse, no env-var validation at construction.  First method call stats
   ``policy.md`` and parses it if the file is new or has changed.

Cache key is ``(mtime_ns, st_size)`` composite (F8).  This catches same-second
edits on 1-second-mtime-granularity filesystems via the size proxy.  Cache
invalidation uses ``os.stat()`` ``st_mtime_ns`` + ``st_size``; any change in
either dimension triggers a re-parse.

Concurrent parse is idempotent and lock-free (F10).  Two threads parsing
simultaneously after an mtime bump both produce identical ``PolicySnapshot``
values; last-write-wins on the cache attributes is correct.

Capabilities: ``cache_ttl_s=0`` (F1) — operators observe edits within 0
seconds of mtime+size change because the check runs on every method call.
``durable=True`` — the filesystem is a durable storage substrate.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .types import CostCaps, PolicyCapabilities

if TYPE_CHECKING:
    # Type-only imports avoid the circular-import cycle (policy_md → policy
    # package __init__ → backend bootstrap → filesystem → policy_md). With
    # ``from __future__ import annotations`` (above), all type annotations are
    # deferred strings, so runtime uses (``PolicySnapshot()`` construction +
    # ``parse_policy_md(...)`` call) are deferred to inside ``_get_snapshot``.
    from ..policy_md import AgentPolicyOverride, PolicySnapshot

_logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────
# Validation patterns (zero I/O at module level)

# agent_name: alphanumeric + underscore + hyphen only.  Rejects path separators,
# dots (leading-dot hidden-file trick), colons, spaces, and control chars.
_AGENT_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")

# Control-character detector (0x00-0x1F + DEL 0x7F).
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


# ──────────────────────────────────────────────────────────────────────────
# Validation helpers


def _validate_agent_name(name: str) -> None:
    """Validate an agent_name at the API boundary.

    Permits: ``[a-zA-Z0-9_-]+``
    Rejects:
    - Non-string or empty string.
    - Leading dot (hidden-file traversal trick).
    - ``..`` anywhere (directory traversal).
    - ``/`` or ``\\`` (path separators).
    - Control characters or newlines (log injection + path-token splitting).
    - Anything not matching ``[a-zA-Z0-9_-]+``.

    Raises ``ValueError`` on any violation.  Called BEFORE any storage or
    dict access in every public method (spec/32 MUST #1).
    """
    if not isinstance(name, str) or not name:
        raise ValueError(f"agent_name must be a non-empty string; got {name!r}")
    if name.startswith("."):
        raise ValueError(f"agent_name must not start with '.'; got {name!r}")
    if ".." in name:
        raise ValueError(f"agent_name must not contain '..'; got {name!r}")
    if "/" in name or "\\" in name:
        raise ValueError(f"agent_name must not contain path separators; got {name!r}")
    if _CONTROL_CHARS.search(name):
        raise ValueError(
            f"agent_name must not contain control characters or newlines; got {name!r}"
        )
    if not _AGENT_NAME_PATTERN.match(name):
        raise ValueError(f"agent_name must match [a-zA-Z0-9_-]+; got {name!r}")


def _validate_tool_or_server_name(name: str) -> None:
    """Validate a tool_name or server_name at the API boundary.

    Permits any non-empty string that does not contain control characters
    or newlines.  Dots, dashes, colons, and slashes are allowed because
    MCP server/tool names like ``mcp:server:tool.name`` are legitimate
    (F9 resolution).

    Raises ``ValueError`` on any violation.  Called BEFORE any storage or
    dict access in every public method (spec/32 MUST #1).
    """
    if not isinstance(name, str) or not name:
        raise ValueError(f"name must be a non-empty string; got {name!r}")
    if _CONTROL_CHARS.search(name):
        raise ValueError(
            f"name must not contain control characters or newlines; got {name!r}"
        )


# ──────────────────────────────────────────────────────────────────────────
# F7 composition helper


def _min_or_other(a: float | None, b: float | None) -> float | None:
    """None-aware MIN for cost cap composition (F7 / D2).

    ``None`` means "no opinion at this layer" and drops out of the MIN.
    Returns ``None`` only when BOTH inputs are ``None``.
    """
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def _is_allowed_per_f7(
    fleet_allow: frozenset[str],
    fleet_deny: frozenset[str],
    agent_override: AgentPolicyOverride | None,
    item_name: str,
    kind: Literal["tools", "mcp"],
) -> bool:
    """F7 field-level composition: merge allow, union deny, deny wins.

    Algorithm (F7):
    1. Effective allow = fleet_allow UNION agent_allow
       (None at agent layer means "no additional allow list")
    2. Effective deny  = fleet_deny UNION agent_deny
       (None at agent layer means "no additional deny list")
    3. If item in effective deny → False (deny wins).
    4. If effective allow is empty → True (default-allow; no allowlist active).
    5. Else → item in effective allow.

    ``kind`` selects whether to read ``tools_allow/deny`` or
    ``mcp_allow/deny`` from the agent override.
    """
    agent_allow: frozenset[str] = frozenset()
    agent_deny: frozenset[str] = frozenset()

    if agent_override is not None:
        if kind == "tools":
            if agent_override.tools_allow is not None:
                agent_allow = agent_override.tools_allow
            if agent_override.tools_deny is not None:
                agent_deny = agent_override.tools_deny
        else:  # "mcp"
            if agent_override.mcp_allow is not None:
                agent_allow = agent_override.mcp_allow
            if agent_override.mcp_deny is not None:
                agent_deny = agent_override.mcp_deny

    effective_allow = fleet_allow | agent_allow
    effective_deny = fleet_deny | agent_deny

    # Deny wins (step 3)
    if item_name in effective_deny:
        return False

    # Default-allow when no allowlist active (step 4)
    if not effective_allow:
        return True

    # Allowlist check (step 5)
    return item_name in effective_allow


# ──────────────────────────────────────────────────────────────────────────
# URL redaction helper


def _redact_url(url: str, max_len: int = 64) -> str:
    """Strip credentials from a URL for safe inclusion in error messages.

    Replaces the ``user:pass@`` portion with ``***@`` to avoid leaking
    secrets into logs or exception strings.  Truncates to ``max_len``
    characters after stripping credentials.
    """
    if "://" not in url:
        return url[:max_len] + ("..." if len(url) > max_len else "")
    scheme, _, rest = url.partition("://")
    if "@" in rest:
        _, _, host_part = rest.partition("@")
        return f"{scheme}://***@{host_part[:max_len]}"
    return f"{scheme}://{rest[:max_len]}"


# ──────────────────────────────────────────────────────────────────────────
# Backend


class FilesystemPolicyBackend:
    """Reference ``PolicyBackend`` for filesystem-resident ``policy.md``.

    Reads operator policy from ``<project_root>/policy.md``.  Construction
    is side-effect-free per spec/32 MUST #4 — no stat, no parse, no env-var
    validation at ``__init__``.  The mtime+size cache initializes to ``None``;
    first method call stats + parses ``policy.md``.

    Concurrent access is safe without a lock per F10: two threads parsing
    simultaneously after an mtime bump both produce identical
    ``PolicySnapshot`` values; last-write-wins on the cache is correct.

    Cache key is ``(mtime_ns, st_size)`` composite per F8: catches same-second
    edits on 1-second-mtime-granularity filesystems via the size proxy.

    Capabilities: ``cache_ttl_s=0`` per F1 — operators observe edits within
    0 seconds of mtime+size change because the check runs on every call.
    ``durable=True`` — filesystem is a durable storage substrate.
    """

    backend_id = "filesystem"

    def __init__(self, project_root: Path) -> None:
        """Construct without I/O.

        ``project_root`` is recorded as a ``Path``; it is NOT validated for
        existence (spec/32 MUST #4 — construction is side-effect-free).  First
        method call stats and parses ``<project_root>/policy.md``.

        Args:
            project_root: Directory containing ``policy.md``.  Need not exist
                at construction time; absence is handled gracefully at first
                call (no-opinion policy).
        """
        self._project_root = Path(project_root)
        self._cached_snapshot: PolicySnapshot | None = None
        self._cached_key: tuple[int, int] | None = None  # (mtime_ns, st_size)

    # ── cache-refreshing internal method ──────────────────────────────

    def _get_snapshot(self) -> PolicySnapshot:
        """Stat + maybe-parse + cache.  Idempotent under concurrent threads.

        Returns a no-opinion ``PolicySnapshot()`` when ``policy.md`` is
        absent.  Otherwise: if ``(mtime_ns, st_size)`` is unchanged since
        the last parse, returns the cached snapshot.  If the file changed,
        re-parses and caches.

        Per F8: cache key is ``(st.st_mtime_ns, st.st_size)`` — catches
        same-second edits via the size proxy.

        Per F10: no lock — concurrent threads may redundantly parse but
        both produce identical ``PolicySnapshot`` values; last-write-wins on
        ``self._cached_snapshot`` / ``self._cached_key`` is correct.
        """
        # Deferred imports (avoids circular: policy_md → policy package init
        # → backend bootstrap → filesystem → policy_md).
        from ..policy_md import PolicySnapshot, parse_policy_md

        policy_md_path = self._project_root / "policy.md"

        try:
            st = policy_md_path.stat()
        except FileNotFoundError:
            # No policy.md → no opinion; do NOT cache absence (file may appear)
            return PolicySnapshot()

        key = (st.st_mtime_ns, st.st_size)

        # Cache hit: key unchanged and snapshot is populated
        if self._cached_key == key and self._cached_snapshot is not None:
            return self._cached_snapshot

        # Cache miss: re-parse
        snapshot = parse_policy_md(policy_md_path)

        # Race-tolerant store: another thread may have set these between
        # our stat and our store.  Both stored snapshots are identical for
        # the same key; last-write-wins is correct (F10).
        self._cached_snapshot = snapshot
        self._cached_key = key
        return snapshot

    # ── PolicyBackend Protocol surface ────────────────────────────────

    def get_effective_caps(self, agent_name: str) -> CostCaps:
        """Return the effective cost caps for ``agent_name``.

        Composition (F7 / D2):
        - effective cap per dimension = MIN(fleet, agent-override).
        - ``None`` at either layer means "no opinion" and drops out of MIN.
        - Returns ``CostCaps()`` (all None) when ``policy.md`` is absent,
          the agent is not in ``agents:``, or no fleet caps are set.

        Raises ``ValueError`` on invalid ``agent_name`` (before any I/O).
        """
        _validate_agent_name(agent_name)  # BEFORE any dict access
        snapshot = self._get_snapshot()

        fleet = snapshot.fleet_cost_caps
        agent_override = snapshot.agent_overrides.get(agent_name)

        if agent_override is None or agent_override.cost_caps is None:
            return fleet

        agent_caps = agent_override.cost_caps
        return CostCaps(
            daily_usd=_min_or_other(fleet.daily_usd, agent_caps.daily_usd),
            monthly_usd=_min_or_other(fleet.monthly_usd, agent_caps.monthly_usd),
            cumulative_usd=_min_or_other(
                fleet.cumulative_usd, agent_caps.cumulative_usd
            ),
        )

    def is_tool_allowed(self, agent_name: str, tool_name: str) -> bool:
        """Return whether ``tool_name`` is allowed for ``agent_name``.

        Composition per F7:
        - effective allow = fleet_tools_allow UNION agent.tools_allow
        - effective deny  = fleet_tools_deny  UNION agent.tools_deny
        - deny wins over allow.
        - empty effective allow → default-allow (no allowlist active).

        Returns ``True`` (default-allow) when ``policy.md`` is absent.

        Raises ``ValueError`` on invalid ``agent_name`` or ``tool_name``
        (before any I/O).
        """
        _validate_agent_name(agent_name)  # BEFORE any dict access
        _validate_tool_or_server_name(tool_name)  # BEFORE any dict access
        snapshot = self._get_snapshot()
        return _is_allowed_per_f7(
            snapshot.fleet_tools_allow,
            snapshot.fleet_tools_deny,
            snapshot.agent_overrides.get(agent_name),
            tool_name,
            kind="tools",
        )

    def is_mcp_server_allowed(self, agent_name: str, server_name: str) -> bool:
        """Return whether ``server_name`` is allowed for ``agent_name``.

        Composition identical to ``is_tool_allowed`` but operating on the
        ``mcp_servers:`` allow/deny lists per F7.

        Returns ``True`` (default-allow) when ``policy.md`` is absent.

        Raises ``ValueError`` on invalid ``agent_name`` or ``server_name``
        (before any I/O).
        """
        _validate_agent_name(agent_name)  # BEFORE any dict access
        _validate_tool_or_server_name(server_name)  # BEFORE any dict access
        snapshot = self._get_snapshot()
        return _is_allowed_per_f7(
            snapshot.fleet_mcp_allow,
            snapshot.fleet_mcp_deny,
            snapshot.agent_overrides.get(agent_name),
            server_name,
            kind="mcp",
        )

    def get_effective_model(self, agent_name: str) -> str | None:
        """Return the effective model override for ``agent_name``, or ``None``.

        Per F7 model resolution: per-agent model REPLACES fleet model (not
        merged — there is one model, not a list).  If the agent has an
        explicit override, it wins regardless of fleet.  If the agent has no
        override, the fleet model is returned.  If neither is set, returns
        ``None`` (no opinion — ``model.md`` governs).

        Raises ``ValueError`` on invalid ``agent_name`` (before any I/O).
        """
        _validate_agent_name(agent_name)  # BEFORE any dict access
        snapshot = self._get_snapshot()

        agent_override = snapshot.agent_overrides.get(agent_name)
        # Per-agent REPLACES fleet (not merged — F7 model resolution)
        if agent_override is not None and agent_override.model is not None:
            return agent_override.model
        return snapshot.fleet_model

    def capabilities(self) -> PolicyCapabilities:
        """Return the backend capability snapshot.

        ``cache_ttl_s=0`` per F1: operators observe edits within 0 seconds
        of mtime+size change (check runs on every method call).
        ``durable=True``: filesystem is a durable storage substrate.

        This method is side-effect-free and does NOT stat the filesystem.
        """
        return PolicyCapabilities(cache_ttl_s=0, durable=True)


# ──────────────────────────────────────────────────────────────────────────
# URL factory


def make_filesystem_policy_backend_from_url(url: str) -> FilesystemPolicyBackend:
    """Construct a ``FilesystemPolicyBackend`` from a ``filesystem://`` URL.

    Per spec/32 MUST #4, does NOT validate path existence at construction.
    Per spec/32 MUST #6, raises ``ValueError`` with credentials redacted
    when the URL is malformed or uses the wrong scheme.

    URL format::

        filesystem:///absolute/path/to/project_root

    The triple-slash convention is standard for local filesystem URLs:
    ``filesystem://`` (scheme + empty authority) + ``/abs/path`` (absolute
    path starting with ``/``).

    Args:
        url: A ``filesystem:///path`` URL string.

    Returns:
        A ``FilesystemPolicyBackend`` targeting the given project root.

    Raises:
        ValueError: If ``url`` is not a string, is empty, is missing the
            ``://`` separator, uses a non-``filesystem`` scheme, or has a
            relative (non-``/``-prefixed) path component.
    """
    if not isinstance(url, str) or not url:
        raise ValueError("URL must be a non-empty string")

    if "://" not in url:
        raise ValueError(f"URL missing scheme separator '://': {_redact_url(url)!r}")

    scheme, _, rest = url.partition("://")
    if scheme.lower() != "filesystem":
        raise ValueError(
            f"Expected 'filesystem://' scheme; got scheme {scheme!r} "
            f"from URL {_redact_url(url)!r}"
        )

    # rest = "/abs/path" for filesystem:///abs/path
    # rest = "abs/path"  for filesystem://abs/path  (relative — rejected)
    if not rest.startswith("/"):
        raise ValueError(
            f"filesystem:// URL must use an absolute path (starting with '/'); "
            f"got {_redact_url(url)!r}"
        )

    return FilesystemPolicyBackend(Path(rest))
