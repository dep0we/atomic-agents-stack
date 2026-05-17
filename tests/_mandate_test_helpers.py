"""Shared test helpers for the mandate subsystem (#124 PR 3a + PR 3b + PR 4).

Centralizes the "inject a mandate into the backend at test time" +
"build a proposal citing the mandate" + "register an extractor against
an agent" boilerplate so test files don't re-implement ~30 LOC each.

Mirrors `tests/test_tool_registry_protocol_conformance.py`'s
`make_tool_in_backend` shape — landed before PR 3a per the
plan-subagent Risk G finding (the same "conformance helper trap"
that re-architecture-cycled #63 PR 3 when `make_agent_dir` couldn't
serve SQLite-backed conformance tests).

When PR 3b adds reservation events + crash recovery, these helpers
extend (don't replace). When PR 4 adds project-root resolution, the
`scope` parameter accepts the project shape naturally.
"""

from __future__ import annotations

import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from atomic_agents.mandate import Mandate, MandateBackend
from atomic_agents.mandate.types import MandateConstraints, RevocationState


# ──────────────────────────────────────────────────────────────────
# mandates.md generation


_DEFAULT_GRANTED_BY = "test-operator@example.com"


def make_mandate_md_content(
    mandate_id: str,
    *,
    granted_by: str = _DEFAULT_GRANTED_BY,
    granted_at: datetime | None = None,
    expires_at: datetime | None = None,
    revocation_state: str = "active",
    allowed_tools: list[str] | None = None,
    allowed_targets: list[str] | None = None,
    blocked_targets: list[str] | None = None,
    daily_token_usd: float | None = None,
    monthly_external_usd: float | None = None,
    unconstrained: bool = False,
    unconstrained_justification: str | None = None,
    extra_constraints_yaml: str | None = None,
) -> str:
    """Build a mandates.md ``## <mandate_id>`` section body as a string.

    Caller wraps with the ``## <id>`` heading and writes to disk.
    Default expires_at: 30 days from now. Default constraints: an
    allowed_tools list with one placeholder tool (so the mandate is
    enforceable per spec/29 §"Constraint enforceability").
    """
    granted_at = granted_at or datetime.now(timezone.utc)
    if expires_at is None and not (unconstrained and not allowed_tools):
        expires_at = granted_at + timedelta(days=30)

    lines = [
        f"granted_by: {granted_by}",
        f"granted_at: {granted_at.isoformat()}",
    ]
    if expires_at is not None:
        lines.append(f"expires_at: {expires_at.isoformat()}")
    lines.append(f"revocation_state: {revocation_state}")

    # Constraints block — only include when something is constrained,
    # OR when unconstrained=True with a justification.
    if any([
        allowed_tools, allowed_targets, blocked_targets,
        daily_token_usd is not None, monthly_external_usd is not None,
        unconstrained, extra_constraints_yaml,
    ]):
        lines.append("constraints:")
        if unconstrained:
            lines.append("  unconstrained: true")
            if unconstrained_justification:
                lines.append(f"  unconstrained_justification: {unconstrained_justification!r}")
        if allowed_tools:
            lines.append(f"  allowed_tools: [{', '.join(allowed_tools)}]")
        if allowed_targets:
            lines.append("  allowed_targets:")
            for t in allowed_targets:
                lines.append(f"    - {t}")
        if blocked_targets:
            lines.append("  blocked_targets:")
            for t in blocked_targets:
                lines.append(f"    - {t}")
        if daily_token_usd is not None:
            lines.append(f"  daily_token_usd: {daily_token_usd}")
        if monthly_external_usd is not None:
            lines.append(f"  monthly_external_usd: {monthly_external_usd}")
        if extra_constraints_yaml:
            for line in extra_constraints_yaml.splitlines():
                lines.append(f"  {line}")

    return "\n".join(lines) + "\n"


def make_mandate_in_backend(
    backend: MandateBackend,
    scope_root: Path,
    scope: str,
    mandate_id: str,
    **kwargs,
) -> Path:
    """Write a mandate section to the appropriate mandates.md for ``scope``.

    Returns the path to the written mandates.md (so the test can edit
    it to simulate operator-edit scenarios — e.g., flipping
    revocation_state for the source-hash + state checks).

    Append-on-existing semantics: if the file already has mandates,
    appends a new section. Idempotent on (mandate_id) — overwrites a
    section with the same heading.

    For the filesystem backend, the path layout follows spec/29:
      - ``scope = "agent:<name>"`` → ``<scope_root>/<name>/mandates.md``
      - ``scope = "project:<name>"`` → ``<scope_root>/mandates.md``

    The function uses the same layout that ``FilesystemMandateBackend``
    expects, NOT the Protocol surface — backends without a write API
    (Protocol has only ``read_state`` / ``write_state``; mandates
    themselves are operator-authored, not framework-written) need this
    test-only injection path.
    """
    section_body = make_mandate_md_content(mandate_id, **kwargs)
    section = f"## {mandate_id}\n\n{section_body}\n"

    if scope.startswith("agent:"):
        agent_name = scope.split(":", 1)[1]
        mandates_path = scope_root / agent_name / "mandates.md"
    elif scope.startswith("project:"):
        mandates_path = scope_root / "mandates.md"
    else:
        raise ValueError(
            f"scope {scope!r} must start with 'agent:' or 'project:'"
        )

    mandates_path.parent.mkdir(parents=True, exist_ok=True)

    if mandates_path.exists():
        existing = mandates_path.read_text(encoding="utf-8")
        # Strip any existing section with same id (overwrite shape)
        lines = existing.splitlines(keepends=True)
        out: list[str] = []
        skipping = False
        target_heading = f"## {mandate_id}"
        for line in lines:
            if line.startswith("## "):
                skipping = line.rstrip().startswith(target_heading)
            if not skipping:
                out.append(line)
        existing = "".join(out)
        if existing and not existing.endswith("\n"):
            existing += "\n"
        mandates_path.write_text(existing + "\n" + section, encoding="utf-8")
    else:
        mandates_path.write_text(section, encoding="utf-8")

    return mandates_path


def make_project_root_meta(
    scope_root: Path,
    *,
    per_agent_mandate_policy: str = "open",
    allowed_per_agent_ids: list[str] | None = None,
) -> Path:
    """Write a ``_meta`` section to the project-root mandates.md.

    Returns the path. Only honored by FilesystemMandateBackend when
    `scope = "project:<name>"` is loaded; per-agent files with `_meta`
    log a doctor warning per spec/29.
    """
    lines = ["## _meta\n", "\n", f"per_agent_mandate_policy: {per_agent_mandate_policy}\n"]
    if allowed_per_agent_ids:
        lines.append(f"allowed_per_agent_ids: [{', '.join(allowed_per_agent_ids)}]\n")
    section = "".join(lines)

    mandates_path = scope_root / "mandates.md"
    mandates_path.parent.mkdir(parents=True, exist_ok=True)

    if mandates_path.exists():
        existing = mandates_path.read_text(encoding="utf-8")
        # Prepend _meta (convention: _meta is the first section)
        mandates_path.write_text(section + "\n" + existing, encoding="utf-8")
    else:
        mandates_path.write_text(section, encoding="utf-8")

    return mandates_path


# ──────────────────────────────────────────────────────────────────
# Target extractor registry injection
#
# Tests for MandateCheck step 5 (target allowlist) need to register a
# named extractor against an agent. The per-agent registry surface
# lives on `AtomicAgent._target_extractors` (per spec/29 §"Target
# extraction" clarification + plan-subagent Risk A). PR 3a will land
# the `register_target_extractor` method on AtomicAgent; until then,
# tests can directly populate the dict via this helper.


def register_extractor(agent_or_dict, name: str, callable_: Callable[[dict], str | None]) -> None:
    """Register a named extractor against an agent or registry-dict.

    PR 3a will provide ``AtomicAgent.register_target_extractor(name, fn)``
    as the public API. Until then, this helper writes directly to the
    target_extractors dict (whichever shape the agent stores it as).

    Tests should prefer this helper over direct dict access so the
    final-PR-3a public API can be swapped in via one rename.
    """
    if hasattr(agent_or_dict, "register_target_extractor"):
        agent_or_dict.register_target_extractor(name, callable_)
    elif hasattr(agent_or_dict, "_target_extractors"):
        agent_or_dict._target_extractors[name] = callable_
    elif isinstance(agent_or_dict, dict):
        agent_or_dict[name] = callable_
    else:
        raise TypeError(
            f"register_extractor: don't know how to register against {type(agent_or_dict).__name__}"
        )


# ──────────────────────────────────────────────────────────────────
# Proposal-citing-mandate construction


def make_proposal_citing(
    mandate_id: str,
    *,
    tool_name: str = "test_tool",
    tool_arguments: dict | None = None,
    actor_agent: str = "test-agent",
    target_canonical: str | None = None,
):
    """Build an ActionProposal that cites `mandate:<mandate_id>` for
    MandateCheck testing.

    Lazy-imports the proposal builder to avoid circular imports when
    this helper is loaded in test collection. Returns a fully-bound
    ActionProposal that MandateCheck can evaluate.

    The `target_canonical` field (spec/29 + #124 PR 3a prep — added
    to ActionProposal here) defaults to None; tests pass an explicit
    value to exercise the step 5 allowlist matching.
    """
    from atomic_agents.judge.types import (
        ActionClass,
        ActionProposal,
        Authorization,
    )
    from hashlib import sha256

    args = tool_arguments or {"placeholder": True}
    args_canonical = repr(sorted(args.items())).encode("utf-8")
    args_hash = "sha256:" + sha256(args_canonical).hexdigest()

    return ActionProposal(
        tool_name=tool_name,
        tool_arguments=args,
        tool_call_id="tc-test",
        tool_definition_hash="sha256:" + sha256(tool_name.encode()).hexdigest(),
        arguments_hash=args_hash,
        classification=ActionClass.EXTERNAL_SIDE_EFFECT,
        classification_source="default",
        actor_agent=actor_agent,
        actor_run_id="run-test",
        proposal_id="prop-test",
        proposal_ts=datetime.now(timezone.utc).isoformat(),
        authorization=Authorization(
            granted_by=f"mandate:{mandate_id}",
            scope=f"mandate cite {mandate_id}",
            granted_at=datetime.now(timezone.utc).isoformat(),
        ),
        target_canonical=target_canonical,
    )