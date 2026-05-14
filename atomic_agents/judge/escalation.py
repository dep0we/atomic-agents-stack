"""ESCALATE state machine: PENDING file writer + resolution poller.

Implements spec/28 §"Escalate" (lines 286-353). When a judge ensemble
returns ESCALATE — or when ``class_policy.<X>=escalate`` synthesizes the
outcome — the framework writes a PENDING file to
``<agent_root>/vault/escalations/<action_class>/<proposal_id>.md``,
defers the actor's run with ``Response.deferred=True``, and on a
subsequent ``agent.call()`` (or via the standalone poller — follow-up)
checks the file for an operator resolution block. If resolved:

- ``Approved by <op>``  → execute the bound action; emit RESOLVED event
- ``Denied by <op>``    → no execution; emit RESOLVED event
- ``Redacted by <op>``  → no execution; emit RESOLVED event (body redacted)
- past ``auto_decide_after_seconds`` → fallback policy applied (default block)

PR 3b ships the ESCALATE machinery only. REVISE — both judge-driven and
operator-driven (``### Revised by <op>``) — is deferred to PR 3c. If a
Revised block appears in PR 3b, it is logged as a warning and treated as
Denied so the action does not execute against an unvalidated amendment.

The module's file-IO surface is intentionally narrow:

- ``write_pending_escalation`` — produces the PENDING file (atomic).
- ``poll_resolutions`` — scans the queue, applies auto-decide timeouts
  with CAS-safe writes, claims emit-sidecars with ``O_EXCL`` semantics,
  parses resolution blocks with a STRICT header parser, and returns
  ``ResolutionEvent`` records the caller (``AtomicAgent``) executes +
  audits.

Execution of approved actions is OUT of this module — it lives on
``AtomicAgent.poll_escalations`` so MCP/tools/persona state can be
sourced from the live agent context (see CLAUDE.md taste rule #1:
vault is source of truth, but the runtime that consults the vault still
needs the agent's loaded config).

Canonical implementations (spec/28 locked at PR 4):

- ``enforcement_action`` enum: 18 values documented in spec/28 §"Audit
  event schema" — ``audit_bypass``, ``block_executed``, ``allow_executed``,
  ``allow_pending_next_judge``, ``revise_pending_second_judgment``,
  ``revise_executed``, ``revise_invalid_amendment``,
  ``revise_loop_exhausted_blocked``, ``escalate_pending``,
  ``approved_executed``, ``approved_stale_tool_definition``,
  ``denied``, ``redacted``, ``auto_decided_block``,
  ``auto_decided_allow``, ``proposal_body_tampered``,
  ``operator_revise_executed``, ``operator_revise_invalid_amendment``.
- ``Response.escalation_queue_ids: list[str]`` — multi-tool_use turns
  can defer N actions simultaneously.
- Duration values are integer seconds (``auto_decide_after_seconds``,
  ``resolution_poll_cycle_seconds``); duration-string parsing not
  shipped.
- Strict resolution-block parser: header MUST match
  ``### <Verb> by <op>`` exactly. Typos surface as doctor warnings.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable

from .._io import atomic_write, safe_resolve_under
from .types import ActionProposal, EscalationConfig


# ──────────────────────────────────────────────────────────────────
# Public types


class ResolutionDecision(StrEnum):
    """What the framework decided when reading a PENDING file."""

    APPROVED = "approved"
    DENIED = "denied"
    REDACTED = "redacted"
    AUTO_DECIDED_BLOCK = "auto_decided_block"
    AUTO_DECIDED_ALLOW = "auto_decided_allow"
    BODY_TAMPERED = "body_tampered"
    OPERATOR_REVISED = "operator_revised"  # PR 3c — real REVISE handling
    UNPARSEABLE = "unparseable"  # Strict-parser surface; framework leaves PENDING as-is


@dataclass(frozen=True)
class PendingFrontmatter:
    """Parsed frontmatter of a PENDING escalation file."""

    type: str
    state: str
    proposal_id: str
    parent_run_id: str
    agent: str
    action_class: str
    judge_id: str
    escalated_at: str  # ISO-8601 UTC
    policy_version: str
    schema_version: int
    triggered_by: str | None = None


@dataclass(frozen=True)
class ResolutionEvent:
    """One operator (or framework) resolution of a PENDING file.

    Returned by ``poll_resolutions``; the caller emits a JSONL audit
    line and (for Approved) executes the bound action.
    """

    file_path: Path
    sidecar_path: Path
    frontmatter: PendingFrontmatter
    proposal: ActionProposal
    decision: ResolutionDecision
    operator: str  # "framework" when auto-decided
    resolved_at: str  # ISO-8601 UTC
    reason: str  # free-prose; empty on parse failure
    enforcement_action: str  # spec/28 enforcement_action enum string
    # PR 3c: operator-supplied ProposalAmendment when decision is
    # OPERATOR_REVISED. Set by ``_claim_operator_resolution`` after
    # parsing the embedded ``amendment:`` YAML block. None for any
    # other decision (or when no amendment block is present, which
    # the framework treats as invalid → enforcement promoted to
    # operator_revise_invalid_amendment).
    amendment: Any | None = None  # ProposalAmendment — avoiding circular type


# ──────────────────────────────────────────────────────────────────
# Configuration


SCHEMA_VERSION = 1

# Strict resolution-block header regex per spec/28 (PR 3b lock-in). The
# operator name is captured for audit. The trailing newline is required
# so we don't match a header that's actually a substring of a longer
# heading.
_RESOLUTION_HEADER_RE = re.compile(
    r"^###\s+(?P<verb>Approved|Denied|Redacted|Revised|Auto-decided)\s+by\s+"
    r"(?P<op>[\w\-.@' ]{1,128})\s*$",
    re.MULTILINE,
)

_VERB_TO_DECISION: dict[str, ResolutionDecision] = {
    "Approved": ResolutionDecision.APPROVED,
    "Denied": ResolutionDecision.DENIED,
    "Redacted": ResolutionDecision.REDACTED,
    "Auto-decided": ResolutionDecision.AUTO_DECIDED_BLOCK,
    "Revised": ResolutionDecision.OPERATOR_REVISED,
}

_DECISION_TO_ENFORCEMENT: dict[ResolutionDecision, str] = {
    ResolutionDecision.APPROVED: "approved_executed",
    ResolutionDecision.DENIED: "denied",
    ResolutionDecision.REDACTED: "redacted",
    ResolutionDecision.AUTO_DECIDED_BLOCK: "auto_decided_block",
    ResolutionDecision.AUTO_DECIDED_ALLOW: "auto_decided_allow",
    ResolutionDecision.BODY_TAMPERED: "proposal_body_tampered",
    ResolutionDecision.OPERATOR_REVISED: "operator_revise_executed",
    ResolutionDecision.UNPARSEABLE: "denied",
}


# ──────────────────────────────────────────────────────────────────
# Destination normalization


def normalize_destination(destination: str) -> str:
    """Map operator-configured destination strings to the spec-correct
    on-disk layout.

    Spec/28:288 mandates ``vault/escalations/<class>/<proposal_id>.md``.
    PR 3a's parser defaulted to ``"vault"`` (truncated path); PR 3b
    fixes the default but normalizes legacy configs so operators who
    explicitly wrote ``destination: vault`` don't see drift.
    """
    if not destination:
        return "vault/escalations/"
    cleaned = destination.strip().rstrip("/")
    if cleaned == "vault":
        return "vault/escalations/"
    if cleaned.endswith("/escalations"):
        return cleaned + "/"
    if "escalations" in cleaned:
        return cleaned + "/" if not cleaned.endswith("/") else cleaned
    return cleaned.rstrip("/") + "/escalations/"


# ──────────────────────────────────────────────────────────────────
# PENDING file writer


def write_pending_escalation(
    *,
    proposal: ActionProposal,
    judgment_reason: str,
    judge_id: str,
    agent_root: Path,
    agent_name: str,
    parent_run_id: str,
    policy_version: str,
    judges_config_escalation: EscalationConfig,
    synthesis_source: str | None = None,
    triggered_by: str | None = None,
    revised_from_proposal_id: str | None = None,
) -> tuple[Path, str]:
    """Write the PENDING escalation file atomically.

    Returns ``(pending_path, queue_id)`` where ``queue_id == proposal_id``.
    Path is validated with ``safe_resolve_under(agent_root)``; traversal
    refused. Frontmatter includes ``triggered_by`` when the framework
    synthesized the ESCALATE outcome from a failure_policy mapping; the
    body carries the full ``ActionProposal`` serialized as a fenced
    YAML block for later integrity verification.
    """
    destination = normalize_destination(judges_config_escalation.destination)
    action_class = proposal.classification.value
    proposal_id = proposal.proposal_id
    pending_rel = f"{destination}{action_class}/{proposal_id}.md"
    pending_path = safe_resolve_under(pending_rel, agent_root)

    escalated_at = _now_iso()
    fm = PendingFrontmatter(
        type="escalation",
        state="pending",
        proposal_id=proposal_id,
        parent_run_id=parent_run_id,
        agent=agent_name,
        action_class=action_class,
        judge_id=judge_id,
        escalated_at=escalated_at,
        policy_version=policy_version,
        schema_version=SCHEMA_VERSION,
        triggered_by=triggered_by,
    )
    content = _render_pending_file(
        fm, proposal, judgment_reason, synthesis_source,
        revised_from_proposal_id=revised_from_proposal_id,
    )
    atomic_write(pending_path, content)
    return pending_path, proposal_id


def _render_pending_file(
    fm: PendingFrontmatter,
    proposal: ActionProposal,
    judgment_reason: str,
    synthesis_source: str | None,
    *,
    revised_from_proposal_id: str | None = None,
) -> str:
    """Render the PENDING file as YAML-frontmatter + markdown body.

    The ``## Proposal`` section is a fenced ``yaml`` block containing
    the canonical ActionProposal dict (asdict). PR 3b's poller rehashes
    ``tool_name`` + ``tool_arguments`` from this block and compares
    against the embedded ``arguments_hash`` to detect operator tamper.
    """
    fm_lines = [
        "---",
        f"type: {fm.type}",
        f"state: {fm.state}",
        f"proposal_id: {fm.proposal_id}",
        f"parent_run_id: {fm.parent_run_id}",
        f"agent: {fm.agent}",
        f"action_class: {fm.action_class}",
        f"judge_id: {fm.judge_id}",
        f"escalated_at: {fm.escalated_at}",
        f"policy_version: {fm.policy_version}",
        f"schema_version: {fm.schema_version}",
    ]
    if fm.triggered_by is not None:
        fm_lines.append(f"triggered_by: {fm.triggered_by}")
    if synthesis_source is not None:
        fm_lines.append(f"synthesis_source: {synthesis_source}")
    if revised_from_proposal_id is not None:
        # P1 #3 (PR 3c): when this PENDING was written from a
        # second-judgment ESCALATE inside a judge-driven REVISE, the
        # original proposal_id appears here so operators can chain
        # back through the audit trail. Forensic linkage; no
        # behavior change.
        fm_lines.append(f"revised_from_proposal_id: {revised_from_proposal_id}")
    fm_lines.append("---")

    proposal_yaml = _serialize_proposal(proposal)
    body = [
        "",
        "## Proposal",
        "",
        "```yaml",
        proposal_yaml,
        "```",
        "",
        "## Judge's reason for escalating",
        "",
        judgment_reason.strip() or "(no reason provided)",
        "",
        "## Resolution",
        "",
        "<!--",
        "Operator: append exactly one resolution block below. PR 3b "
        "accepts:",
        "",
        "  ### Approved by <name>",
        "  ### Denied by <name>",
        "  ### Redacted by <name>",
        "",
        "Headers must match exactly (h3 + verb + 'by' + operator name).",
        "REVISE is NOT supported until PR 3c — use Deny + retry.",
        "-->",
        "",
    ]
    return "\n".join(fm_lines + body)


def _serialize_proposal(proposal: ActionProposal) -> str:
    """Serialize the ActionProposal as a deterministic YAML-ish block.

    We emit minimal YAML by hand rather than importing a library: keys
    are stable, values use JSON encoding for dicts/lists/strings.
    Round-trips losslessly via ``_parse_proposal``.
    """
    data = asdict(proposal)
    # StrEnums and dataclasses are handled by asdict; we use json to
    # render nested values uniformly. The poll-side parser inverts this.
    return json.dumps(data, indent=2, sort_keys=True, default=str)


# ──────────────────────────────────────────────────────────────────
# Polling


def poll_resolutions(
    *,
    agent_root: Path,
    judges_config_escalation: EscalationConfig,
    log_warning: Callable[[str], None] | None = None,
    now: Callable[[], datetime] | None = None,
) -> list[ResolutionEvent]:
    """Scan ``<agent_root>/<destination>/*/*.md`` for PENDING files
    whose state has transitioned (operator resolution or auto-decide
    timeout). Returns one ``ResolutionEvent`` per claimed transition.

    The caller (``AtomicAgent.poll_escalations``) emits the audit event
    and, for ``APPROVED``, re-validates + executes the bound action.

    De-dup is via an O_EXCL sidecar marker ``.<proposal_id>.resolved-emitted``
    next to the PENDING file. Concurrent pollers race the sidecar create;
    exactly one wins and emits the event.
    """
    destination = normalize_destination(judges_config_escalation.destination)
    root = safe_resolve_under(destination.rstrip("/"), agent_root)
    if not root.exists():
        return []

    now_dt = (now or _now_dt)()
    timeout_seconds = judges_config_escalation.auto_decide_after_seconds
    fallback_map = judges_config_escalation.fallback_on_timeout

    events: list[ResolutionEvent] = []
    for class_dir in sorted(root.iterdir()):
        if not class_dir.is_dir():
            continue
        for pending_path in sorted(class_dir.glob("*.md")):
            try:
                event = _process_pending_file(
                    pending_path=pending_path,
                    now_dt=now_dt,
                    timeout_seconds=timeout_seconds,
                    fallback_map=fallback_map,
                    log_warning=log_warning,
                )
            except Exception as exc:
                # A malformed file should not crash the whole poll
                # cycle. Log + skip — the operator (or doctor check)
                # surfaces the bad file on the next pass.
                if log_warning is not None:
                    log_warning(
                        f"poll_resolutions: failed to process "
                        f"{pending_path}: {type(exc).__name__}: {exc}"
                    )
                continue
            if event is not None:
                events.append(event)
    return events


def _process_pending_file(
    *,
    pending_path: Path,
    now_dt: datetime,
    timeout_seconds: int | None,
    fallback_map: dict[str, str],
    log_warning: Callable[[str], None] | None,
) -> ResolutionEvent | None:
    """Process one PENDING file. Returns a ResolutionEvent if a state
    transition was claimed (sidecar created), None otherwise.

    Three branches:
    1. ``state: pending`` + past timeout → write auto-decide block (CAS),
       claim sidecar, emit RESOLVED event.
    2. ``state: pending`` + within timeout → no-op.
    3. ``state: resolved | redacted`` → parse first resolution block,
       claim sidecar, emit event. If body-integrity fails, emit
       BODY_TAMPERED.
    """
    pre_bytes = pending_path.read_bytes()
    pre_sha = hashlib.sha256(pre_bytes).hexdigest()
    text = pre_bytes.decode("utf-8")
    fm_dict, body = _split_frontmatter(text)
    if fm_dict is None:
        if log_warning is not None:
            log_warning(
                f"poll_resolutions: {pending_path} missing/invalid frontmatter; "
                "skipping"
            )
        return None

    try:
        fm = _frontmatter_from_dict(fm_dict)
    except (KeyError, ValueError) as exc:
        if log_warning is not None:
            log_warning(
                f"poll_resolutions: {pending_path} frontmatter invalid: {exc}"
            )
        return None

    if fm.state == "pending":
        if timeout_seconds is None or timeout_seconds <= 0:
            return None
        try:
            escalated_dt = datetime.fromisoformat(fm.escalated_at)
        except ValueError:
            if log_warning is not None:
                log_warning(
                    f"poll_resolutions: {pending_path} has unparseable "
                    f"escalated_at {fm.escalated_at!r}; skipping"
                )
            return None
        # Operator-hand-edited frontmatter may strip the tz offset
        # (Codex round-1 P1-4). Coerce naive → UTC so the timedelta
        # subtraction against tz-aware now_dt doesn't raise TypeError
        # and wedge the file every poll cycle.
        if escalated_dt.tzinfo is None:
            escalated_dt = escalated_dt.replace(tzinfo=timezone.utc)
        if (now_dt - escalated_dt).total_seconds() < timeout_seconds:
            return None
        # Past timeout: apply fallback via CAS write. Per-class
        # resolution keys on the PENDING file's frontmatter
        # ``action_class`` (the authoritative classification recorded
        # at write time) — not on the on-disk directory name, which
        # is just a filesystem layout artifact an operator could typo.
        return _apply_auto_decide(
            pending_path=pending_path,
            pre_sha=pre_sha,
            fm=fm,
            text=text,
            body=body,
            timeout_seconds=timeout_seconds,
            fallback_map=fallback_map,
            now_dt=now_dt,
            log_warning=log_warning,
        )

    if fm.state in {"resolved", "redacted"}:
        return _claim_operator_resolution(
            pending_path=pending_path,
            fm=fm,
            text=text,
            body=body,
            log_warning=log_warning,
        )

    # Unknown state — record warning, skip.
    if log_warning is not None:
        log_warning(
            f"poll_resolutions: {pending_path} has unknown state "
            f"{fm.state!r}; skipping"
        )
    return None


def _apply_auto_decide(
    *,
    pending_path: Path,
    pre_sha: str,
    fm: PendingFrontmatter,
    text: str,
    body: str,
    timeout_seconds: int,
    fallback_map: dict[str, str],
    now_dt: datetime,
    log_warning: Callable[[str], None] | None,
) -> ResolutionEvent | None:
    """Apply the fallback policy via compare-and-swap write.

    Race detection: re-read the file just before rename; if its sha
    changed since ``pre_sha``, an operator beat us — abort + retry on
    the next poll cycle. The auto-decide is idempotent (timeout has
    still passed) so re-attempt is safe.

    Per-class resolution: ``fallback_map`` is the full
    ``EscalationConfig.fallback_on_timeout`` dict (always carries a
    ``"default"`` key after parser normalization). The auto-decide
    looks up the policy for this proposal's class via
    ``fm.action_class`` — the frontmatter is the authoritative source.
    Falls back to the dict's ``"default"`` for any class the operator
    didn't list explicitly.
    """
    # ``"default"`` key invariant is enforced by
    # ``EscalationConfig.__post_init__`` so every config that reaches this
    # site already satisfies it (parser + direct dataclass construction
    # both surface there). The runtime path is safe-by-construction —
    # no defensive check needed here (and a defensive ``assert`` would
    # be stripped by ``python -O`` and swallowed by the outer
    # ``except Exception`` in ``poll_resolutions``).
    fallback = fallback_map.get(fm.action_class, fallback_map["default"])
    sidecar_path = _sidecar_path(pending_path)
    # Re-snapshot to detect operator-edit race.
    current_bytes = pending_path.read_bytes()
    current_sha = hashlib.sha256(current_bytes).hexdigest()
    if current_sha != pre_sha:
        # Operator wrote first. Defer to the next poll; their resolution
        # block will be processed via _claim_operator_resolution.
        if log_warning is not None:
            log_warning(
                f"poll_resolutions: auto-decide skipped for "
                f"{pending_path.name} — file changed between read "
                f"and write (operator likely active)"
            )
        return None
    # Build the auto-decide resolution block + flip frontmatter state.
    resolved_at = _now_iso(now_dt)
    decision_verb = "Auto-decided"
    block = (
        f"### {decision_verb} by framework\n"
        f"resolved_at: {resolved_at}\n"
        f"reason: auto_decide_after_seconds={timeout_seconds} elapsed; "
        f"fallback_on_timeout={fallback} "
        f"(resolved for action_class={fm.action_class})\n"
    )
    # Resolution section already exists in the body; append the block.
    new_text = _flip_state_and_append_resolution(text, "resolved", block)
    # Final CAS: read once more right before atomic_write to make the
    # window tiny. atomic_write itself is a rename so it's all-or-nothing.
    pre_write = pending_path.read_bytes()
    if hashlib.sha256(pre_write).hexdigest() != pre_sha:
        if log_warning is not None:
            log_warning(
                f"poll_resolutions: auto-decide aborted at write-time "
                f"for {pending_path.name} — concurrent operator edit"
            )
        return None
    # Rewrite-then-claim ordering (Codex round-1 P1-1). The earlier
    # claim-then-rewrite order left a stuck file if the poller crashed
    # between the two: state=pending, sidecar present, next poll's
    # claim raises FileExistsError, file is wedged forever. With this
    # order, a mid-crash leaves state=resolved with no sidecar — next
    # poll routes through _claim_operator_resolution which DOES claim
    # the sidecar from the resolved-state path. Crash-recoverable.
    atomic_write(pending_path, new_text)
    try:
        _claim_sidecar(sidecar_path)
    except FileExistsError:
        if log_warning is not None:
            log_warning(
                f"poll_resolutions: auto-decide sidecar already claimed for "
                f"{pending_path.name}; another poller raced ahead "
                "(file already rewritten with our auto-decide block)"
            )
        return None
    # Map fallback to the corresponding decision/enforcement.
    if fallback == "allow":
        decision = ResolutionDecision.AUTO_DECIDED_ALLOW
    else:
        decision = ResolutionDecision.AUTO_DECIDED_BLOCK
    proposal = _parse_proposal_from_body(body)
    return ResolutionEvent(
        file_path=pending_path,
        sidecar_path=sidecar_path,
        frontmatter=fm,
        proposal=proposal,
        decision=decision,
        operator="framework",
        resolved_at=resolved_at,
        reason=f"auto_decide_after_seconds={timeout_seconds} elapsed; "
        f"fallback_on_timeout={fallback} "
        f"(resolved for action_class={fm.action_class})",
        enforcement_action=_DECISION_TO_ENFORCEMENT[decision],
    )


def _claim_operator_resolution(
    *,
    pending_path: Path,
    fm: PendingFrontmatter,
    text: str,
    body: str,
    log_warning: Callable[[str], None] | None,
) -> ResolutionEvent | None:
    """Parse the first operator resolution block and emit an event.

    Sidecar O_EXCL claim happens BEFORE parsing/emit so concurrent
    pollers see exactly one event per resolved file.
    """
    sidecar_path = _sidecar_path(pending_path)
    try:
        _claim_sidecar(sidecar_path)
    except FileExistsError:
        # Already emitted by another poller (or by us on a prior cycle).
        return None

    decision, operator, resolved_at, reason = _parse_first_resolution_block(
        body, fm.state
    )
    if decision is ResolutionDecision.UNPARSEABLE:
        # Strict parser rejected the operator's block. Release the
        # sidecar so the operator can fix the typo and re-trigger.
        try:
            sidecar_path.unlink()
        except FileNotFoundError:
            pass
        if log_warning is not None:
            log_warning(
                f"poll_resolutions: {pending_path.name} state={fm.state} but "
                "no valid resolution block found (strict-parser). "
                "Header must be h3 + verb + 'by' + operator. "
                "Doctor will surface this on next health check."
            )
        return None

    # Body-integrity check: rehash tool_name + tool_arguments from the
    # ``## Proposal`` block and compare against the embedded
    # ``arguments_hash``. If they differ, the operator (or a careless
    # MCP) edited the proposal body — refuse execution.
    proposal = _parse_proposal_from_body(body)
    if not _verify_proposal_body_integrity(proposal):
        decision = ResolutionDecision.BODY_TAMPERED
        if log_warning is not None:
            log_warning(
                f"poll_resolutions: {pending_path.name} body integrity "
                "check FAILED — proposal block edited after PENDING write. "
                "Action refused; original PENDING preserved in audit trail."
            )

    # PR 3c operator-revise: parse the embedded amendment YAML so the
    # agent's resolution handler can run amend_proposal + re-judge.
    # Invalid YAML promotes enforcement to operator_revise_invalid_amendment
    # but the sidecar stays claimed (operator's intent recorded; framework
    # refuses to act on a malformed amendment).
    amendment = None
    enforcement = _DECISION_TO_ENFORCEMENT[decision]
    if decision is ResolutionDecision.OPERATOR_REVISED:
        block_body = _extract_first_block_body(body)
        from . import _revise as _rv

        try:
            amendment = _rv.parse_operator_amendment(block_body)
        except Exception as exc:  # noqa: BLE001
            amendment = None
            enforcement = "operator_revise_invalid_amendment"
            if log_warning is not None:
                log_warning(
                    f"poll_resolutions: {pending_path.name} operator amendment "
                    f"parse failed: {type(exc).__name__}: {exc}"
                )
        if amendment is None and enforcement != "operator_revise_invalid_amendment":
            enforcement = "operator_revise_invalid_amendment"
            if log_warning is not None:
                log_warning(
                    f"poll_resolutions: {pending_path.name} Revised block "
                    "has no embedded amendment YAML; refusing execution."
                )

    return ResolutionEvent(
        file_path=pending_path,
        sidecar_path=sidecar_path,
        frontmatter=fm,
        proposal=proposal,
        decision=decision,
        operator=operator,
        resolved_at=resolved_at,
        reason=reason,
        enforcement_action=enforcement,
        amendment=amendment,
    )


def _extract_first_block_body(body: str) -> str:
    """Return the body text of the FIRST resolution block (between
    its header line and the next h3 header or EOF).

    Used by ``_claim_operator_resolution`` to feed the operator's
    amendment YAML into ``_revise.parse_operator_amendment``.
    """
    matches = list(_RESOLUTION_HEADER_RE.finditer(body))
    if not matches:
        return ""
    first = matches[0]
    start = first.end()
    end = matches[1].start() if len(matches) > 1 else len(body)
    return body[start:end]


def _claim_sidecar(sidecar_path: Path) -> None:
    """Create the sidecar with O_EXCL (exclusive-create) semantics.

    Returns silently on success; raises ``FileExistsError`` when another
    poller already created the file. This is the de-dup primitive: each
    PENDING file resolves into at most one RESOLVED event regardless of
    how many pollers race.
    """
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(str(sidecar_path), flags, 0o644)
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            raise FileExistsError(str(sidecar_path)) from exc
        raise
    try:
        os.write(fd, _now_iso().encode("utf-8") + b"\n")
    finally:
        os.close(fd)


def _sidecar_path(pending_path: Path) -> Path:
    return pending_path.with_name(f".{pending_path.name}.resolved-emitted")


# ──────────────────────────────────────────────────────────────────
# Throttle helper


def is_within_throttle(
    *,
    agent_root: Path,
    judges_config_escalation: EscalationConfig,
    now: Callable[[], datetime] | None = None,
) -> bool:
    """Return True if a poll was performed within
    ``resolution_poll_cycle_seconds``. Caller skips the poll if True.

    The throttle marker is ``<destination>/.last-poll`` (mtime). The
    cycle is read from ``EscalationConfig.resolution_poll_cycle_seconds``.
    """
    cycle = judges_config_escalation.resolution_poll_cycle_seconds
    if cycle <= 0:
        return False
    destination = normalize_destination(judges_config_escalation.destination)
    try:
        marker = safe_resolve_under(
            destination.rstrip("/") + "/.last-poll", agent_root
        )
    except Exception:
        return False
    if not marker.exists():
        return False
    try:
        mtime = datetime.fromtimestamp(marker.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return False
    elapsed = ((now or _now_dt)() - mtime).total_seconds()
    return elapsed < cycle


def touch_last_poll(
    *,
    agent_root: Path,
    judges_config_escalation: EscalationConfig,
) -> None:
    """Update the ``.last-poll`` marker's mtime so the next call's
    throttle check sees the recent poll."""
    destination = normalize_destination(judges_config_escalation.destination)
    try:
        marker = safe_resolve_under(
            destination.rstrip("/") + "/.last-poll", agent_root
        )
    except Exception:
        return
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch(exist_ok=True)


# ──────────────────────────────────────────────────────────────────
# Frontmatter / body parsing


_FM_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$")
_FM_DELIM = "---"


def _split_frontmatter(text: str) -> tuple[dict[str, str] | None, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FM_DELIM:
        return None, text
    fm: dict[str, str] = {}
    i = 1
    while i < len(lines) and lines[i].strip() != _FM_DELIM:
        line = lines[i]
        m = _FM_LINE_RE.match(line)
        if m:
            fm[m.group(1)] = m.group(2).strip()
        i += 1
    if i >= len(lines):
        return None, text
    body = "\n".join(lines[i + 1 :])
    return fm, body


def _frontmatter_from_dict(d: dict[str, str]) -> PendingFrontmatter:
    required = (
        "type",
        "state",
        "proposal_id",
        "parent_run_id",
        "agent",
        "action_class",
        "judge_id",
        "escalated_at",
        "policy_version",
        "schema_version",
    )
    for k in required:
        if k not in d:
            raise KeyError(f"missing required frontmatter field: {k}")
    return PendingFrontmatter(
        type=d["type"],
        state=d["state"],
        proposal_id=d["proposal_id"],
        parent_run_id=d["parent_run_id"],
        agent=d["agent"],
        action_class=d["action_class"],
        judge_id=d["judge_id"],
        escalated_at=d["escalated_at"],
        policy_version=d["policy_version"],
        schema_version=int(d["schema_version"]),
        triggered_by=d.get("triggered_by") or None,
    )


def _parse_proposal_from_body(body: str) -> ActionProposal:
    """Extract the ``## Proposal`` fenced ``yaml`` block and rehydrate
    an ActionProposal dataclass.

    Raises ``ValueError`` if the block is missing or malformed. The
    body-integrity check (``_verify_proposal_body_integrity``) is the
    operator-tamper detection layer downstream.
    """
    m = re.search(
        r"##\s+Proposal\s*\n+```yaml\s*\n(?P<body>.*?)\n```",
        body,
        re.DOTALL,
    )
    if not m:
        raise ValueError("PENDING body missing ## Proposal yaml block")
    raw = m.group("body").strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"PENDING body proposal block not valid JSON: {exc}") from exc
    return _action_proposal_from_dict(data)


def _action_proposal_from_dict(data: dict[str, Any]) -> ActionProposal:
    """Rehydrate ActionProposal from a dict (asdict round-trip).

    Maps StrEnum-valued fields back to their enum types so frozen-dataclass
    equality + downstream typecheck work.
    """
    from .types import ActionClass, Reversibility, Evidence, Authorization, SkillRef

    def _maybe_enum(value: Any, cls: type) -> Any:
        if value is None:
            return None
        if isinstance(value, cls):
            return value
        return cls(value)

    payload = dict(data)
    payload["classification"] = _maybe_enum(payload.get("classification"), ActionClass)
    if "reversibility" in payload:
        payload["reversibility"] = _maybe_enum(payload.get("reversibility"), Reversibility)
    # evidence: list[Evidence]
    evidence_in = payload.get("evidence", []) or []
    payload["evidence"] = [
        Evidence(**e) if not isinstance(e, Evidence) else e for e in evidence_in
    ]
    # authorization: Authorization | None
    auth_in = payload.get("authorization")
    if auth_in is not None and not isinstance(auth_in, Authorization):
        payload["authorization"] = Authorization(**auth_in)
    # loaded_skills: list[SkillRef]
    skills_in = payload.get("loaded_skills", []) or []
    payload["loaded_skills"] = [
        SkillRef(**s) if not isinstance(s, SkillRef) else s for s in skills_in
    ]
    return ActionProposal(**payload)


def _verify_proposal_body_integrity(proposal: ActionProposal) -> bool:
    """Recompute ``arguments_hash`` from the proposal's tool_arguments
    and compare against the embedded value.

    Returns True on match, False on tamper.

    ``tool_definition_hash`` is NOT recomputed here — that hash also
    depends on the live tool registry's input_schema + handler, which
    may legitimately have changed since PENDING-write (tool dropped,
    schema evolved). The Approved-execution path re-verifies
    ``tool_definition_hash`` separately at execution time.
    """
    from .proposal import compute_arguments_hash

    expected = compute_arguments_hash(proposal.tool_arguments)
    return expected == proposal.arguments_hash


def _parse_first_resolution_block(
    body: str, fm_state: str
) -> tuple[ResolutionDecision, str, str, str]:
    """Parse the first valid resolution block top-down.

    Returns ``(decision, operator, resolved_at, reason)``. When no valid
    block is found despite ``state in {resolved, redacted}``, returns
    ``ResolutionDecision.UNPARSEABLE`` so the caller can leave the
    PENDING in place (no sidecar claim) and surface a doctor warning.
    """
    matches = list(_RESOLUTION_HEADER_RE.finditer(body))
    if not matches:
        return ResolutionDecision.UNPARSEABLE, "", "", ""
    first = matches[0]
    verb = first.group("verb")
    operator = first.group("op").strip()
    if not operator:
        return ResolutionDecision.UNPARSEABLE, "", "", ""
    decision = _VERB_TO_DECISION.get(verb, ResolutionDecision.UNPARSEABLE)
    # Body of the block runs from end-of-header to start-of-next-header
    # (or EOF). We extract `resolved_at:` and free-prose `reason`/`note`
    # from it on a best-effort basis.
    start = first.end()
    end = matches[1].start() if len(matches) > 1 else len(body)
    block_body = body[start:end].strip()
    resolved_at = ""
    reason = ""
    for line in block_body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        m = re.match(r"^(\w+)\s*:\s*(.*)$", stripped)
        if m and m.group(1) == "resolved_at":
            resolved_at = m.group(2).strip()
        elif m and m.group(1) in {"reason", "note"}:
            if reason:
                reason += " "
            reason += m.group(2).strip()
        elif m is None:
            # Free prose tail line
            if reason:
                reason += " "
            reason += stripped
    if not resolved_at:
        resolved_at = _now_iso()
    if decision is ResolutionDecision.REDACTED:
        # Redacted state's body may also be replaced wholesale; if the
        # frontmatter says state=redacted, normalize the decision
        # regardless of what verb appeared.
        if fm_state == "redacted":
            decision = ResolutionDecision.REDACTED
    # Auto-decide recovery (round-2 P2-NEW-A): when the verb is
    # Auto-decided, the block was written by the framework, and its
    # reason line carries ``fallback_on_timeout=<policy>``. Recover the
    # correct decision enum (BLOCK vs ALLOW) from that field so the
    # audit record matches the framework's intent — not always the
    # hardcoded ``AUTO_DECIDED_BLOCK`` fallback in _VERB_TO_DECISION.
    if decision is ResolutionDecision.AUTO_DECIDED_BLOCK:
        m = re.search(r"fallback_on_timeout=(\w+)", reason)
        if m and m.group(1).lower() == "allow":
            decision = ResolutionDecision.AUTO_DECIDED_ALLOW
    return decision, operator, resolved_at, reason


def _flip_state_and_append_resolution(text: str, new_state: str, block: str) -> str:
    """Update frontmatter ``state:`` and append a resolution block under
    ``## Resolution``. Returns the new file text.
    """
    lines = text.splitlines()
    if lines and lines[0].strip() == _FM_DELIM:
        for i in range(1, len(lines)):
            if lines[i].strip() == _FM_DELIM:
                break
            m = _FM_LINE_RE.match(lines[i])
            if m and m.group(1) == "state":
                lines[i] = f"state: {new_state}"
                break
    new_text = "\n".join(lines)
    if not new_text.endswith("\n"):
        new_text += "\n"
    # Append the block at end-of-file (after the existing Resolution
    # section's HTML comment). Operators reading the file see one
    # canonical block.
    new_text += "\n" + block
    if not new_text.endswith("\n"):
        new_text += "\n"
    return new_text


# ──────────────────────────────────────────────────────────────────
# Time helpers


def _now_dt() -> datetime:
    return datetime.now(tz=timezone.utc)


def _now_iso(dt: datetime | None = None) -> str:
    return (dt or _now_dt()).isoformat()
