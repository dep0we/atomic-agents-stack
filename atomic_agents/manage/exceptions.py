"""Typed exception ladder for the manage layer (spec/55 — Tier B implementation note).

Each guard has a distinct exception class so the negative-control tests can assert
a strip-testable failure per guard. The ladder is:

  ManageError (base)
    ManageValidationError     — M4: field unknown / invalid enum / format error
      ManageUnknownFieldError    — unknown field name
      ManageInvalidEnumError     — invalid enum value for a known field
      ManageInvalidDateError     — malformed date for a date field (created_at / updated_at)
      ManageControlCharRefused   — value carries a newline / control char (would corrupt on write)
      ManageNestedPathRefused    — dotted path / bare nested sub-record name (reserved, not in PR1)
      ManageListMutationRefused  — --add / --remove / --set-json flag (reserved, not in PR1)
    ManageGovernanceInvalidError — PRESENT_INVALID governance.md (parse_errors non-empty)
    ManageAgentBusyError         — spec/55 M11: another manage write holds the per-agent
                                    manage lease (non-blocking acquire contended)
    ManageLockUnavailableError   — spec/55 M11: the LockBackend could not be constructed
                                    (misconfigured env, missing extra) — fail-closed refusal,
                                    DISTINCT from ManageAgentBusyError so a copilot driver can
                                    tell "someone else is editing this agent, retry" apart
                                    from "the lock infrastructure is down, don't retry-loop"
    ManageSnapshotNotFoundError   — #710: the requested --restore <snapshot-id> does not exist
                                    for this agent (also the cross-agent-restore refusal —
                                    see the class docstring)
    ManageRecNoLongerValidError   — #727: no current recommendation matches <rec-id>
                                    (retryable — re-derive/reload the console)
    ManageRecKindNotApplicableError — #727: matched a non-savings_cost recommendation
    ManageRecGuardFailedError     — #727: the swap exists but its no-quality-cost
                                    guard no longer passes (NOT retryable blindly —
                                    look at the evals first)
    ManageRecSourceNotApplicableError — #727: the matched savings rec's source is
                                    outside apply-rec's PR1 allowlist

Most validation subclasses are raised during S2 step 1 and propagate to the verb
as non-zero, no-write refusals. ManageListMutationRefused is the exception: it is
constructed (not raised) as a pre-S1 scope refusal in ``run_govern`` — the reserved
list-mutation flags are rejected before the registry resolve. Audit-drop is NOT
modelled as an exception: the
audit path uses ``append_management_audit()``'s ``(ok, warnings)`` tuple return
(a dropped audit write warns and still exits 0 on an applied write, per M8), so
there is no audit-drop exception to catch.

``error_type`` on each subclass is the canonical JSON key used by ``--json``
structured refusal output (spec/55 S3).
"""

from __future__ import annotations


class ManageError(Exception):
    """Base class for all manage-layer errors (spec/55)."""

    error_type: str = "manage_error"


class ManageValidationError(ManageError):
    """M4: input validation failure — refused before any write."""

    error_type: str = "validation_error"


class ManageUnknownFieldError(ManageValidationError):
    """Unknown --set field name; not in the PR1 flat-scalar allowlist."""

    error_type: str = "unknown_field"

    def __init__(self, field: str) -> None:
        self.field = field
        super().__init__(f"Unknown governance field: {field!r}")


class ManageInvalidEnumError(ManageValidationError):
    """Invalid enum value for a known field (spec/55 M4)."""

    error_type: str = "invalid_enum"

    def __init__(self, field: str, value: str, allowed: set) -> None:
        self.field = field
        self.value = value
        self.allowed = allowed
        super().__init__(
            f"Invalid value {value!r} for field {field!r}. Allowed: {sorted(allowed)}"
        )


class ManageInvalidDateError(ManageValidationError):
    """Malformed date value for a date field (created_at / updated_at) — M4.

    Date fields MUST be ISO-8601 ``YYYY-MM-DD``. A malformed date is refused
    before write (spec/55 M4 + the conformance outline's 'malformed date' control).
    """

    error_type: str = "invalid_date"

    def __init__(self, field: str, value: str) -> None:
        self.field = field
        self.value = value
        super().__init__(
            f"Invalid date {value!r} for field {field!r}. "
            "Expected an ISO-8601 date (YYYY-MM-DD), e.g. 2026-06-24."
        )


class ManageControlCharRefused(ManageValidationError):
    """Value cannot be emitted to YAML and read back byte-identical — M4.

    Governance scalars are single-line identifiers. A value containing a folding
    or PyYAML-rejected character (newline, carriage return, U+0085 NEL, NUL,
    vertical tab, ...) cannot be emitted losslessly: a single-quoted YAML scalar
    folds an embedded line break into a space on re-read (and PyYAML refuses NUL
    outright), so the persisted value would diverge from BOTH what the operator
    set AND what the ``--json`` ``after`` payload reports. The guard decides
    against PyYAML itself (emit-then-reload round-trip), not a hand-rolled
    control-char blocklist. Rather than silently mangle it, the value is refused
    before any write (the module's refuse-for-a-documented-reason posture).
    """

    error_type: str = "control_char"

    def __init__(self, field: str) -> None:
        self.field = field
        super().__init__(
            f"Value for field {field!r} contains a newline or control character. "
            "Governance fields are single-line; edit governance.md directly for "
            "multi-line content."
        )


class ManageNestedPathRefused(ManageValidationError):
    """Dotted path or a bare nested sub-record name (reserved, not in PR1).

    Raised for a dotted path (``review.reviewer=...``) or a bare nested
    sub-record name (``review`` / ``risk`` / ``sources`` / ``actions``) used
    without a dot. These are named but unimplemented in PR1; list-mutation
    flags are a separate refusal (``ManageListMutationRefused``). The error
    message directs the operator to edit governance.md directly.
    """

    error_type: str = "nested_path_refused"

    def __init__(self, field: str) -> None:
        self.field = field
        super().__init__(
            f"Nested / list field {field!r} is not yet settable via CLI; "
            "edit governance.md directly."
        )


class ManageListMutationRefused(ManageValidationError):
    """List-mutation flag (``--add`` / ``--remove`` / ``--set-json``) — reserved, not in PR1.

    The grammar for these flags is pinned NOW (spec/55 CLI-surface grammar) so the
    recognized-vs-unrecognized status of the flag never shifts in a later PR — an
    unimplemented path returns THIS clean structured refusal rather than argparse's
    ``unrecognized arguments`` parser error (which would also bypass the ``--json``
    contract). PR2 changes only the semantics, not whether the flag parses.
    """

    error_type: str = "list_mutation_unsupported"

    def __init__(self, flag: str, value: str) -> None:
        self.flag = flag
        self.value = value
        super().__init__(
            f"List mutation via {flag} ({value!r}) is not yet settable via CLI; "
            "edit governance.md directly."
        )


class ManageAgentBusyError(ManageError):
    """Spec/55 M11: the per-agent manage lease is held by another process.

    Raised when the non-blocking manage-lease ``acquire(timeout=0)`` contends
    (``LockBusy``) — another ``manage`` write verb (govern --set, govern
    --restore, or a future set-model/apply-rec) is currently inside its
    read-base->snapshot->atomic-write critical section for the SAME agent.
    Caught CENTRALLY in the spine dispatcher (not per-verb) and emitted as
    ``{ok:false, error_type:'agent_busy'}`` exit 1.

    Per M8's pinned status vocabulary, a refusal — including agent_busy —
    does NOT emit a management RunRecord (a RunRecord existing implies the
    write was applied). Contention is visible only via this structured
    refusal + exit 1, never via an audit line.

    The spine does not retry on contention (acquire is non-blocking,
    timeout=0, by design). Automated / fleet-scale callers are responsible
    for their own retry-with-backoff.
    """

    error_type: str = "agent_busy"

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        super().__init__(
            f"Another management write is already in progress for agent "
            f"{agent_id!r} (per-agent manage lease held). Retry shortly, or "
            "wait for the other write to complete."
        )


class ManageLockUnavailableError(ManageError):
    """Spec/55 M11: the LockBackend could not be constructed — fail-closed refusal.

    Raised when ``get_default_lock_backend()`` (or its ``.capabilities()``
    call) raises — a misconfigured ``ATOMIC_AGENTS_LOCK_BACKEND_URL``, an
    unregistered backend id, or a missing optional extra (e.g. ``redis`` not
    installed). Per the maintainer's fail-closed ruling, a lock backend that
    cannot be constructed MUST refuse the write rather than silently proceed
    unlocked. Deliberately a DISTINCT exception (and JSON ``error_type``)
    from ``ManageAgentBusyError`` so a caller can tell "someone else is
    editing this agent, retry" apart from "the lock infrastructure itself is
    misconfigured, an operator must fix it, don't retry-loop against it."
    """

    error_type: str = "lock_backend_unavailable"

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"Management lock backend unavailable — refused: {detail}")


class ManageSnapshotNotFoundError(ManageError):
    """#710: the requested --restore <snapshot-id> does not exist for this agent.

    ALSO the refusal for cross-agent restore (no separate exception type):
    ``resolve_snapshot_path`` resolves a snapshot_id ONLY under the TARGET
    agent's own ``.config-snapshots/<subdir>/`` tree, so a snapshot_id that
    belongs to a DIFFERENT agent simply does not resolve here and hits this
    same not-found refusal. This is deliberate indistinguishability, not a
    gap: a caller must never learn whether a given snapshot id exists under
    some other agent (no cross-agent restore, #710 m3-conformance sub-MUST).
    """

    error_type: str = "snapshot_not_found"

    def __init__(self, snapshot_id: str, agent_id: str) -> None:
        self.snapshot_id = snapshot_id
        self.agent_id = agent_id
        super().__init__(
            f"Snapshot {snapshot_id!r} not found for agent {agent_id!r}. "
            "Use --list-snapshots to see available snapshots."
        )


class ManageUnpricedModelError(ManageError):
    """spec/55 #726 M9 gate (a): ``model_id`` is not in ``atomic_agents._costs.PRICING``.

    Routed through ``atomic_agents.core_api.get_model_rates()`` (TENSIONS T17
    core<->extension boundary — ``manage/`` is an extension package and MUST
    NOT reach into the core-private ``_costs.PRICING`` table directly). Hard
    refuse always (maintainer ruling ``unpriced-model-posture`` — no
    ``--force`` escape hatch): writing an unpriced model id to model.md would
    silently over-bill the agent through fallback pricing at runtime, with no
    operator-visible warning until the invoice.
    """

    error_type: str = "unpriced_model"

    def __init__(self, model_id: str) -> None:
        self.model_id = model_id
        super().__init__(
            f"Model {model_id!r} is unpriced (not in atomic_agents PRICING) — "
            "refusing to write it to model.md. An unpriced model would bill "
            "through fallback pricing at runtime with no advance warning."
        )


class ManageUnknownModelError(ManageError):
    """spec/55 #726 M9 gate (b): zero registered LLM backends claim ``model_id``.

    Raised when ``atomic_agents.llm.find_backend_for_model()`` raises
    ``UnknownModelError``. ``no_backends_registered`` distinguishes a
    deployment with ZERO registered LLM backends (a distinct, more useful
    hint than "typo'd model id") from a deployment with backends registered
    but none of them claiming this specific model id (spec/55 P1 prep
    finding).
    """

    error_type: str = "unknown_model"

    def __init__(self, model_id: str, *, no_backends_registered: bool = False) -> None:
        self.model_id = model_id
        self.no_backends_registered = no_backends_registered
        if no_backends_registered:
            detail = "no LLM backend is registered in this deployment"
        else:
            detail = f"no registered LLM backend claims model {model_id!r}"
        super().__init__(f"Unknown model {model_id!r} — {detail}.")


class ManageAmbiguousModelBackendError(ManageError):
    """spec/55 #726 M9 gate (b): more than one registered backend claims ``model_id``.

    Raised when ``find_backend_for_model()`` raises ``AmbiguousBackendError``.
    Maintainer ruling ``provider-disambiguation-posture``: the message is
    DELIBERATELY NOT ``str(exc)`` — the upstream ``AmbiguousBackendError``
    text tells the operator to "pass --provider", which PR1 forbids
    (``--provider`` is grammar-recognized but deferred to #755). This message
    points at the deferred capability instead, and never mentions
    ``--provider``.
    """

    error_type: str = "ambiguous_backend"

    def __init__(self, model_id: str, candidates: list[str]) -> None:
        self.model_id = model_id
        self.candidates = candidates
        super().__init__(
            f"Model {model_id!r} is claimed by more than one registered "
            f"backend ({', '.join(candidates)}) — refusing to guess. "
            "Backend disambiguation is not yet settable via CLI in PR1 "
            "(tracked in #755)."
        )


class ManagePolicyBackendUnavailableError(ManageError):
    """spec/55 #726: ``PolicyBackend`` could not be constructed or read.

    Tier B decision (fail-closed, spec/55 P1/P2 prep findings): a broken or
    misconfigured PolicyBackend refuses the write rather than silently
    skipping the CAPS-COMPOSE consult and the policy-override WARN-AND-WRITE
    check — mirrors ``ManageLockUnavailableError``'s fail-closed posture for
    the PRE-write consult only; the POST-write recompute degrades instead of
    refusing an already-applied write (see ``set_model.py``'s module
    docstring for the full two-posture rationale).
    """

    error_type: str = "policy_backend_unavailable"

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"PolicyBackend unavailable — refused: {detail}")


class ManageUnwritableModelIdError(ManageError):
    """spec/55 #726 Fix 2 (defense-in-depth): ``new_value`` is outside the
    surgical writer's value charset (``[a-zA-Z0-9._/-]+``).

    Checked BEFORE any slicing into model.md's content, independent of the
    M9 PRICING-membership check — PRICING is a plain dict with no charset
    constraint of its own, so a future PRICING key containing a character
    outside this class (e.g. "+", a space) would otherwise slice into the
    file and silently truncate on the next ``parse_model_md`` read.
    """

    error_type: str = "unwritable_model_id"

    def __init__(self, model_id: str) -> None:
        self.model_id = model_id
        super().__init__(
            f"Model id {model_id!r} contains a character outside the "
            "writer's value charset ([a-zA-Z0-9._/-]+) and would not "
            "round-trip through model.md — refusing to write it."
        )


class ManageModelMdAbsentError(ManageError):
    """spec/55 #726: model.md is absent for this agent.

    UNREACHABLE via the real CLI/registry surface: model.md-presence IS the
    ``AgentRegistryBackend`` discovery predicate (spec/37:314), so an agent
    with no model.md never resolves a ``ref`` in the first place — S1
    refuses with ``agent_not_found`` before ``_run_set_model`` is ever
    called. Kept as a direct-API safety net for a caller that bypasses the
    registry resolve entirely, and for the TOCTOU race where model.md is
    deleted between the registry resolve and this module's own re-check
    (see ``set_model.py``'s ``_read_base`` for the in-lock re-check).
    """

    error_type: str = "model_md_absent"

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        super().__init__(f"model.md is absent for agent {agent_id!r}.")


class ManageDefaultModelHeadingAbsentError(ManageError):
    """spec/55 #726: model.md has no '## Default model' heading.

    set-model is a surgical value-span editor, not a scaffolder — a
    model.md missing the heading entirely cannot be edited in place.
    """

    error_type: str = "default_model_heading_absent"

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        super().__init__(
            f"model.md for agent {agent_id!r} has no '## Default model' "
            "heading — refusing to write. Edit model.md directly to add "
            "the heading first."
        )


class ManageDuplicateDefaultModelHeadingError(ManageError):
    """spec/55 #726: model.md has more than one '## Default model' heading.

    The surgical writer targets the FIRST match only (``re.search``, not
    ``finditer``) — a duplicate heading is refused rather than silently
    editing whichever occurrence the regex happens to find, or leaving the
    second occurrence stale.
    """

    error_type: str = "duplicate_default_model_heading"

    def __init__(self, agent_id: str, heading_count: int) -> None:
        self.agent_id = agent_id
        self.heading_count = heading_count
        super().__init__(
            f"model.md for agent {agent_id!r} has {heading_count} "
            "'## Default model' headings (expected exactly 1) — refusing "
            "to guess which one to edit. Edit model.md directly to remove "
            "the duplicate."
        )


class ManageDefaultModelValueUnparseableError(ManageError):
    """spec/55 #726: the '## Default model' heading is present, but no value
    token immediately follows it.

    Raised when the heading exists (so
    ``ManageDefaultModelHeadingAbsentError`` does not fire) but the writer's
    value-span regex cannot locate a value token to replace — e.g. the
    heading is followed immediately by another heading, or by content
    outside the writer's value charset.
    """

    error_type: str = "default_model_value_unparseable"

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        super().__init__(
            f"model.md for agent {agent_id!r} has a '## Default model' "
            "heading but no parseable value immediately follows it — "
            "refusing to write. Edit model.md directly to fix the value."
        )


class ManageDeferredFlagRefused(ManageError):
    """spec/55 #726 PR1 scope (maintainer ruling ``pr1-flag-scope``):
    ``--fallback`` / ``--provider`` are grammar-recognized by the CLI parser
    but not yet settable in PR1.

    Fires BEFORE the registry resolve (mirrors govern's grammar-pin-but-defer
    precedent for --add/--remove/--set-json) — a scope refusal independent
    of whether the target agent exists.
    """

    error_type: str = "not_yet_settable_in_pr1"

    def __init__(self, flag: str, issue: str) -> None:
        self.flag = flag
        self.issue = issue
        super().__init__(
            f"{flag} is not yet settable via CLI in PR1 (tracked in {issue}); "
            "edit model.md directly."
        )


class ManageRecNoLongerValidError(ManageError):
    """spec/55 #727 apply-rec: no CURRENT recommendation matches ``<rec-id>``.

    ``apply-rec``'s match universe (``build_rec_match_universe`` —
    ``atomic_agents/advisor/recommend.py``) is recomputed fresh on every
    invocation; recommendations are never persisted (spec/54). A rec-id that
    hashed to a real recommendation at console-render time can stop matching
    for entirely benign reasons — the agent's 30d usage repriced, the
    candidate model changed, the underlying JSONL data moved — none of which
    are errors. This is a first-class, EXPECTED refusal (the console card is
    stale), not a bug: re-derive the recommendation (reload the console) and
    retry with the new rec-id.
    """

    error_type: str = "rec_no_longer_valid"

    def __init__(self, rec_id: str) -> None:
        self.rec_id = rec_id
        super().__init__(
            f"No current recommendation matches rec-id {rec_id!r} — the "
            "console card is stale (the agent's usage repriced, the "
            "candidate changed, or the underlying data moved). Reload the "
            "console and retry with the new rec-id."
        )


class ManageRecKindNotApplicableError(ManageError):
    """spec/55 #727 apply-rec: the matched recommendation is not ``savings_cost``.

    PR1 applies ``savings_cost`` recommendations only — the one kind with a
    mechanical, unambiguous apply action (a model swap). ``quality_report``
    and ``governance`` recommendations are, and remain, advisory-only; there
    is no mechanical "apply" for "go read this tuning report" or "go write
    governance.md by hand" (the latter has its own editor, ``manage govern``,
    which ``apply-rec`` does not chain into).
    """

    error_type: str = "rec_kind_not_applicable"

    def __init__(self, rec_id: str, kind: str) -> None:
        self.rec_id = rec_id
        self.kind = kind
        super().__init__(
            f"Recommendation {rec_id!r} matched a {kind!r} recommendation, "
            "but apply-rec only applies 'savings_cost' recommendations "
            "(quality_report/governance are advisory-only; there is no "
            "mechanical apply for either)."
        )


class ManageRecGuardFailedError(ManageError):
    """spec/55 #727 apply-rec: the swap still exists but its no-quality-cost
    guard no longer passes.

    The matched candidate's ``.safety.passed`` (an ``EvalHeadroom`` recomputed
    fresh in the SAME ``build_rec_match_universe`` call that found the match —
    apply-rec never re-imports advisor privates like ``_eval_headroom``
    itself, Principle #3/T17 layering) is ``False``: a hard-fail landed, or a
    margin eroded, since the recommendation last passed. This is distinct
    from ``rec_no_longer_valid`` (no match at all) — the swap is a real,
    currently-computable candidate, it just is not currently safe to apply.
    STOP and look at the agent's evals; do NOT blindly retry — retrying
    without investigating just re-hits this same refusal until the
    underlying quality signal actually recovers.
    """

    error_type: str = "rec_guard_failed"

    def __init__(self, rec_id: str) -> None:
        self.rec_id = rec_id
        super().__init__(
            f"Recommendation {rec_id!r} still names a real candidate swap, "
            "but its no-quality-cost guard no longer passes (a hard-fail "
            "landed, or a margin eroded, since it last passed) — refusing "
            "to apply. Look at the agent's evals before retrying; this is "
            "not a transient failure to retry blindly."
        )


class ManageRecSourceNotApplicableError(ManageError):
    """spec/55 #727 apply-rec: the matched savings rec's ``source`` is not in
    apply-rec's allowlisted sources (skeptic's guard).

    Only ``default_same_family``-sourced candidates are applicable in PR1
    (``_APPLICABLE_REC_SOURCES`` in ``apply_rec.py``). ``operator_configured``
    is excluded by design — apply-rec cannot yet re-validate the ground-truth
    basis an operator-configured candidate was selected on, so it refuses
    rather than silently applying a swap PR1 has no basis to vouch for. A
    future ``measured_scorecard`` source (does not exist in code yet, see
    #649/#644-child-D) is the primary target this refusal is designed to
    widen for later, once a re-validation path for it is designed.
    """

    error_type: str = "rec_source_not_applicable"

    def __init__(self, rec_id: str, source: str | None) -> None:
        self.rec_id = rec_id
        self.source = source
        super().__init__(
            f"Recommendation {rec_id!r} was selected via source {source!r}, "
            "which PR1 apply-rec cannot re-validate the ground-truth basis "
            "of — refusing rather than silently applying it (spec/55 "
            "measured-scorecard-source ruling)."
        )


class ManageGovernanceInvalidError(ManageError):
    """governance.md has parse_errors — write refused (spec/55 M4 / PRESENT_INVALID guard).

    A PRESENT_INVALID governance.md has schema errors that prevent the surgical
    editor from operating safely. The operator must fix the YAML block first.
    """

    error_type: str = "governance_invalid"

    def __init__(self, agent_id: str, parse_errors: tuple[str, ...]) -> None:
        self.agent_id = agent_id
        self.parse_errors = parse_errors
        errors_str = "; ".join(parse_errors)
        super().__init__(
            f"governance.md for {agent_id!r} has validation errors: {errors_str}. "
            "Fix the YAML block before using --set (run --show for details)."
        )
