"""``MandateCheck`` — rule-engine judge specialist for mandate cite validation (spec/29).

Runs when ``proposal.authorization.granted_by`` starts with ``"mandate:"``.
Returns an ALLOW pass-through for proposals that don't cite a mandate
(the common case — fast-path at the top of ``evaluate``).

Per spec/29 §"MandateCheck judge specialist" (line 347-394), sibling of
``PolicyJudge`` in the composition ``[PolicyJudge, MandateCheck, LLMCatchAll]``.
Both are rule-engine, both always-on, both fail-fast.

PR 3a of #124. Validation steps 1-6 implemented (existence, source hash
no-op stub, state, tool allowlist, target allowlist, time window). Steps
7-9 (token budget, external budget, escalation thresholds) stubbed to
fail-closed with forever-stable reason ``mandate_budget_check_unavailable``;
PR 3b ungates them.

BLOCK reason naming discipline (spec/29 §"BLOCK reason naming discipline",
line 433): all reasons are forever-stable strings — no PR identifiers,
version suffixes, or transient context embedded in reason strings.

Defensive imports for parallel-agent interfaces
(Agent B: MandateStateManager, Agent C: TargetExtractorRegistry):
both are imported with try/except ImportError → runtime stub. The
integration session wires real imports; this module compiles and passes
basic tests with the stubs in place.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from ..mandate.backend import MandateBackend
from ..mandate.types import (
    MandateInvalid,
    MandateNotFound,
    RevocationState,
    TargetPattern,
)
from .backend import Judgment, JudgmentOutcome
from .types import ActionProposal, JudgmentContext

if TYPE_CHECKING:
    from ..judges_md import MandateSettings
    from ..logs.backend import LogBackend

_logger = logging.getLogger(__name__)


# ── Defensive shims for parallel-agent interfaces ────────────────────────────
# Agent B ships MandateStateManager; Agent C ships TargetExtractorRegistry.
# These stubs keep mandate_check.py importable standalone before the
# integration session wires the real modules. The stubs implement the
# minimal interface MandateCheck calls; real impls replace them at integration.


from .mandate_state import MandateStateManager
from .target_extractor_registry import TargetExtractorRegistry


# ── Judge ID ─────────────────────────────────────────────────────────────────

_JUDGE_ID = "mandate-check"
_POLICY_VERSION_SENTINEL = "unimplemented"  # replaced at construction when judges.md lands


class MandateCheck:
    """Rule-engine judge specialist for mandate cite validation.

    Runs when ``proposal.authorization.granted_by`` starts with
    ``"mandate:"``. Validates the cite against live mandate state via
    the ``MandateBackend`` Protocol (spec/29 §"MandateCheck judge
    specialist", line 347-394).

    Per spec/29, sibling of ``PolicyJudge`` in the composition
    ``[PolicyJudge, MandateCheck, LLMCatchAll]`` — both rule-engine,
    both always-on, both fail-fast.

    Validation steps (spec/29 §"Validation steps (in order)", line 360):
      0.  Pass-through for proposals not citing a mandate (fast path).
      0.5 Suspicious-rebind throttle check (spec/29 §"Suspicious-rebind
          throttle", line 394).
      1.  Existence — ``MandateNotFound`` / ``MandateInvalid`` → BLOCK.
      2.  Source hash — NO-OP in PR 3a; see TODO below.
      3.  State (active / revoked / expired) → BLOCK.
      4.  Tool allowlist → BLOCK.
      5.  Target allowlist / blocklist + unextractable-target action → BLOCK or ESCALATE.
      6.  Time window → BLOCK.
      7-9 Budget checks — STUBBED to fail-closed in PR 3a; PR 3b ungates.

    Thread safety: one ``threading.Lock`` per instance guards the
    per-scope state read-modify-write that ``arm_rebind_throttle`` uses
    (spec/29 §"Lifecycle event deduplication", line 741 — within-process
    serialization is sufficient; cross-process consistency is documented
    eventual).
    """

    def __init__(
        self,
        *,
        mandate_backend: MandateBackend,
        scope: str,
        target_extractor_registry: TargetExtractorRegistry,
        mandate_state_manager: MandateStateManager,
        mandate_settings: "MandateSettings",
        log_backend: "LogBackend",
        judge_id: str = _JUDGE_ID,
        policy_version: str = _POLICY_VERSION_SENTINEL,
    ) -> None:
        """Construct the ``MandateCheck`` specialist.

        Args:
            mandate_backend: the backend that resolves mandate IDs against
                ``mandates.md`` (or equivalent). Must satisfy
                ``MandateBackend`` Protocol.
            scope: ``"agent:<name>"`` or ``"project:<name>"`` — the scope
                this instance validates against. Passed to
                ``mandate_backend.load_mandate(id, scope)`` on every call.
            target_extractor_registry: per-agent registry of named
                target extractors (spec/29 §"Target extraction"). Parallel
                agent C's ``TargetExtractorRegistry``; a stub is used until
                that module ships.
            mandate_state_manager: per-scope state manager that tracks
                lifecycle events and rebind throttle state. Parallel agent
                B's ``MandateStateManager``; a stub is used until that
                module ships.
            mandate_settings: parsed ``## Mandates`` section from
                ``judges.md`` (``MandateSettings`` from ``judges_md.py``).
                Controls ``suspicious_rebind_throttle_s`` and
                ``unextractable_target_action``.
            log_backend: the agent's ``LogBackend`` instance. Used to emit
                lifecycle events (throttle, budget-stub) as ``RunRecord``
                entries with ``primitive="judgment"``.
            judge_id: stable judge identifier recorded in every
                ``Judgment``. Defaults to ``"mandate-check"``.
            policy_version: sha256-derived policy snapshot string.
                Defaults to ``"unimplemented"`` sentinel until PR 3a
                wires the judges.md source hash here.
        """
        self._backend = mandate_backend
        self._scope = scope
        self._extractors = target_extractor_registry
        self._state_manager = mandate_state_manager
        self._settings = mandate_settings
        self._log = log_backend
        self._judge_id = judge_id
        self._policy_version = policy_version
        # Within-process lock for state read-modify-write (spec/29 line 741).
        self._state_lock = threading.Lock()

    # ── JudgeBackend Protocol surface ─────────────────────────────────────────

    def evaluate(
        self,
        proposal: ActionProposal,
        context: JudgmentContext | None = None,
    ) -> Judgment:
        """Apply mandate cite validation checks to ``proposal``.

        Outcome flow (spec/29 §"Validation steps (in order)", line 360):

        1. Non-mandate proposals pass through immediately (fast path).
        2. Throttle check — blocks re-binding on recently-inconsistent mandates.
        3. Existence check via ``MandateBackend.load_mandate``.
        4. Source hash — NO-OP in PR 3a (see inline TODO).
        5. State check (active / revoked / expired).
        6. Tool allowlist check.
        7. Target allowlist / blocklist check.
        8. Time window check.
        9-11. Budget checks — stubbed fail-closed; PR 3b ungates.

        Does NOT mutate ``proposal`` or ``context`` (spec/28 idempotency
        invariant). Each call is side-effect-free from the proposal's
        perspective; state manager writes are contained within the manager.
        """
        # ── Step 0: Fast-path pass-through for non-mandate-citing proposals ───
        # Most proposals don't cite a mandate; this check is O(1) and
        # runs first so the mandate subsystem adds zero latency to the
        # common case.
        if proposal.authorization is None:
            return self._allow_passthrough(proposal)
        if not proposal.authorization.granted_by.startswith("mandate:"):
            return self._allow_passthrough(proposal)

        mandate_id = proposal.authorization.granted_by.removeprefix("mandate:")

        # ── Step 0.5: Suspicious-rebind throttle ──────────────────────────────
        # Spec/29 §"Suspicious-rebind throttle" (line 394-428).
        # Guards against the source-hash-before-state edit window: a
        # malicious or hallucinating actor that sees mandate_state_inconsistent
        # is throttled from re-citing the same mandate for
        # ``suspicious_rebind_throttle_s`` seconds. The throttle is per
        # (mandate_id, agent_run_id) — different agent runs are not throttled.
        if self._state_manager.is_rebind_throttled(mandate_id, proposal.actor_run_id):
            self._emit_event(
                "mandate_rebind_suspicious_throttled",
                proposal=proposal,
                mandate_id=mandate_id,
            )
            return self._block(
                # spec/29 §"BLOCK reason naming discipline" (line 433):
                # forever-stable, no PR identifier embedded.
                reason="mandate_rebind_suspicious_throttled",
                mandate_id=mandate_id,
                detail=(
                    "Re-binding to this mandate is throttled because a recent "
                    "mandate_state_inconsistent was detected. Wait for the "
                    f"throttle window ({self._settings.suspicious_rebind_throttle_s}s) "
                    "to expire, or request a new mandate from the operator."
                ),
            )

        # ── Step 1: Existence (spec/29 line 362) ──────────────────────────────
        # Load the mandate — raises MandateNotFound when the id is valid but
        # absent; raises MandateInvalid on parse-level failures.
        try:
            mandate = self._backend.load_mandate(mandate_id, self._scope)
        except MandateNotFound:
            # spec/29 line 362: "Not found → BLOCK with reason mandate_not_found"
            return self._block(
                reason="mandate_not_found",
                mandate_id=mandate_id,
                detail=(
                    f"No mandate with id {mandate_id!r} found in scope "
                    f"{self._scope!r}. Re-author the cite against a valid "
                    "mandate id, or request the operator to create one."
                ),
            )
        except MandateInvalid as exc:
            # Covers parser-level failures (malformed YAML, invalid ID charset,
            # missing unconstrained justification, etc.). spec/29 line 362.
            return self._block(
                reason="mandate_invalid",
                mandate_id=mandate_id,
                detail=f"Mandate {mandate_id!r} failed validation: {exc}",
            )

        # ── Step 2: Source hash (spec/29 line 363) ────────────────────────────
        # Spec/29 line 354: re-read mandates.md, recompute source_hash of the
        # cited mandate's section, compare against the value bound at proposal
        # time. The framework re-reads FIRST (before state) because
        # state-from-stale-bytes is misleading — if the operator just revoked
        # the mandate, the hash check catches it before the state check would
        # (spec/29 §"Source-hash-before-state ordering", line 363 + the
        # §"Suspicious-rebind throttle" rationale at line 396).
        #
        # TODO (PR 4): wire proposal assembly to bind mandate_source_hash into
        # the proposal so this check has something to compare against.
        # ActionProposal does NOT yet carry a mandate_source_hash field in
        # its binding — that wiring lands in PR 4 (proposal assembly extension).
        # Until then, step 2 is a no-op pass-through: we have the freshly-loaded
        # mandate.source_hash from step 1, but nothing to compare it against.
        #
        # When step 2 DOES surface a mismatch the correct action is:
        #   arm_rebind_throttle(mandate_id, proposal.actor_run_id, throttle_seconds)
        #   return self._block(reason="mandate_state_inconsistent", ...)
        # The throttle arms BEFORE returning so the next call from the same
        # actor_run_id hits step 0.5 immediately.
        #
        # RFC note: the proposal_binding hash (ActionProposal.arguments_hash)
        # is distinct from mandate_source_hash. The former pins the tool call;
        # the latter pins the mandate text version. Both live in the Judgment
        # event's binding field at execution time. PR 4 adds the
        # mandate_source_hash field to the proposal assembly path.
        pass  # step 2 is intentionally no-op in PR 3a

        # ── Step 3: State (spec/29 line 364) ──────────────────────────────────
        # Check revocation_state; also covers time-based expiry (the backend
        # infers EXPIRED from expires_at at load time per mandate/types.py
        # RevocationState docstring).
        if mandate.revocation_state == RevocationState.REVOKED:
            return self._block(
                reason="mandate_revoked",
                mandate_id=mandate_id,
                detail=(
                    f"Mandate {mandate_id!r} has been revoked. "
                    "Request a new mandate from the operator."
                ),
            )
        if mandate.revocation_state == RevocationState.EXPIRED:
            return self._block(
                reason="mandate_expired",
                mandate_id=mandate_id,
                detail=(
                    f"Mandate {mandate_id!r} has expired"
                    + (
                        f" (expired_at={mandate.expires_at.isoformat()})"
                        if mandate.expires_at
                        else ""
                    )
                    + ". Request a renewed mandate from the operator."
                ),
            )

        # ── Step 4: Tool allowlist (spec/29 line 365) ─────────────────────────
        # If allowed_tools is set (non-empty frozenset), the proposal's
        # tool_name MUST appear in it. An empty frozenset means "unconstrained
        # by tool" — the constraint is absent, not zero-length restriction.
        if mandate.constraints.allowed_tools and (
            proposal.tool_name not in mandate.constraints.allowed_tools
        ):
            return self._block(
                reason="mandate_tool_not_allowed",
                mandate_id=mandate_id,
                detail=(
                    f"Tool {proposal.tool_name!r} is not in mandate "
                    f"{mandate_id!r}'s allowed_tools list "
                    f"({sorted(mandate.constraints.allowed_tools)!r}). "
                    "Use an allowed tool or request a mandate that covers this tool."
                ),
            )

        # ── Step 5: Target allowlist + blocklist (spec/29 line 366-370) ───────
        # Target extraction is framework-owned (spec/29 §"Target extraction",
        # line 439). The actor cannot influence target_canonical; it is
        # extracted from the proposal's tool_arguments by the per-agent
        # TargetExtractorRegistry (Agent C's implementation).
        #
        # Three sub-cases:
        #   (a) allowed_targets set + extraction returns None → fail-closed
        #       (block or escalate per unextractable_target_action config).
        #   (b) allowed_targets set + target extracted → must match ≥1 pattern.
        #   (c) blocked_targets set + target extracted → must NOT match any pattern.
        has_allowed_targets = bool(mandate.constraints.allowed_targets)
        has_blocked_targets = bool(mandate.constraints.blocked_targets)

        if has_allowed_targets or has_blocked_targets:
            target = self._extractors.extract(
                proposal.tool_name,
                proposal.tool_arguments,
                mcp_server=proposal.mcp_server,
            )

            # Sub-case (a): unextractable target with an allowlist set.
            if target is None and has_allowed_targets:
                # spec/29 line 366-370: behavior controlled by
                # judges.md mandate_settings.unextractable_target_action.
                if self._settings.unextractable_target_action == "escalate":
                    return self._escalate(
                        reason="mandate_target_unextractable",
                        mandate_id=mandate_id,
                        detail=(
                            f"No target could be extracted from "
                            f"{proposal.tool_name!r} arguments for mandate "
                            f"{mandate_id!r} which requires a target allowlist "
                            "match. Configured action: escalate."
                        ),
                    )
                # Default: block (fail-closed per spec/29 line 367).
                return self._block(
                    reason="mandate_target_unextractable",
                    mandate_id=mandate_id,
                    detail=(
                        f"No target could be extracted from "
                        f"{proposal.tool_name!r} arguments for mandate "
                        f"{mandate_id!r} which requires a target allowlist "
                        "match. Register a target_extractor on the tool "
                        "definition or omit allowed_targets from the mandate."
                    ),
                )

            # Sub-case (b): target extracted + allowlist check.
            if target is not None and has_allowed_targets:
                if not self._matches_any(target, mandate.constraints.allowed_targets):
                    return self._block(
                        reason="mandate_target_not_allowed",
                        mandate_id=mandate_id,
                        detail=(
                            f"Extracted target {target!r} does not match any "
                            f"allowed_targets pattern in mandate {mandate_id!r}. "
                            "Use a target that satisfies the mandate's allowlist."
                        ),
                    )

            # Sub-case (c): target extracted + blocklist check.
            if target is not None and has_blocked_targets:
                if self._matches_any(target, mandate.constraints.blocked_targets):
                    return self._block(
                        reason="mandate_target_blocked",
                        mandate_id=mandate_id,
                        detail=(
                            f"Extracted target {target!r} matches a blocked_targets "
                            f"pattern in mandate {mandate_id!r}. "
                            "The mandate explicitly prohibits this target."
                        ),
                    )

        # ── Step 6: Time window (spec/29 line 371) ────────────────────────────
        # If constraints.time_window is set, current UTC time must fall within
        # the window. Supports contiguous windows (start < end) and
        # wrap-around windows that span midnight (start > end, e.g., 22:00–06:00).
        # TimeWindow.start_utc == TimeWindow.end_utc is refused by the parser as
        # ambiguous (mandate/types.py TimeWindow docstring).
        if mandate.constraints.time_window is not None:
            tw = mandate.constraints.time_window
            now_utc = datetime.now(timezone.utc).time()

            if tw.start_utc < tw.end_utc:
                # Contiguous window (e.g., 09:00–17:00 UTC).
                in_window = tw.start_utc <= now_utc <= tw.end_utc
            else:
                # Wrap-around window spanning midnight (e.g., 22:00–06:00 UTC).
                # "now" is in-window if it's at or after start OR at or before end.
                in_window = now_utc >= tw.start_utc or now_utc <= tw.end_utc

            if not in_window:
                return self._block(
                    reason="mandate_outside_time_window",
                    mandate_id=mandate_id,
                    detail=(
                        f"Current UTC time {now_utc.isoformat()} is outside "
                        f"mandate {mandate_id!r}'s allowed window "
                        f"({tw.start_utc.isoformat()}–{tw.end_utc.isoformat()} UTC). "
                        "Wait until the window opens."
                    ),
                )

        # ── Steps 7, 8, 9: Budget checks — STUBBED (PR 3a) ──────────────────
        # Spec/29 §"Validation step split between PR 3a and PR 3b" (line 429):
        # steps 7-9 (token budget, external budget, escalation thresholds)
        # land in PR 3b. PR 3a stubs them to fail-closed: any mandate with a
        # *_budget_usd or *_external_usd or *_escalation_above_* cap returns
        # BLOCK with reason mandate_budget_check_unavailable.
        #
        # BLOCK reason discipline (spec/29 line 433): "mandate_budget_check_unavailable"
        # is the forever-stable reason. NOT "mandate_budget_check_unavailable_in_3a".
        # The temporary cause is documented in the PR 3a CHANGELOG; the reason
        # string itself is stable across the eventual PR 3b unlock.
        has_budget_cap = any([
            mandate.constraints.daily_token_usd is not None,
            mandate.constraints.monthly_token_usd is not None,
            mandate.constraints.cumulative_token_usd is not None,
            mandate.constraints.daily_external_usd is not None,
            mandate.constraints.monthly_external_usd is not None,
            mandate.constraints.cumulative_external_usd is not None,
            mandate.constraints.requires_escalation_above_token_usd is not None,
            mandate.constraints.requires_escalation_above_external_usd is not None,
        ])
        if has_budget_cap:
            return self._block(
                reason="mandate_budget_check_unavailable",
                mandate_id=mandate_id,
                detail=(
                    f"Mandate {mandate_id!r} specifies a budget cap, but "
                    "budget enforcement (steps 7-9) is not yet available. "
                    "Remove the budget cap from the mandate to proceed, or "
                    "wait for PR 3b which implements budget checking."
                ),
            )

        # ── All checks pass → ALLOW ────────────────────────────────────────────
        # Spec/29 line 390: "If all checks pass: ALLOW. The judgment event's
        # `binding` carries the `mandate_source_hash` so execution-time
        # re-binding can re-verify if needed."
        # mandate_source_hash is included in the Judgment's reason (as a
        # structured comment) so it surfaces in audit; the formal binding-field
        # wiring is PR 4 (proposal assembly wiring closes the loop).
        return self._allow(proposal, mandate_id=mandate_id, mandate=mandate)

    def supported_outcomes(self) -> set[JudgmentOutcome]:
        """Declare which outcomes MandateCheck can return.

        Per spec/29, MandateCheck returns ALLOW, BLOCK, and ESCALATE.
        REVISE is never returned — mandate cites cannot be repaired by
        the judge (spec/29 line 343-345).
        """
        return {
            JudgmentOutcome.ALLOW,
            JudgmentOutcome.BLOCK,
            JudgmentOutcome.ESCALATE,
        }

    def supports_read_audit(self) -> bool:
        # Rule engine is deterministic and free — supports audit mode.
        return True

    def supports_specialist_composition(self) -> bool:
        # MandateCheck composes into the [PolicyJudge, MandateCheck,
        # LLMCatchAll] ensemble (spec/29 line 392).
        return True

    @property
    def judge_id(self) -> str:
        return self._judge_id

    @property
    def policy_version(self) -> str:
        return self._policy_version

    def close(self) -> None:
        # No I/O resources to release. The state_lock is GC'd with the instance.
        return None

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _allow_passthrough(self, proposal: ActionProposal) -> Judgment:
        """ALLOW for proposals that don't cite a mandate.

        The most common path — proposals from actors who don't use the
        mandate subsystem (or cite a non-mandate authorization) pass
        through with zero mandate-subsystem overhead beyond this check.
        """
        return Judgment(
            outcome=JudgmentOutcome.ALLOW,
            reason="no_mandate_cite",
            judge_id=self._judge_id,
            policy_version=self._policy_version,
            latency_ms=0,
            cost_usd=0.0,
        )

    def _allow(
        self,
        proposal: ActionProposal,
        *,
        mandate_id: str,
        mandate: Any,
    ) -> Judgment:
        """ALLOW for a proposal that has passed all mandate validation steps.

        Includes the ``mandate_source_hash`` in the reason for audit
        traceability. PR 4 will move this into the formal binding field
        once proposal assembly wires mandate_source_hash into ActionProposal.
        """
        return Judgment(
            outcome=JudgmentOutcome.ALLOW,
            reason=(
                f"all mandate checks passed for {mandate_id!r}; "
                f"source_hash={mandate.source_hash!r}"
            ),
            judge_id=self._judge_id,
            policy_version=self._policy_version,
            latency_ms=0,
            cost_usd=0.0,
        )

    def _block(
        self,
        *,
        reason: str,
        mandate_id: str,
        detail: str = "",
    ) -> Judgment:
        """BLOCK with a forever-stable reason string.

        ``reason`` must be one of the spec/29 BLOCK reasons (per §"BLOCK
        reason naming discipline", line 433). ``detail`` carries the
        human-readable explanation that may change across versions;
        ``reason`` does not.
        """
        full_reason = reason
        if detail:
            full_reason = f"{reason}: {detail}"
        return Judgment(
            outcome=JudgmentOutcome.BLOCK,
            reason=full_reason,
            judge_id=self._judge_id,
            policy_version=self._policy_version,
            latency_ms=0,
            cost_usd=0.0,
        )

    def _escalate(
        self,
        *,
        reason: str,
        mandate_id: str,
        detail: str = "",
    ) -> Judgment:
        """ESCALATE for cases where the operator should decide.

        Currently used for ``mandate_target_unextractable`` when
        ``unextractable_target_action == "escalate"`` (spec/29 line 368).
        """
        full_reason = reason
        if detail:
            full_reason = f"{reason}: {detail}"
        return Judgment(
            outcome=JudgmentOutcome.ESCALATE,
            reason=full_reason,
            judge_id=self._judge_id,
            policy_version=self._policy_version,
            latency_ms=0,
            cost_usd=0.0,
        )

    def _matches_any(
        self,
        target: str,
        patterns: tuple[TargetPattern, ...],
    ) -> bool:
        """Return True if ``target`` matches any pattern in ``patterns``.

        Pattern kinds (per ``TargetPattern.kind`` in mandate/types.py):
          - ``"exact"`` — target must equal pattern.pattern exactly.
          - ``"prefix"`` — target must start with pattern.pattern
            (after stripping a trailing ``"*"`` wildcard if present —
            the mandates.md parser may normalize ``"foo.*"`` to a
            prefix TargetPattern with ``pattern="foo."``).

        MCP tool targets are prefixed with ``"mcp:<server>:"`` by the
        framework before reaching this method (spec/29 line 464). Pattern
        strings in ``mandates.md`` must include the prefix to match:
        ``mcp:stripe:`` as a prefix pattern covers all Stripe MCP targets.
        """
        for tp in patterns:
            if tp.kind == "exact":
                if target == tp.pattern:
                    return True
            elif tp.kind == "prefix":
                # Strip a trailing "*" from the pattern string (mandates.md
                # convention: "foo.*" → prefix match against "foo.").
                prefix = tp.pattern.rstrip("*")
                if target.startswith(prefix):
                    return True
            else:
                # Unknown kind — fail-closed: unknown kinds never match.
                # Logged at DEBUG so operator tooling can surface the misconfiguration.
                _logger.debug(
                    "MandateCheck: unknown TargetPattern kind %r for pattern %r; "
                    "treating as no-match (fail-closed)",
                    tp.kind,
                    tp.pattern,
                )
        return False

    def _emit_event(
        self,
        event_name: str,
        *,
        proposal: ActionProposal,
        mandate_id: str,
        extra: dict | None = None,
    ) -> None:
        """Write a lifecycle event to the LogBackend.

        Emits a ``RunRecord`` with ``primitive="judgment"`` and
        ``trigger=event_name`` so the JSONL audit log captures mandate
        lifecycle events alongside normal judgment records. Lifecycle
        events are documented in spec/29 §"Lifecycle event deduplication".

        Failures to emit are logged at WARNING but never re-raised —
        the judgment itself is not dependent on log-write success
        (matches the framework's "fail the tool, not the framework"
        pattern from _log() in agent.py).
        """
        try:
            from ..logs.types import RunRecord

            now_iso = datetime.now(timezone.utc).isoformat()
            record_extra = {
                "event": event_name,
                "mandate_id": mandate_id,
                "judge_id": self._judge_id,
                "proposal_id": proposal.proposal_id,
                "actor_run_id": proposal.actor_run_id,
            }
            if extra:
                record_extra.update(extra)

            record = RunRecord(
                ts=now_iso,
                run_id=proposal.actor_run_id,
                primitive="judgment",
                status="ok",
                summary=f"mandate lifecycle: {event_name} for {mandate_id!r}",
                model="n/a",
                input_tokens=0,
                output_tokens=0,
                trigger=event_name,
                agent_name=proposal.actor_agent,
                mandate_id=mandate_id,
                extra=record_extra,
            )
            self._log.append(record)
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "MandateCheck: failed to emit lifecycle event %r: %s",
                event_name,
                exc,
            )
