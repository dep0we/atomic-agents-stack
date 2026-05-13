"""Framework-side proposal assembly for the JudgeBackend Protocol (spec/28).

The judge inspects an ``ActionProposal`` — never raw tool arguments
outside the proposal binding. This module is the proposal-assembly
boundary: it takes a tool_use block + the bound atomic_action marker
+ runtime context and produces a fully-bound ``ActionProposal`` with
framework-computed hashes (per spec/28 §"Proposal binding (TOCTOU
defense)").

Why split framework-introspected from actor-supplied fields:

- Pure framework introspection would lose the actor's *reason*, which
  is the most important judgment input.
- Pure actor-builds-proposal would add latency and create a failure
  mode (actor refuses to build a coherent proposal).

The split is structural: framework guarantees proposal-execution
binding (TOCTOU defense via tool_call_id + tool_definition_hash +
arguments_hash); actor commits to reason / evidence / authorization in
writing via the atomic_action side-channel marker.

PR 2a of #112. Companion to ``judge/atomic_action.py`` (the marker
extractor) and ``judge/rules.py`` (the rule-engine reference judge).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from .._canonical import canonical_sha256
from ..exceptions import JudgeProposalInvalid
from .types import (
    ActionClass,
    ActionProposal,
    Authorization,
    Evidence,
    Provenance,
    Reversibility,
    SkillRef,
)


# Tools that are framework-managed side-channel mechanisms and are NOT
# subject to judge dispatch. ``atomic_capture`` writes to the agent's
# OWN memory (handled by the existing capture pipeline per spec/05).
# ``atomic_action`` IS the side-channel marker — the framework consumes
# it for proposal assembly, not for execution. Both bypass the judge.
FRAMEWORK_MANAGED_TOOLS: frozenset[str] = frozenset({
    "atomic_capture",
    "atomic_action",
})


# Spec/28's default class for tools that lack an explicit
# classification source. The safe default is the most-restrictive
# non-high-risk class, so unknown tools require justification and
# judgment but don't escalate by default.
DEFAULT_UNKNOWN_CLASSIFICATION = ActionClass.EXTERNAL_SIDE_EFFECT
DEFAULT_UNKNOWN_CLASSIFICATION_SOURCE = "default_unknown"


def _tool_definition_canonical_payload(
    tool_name: str,
    input_schema: dict,
    handler: Any = None,
    *,
    mcp_server: str | None = None,
    mcp_server_version: str | None = None,
) -> dict:
    """Build the canonical payload that hashes into ``tool_definition_hash``.

    Per spec/28:224-229. Two shapes:

    - Custom tools: ``{"kind": "custom", "name", "input_schema",
      "handler_module", "handler_qualname"}``. The handler's module +
      qualname names *who* will execute, not the bytecode (handler
      updates are framework-version events, recorded by
      tool_definition_hash change at the registry layer).
    - MCP tools: ``{"kind": "mcp", "server", "name", "input_schema",
      "server_version"}``.
    """
    if mcp_server is not None:
        return {
            "kind": "mcp",
            "server": mcp_server,
            "name": tool_name,
            "input_schema": input_schema,
            "server_version": mcp_server_version or "",
        }
    # Custom-tool path.
    handler_module = getattr(handler, "__module__", "") if handler else ""
    handler_qualname = getattr(handler, "__qualname__", "") if handler else ""
    return {
        "kind": "custom",
        "name": tool_name,
        "input_schema": input_schema,
        "handler_module": handler_module,
        "handler_qualname": handler_qualname,
    }


def compute_tool_definition_hash(
    tool_name: str,
    input_schema: dict,
    handler: Any = None,
    *,
    mcp_server: str | None = None,
    mcp_server_version: str | None = None,
) -> str:
    """sha256 hex of the canonical tool-definition payload.

    Stable across framework restarts on the same code revision. Changes
    deterministically when handler module/qualname changes, when the
    JSON schema changes, or (for MCP tools) when the server version
    changes. See spec/28:229.
    """
    payload = _tool_definition_canonical_payload(
        tool_name,
        input_schema,
        handler,
        mcp_server=mcp_server,
        mcp_server_version=mcp_server_version,
    )
    return canonical_sha256(payload)


def compute_arguments_hash(parsed_tool_arguments: dict) -> str:
    """sha256 hex of canonical-JSON-encoded tool arguments.

    Input MUST be the already-parsed dict (Anthropic's ToolUseBlock.input
    is dict; OpenAI's tool_call.function.arguments is a JSON string and
    must be parsed via json.loads before reaching here, with the
    existing malformed-JSON-→-{} fallback per ``_llm.py``). If parsing
    fails, the caller raises ``JudgeProposalInvalid`` BEFORE invoking
    this — no judgment is rendered on an unparseable proposal per
    spec/28:222.
    """
    return canonical_sha256(parsed_tool_arguments)


def _marker_to_evidence(raw: list[dict]) -> list[Evidence]:
    """Convert the atomic_action schema's evidence array into a list of
    typed Evidence dataclasses. Skips items missing required fields
    rather than raising — the judge inspects what was provided and
    weighs missingness itself."""
    out: list[Evidence] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        source = item.get("source")
        claim = item.get("claim")
        prov_str = item.get("provenance")
        if not (source and claim and prov_str):
            continue
        try:
            provenance = Provenance(prov_str)
        except ValueError:
            # Unknown provenance label — keep evidence but mark legacy.
            provenance = Provenance.LEGACY
        out.append(
            Evidence(
                source=str(source),
                claim=str(claim),
                provenance=provenance,
                source_hash=item.get("source_hash"),
            )
        )
    return out


def _marker_to_authorization(raw: dict | None) -> Authorization | None:
    """Convert the atomic_action schema's authorization object into a
    typed Authorization, or None if absent / incomplete."""
    if not raw or not isinstance(raw, dict):
        return None
    granted_by = raw.get("granted_by")
    scope = raw.get("scope")
    granted_at = raw.get("granted_at")
    if not (granted_by and scope and granted_at):
        return None
    return Authorization(
        granted_by=str(granted_by),
        scope=str(scope),
        granted_at=str(granted_at),
        expires_at=raw.get("expires_at"),
    )


def _marker_to_reversibility(raw: str | None) -> Reversibility | None:
    """Convert the reversibility enum string to typed Reversibility, or
    None if absent / invalid."""
    if not raw:
        return None
    try:
        return Reversibility(raw)
    except ValueError:
        return None


def _proposal_id() -> str:
    """Generate a unique proposal_id per spec/28's example shape
    (``proposal_YYYYMMDDTHHMMSS_<random>``).
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    rand = uuid.uuid4().hex[:8]
    return f"proposal_{ts}_{rand}"


def _proposal_ts() -> str:
    """ISO-8601 UTC timestamp matching the rest of the framework's
    audit log convention (e.g., ``2026-05-12T14:30:52Z``)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def compute_policy_version(
    tools_md_text: str,
    judges_md_text: str | None = None,
) -> str:
    """Compute the canonical ``policy_version`` string per spec/28:302.

    Centralized here so every ``JudgeBackend`` implementation produces
    the same policy_version for the same ``(tools.md, judges.md)``
    snapshot. Without centralization, audit lines from the same
    proposal but different judges (PolicyJudge + LLMJudgeBackend in
    PR 2b's ensemble) would carry different policy snapshots even
    when both judges read the same files — confusing for operators
    reconciling decisions.

    Format: ``tools.md@sha256:<64hex>+judges.md@sha256:<64hex|absent>``.

    ``judges.md_text`` is ``None`` until PR 3's parser lands; the
    "absent" sentinel signals "no judges.md content was hashed" — a
    real sha256 hex cannot equal "absent" (sha256 is 64 lowercase hex
    chars, not the literal word). Audit-log readers can reliably split
    on ``+`` to recover both halves.
    """
    import hashlib
    tools_hash = hashlib.sha256(tools_md_text.encode("utf-8")).hexdigest()
    if judges_md_text is None:
        judges_part = "absent"
    else:
        judges_part = hashlib.sha256(judges_md_text.encode("utf-8")).hexdigest()
    return f"tools.md@sha256:{tools_hash}+judges.md@sha256:{judges_part}"


def is_framework_managed_tool(tool_name: str) -> bool:
    """Return True if the tool is framework-managed (capture or action
    marker) and should bypass judge dispatch entirely.

    Used by ``agent.call()``'s judge wiring to filter the
    custom_tool_uses list before proposal assembly.
    """
    return tool_name in FRAMEWORK_MANAGED_TOOLS


def assemble_proposal(
    tool_use: dict,
    atomic_action_marker: dict | None,
    *,
    classification: ActionClass,
    classification_source: str,
    tool_definition_hash: str,
    actor_agent: str,
    actor_run_id: str,
    actor_model_id: str | None = None,
    delegate_chain: list[str] | None = None,
    loaded_skills: list[SkillRef] | None = None,
    mcp_server: str | None = None,
) -> ActionProposal:
    """Build a fully-bound ``ActionProposal`` from a tool_use + its
    side-channel marker + runtime context.

    Per spec/28:177-183 field-presence rules:

    - For ``read_only`` classification: ``atomic_action_marker`` may be
      None. Resulting proposal has all actor-supplied fields as None /
      empty.
    - For ``reversible_write`` / ``external_side_effect`` /
      ``high_risk``: marker MUST be present AND bind to this tool_use's
      id via ``for_tool_call_id``. Missing / mismatched binding raises
      ``JudgeProposalInvalid``.

    Raises:
        JudgeProposalInvalid: when the marker is required-but-absent,
            when ``for_tool_call_id`` does not match ``tool_use["id"]``,
            or when ``tool_use`` is missing fields the proposal
            requires (``name``, ``input``, ``id``).
    """
    tool_name = tool_use.get("name")
    tool_arguments = tool_use.get("input")
    tool_call_id = tool_use.get("id")
    if not (tool_name and isinstance(tool_arguments, dict) and tool_call_id):
        raise JudgeProposalInvalid(
            f"tool_use is missing required fields (name, input, id): "
            f"{tool_use!r}"
        )

    requires_marker = classification != ActionClass.READ_ONLY
    if requires_marker and atomic_action_marker is None:
        raise JudgeProposalInvalid(
            f"tool_call_id={tool_call_id!r} (tool={tool_name!r}, "
            f"class={classification.value}) requires an atomic_action "
            f"side-channel marker; none was bound. Emit an atomic_action "
            f"tool call in the same turn with for_tool_call_id="
            f"{tool_call_id!r}."
        )
    if atomic_action_marker is not None:
        bind_id = atomic_action_marker.get("for_tool_call_id")
        if bind_id != tool_call_id:
            raise JudgeProposalInvalid(
                f"atomic_action marker binding mismatch: marker says "
                f"for_tool_call_id={bind_id!r}, but proposal is for "
                f"tool_call_id={tool_call_id!r}"
            )

    arguments_hash = compute_arguments_hash(tool_arguments)
    marker = atomic_action_marker or {}

    return ActionProposal(
        # Framework-introspected
        tool_name=tool_name,
        tool_arguments=tool_arguments,
        tool_call_id=tool_call_id,
        tool_definition_hash=tool_definition_hash,
        arguments_hash=arguments_hash,
        classification=classification,
        classification_source=classification_source,
        actor_agent=actor_agent,
        actor_run_id=actor_run_id,
        proposal_id=_proposal_id(),
        proposal_ts=_proposal_ts(),
        actor_model_id=actor_model_id,
        delegate_chain=list(delegate_chain or []),
        loaded_skills=list(loaded_skills or []),
        mcp_server=mcp_server,
        # Actor-supplied via side-channel
        side_channel_for_tool_call_id=marker.get("for_tool_call_id"),
        reason=marker.get("reason"),
        evidence=_marker_to_evidence(marker.get("evidence", [])),
        authorization=_marker_to_authorization(marker.get("authorization")),
        expected_consequence=marker.get("expected_consequence"),
        reversibility=_marker_to_reversibility(marker.get("reversibility")),
        rollback_path=marker.get("rollback_path"),
        target_audience=marker.get("target_audience"),
    )
