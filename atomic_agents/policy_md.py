"""Parse ``policy.md`` operator config for the policy layer (spec/32).

``policy.md`` is the operator's fleet-level and per-agent policy configuration.
The framework reads it; the framework never writes to it (policy authorship
belongs exclusively to the operator).

File shape:

- **Single file, two sections**: top-level YAML block carries fleet defaults
  that apply to all agents; the optional ``agents:`` block carries per-agent
  overrides.  There is no per-agent ``policy.md`` file — all policy lives
  in one operator-owned file at ``<project_root>/policy.md`` (spec/32
  §"Single-file shape" Decision 1).

Embedded-YAML shape: the entire ``policy.md`` body is YAML (either bare or
inside a fenced ````yaml`` block).  The convention mirrors ``model.md``'s
``cost_guardrails:`` block — YAML-in-markdown, operator-editable in any text
editor or Obsidian.

YAML shape supported::

    # Fleet defaults
    cost_caps:
      daily_usd: 50.0
      monthly_usd: 1000.0

    tools:
      allow: [read_file, search, write_note]
      deny: [delete_file]

    mcp_servers:
      allow: [filesystem, weather]
      deny: [insecure-server]

    model: claude-opus-4-7

    # Per-agent overrides
    agents:
      caldwell:
        cost_caps:
          daily_usd: 30.0       # tighter cap; MIN(50, 30) = 30 effective
        tools:
          deny: [search]       # in addition to fleet deny
      procurement:
        model: gpt-4           # replaces fleet model for this agent
      empty-agent: {}          # F12: no override; fleet defaults apply

Parser contract:

- File missing → returns no-opinion ``PolicySnapshot()``.  Caller decides
  what to do.
- File empty or whitespace-only → returns no-opinion ``PolicySnapshot()``.
- Any malformed YAML → raises ``PolicyInvalid`` for the whole file.
- Negative cost-cap value → ``PolicyInvalid``.
- ``agents:`` is a list instead of a dict → ``PolicyInvalid``.
- agent_name in ``agents:`` with control char / newline → ``PolicyInvalid``.
- Tool/MCP name with control char / newline → ``PolicyInvalid``.
- Same tool in both fleet allow AND fleet deny → not a structural error
  (deny wins per F7) but emits a ``logging.warning`` at parse time.
- ``agents.foo: {}`` (empty body) → no override applied; fleet defaults apply
  (F12 resolution — explicit no-opinion at agent layer).

Parser has NO state machinery (unlike ``mandates_md.py`` which carries
reservation/lifecycle state).  It is a pure function: ``Path → PolicySnapshot``.

Used by ``FilesystemPolicyBackend`` ONLY in v1.  Future SaaS / Postgres backends
use their own canonical storage and parse format — they do NOT use this module.

PR 1 of #89.  Parser only — returns ``PolicySnapshot`` + ``AgentPolicyOverride``
dataclass instances.  ``FilesystemPolicyBackend`` (``policy/filesystem.py``)
holds one snapshot cached per ``(mtime_ns, st_size)`` of ``policy.md``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .policy.types import CostCaps, PolicyInvalid

_logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────
# Size cap — alias-bomb DoS defense (spec/25 PR 1 lesson)
#
# YAML ``safe_load`` blocks arbitrary-object construction but DOES expand
# aliases; a billion-laughs-shaped policy.md can expand to multi-GB RSS in
# milliseconds. The ToolRegistry arc (spec/25) paid this learning at 33 GB
# RSS in PR 1 Step 11 testing and capped descriptor size at 256 KB. The
# same cap applies here. Operators whose policy.md legitimately needs more
# space should split per-agent overrides into multiple files (future work)
# or file an issue.

MAX_POLICY_MD_BYTES = 256 * 1024  # 256 KiB

# ──────────────────────────────────────────────────────────────────────────
# Compiled patterns (zero I/O at module level)

# Fenced YAML block: ```yaml ... ``` (markdown code-fence convention)
_FENCED_YAML_RE = re.compile(
    r"```(?:yaml)?\s*\n(.*?)```",
    re.DOTALL | re.IGNORECASE,
)

# agent_name: same loosened pattern used in FilesystemPolicyBackend._validate_agent_name
# (D2 — permits dots, plus, at-sign in addition to the original alphanumeric + _-).
# Checked at parse time to catch operator file errors at load, not runtime.
_AGENT_NAME_RE = re.compile(r"^[a-zA-Z0-9_.+@-]+$")

# Control-character detector (used for tool/MCP names and agent names).
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


# ──────────────────────────────────────────────────────────────────────────
# Output dataclasses


@dataclass(frozen=True)
class AgentPolicyOverride:
    """Per-agent policy override block parsed from the ``agents:`` section.

    All fields are optional.  ``None`` means "no opinion at this layer"
    (distinct from an empty frozenset, which means "explicit empty list").
    Per F12: an agent entry with an empty body ``{}`` produces an instance
    where ALL fields are ``None`` — fleet defaults apply everywhere.
    """

    cost_caps: CostCaps | None = None
    tools_allow: frozenset[str] | None = None
    tools_deny: frozenset[str] | None = None
    mcp_allow: frozenset[str] | None = None
    mcp_deny: frozenset[str] | None = None
    model: str | None = None


@dataclass(frozen=True)
class PolicySnapshot:
    """Parsed ``policy.md`` content returned by ``parse_policy_md``.

    Internal to ``FilesystemPolicyBackend`` — not part of the
    ``PolicyBackend`` Protocol surface.  ``FilesystemPolicyBackend`` holds
    one of these cached per ``(mtime_ns, st_size)`` of ``policy.md``.

    All fleet-level fields default to "no opinion" (empty frozensets,
    ``None`` caps, ``None`` model).  A ``PolicySnapshot()`` constructed with
    no arguments is the correct representation of an absent or empty
    ``policy.md``.
    """

    fleet_cost_caps: CostCaps = field(default_factory=CostCaps)
    fleet_tools_allow: frozenset[str] = field(default_factory=frozenset)
    fleet_tools_deny: frozenset[str] = field(default_factory=frozenset)
    fleet_mcp_allow: frozenset[str] = field(default_factory=frozenset)
    fleet_mcp_deny: frozenset[str] = field(default_factory=frozenset)
    fleet_model: str | None = None

    # Per-agent overrides: agent_name → AgentPolicyOverride.
    # An empty body ``{}`` maps to AgentPolicyOverride() (all None — F12).
    # Absent agent name means no entry at all; same effective result.
    agent_overrides: dict[str, AgentPolicyOverride] = field(default_factory=dict)


# ──────────────────────────────────────────────────────────────────────────
# Public entry point


def parse_policy_md(path: Path) -> PolicySnapshot:
    """Parse ``<project_root>/policy.md`` into a ``PolicySnapshot``.

    Args:
        path: Filesystem path to ``policy.md``.

    Returns:
        A no-opinion ``PolicySnapshot()`` when the file is absent or empty.
        A fully-populated ``PolicySnapshot`` when the file contains valid
        policy configuration.

    Raises:
        PolicyInvalid: On malformed YAML, negative cap values, structural
            errors (``agents:`` is a list instead of a dict), agent names
            with control chars / path-traversal tokens, or tool/MCP names
            with control chars / newlines.
    """
    if not path.exists():
        return PolicySnapshot()

    # Size cap (alias-bomb / billion-laughs DoS defense — mirrors the
    # ToolRegistry spec/25 PR 1 fix; 256 KB is the same cap that bounded the
    # 33 GB RSS regression in #64 PR 1 Step 11 testing).
    try:
        st = path.stat()
    except OSError as exc:
        raise PolicyInvalid(f"could not stat policy.md at {path}: {exc}") from exc
    if st.st_size > MAX_POLICY_MD_BYTES:
        raise PolicyInvalid(
            f"policy.md at {path} exceeds the {MAX_POLICY_MD_BYTES}-byte "
            f"cap (got {st.st_size}B). The cap defends against YAML "
            f"alias-bomb DoS; if your policy genuinely needs more space, "
            f"split per-agent overrides across multiple files or file an issue."
        )

    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise PolicyInvalid(f"could not read policy.md at {path}: {exc}") from exc

    try:
        text = raw_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise PolicyInvalid(f"policy.md at {path} is not valid UTF-8: {exc}") from exc

    if not text.strip():
        return PolicySnapshot()

    yaml_text = _extract_yaml(text)
    if not yaml_text.strip():
        return PolicySnapshot()

    raw = _load_yaml(yaml_text, path)
    if raw is None:
        return PolicySnapshot()

    return _build_snapshot(raw, path)


# ──────────────────────────────────────────────────────────────────────────
# YAML extraction


def _extract_yaml(text: str) -> str:
    """Extract YAML content from markdown text.

    Handles two shapes:
    1. Fenced block: ````yaml ... ``` — extracts the block body.
    2. Bare file: the entire text is YAML (no fences present).

    When multiple fenced blocks are present, the first one wins.  Content
    outside the fence is ignored (allows operators to annotate their
    ``policy.md`` with markdown comments/prose above the YAML block).
    """
    match = _FENCED_YAML_RE.search(text)
    if match:
        return match.group(1)
    return text


# ──────────────────────────────────────────────────────────────────────────
# YAML loading


def _load_yaml(yaml_text: str, path: Path) -> dict[str, Any] | None:
    """Parse ``yaml_text`` as YAML and return the top-level mapping.

    Returns ``None`` when the document is empty.  Raises ``PolicyInvalid``
    on parse errors or when the top-level value is not a mapping.

    Uses ``yaml.safe_load`` (blocks arbitrary-object construction). The
    alias-bomb DoS defense is the ``MAX_POLICY_MD_BYTES`` size cap enforced
    in ``parse_policy_md`` BEFORE this function is called — ``safe_load``
    itself still expands aliases and is vulnerable to billion-laughs-style
    expansion absent a size bound (spec/25 PR 1 lesson; the ToolRegistry
    arc paid this learning at 33 GB RSS pre-fix).
    """
    try:
        obj = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise PolicyInvalid(
            f"policy.md at {path} contains invalid YAML: {exc}"
        ) from exc

    if obj is None:
        return None

    if not isinstance(obj, dict):
        raise PolicyInvalid(
            f"policy.md at {path}: top-level YAML must be a mapping "
            f"(got {type(obj).__name__}). The file must be a YAML document "
            f"with top-level keys like cost_caps, tools, mcp_servers, model, agents."
        )

    return obj


# ──────────────────────────────────────────────────────────────────────────
# Snapshot builder


def _build_snapshot(raw: dict[str, Any], path: Path) -> PolicySnapshot:
    """Build a ``PolicySnapshot`` from a validated top-level YAML dict.

    All field parsing is delegated to typed helpers.  Collected errors are
    raised as a single ``PolicyInvalid`` at the end of parsing.
    """
    errors: list[str] = []

    # ── fleet cost_caps ────────────────────────────────────────────────
    fleet_cost_caps = CostCaps()
    cost_caps_raw = raw.get("cost_caps")
    if cost_caps_raw is not None:
        try:
            fleet_cost_caps = _parse_cost_caps(cost_caps_raw, section="cost_caps")
        except PolicyInvalid as exc:
            errors.append(str(exc))

    # ── fleet tools ───────────────────────────────────────────────────
    fleet_tools_allow: frozenset[str] = frozenset()
    fleet_tools_deny: frozenset[str] = frozenset()
    tools_raw = raw.get("tools")
    if tools_raw is not None:
        try:
            fleet_tools_allow, fleet_tools_deny = _parse_allow_deny(
                tools_raw, section="tools"
            )
        except PolicyInvalid as exc:
            errors.append(str(exc))

    # ── fleet mcp_servers ─────────────────────────────────────────────
    fleet_mcp_allow: frozenset[str] = frozenset()
    fleet_mcp_deny: frozenset[str] = frozenset()
    mcp_raw = raw.get("mcp_servers")
    if mcp_raw is not None:
        try:
            fleet_mcp_allow, fleet_mcp_deny = _parse_allow_deny(
                mcp_raw, section="mcp_servers"
            )
        except PolicyInvalid as exc:
            errors.append(str(exc))

    # ── fleet model ───────────────────────────────────────────────────
    fleet_model: str | None = None
    model_raw = raw.get("model")
    if model_raw is not None:
        try:
            fleet_model = _parse_model(model_raw, section="model")
        except PolicyInvalid as exc:
            errors.append(str(exc))

    # ── warn on fleet allow+deny overlap ──────────────────────────────
    if not errors:
        _warn_allow_deny_overlap(fleet_tools_allow, fleet_tools_deny, kind="tools")
        _warn_allow_deny_overlap(fleet_mcp_allow, fleet_mcp_deny, kind="mcp_servers")

    # ── per-agent overrides ────────────────────────────────────────────
    agent_overrides: dict[str, AgentPolicyOverride] = {}
    agents_raw = raw.get("agents")
    if agents_raw is not None:
        if not isinstance(agents_raw, dict):
            errors.append(
                f"policy.md at {path}: 'agents' must be a YAML mapping "
                f"(agent_name → override block); got {type(agents_raw).__name__}. "
                f"Example: agents:\\n  myagent:\\n    model: gpt-4"
            )
        else:
            for agent_name, override_raw in agents_raw.items():
                try:
                    override = _parse_agent_override(
                        agent_name, override_raw, path=path
                    )
                    agent_overrides[agent_name] = override
                except PolicyInvalid as exc:
                    errors.append(str(exc))

    if errors:
        bullet_list = "\n".join(f"  - {e}" for e in errors)
        raise PolicyInvalid(
            f"policy.md at {path} failed validation with "
            f"{len(errors)} error(s):\n{bullet_list}"
        )

    return PolicySnapshot(
        fleet_cost_caps=fleet_cost_caps,
        fleet_tools_allow=fleet_tools_allow,
        fleet_tools_deny=fleet_tools_deny,
        fleet_mcp_allow=fleet_mcp_allow,
        fleet_mcp_deny=fleet_mcp_deny,
        fleet_model=fleet_model,
        agent_overrides=agent_overrides,
    )


# ──────────────────────────────────────────────────────────────────────────
# Field parsers


def _parse_cost_caps(raw: Any, *, section: str) -> CostCaps:
    """Parse a ``cost_caps:`` dict into a ``CostCaps`` dataclass.

    Both dimensions (daily_usd, monthly_usd) are optional; ``None`` means
    "no opinion at this layer."  Negative values raise ``PolicyInvalid``.

    The ``cumulative_usd`` dimension from the original RFC is deferred to
    v1.1 (plan-subagent D1) — any ``cumulative_usd`` key present in the
    YAML is silently ignored so operators do not get a parse error after
    upgrading from a pre-PR-3a policy.md that included it.
    """
    if not isinstance(raw, dict):
        raise PolicyInvalid(
            f"{section}: must be a mapping with optional keys daily_usd, "
            f"monthly_usd; got {type(raw).__name__}"
        )

    daily_usd = _parse_usd_cap(raw.get("daily_usd"), field=f"{section}.daily_usd")
    monthly_usd = _parse_usd_cap(raw.get("monthly_usd"), field=f"{section}.monthly_usd")

    return CostCaps(
        daily_usd=daily_usd,
        monthly_usd=monthly_usd,
    )


def _parse_usd_cap(raw: Any, *, field: str) -> float | None:
    """Coerce a YAML value to a non-negative float USD cap, or ``None``.

    Returns ``None`` when ``raw`` is ``None`` (no opinion at this layer).
    Raises ``PolicyInvalid`` on booleans (bool is an int subclass — reject
    explicitly), non-numeric types, or negative values.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise PolicyInvalid(f"{field}: USD cap must be a number; got boolean {raw!r}")
    if not isinstance(raw, (int, float)):
        raise PolicyInvalid(
            f"{field}: USD cap must be a number; got {type(raw).__name__}={raw!r}"
        )
    value = float(raw)
    if value < 0:
        raise PolicyInvalid(f"{field}: USD cap must be >= 0; got {value}")
    return value


def _parse_allow_deny(
    raw: Any, *, section: str
) -> tuple[frozenset[str], frozenset[str]]:
    """Parse a ``tools:`` or ``mcp_servers:`` block into (allow, deny) frozensets.

    Accepted shape::

        tools:
          allow: [read_file, search]
          deny: [delete_file]

    Both ``allow`` and ``deny`` are optional; absent → empty frozenset.
    Tool/MCP names with control chars / newlines → ``PolicyInvalid``.
    """
    if not isinstance(raw, dict):
        raise PolicyInvalid(
            f"{section}: must be a mapping with optional 'allow' and 'deny' "
            f"list keys; got {type(raw).__name__}"
        )

    allow_raw = raw.get("allow")
    deny_raw = raw.get("deny")

    allow = _parse_name_list(allow_raw, field=f"{section}.allow", kind="tool/server")
    deny = _parse_name_list(deny_raw, field=f"{section}.deny", kind="tool/server")

    return allow, deny


def _parse_name_list(raw: Any, *, field: str, kind: str) -> frozenset[str]:
    """Parse a YAML list of name strings into a ``frozenset[str]``.

    Returns an empty frozenset when ``raw`` is ``None``.  Each name is
    validated: non-string, empty, or control-char-containing names raise
    ``PolicyInvalid``.  MCP server names like ``mcp:server:tool.name`` are
    valid (dots, colons, dashes permitted per F9).
    """
    if raw is None:
        return frozenset()
    if not isinstance(raw, list):
        raise PolicyInvalid(
            f"{field}: must be a list of {kind} name strings; got {type(raw).__name__}"
        )
    names: list[str] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, str):
            raise PolicyInvalid(
                f"{field}[{idx}]: {kind} name must be a string; "
                f"got {type(item).__name__}={item!r}"
            )
        if not item:
            raise PolicyInvalid(f"{field}[{idx}]: {kind} name must not be empty")
        if _CONTROL_CHARS_RE.search(item):
            raise PolicyInvalid(
                f"{field}[{idx}]: {kind} name {item!r} contains a control "
                f"character or newline — refused"
            )
        names.append(item)
    return frozenset(names)


def _parse_model(raw: Any, *, section: str) -> str:
    """Parse the ``model:`` field into a plain string.

    Raises ``PolicyInvalid`` on non-string or empty values.
    """
    if not isinstance(raw, str):
        raise PolicyInvalid(
            f"{section}: model must be a non-empty string; "
            f"got {type(raw).__name__}={raw!r}"
        )
    value = raw.strip()
    if not value:
        raise PolicyInvalid(
            f"{section}: model must be a non-empty string; got empty string"
        )
    return value


def _parse_agent_override(
    agent_name: Any, override_raw: Any, *, path: Path
) -> AgentPolicyOverride:
    """Parse one entry from the ``agents:`` block into an ``AgentPolicyOverride``.

    ``agent_name`` is operator-supplied and validated before use.
    ``override_raw`` may be ``None`` (YAML null) or ``{}`` (empty body) —
    both produce an ``AgentPolicyOverride()`` with all fields ``None`` (F12).

    Raises ``PolicyInvalid`` for invalid agent names or malformed override
    fields.
    """
    # ── validate agent name ────────────────────────────────────────────
    if not isinstance(agent_name, str):
        raise PolicyInvalid(
            f"policy.md at {path}: agents: key must be a string agent name; "
            f"got {type(agent_name).__name__}={agent_name!r}"
        )
    if not agent_name:
        raise PolicyInvalid(f"policy.md at {path}: agents: key must not be empty")
    if _CONTROL_CHARS_RE.search(agent_name):
        raise PolicyInvalid(
            f"policy.md at {path}: agents: key {agent_name!r} contains a "
            f"control character or newline — refused"
        )
    if not _AGENT_NAME_RE.match(agent_name):
        raise PolicyInvalid(
            f"policy.md at {path}: agents: key {agent_name!r} must match "
            f"[a-zA-Z0-9_.+@-]+ (no path separators, leading dot, or control chars)"
        )

    # ── F12: empty/null body → no override (all fields None) ──────────
    if override_raw is None or override_raw == {}:
        return AgentPolicyOverride()

    if not isinstance(override_raw, dict):
        raise PolicyInvalid(
            f"policy.md at {path}: agents.{agent_name}: override must be a "
            f"mapping or empty ({{}}); got {type(override_raw).__name__}"
        )

    prefix = f"agents.{agent_name}"
    errors: list[str] = []

    # ── cost_caps ──────────────────────────────────────────────────────
    cost_caps: CostCaps | None = None
    caps_raw = override_raw.get("cost_caps")
    if caps_raw is not None:
        try:
            cost_caps = _parse_cost_caps(caps_raw, section=f"{prefix}.cost_caps")
        except PolicyInvalid as exc:
            errors.append(str(exc))

    # ── tools ──────────────────────────────────────────────────────────
    tools_allow: frozenset[str] | None = None
    tools_deny: frozenset[str] | None = None
    tools_raw = override_raw.get("tools")
    if tools_raw is not None:
        try:
            raw_allow, raw_deny = _parse_allow_deny(
                tools_raw, section=f"{prefix}.tools"
            )
            # Preserve None-vs-empty distinction: only set if key was present
            tools_allow = raw_allow
            tools_deny = raw_deny
        except PolicyInvalid as exc:
            errors.append(str(exc))
    else:
        # Key absent → no opinion at this layer (None, not empty frozenset)
        tools_allow = None
        tools_deny = None

    # ── mcp_servers ────────────────────────────────────────────────────
    mcp_allow: frozenset[str] | None = None
    mcp_deny: frozenset[str] | None = None
    mcp_raw = override_raw.get("mcp_servers")
    if mcp_raw is not None:
        try:
            raw_mcp_allow, raw_mcp_deny = _parse_allow_deny(
                mcp_raw, section=f"{prefix}.mcp_servers"
            )
            mcp_allow = raw_mcp_allow
            mcp_deny = raw_mcp_deny
        except PolicyInvalid as exc:
            errors.append(str(exc))
    else:
        mcp_allow = None
        mcp_deny = None

    # ── model ──────────────────────────────────────────────────────────
    model: str | None = None
    model_raw = override_raw.get("model")
    if model_raw is not None:
        try:
            model = _parse_model(model_raw, section=f"{prefix}.model")
        except PolicyInvalid as exc:
            errors.append(str(exc))

    if errors:
        bullet_list = "\n".join(f"  - {e}" for e in errors)
        raise PolicyInvalid(
            f"agents.{agent_name} override failed validation with "
            f"{len(errors)} error(s):\n{bullet_list}"
        )

    return AgentPolicyOverride(
        cost_caps=cost_caps,
        tools_allow=tools_allow,
        tools_deny=tools_deny,
        mcp_allow=mcp_allow,
        mcp_deny=mcp_deny,
        model=model,
    )


# ──────────────────────────────────────────────────────────────────────────
# Warning helpers


def _warn_allow_deny_overlap(
    allow: frozenset[str],
    deny: frozenset[str],
    *,
    kind: str,
) -> None:
    """Emit a warning when a name appears in both fleet allow and deny.

    Per spec/32: same name in both is not a structural error (deny wins per
    F7), but the operator likely has a typo or config drift.  Surface it at
    parse time rather than silently applying deny-wins.
    """
    overlap = allow & deny
    if overlap:
        sorted_overlap = sorted(overlap)
        _logger.warning(
            "policy.md: %s names %r appear in both 'allow' and 'deny' "
            "at fleet level. Deny wins (F7), but this is likely a "
            "configuration mistake. Remove the name from one list.",
            kind,
            sorted_overlap,
        )
