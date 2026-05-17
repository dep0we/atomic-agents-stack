"""``MandateCheck`` — rule-engine judge specialist for mandate cite validation (spec/29).

Runs when ``proposal.authorization.granted_by`` starts with ``"mandate:"``.
Returns an ALLOW pass-through for proposals that don't cite a mandate
(the common case — fast-path at the top of ``evaluate``).

Per spec/29 §"MandateCheck judge specialist" (line 347-394), sibling of
``PolicyJudge`` in the composition ``[PolicyJudge, MandateCheck, LLMCatchAll]``.
Both are rule-engine, both always-on, both fail-fast.

PR 3b of #124. Validation steps 1-6 implemented in PR 3a (existence, source hash
no-op stub, state, tool allowlist, target allowlist, time window). Steps 7-9
(token budget, external budget, escalation thresholds) ungated here.

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
from .cost_estimator_registry import CostEstimatorRegistry
from .types import ActionClass, ActionProposal, JudgmentContext

if TYPE_CHECKING:
    from ..judges_md import MandateSettings
    from ..logs.backend import LogBackend
    from ..tools import ToolRegistry

_logger = logging.getLogger(__name__)


# ── Defensive shims for parallel-agent interfaces ────────────────────────────
# Agent B ships MandateStateManager; Agent C ships TargetExtractorRegistry.
# These stubs keep mandate_check.py importable standalone before the
# integration session wires the real modules. The stubs implement the
# minimal interface MandateCheck calls; real impls replace them at integration.


from .mandate_state import MandateStateManager  # noqa: E402
from .target_extractor_registry import TargetExtractorRegistry  # noqa: E402

# ── Sub-agent A: mandate_reservations ─────────────────────────────────────
# compute_outstanding shipped by sub-agent A in parallel. Import is
# intentionally un-guarded: the integration session wires the real module;
# if the file doesn't exist yet at import time the ImportError surfaces
# at construction rather than at the first budget check.
from .mandate_reservations import compute_outstanding  # noqa: E402


# ── Judge ID ─────────────────────────────────────────────────────────────────

_JUDGE_ID = "mandate-check"
_POLICY_VERSION_SENTINEL = (
    "unimplemented"  # replaced at construction when judges.md lands
)


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
        cost_estimator_registry: CostEstimatorRegistry | None = None,
        tool_registry: "ToolRegistry | None" = None,
        expected_cost_per_call_usd: float | None = None,
        iteration_start_ts: str | None = None,
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
                lifecycle events and query prior cost events for budget
                checks.
            cost_estimator_registry: per-agent registry of named cost
                estimators (spec/29 step 8). Used to project external cost
                from tool arguments at evaluation time.
            tool_registry: the agent's tool registry, used to look up
                ``ToolDefinition.expected_external_cost_usd`` and
                ``cost_estimator_id`` for step 8. May be ``None`` when
                the caller doesn't wire the mandate layer (budget steps
                fail-closed when the tool definition is unavailable).
            expected_cost_per_call_usd: the agent's model.md
                ``expected_cost_per_call_usd`` field. Used as the
                first-iteration baseline for token-cost projection (step 7)
                when no preceding cost event exists for the current run.
                When ``None``, the conservative default ($0.10) applies.
            iteration_start_ts: ISO-8601 timestamp marking the start of
                the current ``agent.call()`` iteration. Used by step 7's
                Risk 2 defensive check: if the most-recent matching cost
                event's ``ts`` is before this value, it is from a prior
                iteration and the baseline falls back to
                ``expected_cost_per_call_usd``. When ``None``, the Risk 2
                check is skipped (all matching events are treated as current).
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
        self._cost_estimators = (
            cost_estimator_registry
            if cost_estimator_registry is not None
            else CostEstimatorRegistry()
        )
        self._tool_registry = tool_registry
        self._expected_cost_per_call_usd = expected_cost_per_call_usd
        self._iteration_start_ts = iteration_start_ts
        self._judge_id = judge_id
        self._policy_version = policy_version
        # Within-process lock for state read-modify-write (spec/29 line 741).
        self._state_lock = threading.Lock()
        # Round 1 Finding 6: per-proposal projection cache so callers can
        # pass non-zero ``projected_usd`` to MandateReservationManager.create().
        # Keyed by proposal_id; entries are tuples (projected_token_usd,
        # projected_external_usd). Cleared by callers via pop_projection()
        # after consumption (typical lifetime = one iteration of the agent
        # loop). NOT shared across agent instances.
        self._projection_cache: dict[str, tuple[float, float]] = {}
        self._projection_lock = threading.Lock()

    def record_projection(
        self,
        proposal_id: str,
        projected_token_usd: float,
        projected_external_usd: float,
    ) -> None:
        """Cache the projection for ``proposal_id`` so caller can read it
        before creating a reservation. Internal; called from ``evaluate()``
        on every ALLOW path."""
        with self._projection_lock:
            self._projection_cache[proposal_id] = (
                projected_token_usd,
                projected_external_usd,
            )

    def pop_projection(self, proposal_id: str) -> tuple[float, float]:
        """Read-and-clear the cached projection for ``proposal_id``.

        Returns ``(0.0, 0.0)`` when no projection was recorded (no caps on
        the mandate, or the proposal didn't go through evaluate()).
        """
        with self._projection_lock:
            return self._projection_cache.pop(proposal_id, (0.0, 0.0))

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
        9. Token budget check (step 7).
        10. External budget check (step 8) + escalation threshold check (step 9)
            evaluated together; step 9 ESCALATE preempts step 8 BLOCK (spec/29
            line 384, Risk 7 amendment).

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

        # ── Step 7: Token budget (spec/29 line 372-380) ───────────────────────
        # Collect all token-cap exceeded entries. Do NOT return here; feed into
        # the unified cap-exceeded helper at the end (spec says collect all
        # exceeded caps into an intermediate, then select primary by priority).
        caps_exceeded: dict[str, dict[str, Any]] = {}
        contributing_reservation_ids: list[str] = []

        has_token_cap = any(
            [
                mandate.constraints.daily_token_usd is not None,
                mandate.constraints.monthly_token_usd is not None,
                mandate.constraints.cumulative_token_usd is not None,
            ]
        )
        has_token_escalation_threshold = (
            mandate.constraints.requires_escalation_above_token_usd is not None
        )
        projected_token_usd: float = 0.0
        if has_token_cap or has_token_escalation_threshold:
            token_result = self._step7_token_budget(
                proposal=proposal,
                mandate=mandate,
                mandate_id=mandate_id,
            )
            projected_token_usd = token_result["projected_usd"]
            caps_exceeded.update(token_result["caps_exceeded"])
            contributing_reservation_ids.extend(token_result.get("reservation_ids", []))

        # ── Step 8: External budget (spec/29 line 381) ────────────────────────
        has_external_escalation_threshold = (
            mandate.constraints.requires_escalation_above_external_usd is not None
        )
        projected_external_usd: float = 0.0
        has_external_cap = any(
            [
                mandate.constraints.daily_external_usd is not None,
                mandate.constraints.monthly_external_usd is not None,
                mandate.constraints.cumulative_external_usd is not None,
                mandate.constraints.per_action_max_usd is not None
                if hasattr(mandate.constraints, "per_action_max_usd")
                else False,
            ]
        )
        if has_external_cap or has_external_escalation_threshold:
            ext_result = self._step8_external_budget(
                proposal=proposal,
                mandate=mandate,
                mandate_id=mandate_id,
            )
            # Fail-closed: unprojectable → immediate BLOCK, bypass step 9
            if ext_result.get("unprojectable"):
                return self._block(
                    reason="mandate_external_cost_unprojectable",
                    mandate_id=mandate_id,
                    detail=(
                        f"Mandate {mandate_id!r} requires external budget projection "
                        f"for tool {proposal.tool_name!r}, but neither a registered "
                        "cost_estimator_id nor a static expected_external_cost_usd "
                        "is configured for this tool. Register an estimator via "
                        "agent.register_cost_estimator() or add "
                        "expected_external_cost_usd to the tool definition."
                    ),
                )
            projected_external_usd = ext_result["projected_usd"]
            caps_exceeded.update(ext_result["caps_exceeded"])
            contributing_reservation_ids.extend(ext_result.get("reservation_ids", []))

        # Round 1 Finding 6: cache the projection so the agent loop can
        # create a reservation with non-zero ``projected_usd``. Without this,
        # the stale-budget race defense in compute_outstanding is vacuous
        # (outstanding reservations contribute 0 to cumulative; concurrent
        # mandate-citing actions all see "no outstanding spend" and exceed
        # the cap silently). Recorded BEFORE every potential ALLOW return so
        # the cache reflects the most recent projection for this proposal.
        self.record_projection(
            proposal.proposal_id, projected_token_usd, projected_external_usd
        )

        # ── Step 9: Escalation thresholds (spec/29 line 382-384, Risk 7) ─────
        # Spec/29 line 384 amendment: step 9 ESCALATE preempts step 8 BLOCK
        # when both fire for the same projection.  Evaluate before returning
        # any cap-exceeded verdict.
        escalation_judgment = self._step9_escalation_thresholds(
            proposal=proposal,
            mandate=mandate,
            mandate_id=mandate_id,
            projected_token_usd=projected_token_usd,
            projected_external_usd=projected_external_usd,
        )
        if escalation_judgment is not None:
            return escalation_judgment

        # ── Cap-exceeded verdict (steps 7 + 8) ───────────────────────────────
        if caps_exceeded:
            cumulative_token_now = self._sum_prior_token_cost(
                mandate_id, proposal.actor_run_id
            )
            cumulative_external_now = self._sum_prior_external_cost(mandate_id)
            return self._build_cap_exceeded_judgment(
                proposal=proposal,
                mandate=mandate,
                caps_exceeded=caps_exceeded,
                contributing_reservation_ids=list(set(contributing_reservation_ids)),
                action_class=proposal.classification,
                cumulative_token_now=cumulative_token_now,
                cumulative_external_now=cumulative_external_now,
                projected_usd=max(projected_token_usd, projected_external_usd),
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

    # ── Budget step helpers ───────────────────────────────────────────────────

    # Default token-cost baseline when no preceding iteration cost event
    # exists AND model.md expected_cost_per_call_usd is not set.
    _DEFAULT_EXPECTED_TOKEN_COST_USD: float = 0.10

    def _sum_prior_token_cost(
        self,
        mandate_id: str,
        run_id: str,
    ) -> float:
        """Sum prior actor-source cost events tagged with ``mandate_id``.

        These are the actual committed token costs for this mandate.  The
        per-action projection is approximate (spec/29 line 378); this sum
        is the ground truth after the fact.
        """
        from ..logs.types import LogQuery

        try:
            records = self._log.query(
                LogQuery(
                    cost_source="actor",
                    mandate_id=mandate_id,
                )
            )
            return sum(
                r.cost_usd
                for r in records
                if r.cost_usd is not None
                # Exclude records without mandate_id set (legacy safeguard)
                and r.mandate_id == mandate_id
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "MandateCheck step 7: failed to sum prior token cost for "
                "mandate %r: %s — treating as 0 (optimistic fallback)",
                mandate_id,
                exc,
            )
            return 0.0

    def _sum_prior_external_cost(self, mandate_id: str) -> float:
        """Sum prior external_cost events tagged with ``mandate_id``.

        Per spec/29 §"Cost integration" (line 537-554): external costs land
        in a cost event with ``extra["cost_kind"] == "external"`` and
        ``mandate_id`` set.  The sum drives the cumulative external budget.
        """
        from ..logs.types import LogQuery

        try:
            records = self._log.query(
                LogQuery(
                    mandate_id=mandate_id,
                )
            )
            return sum(
                r.extra.get("external_cost_usd", 0.0)
                for r in records
                if r.extra.get("cost_kind") == "external" and r.mandate_id == mandate_id
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "MandateCheck step 8: failed to sum prior external cost for "
                "mandate %r: %s — treating as 0 (optimistic fallback)",
                mandate_id,
                exc,
            )
            return 0.0

    def _project_token_cost(self, proposal: ActionProposal, n_tool_calls: int) -> float:
        """Project this action's share of the upcoming turn's token cost.

        Uses the preceding iteration's actual token cost as the baseline
        (spec/29 line 374-376).  Falls back to ``expected_cost_per_call_usd``
        (from model.md) or the $0.10 conservative default when no prior
        iteration event exists for the current run (Risk 2 discipline).

        Token cost is apportioned as ``turn_cost / N`` across N concurrent
        tool calls in the turn (v1 simplification — argument-token-weighted
        apportionment is a follow-up issue per spec/29 line 376).
        """
        from ..logs.types import LogQuery

        # Find the preceding iteration's cost event for this run.
        turn_cost: float | None = None
        try:
            records = self._log.query(
                LogQuery(
                    run_id=proposal.actor_run_id,
                    cost_source="actor",
                    limit=1,
                )
            )
            # query() returns chronological order; take the last entry.
            if records:
                most_recent = records[-1]
                # Risk 2 defensive check: if the most recent event's ts is
                # before the current iteration's start, it is from a prior
                # agent.call() iteration and represents a stale baseline.
                # Fall back to expected_cost_per_call_usd to avoid drift.
                if (
                    self._iteration_start_ts is not None
                    and most_recent.ts < self._iteration_start_ts
                ):
                    # Stale baseline — treat as first iteration.
                    turn_cost = None
                elif most_recent.cost_usd is not None:
                    turn_cost = most_recent.cost_usd
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "MandateCheck step 7: failed to read preceding iteration "
                "cost event for run %r: %s — falling back to default baseline",
                proposal.actor_run_id,
                exc,
            )

        if turn_cost is None:
            # First iteration or stale baseline: use model.md field or default.
            turn_cost = (
                self._expected_cost_per_call_usd
                if self._expected_cost_per_call_usd is not None
                else self._DEFAULT_EXPECTED_TOKEN_COST_USD
            )

        # Simple N-way apportionment (v1 — see spec/29 line 376).
        n = max(1, n_tool_calls)
        return turn_cost / n

    def _step7_token_budget(
        self,
        *,
        proposal: ActionProposal,
        mandate: Any,
        mandate_id: str,
    ) -> dict[str, Any]:
        """Evaluate step 7 token budget caps.

        Returns a dict with keys:
          ``projected_usd`` — the apportioned token cost projection.
          ``caps_exceeded`` — dict of cap_kind → metadata for caps that fire.
          ``reservation_ids`` — list of outstanding reservation IDs that
              contributed to pushing the cumulative over the cap.
        """
        # Simple assumption: one tool call per turn for v1 apportionment.
        # The framework does not yet pass N across tool calls per turn to
        # MandateCheck; argument-token-weighted apportionment is a follow-up.
        n_tool_calls = 1
        projected = self._project_token_cost(proposal, n_tool_calls)

        prior_spend = self._sum_prior_token_cost(mandate_id, proposal.actor_run_id)

        # Sum outstanding reservations for this mandate (token kind).
        try:
            outstanding = compute_outstanding(self._log, self._scope, mandate_id)
            token_reservations = [r for r in outstanding if r.cost_kind == "token"]
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "MandateCheck step 7: compute_outstanding failed for mandate "
                "%r: %s — treating outstanding reservations as 0",
                mandate_id,
                exc,
            )
            token_reservations = []

        reserved = sum(r.projected_usd for r in token_reservations)
        reservation_ids = [r.proposal_id for r in token_reservations]
        cumulative = prior_spend + projected + reserved

        caps_exceeded: dict[str, dict[str, Any]] = {}
        c = mandate.constraints

        if c.cumulative_token_usd is not None and cumulative > c.cumulative_token_usd:
            caps_exceeded["cumulative_token_usd"] = {
                "projected": projected,
                "cumulative": cumulative,
                "cap": c.cumulative_token_usd,
            }

        # For daily/monthly window sums: in v1 we conservatively re-use the
        # full cumulative spend (no per-window scan implemented yet — a
        # follow-up issue should add LogQuery.since/until window scoping).
        # The full cumulative is always ≥ the windowed sum, so this never
        # under-blocks.  It may over-block near cap exhaustion within the
        # window; operators can widen the window cap as a workaround.
        if c.daily_token_usd is not None and cumulative > c.daily_token_usd:
            caps_exceeded["daily_token_usd"] = {
                "projected": projected,
                "cumulative": cumulative,
                "cap": c.daily_token_usd,
            }

        if c.monthly_token_usd is not None and cumulative > c.monthly_token_usd:
            caps_exceeded["monthly_token_usd"] = {
                "projected": projected,
                "cumulative": cumulative,
                "cap": c.monthly_token_usd,
            }

        return {
            "projected_usd": projected,
            "caps_exceeded": caps_exceeded,
            "reservation_ids": reservation_ids,
        }

    def _step8_external_budget(
        self,
        *,
        proposal: ActionProposal,
        mandate: Any,
        mandate_id: str,
    ) -> dict[str, Any]:
        """Evaluate step 8 external budget caps.

        Returns a dict with keys:
          ``projected_usd`` — the projected external cost for this action.
          ``caps_exceeded`` — dict of cap_kind → metadata for caps that fire.
          ``reservation_ids`` — list of outstanding reservation IDs.
          ``unprojectable`` — True when the external cost cannot be
              projected (fail-closed per spec/29 line 381).
        """
        # Resolve the tool definition for cost_estimator_id and
        # expected_external_cost_usd.
        tool_def = None
        if self._tool_registry is not None:
            try:
                tool_def = self._tool_registry.get(proposal.tool_name)
            except Exception:
                tool_def = None

        estimator_id: str | None = (
            tool_def.cost_estimator_id if tool_def is not None else None
        )
        static_cost: float | None = (
            tool_def.expected_external_cost_usd if tool_def is not None else None
        )

        # Project external cost — prefer registry estimator, fall back to
        # static estimate, fail-closed when neither is available.
        projected: float
        if estimator_id is not None:
            try:
                projected = self._cost_estimators.estimate(
                    proposal.tool_name,
                    proposal.tool_arguments,
                    estimator_id=estimator_id,
                )
            except Exception as exc:  # noqa: BLE001
                # Round 1 Finding 3: estimator raise must fail-closed to the
                # spec-stable reason, NOT bubble up as "judge dispatch error".
                _logger.warning(
                    "MandateCheck step 8: cost_estimator_id %r raised %s — "
                    "treating as unprojectable",
                    estimator_id,
                    type(exc).__name__,
                )
                projected = float("inf")
            # +inf from estimator means unprojectable.
            if projected == float("inf"):
                if static_cost is not None:
                    projected = static_cost
                else:
                    return {
                        "projected_usd": float("inf"),
                        "caps_exceeded": {},
                        "reservation_ids": [],
                        "unprojectable": True,
                    }
        elif static_cost is not None:
            projected = static_cost
        else:
            # Neither estimator nor static estimate configured — fail-closed.
            return {
                "projected_usd": float("inf"),
                "caps_exceeded": {},
                "reservation_ids": [],
                "unprojectable": True,
            }

        prior_external = self._sum_prior_external_cost(mandate_id)

        # Outstanding external reservations.
        try:
            outstanding = compute_outstanding(self._log, self._scope, mandate_id)
            ext_reservations = [r for r in outstanding if r.cost_kind == "external"]
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "MandateCheck step 8: compute_outstanding failed for mandate "
                "%r: %s — treating outstanding external reservations as 0",
                mandate_id,
                exc,
            )
            ext_reservations = []

        reserved = sum(r.projected_usd for r in ext_reservations)
        reservation_ids = [r.proposal_id for r in ext_reservations]
        cumulative = prior_external + projected + reserved

        caps_exceeded: dict[str, dict[str, Any]] = {}
        c = mandate.constraints

        if (
            c.cumulative_external_usd is not None
            and cumulative > c.cumulative_external_usd
        ):
            caps_exceeded["cumulative_external_usd"] = {
                "projected": projected,
                "cumulative": cumulative,
                "cap": c.cumulative_external_usd,
            }

        if c.daily_external_usd is not None and cumulative > c.daily_external_usd:
            caps_exceeded["daily_external_usd"] = {
                "projected": projected,
                "cumulative": cumulative,
                "cap": c.daily_external_usd,
            }

        if c.monthly_external_usd is not None and cumulative > c.monthly_external_usd:
            caps_exceeded["monthly_external_usd"] = {
                "projected": projected,
                "cumulative": cumulative,
                "cap": c.monthly_external_usd,
            }

        # per_action_max_usd — single-action cap, compared against projected
        # only (not cumulative), per spec/29 §"Validation steps" step 8.
        if (
            hasattr(c, "per_action_max_usd")
            and c.per_action_max_usd is not None
            and projected > c.per_action_max_usd
        ):
            caps_exceeded["per_action_max_usd"] = {
                "projected": projected,
                "cumulative": cumulative,
                "cap": c.per_action_max_usd,
            }

        return {
            "projected_usd": projected,
            "caps_exceeded": caps_exceeded,
            "reservation_ids": reservation_ids,
            "unprojectable": False,
        }

    def _step9_escalation_thresholds(
        self,
        *,
        proposal: ActionProposal,
        mandate: Any,
        mandate_id: str,
        projected_token_usd: float,
        projected_external_usd: float,
    ) -> Judgment | None:
        """Evaluate step 9 escalation thresholds.

        Returns an ESCALATE Judgment when any threshold is exceeded; else
        None.  Step 9 ESCALATE preempts step 8 BLOCK per spec/29 line 384
        (Risk 7 amendment) — the caller checks this BEFORE returning the
        cap-exceeded verdict.
        """
        c = mandate.constraints

        # Token escalation threshold (spec/29 line 382).
        if (
            c.requires_escalation_above_token_usd is not None
            and projected_token_usd > c.requires_escalation_above_token_usd
        ):
            return self._escalate(
                reason="mandate_escalation_threshold_hit_token",
                mandate_id=mandate_id,
                detail=(
                    f"Projected token cost ${projected_token_usd:.6f} exceeds "
                    f"escalation threshold ${c.requires_escalation_above_token_usd:.6f} "
                    f"for mandate {mandate_id!r}. Operator approval required."
                ),
            )

        # External escalation threshold (spec/29 line 382).
        if (
            c.requires_escalation_above_external_usd is not None
            and projected_external_usd > c.requires_escalation_above_external_usd
        ):
            return self._escalate(
                reason="mandate_escalation_threshold_hit_external",
                mandate_id=mandate_id,
                detail=(
                    f"Projected external cost ${projected_external_usd:.6f} exceeds "
                    f"escalation threshold ${c.requires_escalation_above_external_usd:.6f} "
                    f"for mandate {mandate_id!r}. Operator approval required."
                ),
            )

        return None

    # Priority order for cap_kind selection in mandate_cap_exceeded_block event.
    # Forever-stable per spec/29 line 629 (Risk 1 amendment):
    # monthly_external > daily_external > cumulative_external >
    # monthly_token > daily_token > cumulative_token > per_action_max
    _CAP_KIND_PRIORITY: tuple[str, ...] = (
        "monthly_external_usd",
        "daily_external_usd",
        "cumulative_external_usd",
        "monthly_token_usd",
        "daily_token_usd",
        "cumulative_token_usd",
        "per_action_max_usd",
    )

    def _build_cap_exceeded_judgment(
        self,
        *,
        proposal: ActionProposal,
        mandate: Any,
        caps_exceeded: dict[str, dict[str, Any]],
        contributing_reservation_ids: list[str],
        action_class: ActionClass,
        cumulative_token_now: float,
        cumulative_external_now: float,
        projected_usd: float,
    ) -> Judgment:
        """Build a ``mandate_cap_exceeded_block`` judgment with priority-selected cap_kind.

        Priority order (spec/29 line 629, Risk 1 amendment — forever-stable):
        ``monthly_external > daily_external > cumulative_external >
        monthly_token > daily_token > cumulative_token > per_action_max``.

        Action class → outcome (spec/29 line 386-390):
        - ``high_risk`` → ESCALATE with reason ``mandate_cap_would_exceed_high_risk``
        - ``external_side_effect``, ``reversible_write`` → BLOCK with reason ``mandate_cap_would_exceed``
        """
        # Select primary cap_kind by priority; collect the rest as additional.
        primary_cap_kind: str | None = None
        for candidate in self._CAP_KIND_PRIORITY:
            if candidate in caps_exceeded:
                primary_cap_kind = candidate
                break

        if primary_cap_kind is None:
            # Defensive: caps_exceeded was non-empty so at least one key exists.
            primary_cap_kind = next(iter(caps_exceeded))

        additional_caps_exceeded: tuple[str, ...] = tuple(
            k
            for k in self._CAP_KIND_PRIORITY
            if k in caps_exceeded and k != primary_cap_kind
        )

        mandate_id = mandate.mandate_id

        self._emit_event(
            "mandate_cap_exceeded_block",
            proposal=proposal,
            mandate_id=mandate_id,
            extra={
                "cumulative_token_now": cumulative_token_now,
                "cumulative_external_now": cumulative_external_now,
                "projected_usd": projected_usd,
                "cap_kind": primary_cap_kind,
                "additional_caps_exceeded": additional_caps_exceeded,
                "contributing_reservation_ids": contributing_reservation_ids,
                "reconcile_cli_hint": None,  # wired by sub-agent C when recovery orphans present
            },
        )

        # Budget-breach action per action class (spec/29 line 386-390).
        is_high_risk = (
            action_class == ActionClass.HIGH_RISK
            # Also accept string form (StrEnum comparison).
            or str(action_class) == "high_risk"
        )
        # Round 1 Finding 11: per_action_max_usd is compared against the
        # projection only (not cumulative); the detail string must reflect that.
        is_per_action_max = primary_cap_kind == "per_action_max_usd"
        if is_high_risk:
            if is_per_action_max:
                detail = (
                    f"High-risk action: projected cost would exceed "
                    f"{primary_cap_kind} per-action cap for mandate "
                    f"{mandate_id!r}. Operator approval required."
                )
            else:
                detail = (
                    f"High-risk action: cumulative + projected would exceed "
                    f"{primary_cap_kind} cap for mandate {mandate_id!r}. "
                    "Operator approval required."
                )
            return self._escalate(
                reason="mandate_cap_would_exceed_high_risk",
                mandate_id=mandate_id,
                detail=detail,
            )

        if is_per_action_max:
            detail = (
                f"Projected cost would exceed {primary_cap_kind} per-action cap "
                f"for mandate {mandate_id!r}. Cap: "
                f"{caps_exceeded[primary_cap_kind].get('cap')}, "
                f"projected: {caps_exceeded[primary_cap_kind].get('projected'):.6f}."
            )
        else:
            detail = (
                f"Cumulative + projected cost would exceed {primary_cap_kind} "
                f"cap for mandate {mandate_id!r}. Cap: "
                f"{caps_exceeded[primary_cap_kind].get('cap')}, "
                f"cumulative: {caps_exceeded[primary_cap_kind].get('cumulative'):.6f}."
            )
        return self._block(
            reason="mandate_cap_would_exceed",
            mandate_id=mandate_id,
            detail=detail,
        )

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
