"""Parse ``judges.md`` operator config for the judge layer (spec/28).

``judges.md`` is the operator's contract with the judge layer:

- Per-class policy (``bypass | allow_with_audit | judge_required | escalate``)
- Per-exception-type ``failure_policy`` (default: block for everything)
- Default judge backend + model + timeout + budget
- Escalation destination + auto-decide cadence + fallback
- ``judge_captures`` and ``read_audit_mode`` toggles

PR 3a of #112. Parser only — reads the file and returns a typed
``JudgesConfig``. Consumers (``agent.call()``'s judge dispatch, the
two reference judges, future doctor checks) read the parsed config
directly. ESCALATE state machine, polling loop, doctor checks, and
specialist-composition enforcement land in PR 3b.

Embedded-YAML-in-markdown shape, mirroring ``model.md``'s
``cost_guardrails`` precedent (CLAUDE.md taste rule #7).

Cascade-aware project floor (spec/28:408):

- Single-agent layouts: only ``<agent_root>/judges.md``.
- Cascade layouts: ``<project>/judges.md`` is the **floor**; the
  delegate's own ``<instance>/judges.md`` may *strengthen* class
  policy (escalate is strictest; bypass is least strict) but
  cannot *relax* it. Relax violations raise
  ``JudgePolicyInvalid`` at load.

When ``judges.md`` is absent, the loader returns ``None`` — the
opt-in gate in ``agent.call()`` from PR 2a keeps the framework
backward-compatible.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from .exceptions import JudgePolicyInvalid
from .judge.types import (
    ActionClass,
    BudgetConfig,
    ClassPolicySnapshot,
    ClassPolicyValue,
    EscalationConfig,
)


# Spec/28's per-class default-fill (line ~800). Operators who omit a
# class get these defaults — bypass for read_only, escalate for
# high_risk.
_DEFAULT_CLASS_POLICY: dict[ActionClass, ClassPolicyValue] = {
    ActionClass.READ_ONLY: ClassPolicyValue.BYPASS,
    ActionClass.REVERSIBLE_WRITE: ClassPolicyValue.JUDGE_REQUIRED,
    ActionClass.EXTERNAL_SIDE_EFFECT: ClassPolicyValue.JUDGE_REQUIRED,
    ActionClass.HIGH_RISK: ClassPolicyValue.ESCALATE,
}

# Strictness ordering for project-floor relax detection. Higher number
# = stricter. A delegate's own value must be >= the project floor's
# value for that class.
_POLICY_STRICTNESS: dict[ClassPolicyValue, int] = {
    ClassPolicyValue.BYPASS: 0,
    ClassPolicyValue.ALLOW_WITH_AUDIT: 1,
    ClassPolicyValue.JUDGE_REQUIRED: 2,
    ClassPolicyValue.ESCALATE: 3,
}

# Accepted ``validation:`` values (PR 5b of #112). Strictness ordering:
# higher number = stricter. A delegate that explicitly sets
# ``validation`` must be at least as strict as the project floor's.
# ``audit`` and ``paranoid`` are reserved namespaces — the parser
# rejects them with "not yet implemented" pointing at their tracking
# issue, so operator typos surface differently from operators reaching
# for a future feature.
#
# Indices start at 1 (not 0) to leave headroom for a future weaker
# tier — e.g. a hypothetical ``validation: off`` (no weakened-mode
# checks at all) would land at 0 without renumbering existing entries
# or rebasing the integer-pinning regression tests in
# ``tests/test_judges_md_parser.py::TestValidationFloor``. Mirrors the
# 0-based ``_POLICY_STRICTNESS`` precedent above where ``bypass`` is
# the floor at 0; we just don't have a "bypass" equivalent for
# ``validation`` shipped today.
_VALID_VALIDATION_VALUES: tuple[str, ...] = ("weakened", "strict")
_VALIDATION_STRICTNESS: dict[str, int] = {"weakened": 1, "strict": 2}
_RESERVED_VALIDATION_VALUES: dict[str, str] = {
    "audit": (
        "validation: audit is not yet implemented; tracked at "
        "https://github.com/dep0we/atomic-agents-stack/issues/176"
    ),
    "paranoid": (
        "validation: paranoid is not yet implemented; tracked at "
        "https://github.com/dep0we/atomic-agents-stack/issues/179"
    ),
}


# Default failure_policy per spec/28:570 — fail-closed for everything.
_DEFAULT_FAILURE_POLICY_PER_EXCEPTION: dict[str, str] = {
    "JudgeUnavailable": "block",
    "JudgePolicyInvalid": "block",
    "JudgeBudgetExhausted": "block",
    "JudgeProposalInvalid": "block",
    "JudgeAmendedProposalRejected": "block",
}

# Recognized exception names from atomic_agents.exceptions. Operators
# referencing names outside this set raise JudgePolicyInvalid — fail
# loud per CLAUDE.md taste rule (operator typos shouldn't fail-closed
# at runtime).
_RECOGNIZED_EXCEPTION_NAMES: frozenset[str] = frozenset(
    _DEFAULT_FAILURE_POLICY_PER_EXCEPTION.keys()
)


@dataclass(frozen=True)
class MandateSettings:
    """Mandate-specific operator config (spec/29 + #124 PR 3a).

    Parsed from ``judges.md`` ``## Mandates`` section. All fields
    default-fill from the spec/29 documented defaults when the section
    is absent — operators upgrading from pre-#124 deployments see zero
    behavior change unless they explicitly add a ``## Mandates`` section.

    Spec/29 amendments (via #213) added several fields ahead of impl;
    the ones PR 3a consumes are pre-declared here so the implementer
    doesn't have to extend the dataclass during PR 3a.
    """

    # Spec/29 §"Suspicious-rebind throttle" — defends against the
    # source-hash-before-state edit window. Throttle re-binding on
    # (mandate_id, agent_run_id) for N seconds after MandateCheck
    # surfaces mandate_state_inconsistent. Default 60s per spec/29.
    suspicious_rebind_throttle_s: int = 60

    # Spec/29 §"Validation steps" step 5 — target extraction failure mode.
    # When the per-agent target_extractor registry can't produce a
    # target_canonical AND the mandate's constraints.allowed_targets is
    # set, this controls the BLOCK vs ESCALATE choice.
    unextractable_target_action: Literal["block", "escalate"] = "block"

    # Spec/29 §"Cost reservation pattern" (PR 3b) — reservation TTL in
    # seconds. PR 3a stores; PR 3b consumes.
    reservation_ttl_s: int = 60

    # Spec/29 §"Validation steps" budget-breach action defaults — per-
    # class override (PR 3b). PR 3a stores; PR 3b consumes.
    cap_breach_action_class_default: dict[str, str] = field(
        default_factory=lambda: {
            "external_side_effect": "block",
            "high_risk": "escalate",
            "reversible_write": "block",
        }
    )

    # Spec/29 §"High-risk lock specification" (PR 4) — lock acquisition
    # timeout in seconds. PR 3a stores for forward-compat; PR 4 consumes.
    high_risk_lock_timeout_s: int = 30

    # Doctor toggle — when True, emit check_mandate_no_expiry on
    # mandates with expires_at == None. Default per spec/29 §"Doctor
    # integration".
    no_expiry_warning: bool = True


@dataclass(frozen=True)
class JudgesConfig:
    """Parsed operator config from ``judges.md``.

    Returned by ``parse_judges_md`` and ``load_judges_config``. PR 3a
    reads every field documented in spec/28 even when the consumer
    (PR 3a wiring) doesn't use it yet — PR 3b's escalation state
    machine consumes ``escalation``, the LLM judge construction
    consumes ``default_model``, etc.
    """

    # Backend selection
    default_backend: str = "rules"  # "rules" | "llm" | custom name
    default_model: str | None = None
    timeout_ms: int = 5000

    # Budget (separate ledger from actor)
    budget: BudgetConfig = field(default_factory=BudgetConfig)

    # Per-class policy
    class_policy: ClassPolicySnapshot = field(
        default_factory=lambda: ClassPolicySnapshot(
            read_only=_DEFAULT_CLASS_POLICY[ActionClass.READ_ONLY],
            reversible_write=_DEFAULT_CLASS_POLICY[ActionClass.REVERSIBLE_WRITE],
            external_side_effect=_DEFAULT_CLASS_POLICY[ActionClass.EXTERNAL_SIDE_EFFECT],
            high_risk=_DEFAULT_CLASS_POLICY[ActionClass.HIGH_RISK],
            source={cls.value: "default" for cls in ActionClass},
        )
    )

    # Per-class failure_policy (Codex round-1 P2 #2). Nested
    # ``{ActionClass: {exception_name: outcome_string}}`` so operators
    # can override fallback per-class-per-exception. Default-fill from
    # _DEFAULT_FAILURE_POLICY_PER_EXCEPTION when omitted.
    failure_policy: dict[ActionClass, dict[str, str]] = field(
        default_factory=lambda: {
            cls: dict(_DEFAULT_FAILURE_POLICY_PER_EXCEPTION)
            for cls in ActionClass
        }
    )

    # Escalation behavior — parsed but only enforced in PR 3b's state
    # machine. PR 3a stores it so PR 3b can consume without re-parsing.
    escalation: EscalationConfig = field(default_factory=EscalationConfig)

    # Audit toggles
    judge_captures: bool = False
    read_audit_mode: bool = False

    # Amendment validation (PR 5b of #112). ``weakened`` (the default)
    # matches PR 3c behavior — tool registered + dict shape +
    # canonical args_hash. ``strict`` runs jsonschema.validate against
    # the tool's registered ``input_schema``. Operators must install
    # the ``[validation]`` extra BEFORE setting ``validation: strict``;
    # the parser fails LOUD at agent-load otherwise.
    #
    # ``validation_source`` mirrors ``class_policy.source`` and takes
    # one of four values across the parse → cascade-merge pipeline:
    #
    # - ``"default"``   — operator omitted the ``validation:`` field;
    #                     parser default-filled to ``weakened``.
    #                     Pre-merge state for delegate configs the
    #                     cascade-floor check treats as "inherit the
    #                     floor" rather than "explicit relax-attempt".
    # - ``"judges.md"`` — operator explicitly set the value in
    #                     the agent's own ``judges.md``. Pre-merge.
    #                     The cascade-floor strictness check fires
    #                     against this state.
    # - ``"delegate"``  — post-``apply_project_floor`` resolved value
    #                     came from the delegate's explicit setting.
    # - ``"floor"``     — post-``apply_project_floor`` resolved value
    #                     came from the project floor (delegate
    #                     omitted; inherits the floor's value).
    validation: Literal["weakened", "strict"] = "weakened"
    validation_source: str = "default"

    # Specialist composition axes — parsed-but-unused in PR 3a.
    # Operators may author the section now; PR 3b/4's ensemble dispatch
    # consumes it.
    specialist_axes: list[str] = field(default_factory=list)

    # Mandate settings (spec/29 + #124 PR 3a). Operators configure
    # MandateCheck behavior via judges.md ``## Mandates`` section. Fields
    # default-fill from spec/29 when the section is absent (zero
    # operator-action upgrade from pre-#124 deployments). PR 3a consumes
    # ``suspicious_rebind_throttle_s`` and ``unextractable_target_action``;
    # PR 3b consumes ``reservation_ttl_s`` and the budget-breach action
    # defaults; PR 4 consumes ``high_risk_lock_timeout_s``.
    mandate_settings: "MandateSettings" = field(
        default_factory=lambda: MandateSettings()
    )

    # Snapshot hashes for centralized policy_version computation
    # (compute_policy_version reads these). Empty string means
    # "this part of the snapshot wasn't loaded" — distinguishable
    # from a real sha256.
    tools_md_hash: str = ""
    judges_md_hash: str = ""

    # Provenance — useful for doctor checks + audit.
    source_path: str | None = None

    def failure_policy_for(
        self,
        action_class: ActionClass,
        exception_name: str,
    ) -> str:
        """Look up the operator-configured fallback outcome for the
        given ``(class, exception)`` pair. Returns the spec default
        (``"block"``) if no override is configured. Always returns a
        valid ``JudgmentOutcome`` value string."""
        per_class = self.failure_policy.get(action_class, {})
        return per_class.get(exception_name) or "block"


# ──────────────────────────────────────────────────────────────────
# Parser


def parse_judges_md(path: Path | None) -> JudgesConfig | None:
    """Load + parse a ``judges.md`` file. Returns ``None`` when the
    file doesn't exist (the opt-in gate in ``agent.call()`` then runs
    pre-#112 behavior). Raises ``JudgePolicyInvalid`` on malformed
    content per spec/28 — fail-loud at load time so operator typos
    don't fail-closed at runtime on every action.

    Atomic-snapshot read per spec/28:588: ``read_bytes()`` returns the
    file's content in a single syscall; we decode strictly and hash
    that exact byte sequence. Operators editing the file mid-read
    either see the pre-edit snapshot or the post-edit one — never a
    torn read.
    """
    if path is None or not path.exists():
        return None
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise JudgePolicyInvalid(
            f"could not read judges.md at {path}: {exc}"
        ) from exc
    try:
        text = raw_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise JudgePolicyInvalid(
            f"judges.md at {path} is not valid UTF-8: {exc}"
        ) from exc
    judges_md_hash = hashlib.sha256(raw_bytes).hexdigest()
    cfg = parse_judges_md_text(text)
    return JudgesConfig(
        default_backend=cfg.default_backend,
        default_model=cfg.default_model,
        timeout_ms=cfg.timeout_ms,
        budget=cfg.budget,
        class_policy=cfg.class_policy,
        failure_policy=cfg.failure_policy,
        escalation=cfg.escalation,
        judge_captures=cfg.judge_captures,
        read_audit_mode=cfg.read_audit_mode,
        specialist_axes=cfg.specialist_axes,
        validation=cfg.validation,
        validation_source=cfg.validation_source,
        mandate_settings=cfg.mandate_settings,
        tools_md_hash=cfg.tools_md_hash,
        judges_md_hash=judges_md_hash,
        source_path=str(path),
    )


def parse_judges_md_text(text: str) -> JudgesConfig:
    """Parse in-memory ``judges.md`` text. Useful for cascade-merged
    content. Raises ``JudgePolicyInvalid`` on malformed content.

    Two config shapes co-exist:

    1. **Fenced YAML blocks** (legacy, first-class) — every config block
       lives inside a ```` ```yaml ```` fenced block. Operators may have
       multiple such blocks; we merge them in document order with later
       blocks winning on per-key conflicts (matches the precedent in
       ``_model.py``'s ``cost_guardrails`` parser).

    2. **Named sections** (spec/29 + #124 PR 3a) — ``## <SectionName>``
       headings introduce a section whose body is parsed as inline YAML
       (no fence markers). Recognized sections: ``Mandates``. Unknown
       section headings are silently ignored (operator-authored headings
       for prose / TOC are fine). The ``## Mandates`` section body is
       plain YAML — the operator writes field:value lines directly under
       the heading rather than inside a fenced block.
    """
    # ── Pass 1: extract inline ``## Mandates`` section body ──────────
    # Split by ``## `` headings to find the Mandates section body.
    # The body runs from the heading line's end to the next ``## ``
    # heading or EOF. Fenced YAML blocks INSIDE a section body are NOT
    # double-parsed here — they go through the fenced-block pass below.
    mandates_section_body: str | None = _extract_named_section(text, "Mandates")

    # ── Pass 2: fenced ```yaml blocks (existing path) ────────────────
    parsed_blocks: list[dict[str, Any]] = []
    for block in re.findall(r"```yaml\s*\n(.*?)```", text, re.DOTALL):
        try:
            obj = yaml.safe_load(block)
        except yaml.YAMLError as exc:
            raise JudgePolicyInvalid(
                f"judges.md contains invalid YAML: {exc}"
            ) from exc
        if obj is None:
            continue
        if not isinstance(obj, dict):
            raise JudgePolicyInvalid(
                f"judges.md YAML block must be a mapping; got "
                f"{type(obj).__name__}"
            )
        parsed_blocks.append(obj)

    # Merge top-level keys with later blocks winning.
    merged: dict[str, Any] = {}
    for block in parsed_blocks:
        merged.update(block)

    # Extract the recognized sections.
    default_backend = str(merged.get("backend") or merged.get("default_backend") or "rules")
    default_model_raw = merged.get("model") or merged.get("default_model")
    default_model = str(default_model_raw) if default_model_raw is not None else None
    timeout_ms = _coerce_int(merged.get("timeout_ms"), default=5000, field_label="timeout_ms")

    budget = _parse_budget(merged.get("budget"))
    class_policy = _parse_class_policy(merged.get("class_policy"))
    failure_policy = _parse_failure_policy(merged.get("failure_policy"))
    escalation = _parse_escalation(merged.get("escalation"))
    judge_captures = bool(merged.get("judge_captures", False))
    read_audit_mode = bool(merged.get("read_audit_mode", False))
    specialist_axes = _parse_specialist_axes(merged.get("specialist_composition"))
    validation, validation_source = _parse_validation(merged.get("validation"))

    # ── Pass 3: parse named sections ─────────────────────────────────
    mandate_settings = _parse_mandates_section(mandates_section_body)

    return JudgesConfig(
        default_backend=default_backend,
        default_model=default_model,
        timeout_ms=timeout_ms,
        budget=budget,
        class_policy=class_policy,
        failure_policy=failure_policy,
        escalation=escalation,
        judge_captures=judge_captures,
        read_audit_mode=read_audit_mode,
        specialist_axes=specialist_axes,
        validation=validation,
        validation_source=validation_source,
        mandate_settings=mandate_settings,
    )


def apply_project_floor(
    own: JudgesConfig,
    floor: JudgesConfig | None,
) -> JudgesConfig:
    """Apply a project-floor ``JudgesConfig`` to a delegate's own
    config per spec/28:408. The floor is non-relaxable — delegate's
    per-class policy must be at least as strict as the floor's. Raises
    ``JudgePolicyInvalid`` at load time when relaxation is attempted.

    Other fields (model, budget, escalation, failure_policy) merge
    via "delegate overrides floor on present keys"; the floor's
    values fill in when the delegate didn't specify.

    Returns the merged ``JudgesConfig`` to use at the delegate.
    """
    if floor is None:
        return own

    # Per-class strictness check — only enforce for classes the
    # delegate EXPLICITLY overrode. Classes the delegate didn't
    # specify get default-filled to spec/28 defaults during parsing
    # (source="default"); those should inherit the floor rather than
    # raise a false-positive relax violation. Codex round-2 P2 fix.
    own_source = own.class_policy.source or {}
    resolved_class_policy: dict[ActionClass, ClassPolicyValue] = {}
    resolved_source: dict[str, str] = {}
    for cls in ActionClass:
        floor_v = _class_policy_get(floor.class_policy, cls)
        own_v = _class_policy_get(own.class_policy, cls)
        delegate_explicit = own_source.get(cls.value) == "judges.md"
        if delegate_explicit:
            if _POLICY_STRICTNESS[own_v] < _POLICY_STRICTNESS[floor_v]:
                raise JudgePolicyInvalid(
                    f"delegate's judges.md relaxes the project floor for "
                    f"action class {cls.value!r}: floor={floor_v.value!r} "
                    f"but delegate={own_v.value!r}. Stricter values are: "
                    f"{[v.value for v in sorted(ClassPolicyValue, key=_POLICY_STRICTNESS.get)]}"
                    f". Make the delegate's value at least as strict as "
                    f"the floor's, or remove the override entirely."
                )
            resolved_class_policy[cls] = own_v
            resolved_source[cls.value] = "delegate"
        else:
            # Delegate didn't specify — inherit floor's value (which may
            # itself be the spec default if floor didn't specify either).
            resolved_class_policy[cls] = floor_v
            resolved_source[cls.value] = floor.class_policy.source.get(cls.value, "floor")

    new_class_policy = ClassPolicySnapshot(
        read_only=resolved_class_policy[ActionClass.READ_ONLY],
        reversible_write=resolved_class_policy[ActionClass.REVERSIBLE_WRITE],
        external_side_effect=resolved_class_policy[ActionClass.EXTERNAL_SIDE_EFFECT],
        high_risk=resolved_class_policy[ActionClass.HIGH_RISK],
        source=resolved_source,
    )

    # PR 5b of #112: cascade-floor strictness on ``validation``.
    # Mirrors ``class_policy``: only enforce relax violations when the
    # delegate EXPLICITLY set the field (source=="judges.md"). A
    # delegate that omits ``validation:`` default-fills to "weakened"
    # with source=="default" and inherits the floor's value rather
    # than tripping a false-positive relax violation against a
    # ``validation: strict`` floor.
    if own.validation_source == "judges.md":
        if _VALIDATION_STRICTNESS[own.validation] < _VALIDATION_STRICTNESS[floor.validation]:
            raise JudgePolicyInvalid(
                f"delegate's judges.md relaxes the project floor for "
                f"``validation``: floor={floor.validation!r} but "
                f"delegate={own.validation!r}. Stricter values are: "
                f"{list(_VALID_VALIDATION_VALUES)}. Make the delegate's "
                f"value at least as strict as the floor's, or remove the "
                f"override entirely."
            )
        resolved_validation = own.validation
        resolved_validation_source = "delegate"
    else:
        resolved_validation = floor.validation
        resolved_validation_source = (
            "floor" if floor.validation_source == "judges.md" else floor.validation_source
        )

    # ── mandate_settings cascade-merge ───────────────────────────────
    # Spec/29 §"judges.md integration" documents the ## Mandates section
    # shape but does NOT pin the cascade-merge direction for each field.
    # The merge discipline below defaults to "floor-wins where stricter;
    # own-wins where ambiguous." PR 5 / spec/29 amendment should lock
    # the merge direction once the mandate arc is complete.
    #
    # Field-by-field merge rationale (document for PR 5 input):
    #
    # suspicious_rebind_throttle_s: floor's HIGHER value wins.
    #   Longer throttle is safer (harder to re-bind on an inconsistent
    #   mandate); a delegate that set a shorter throttle must not be
    #   allowed to weaken the project floor's window.
    #
    # unextractable_target_action: if floor is "block", floor wins.
    #   "block" is stricter than "escalate" — floor-wins when stricter.
    #   If both are "escalate" or floor is "escalate", delegate's value
    #   is used (owns the escalation routing for its own mandates).
    #
    # reservation_ttl_s: floor's LOWER value wins.
    #   A shorter TTL frees reserved budget faster, reducing the window
    #   in which an abandoned reservation holds capacity hostage.
    #   Spec/29 doesn't pin direction; "shorter is safer from budget-
    #   overrun perspective" is the conservative read. PR 5 to confirm.
    #
    # high_risk_lock_timeout_s: floor's HIGHER value wins.
    #   More time to acquire the lock reduces spurious BLOCK on contention
    #   while remaining under operator control. Floor's higher value
    #   ensures the project-wide minimum lock patience is respected.
    #
    # no_expiry_warning: floor-wins if floor is True (more conservative).
    #   The doctor emits check_mandate_no_expiry when True. A floor that
    #   opts into the warning must not be silenced by a delegate omitting
    #   or disabling it.
    #
    # cap_breach_action_class_default: per-class merge with floor-wins.
    #   "block" is stricter than "escalate". For each per-class entry,
    #   if floor's value is "block" and own's is "escalate", floor wins.
    merged_mandate_settings = _merge_mandate_settings(own.mandate_settings, floor.mandate_settings)

    return JudgesConfig(
        default_backend=own.default_backend or floor.default_backend,
        default_model=own.default_model or floor.default_model,
        timeout_ms=own.timeout_ms if own.timeout_ms else floor.timeout_ms,
        budget=own.budget,  # delegate's budget takes precedence
        class_policy=new_class_policy,
        failure_policy=_merge_failure_policy(own.failure_policy, floor.failure_policy),
        # Cascade-floor scope (spec/28:408): class_policy is the
        # non-relaxable floor. ``escalation.fallback_on_timeout`` is NOT
        # floor-protected today — a delegate's ``judges.md`` may set its
        # own per-class fallback shape, which silently overrides the
        # project floor's. Pre-PR-5a this gap was acceptable because
        # ``fallback_on_timeout`` was a single string (operator-level
        # "do something safe on timeout"), but PR 5a made the field
        # per-class-configurable and therefore security-relevant.
        # Tracked: https://github.com/dep0we/atomic-agents-stack/issues/173
        escalation=own.escalation,
        judge_captures=own.judge_captures or floor.judge_captures,
        read_audit_mode=own.read_audit_mode or floor.read_audit_mode,
        specialist_axes=own.specialist_axes or floor.specialist_axes,
        validation=resolved_validation,
        validation_source=resolved_validation_source,
        mandate_settings=merged_mandate_settings,
        tools_md_hash=own.tools_md_hash,
        judges_md_hash=own.judges_md_hash,
        source_path=own.source_path,
    )


def load_judges_config(
    agent_root: Path,
    cascade: Any = None,  # CascadePaths or None
    *,
    tools_md_text: str = "",
) -> JudgesConfig | None:
    """Cascade-aware judges.md loader.

    1. Parse ``<agent_root>/judges.md`` (the delegate's own config).
    2. If ``cascade`` is non-None and ``<project_root>/judges.md``
       exists, parse it as the project floor.
    3. Apply ``apply_project_floor`` — raises ``JudgePolicyInvalid`` if
       the delegate's config relaxes the floor for any class.
    4. Compute ``tools_md_hash`` from the passed text so the returned
       ``JudgesConfig`` carries the full policy_version snapshot
       (per spec/28's centralized policy_version computation).

    Returns ``None`` when neither ``judges.md`` exists — the agent.py
    opt-in gate from PR 2a falls back to pre-#112 behavior.
    """
    own_path = agent_root / "judges.md"
    own = parse_judges_md(own_path) if own_path.exists() else None

    floor = None
    if cascade is not None:
        floor_path = cascade.project_root / "judges.md"
        if floor_path.exists():
            floor = parse_judges_md(floor_path)

    if own is None and floor is None:
        return None
    # If only the floor exists, the delegate inherits it wholesale —
    # spec/28 says the floor is always applied even when the delegate
    # doesn't author its own. Skip the strictness check entirely
    # (there's nothing to compare against) and treat the floor as
    # the effective config.
    if own is None:
        merged = floor
    else:
        merged = apply_project_floor(own, floor)

    # Stamp the tools_md_hash so policy_version is fully derived.
    tools_md_hash = hashlib.sha256(tools_md_text.encode("utf-8")).hexdigest()
    return JudgesConfig(
        default_backend=merged.default_backend,
        default_model=merged.default_model,
        timeout_ms=merged.timeout_ms,
        budget=merged.budget,
        class_policy=merged.class_policy,
        failure_policy=merged.failure_policy,
        escalation=merged.escalation,
        judge_captures=merged.judge_captures,
        read_audit_mode=merged.read_audit_mode,
        specialist_axes=merged.specialist_axes,
        validation=merged.validation,
        validation_source=merged.validation_source,
        mandate_settings=merged.mandate_settings,
        tools_md_hash=tools_md_hash,
        judges_md_hash=merged.judges_md_hash,
        source_path=merged.source_path,
    )


# ──────────────────────────────────────────────────────────────────
# Section parsers (internal)


def _coerce_int(raw: Any, *, default: int, field_label: str) -> int:
    if raw is None:
        return default
    if isinstance(raw, bool):  # bool is int subclass; reject explicitly
        raise JudgePolicyInvalid(
            f"judges.md ``{field_label}`` must be an integer; got {raw!r}"
        )
    if not isinstance(raw, int):
        raise JudgePolicyInvalid(
            f"judges.md ``{field_label}`` must be an integer; got "
            f"{type(raw).__name__}={raw!r}"
        )
    if raw < 0:
        raise JudgePolicyInvalid(
            f"judges.md ``{field_label}`` must be >= 0; got {raw}"
        )
    return raw


def _coerce_float(raw: Any, *, field_label: str) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise JudgePolicyInvalid(
            f"judges.md ``{field_label}`` must be a number; got {raw!r}"
        )
    if not isinstance(raw, (int, float)):
        raise JudgePolicyInvalid(
            f"judges.md ``{field_label}`` must be a number; got "
            f"{type(raw).__name__}={raw!r}"
        )
    if raw < 0:
        raise JudgePolicyInvalid(
            f"judges.md ``{field_label}`` must be >= 0; got {raw}"
        )
    return float(raw)


def _parse_budget(raw: Any) -> BudgetConfig:
    if raw is None:
        return BudgetConfig()
    if not isinstance(raw, dict):
        raise JudgePolicyInvalid(
            f"judges.md ``budget`` must be a mapping; got "
            f"{type(raw).__name__}"
        )
    return BudgetConfig(
        daily_usd=_coerce_float(raw.get("daily_usd"), field_label="budget.daily_usd"),
        monthly_usd=_coerce_float(raw.get("monthly_usd"), field_label="budget.monthly_usd"),
        per_action_usd=_coerce_float(raw.get("per_action_usd"), field_label="budget.per_action_usd"),
    )


def _parse_class_policy_value(raw: Any, *, class_name: str) -> ClassPolicyValue:
    if not isinstance(raw, str):
        raise JudgePolicyInvalid(
            f"judges.md ``class_policy.{class_name}`` must be a string "
            f"({sorted(v.value for v in ClassPolicyValue)}); got "
            f"{type(raw).__name__}={raw!r}"
        )
    try:
        return ClassPolicyValue(raw.lower().strip())
    except ValueError as exc:
        raise JudgePolicyInvalid(
            f"judges.md ``class_policy.{class_name}`` is not a valid "
            f"value: {raw!r}. Allowed: "
            f"{sorted(v.value for v in ClassPolicyValue)}"
        ) from exc


def _parse_class_policy(raw: Any) -> ClassPolicySnapshot:
    if raw is None:
        # All defaults.
        return ClassPolicySnapshot(
            read_only=_DEFAULT_CLASS_POLICY[ActionClass.READ_ONLY],
            reversible_write=_DEFAULT_CLASS_POLICY[ActionClass.REVERSIBLE_WRITE],
            external_side_effect=_DEFAULT_CLASS_POLICY[ActionClass.EXTERNAL_SIDE_EFFECT],
            high_risk=_DEFAULT_CLASS_POLICY[ActionClass.HIGH_RISK],
            source={cls.value: "default" for cls in ActionClass},
        )
    if not isinstance(raw, dict):
        raise JudgePolicyInvalid(
            f"judges.md ``class_policy`` must be a mapping; got "
            f"{type(raw).__name__}"
        )
    source: dict[str, str] = {}
    resolved: dict[ActionClass, ClassPolicyValue] = {}
    for cls in ActionClass:
        operator_raw = raw.get(cls.value)
        if operator_raw is None:
            resolved[cls] = _DEFAULT_CLASS_POLICY[cls]
            source[cls.value] = "default"
        else:
            resolved[cls] = _parse_class_policy_value(operator_raw, class_name=cls.value)
            source[cls.value] = "judges.md"
    # Reject extraneous keys — operator typos surface immediately.
    extra = set(raw.keys()) - {cls.value for cls in ActionClass}
    if extra:
        raise JudgePolicyInvalid(
            f"judges.md ``class_policy`` has unrecognized keys: "
            f"{sorted(extra)}. Recognized: "
            f"{sorted(cls.value for cls in ActionClass)}"
        )
    return ClassPolicySnapshot(
        read_only=resolved[ActionClass.READ_ONLY],
        reversible_write=resolved[ActionClass.REVERSIBLE_WRITE],
        external_side_effect=resolved[ActionClass.EXTERNAL_SIDE_EFFECT],
        high_risk=resolved[ActionClass.HIGH_RISK],
        source=source,
    )


def _parse_failure_policy_outcome(
    raw: Any, *, exception_name: str, class_label: str
) -> str:
    if not isinstance(raw, str):
        raise JudgePolicyInvalid(
            f"judges.md ``failure_policy.{class_label}.{exception_name}`` "
            f"must be a string outcome name (allow/block/revise/escalate); "
            f"got {type(raw).__name__}={raw!r}"
        )
    val = raw.lower().strip()
    if val not in {"allow", "block", "revise", "escalate"}:
        raise JudgePolicyInvalid(
            f"judges.md ``failure_policy.{class_label}.{exception_name}`` "
            f"is not a valid outcome: {raw!r}. Allowed: "
            f"['allow', 'block', 'revise', 'escalate']"
        )
    return val


def _parse_failure_policy(raw: Any) -> dict[ActionClass, dict[str, str]]:
    """Parse per-class-per-exception override map.

    Two shapes accepted for operator ergonomics:

    1. **Flat** (most common, operator-friendly): ``{exception_name:
       outcome}``. Applied uniformly to all classes.
    2. **Nested per-class** (advanced): ``{class_name: {exception_name:
       outcome}}``. Lets operators override fallback differently per
       action class.

    The returned shape is always nested per-class. Default-fill from
    ``_DEFAULT_FAILURE_POLICY_PER_EXCEPTION`` for any unspecified
    ``(class, exception)`` pair.
    """
    # Start with default-fill for every class.
    out: dict[ActionClass, dict[str, str]] = {
        cls: dict(_DEFAULT_FAILURE_POLICY_PER_EXCEPTION) for cls in ActionClass
    }
    if raw is None:
        return out
    if not isinstance(raw, dict):
        raise JudgePolicyInvalid(
            f"judges.md ``failure_policy`` must be a mapping; got "
            f"{type(raw).__name__}"
        )

    # Detect shape: if any TOP-LEVEL key matches an exception name, it's
    # flat. Otherwise it should be class names with nested dicts.
    top_level_exception_keys = set(raw.keys()) & _RECOGNIZED_EXCEPTION_NAMES
    if top_level_exception_keys:
        # Flat shape: apply uniformly to all classes.
        # Validate that EVERY key is a recognized exception name.
        extra = set(raw.keys()) - _RECOGNIZED_EXCEPTION_NAMES
        if extra:
            raise JudgePolicyInvalid(
                f"judges.md ``failure_policy`` (flat shape) has "
                f"unrecognized exception names: {sorted(extra)}. "
                f"Recognized: {sorted(_RECOGNIZED_EXCEPTION_NAMES)}"
            )
        for exc_name, outcome_raw in raw.items():
            outcome = _parse_failure_policy_outcome(
                outcome_raw, exception_name=exc_name, class_label="<all classes>"
            )
            for cls in ActionClass:
                out[cls][exc_name] = outcome
        return out

    # Nested per-class shape.
    extra_classes = set(raw.keys()) - {cls.value for cls in ActionClass}
    if extra_classes:
        raise JudgePolicyInvalid(
            f"judges.md ``failure_policy`` (nested shape) has "
            f"unrecognized class names: {sorted(extra_classes)}. "
            f"Recognized: {sorted(cls.value for cls in ActionClass)}"
        )
    for class_name, per_class_raw in raw.items():
        cls = ActionClass(class_name)
        if not isinstance(per_class_raw, dict):
            raise JudgePolicyInvalid(
                f"judges.md ``failure_policy.{class_name}`` must be a "
                f"mapping; got {type(per_class_raw).__name__}"
            )
        unknown_exc = set(per_class_raw.keys()) - _RECOGNIZED_EXCEPTION_NAMES
        if unknown_exc:
            raise JudgePolicyInvalid(
                f"judges.md ``failure_policy.{class_name}`` has "
                f"unrecognized exception names: {sorted(unknown_exc)}. "
                f"Recognized: {sorted(_RECOGNIZED_EXCEPTION_NAMES)}"
            )
        for exc_name, outcome_raw in per_class_raw.items():
            out[cls][exc_name] = _parse_failure_policy_outcome(
                outcome_raw, exception_name=exc_name, class_label=class_name
            )
    return out


# Outcomes the auto-decide path can actually enforce. /ship Step 9.1
# adversarial review (PR 5a) flagged a silent-coercion gap: the parser
# previously accepted all four ``JudgmentOutcome`` values
# (``allow|block|revise|escalate``) but ``_apply_auto_decide`` only
# branches on ``allow`` — every other value silently collapsed to
# ``AUTO_DECIDED_BLOCK`` with no warning, producing audit text that
# contradicted the operator's stated intent. Narrowing the accepted
# set here makes the operator-intent / framework-behavior mismatch
# fail LOUD at parse time instead. ``revise`` and ``escalate`` are
# judge-driven outcomes that require a live judge; they have no
# meaningful semantics in the auto-decide-when-no-judge-responded
# scenario. Wiring them to real machinery (e.g. re-enqueueing as a
# fresh ESCALATE proposal) is tracked separately.
_VALID_FALLBACK_OUTCOMES: tuple[str, ...] = ("allow", "block")


def _parse_fallback_on_timeout(raw: Any) -> dict[str, str]:
    """Parse the ``escalation.fallback_on_timeout`` field.

    Accepts two shapes, both normalize to a ``dict[str, str]`` keyed by
    ``ActionClass.value`` strings with a mandatory ``"default"`` key:

    1. **Legacy string** (PR 3a shape — still accepted):
       ``fallback_on_timeout: block`` → ``{"default": "block"}``.
    2. **Per-class mapping** (PR 5a of #112):

           fallback_on_timeout:
             default: block
             high_risk: block
             reversible_write: allow

       ``default`` is REQUIRED in the dict form — there is no implicit
       fall-through. Operators who want every class to share a policy
       use the legacy string shape; operators who want differentiated
       policies use the dict shape with an explicit ``default``.

    Per-class keys must be one of the four ``ActionClass.value`` strings.
    Values must be one of ``{allow, block, revise, escalate}``. Any
    violation raises ``JudgePolicyInvalid`` naming the offending key
    or value — fail loud at parse time, never silently downgrade.
    """
    if isinstance(raw, str):
        normalized = raw.lower().strip()
        if normalized not in _VALID_FALLBACK_OUTCOMES:
            raise JudgePolicyInvalid(
                f"judges.md ``escalation.fallback_on_timeout`` is not a "
                f"valid outcome: {normalized!r}. Allowed: "
                f"{list(_VALID_FALLBACK_OUTCOMES)}"
            )
        return {"default": normalized}

    if not isinstance(raw, dict):
        raise JudgePolicyInvalid(
            f"judges.md ``escalation.fallback_on_timeout`` must be a "
            f"string or a mapping; got {type(raw).__name__}"
        )

    if "default" not in raw:
        raise JudgePolicyInvalid(
            "judges.md ``escalation.fallback_on_timeout`` mapping is "
            "missing required ``default:`` key. Either spell out a "
            "default outcome (e.g. ``default: block``) or use the "
            "legacy string shape (``fallback_on_timeout: block``)."
        )

    allowed_keys = {"default"} | {c.value for c in ActionClass}
    parsed: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise JudgePolicyInvalid(
                f"judges.md ``escalation.fallback_on_timeout`` keys "
                f"must be strings; got {type(key).__name__}={key!r}"
            )
        if key not in allowed_keys:
            raise JudgePolicyInvalid(
                f"judges.md ``escalation.fallback_on_timeout`` key "
                f"{key!r} is not a recognised ActionClass. Allowed: "
                f"{sorted(allowed_keys)}"
            )
        if not isinstance(value, str):
            raise JudgePolicyInvalid(
                f"judges.md ``escalation.fallback_on_timeout[{key!r}]`` "
                f"must be a string; got {type(value).__name__}"
            )
        normalized_value = value.lower().strip()
        if normalized_value not in _VALID_FALLBACK_OUTCOMES:
            raise JudgePolicyInvalid(
                f"judges.md ``escalation.fallback_on_timeout[{key!r}]`` "
                f"is not a valid outcome: {normalized_value!r}. "
                f"Allowed: {list(_VALID_FALLBACK_OUTCOMES)}"
            )
        parsed[key] = normalized_value
    return parsed


def _parse_escalation(raw: Any) -> EscalationConfig:
    if raw is None:
        return EscalationConfig()
    if not isinstance(raw, dict):
        raise JudgePolicyInvalid(
            f"judges.md ``escalation`` must be a mapping; got "
            f"{type(raw).__name__}"
        )
    # Default ``vault/escalations/`` matches spec/28:288. Operators
    # who set destination=vault explicitly get normalized at write
    # time (see escalation.py).
    destination = str(raw.get("destination", "vault/escalations/"))
    auto_decide = raw.get("auto_decide_after_seconds")
    if auto_decide is not None:
        auto_decide = _coerce_int(
            auto_decide, default=0, field_label="escalation.auto_decide_after_seconds"
        )
    fallback_raw = raw.get("fallback_on_timeout", "block")
    fallback = _parse_fallback_on_timeout(fallback_raw)
    poll_cycle_raw = raw.get("resolution_poll_cycle_seconds", 60)
    poll_cycle = _coerce_int(
        poll_cycle_raw,
        default=60,
        field_label="escalation.resolution_poll_cycle_seconds",
    )
    if poll_cycle < 0:
        raise JudgePolicyInvalid(
            f"judges.md ``escalation.resolution_poll_cycle_seconds`` "
            f"must be >= 0; got {poll_cycle}"
        )
    return EscalationConfig(
        destination=destination,
        auto_decide_after_seconds=auto_decide,
        fallback_on_timeout=fallback,
        resolution_poll_cycle_seconds=poll_cycle,
    )


def _parse_specialist_axes(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, str):
                raise JudgePolicyInvalid(
                    f"judges.md ``specialist_composition`` items must "
                    f"be strings; got {type(item).__name__}={item!r}"
                )
        return [s.strip() for s in raw if s.strip()]
    if isinstance(raw, dict):
        # Allow shape: ``specialist_composition: {axes: [...]}``
        axes = raw.get("axes")
        if axes is None:
            return []
        return _parse_specialist_axes(axes)
    raise JudgePolicyInvalid(
        f"judges.md ``specialist_composition`` must be a list of axis "
        f"names or a mapping with ``axes:`` key; got "
        f"{type(raw).__name__}"
    )


def _class_policy_get(
    snapshot: ClassPolicySnapshot, cls: ActionClass
) -> ClassPolicyValue:
    return {
        ActionClass.READ_ONLY: snapshot.read_only,
        ActionClass.REVERSIBLE_WRITE: snapshot.reversible_write,
        ActionClass.EXTERNAL_SIDE_EFFECT: snapshot.external_side_effect,
        ActionClass.HIGH_RISK: snapshot.high_risk,
    }[cls]


def _merge_failure_policy(
    own: dict[ActionClass, dict[str, str]],
    floor: dict[ActionClass, dict[str, str]],
) -> dict[ActionClass, dict[str, str]]:
    """Merge two failure_policy maps: delegate wins on present keys;
    floor fills in defaults the delegate didn't specify."""
    out: dict[ActionClass, dict[str, str]] = {}
    for cls in ActionClass:
        merged = dict(floor.get(cls, {}))
        merged.update(own.get(cls, {}))
        out[cls] = merged
    return out


def _check_jsonschema_importable() -> None:
    """Probe ``import jsonschema`` and raise ``JudgePolicyInvalid`` with
    an actionable install message if it isn't available.

    Indirected so tests can monkeypatch the probe without monkeypatching
    the global import machinery. The probe runs at agent-load time when
    ``validation: strict`` is configured — operators see the failure
    LOUD at parse time, never at the first amendment.
    """
    try:
        import jsonschema  # noqa: F401
    except ImportError as exc:
        raise JudgePolicyInvalid(
            "judges.md sets ``validation: strict`` but the ``jsonschema`` "
            "package is not importable. Install the ``[validation]`` "
            "extra BEFORE setting ``validation: strict`` in judges.md: "
            "``pip install 'atomic-agents-stack[validation]'`` (or "
            "``uv sync --extra validation`` for uv-managed projects). "
            f"Underlying import error: {exc}"
        ) from exc


# ──────────────────────────────────────────────────────────────────
# Named-section helpers (## Mandates, spec/29 §"judges.md integration")


_SECTION_HEADING_RE = re.compile(r"^## (.+)$", re.MULTILINE)


def _extract_named_section(text: str, section_name: str) -> str | None:
    """Extract the body of a named ``## <section_name>`` section.

    Searches ``text`` for a line matching ``## <section_name>`` (exact
    case). Returns the text between that heading and the next ``## ``
    heading (or EOF), stripped. Returns ``None`` when the section is not
    found.

    Unknown section headings are silently ignored — operators may author
    ``## Notes`` or ``## Overview`` prose sections in their ``judges.md``
    without triggering an error.

    The returned body may still contain fenced ```yaml blocks; callers
    that want plain inline YAML should expect only the non-fenced content.
    The ``## Mandates`` body in spec/29 is inline (no fence markers), so
    the full body string is passed directly to ``yaml.safe_load``.
    """
    matches = list(_SECTION_HEADING_RE.finditer(text))
    for i, match in enumerate(matches):
        heading = match.group(1).strip()
        if heading != section_name:
            continue
        body_start = match.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[body_start:body_end].strip()
        return body if body else ""
    return None


def _parse_mandates_section(body: str | None) -> MandateSettings:
    """Parse the ``## Mandates`` section body into a ``MandateSettings``.

    The body is inline YAML (no fenced code block) per spec/29
    §"judges.md integration" line 661. All fields are optional; missing
    fields fall back to the ``MandateSettings`` dataclass defaults
    (spec/29-documented defaults).

    When ``body`` is ``None`` (section absent from the file), returns a
    default-constructed ``MandateSettings()`` — operators upgrading from
    pre-#124 deployments see zero behavior change.

    When ``body`` is an empty string (heading present, body empty),
    returns ``MandateSettings()`` defaults with no error — same as absent.

    Validation:
    - ``unextractable_target_action`` must be ``"block"`` or
      ``"escalate"`` (spec/29 line 367). Other values raise
      ``JudgePolicyInvalid``.
    - ``suspicious_rebind_throttle_s``, ``reservation_ttl_s``, and
      ``high_risk_lock_timeout_s`` must be non-negative integers
      (delegated to ``_coerce_int``).
    - ``no_expiry_warning`` must be a boolean; non-bool values raise
      ``JudgePolicyInvalid``.
    - ``cap_breach_action_class_default`` must be a mapping when present;
      non-mapping values raise ``JudgePolicyInvalid``.
    """
    if body is None or not body.strip():
        return MandateSettings()

    try:
        raw = yaml.safe_load(body)
    except yaml.YAMLError as exc:
        raise JudgePolicyInvalid(
            f"judges.md ``## Mandates`` section contains invalid YAML: {exc}"
        ) from exc

    if raw is None:
        return MandateSettings()
    if not isinstance(raw, dict):
        raise JudgePolicyInvalid(
            f"judges.md ``## Mandates`` section must be a YAML mapping; "
            f"got {type(raw).__name__}"
        )

    # suspicious_rebind_throttle_s — spec/29 §"Suspicious-rebind throttle"
    # default 60s per spec/29 line 398.
    suspicious_rebind_throttle_s = _coerce_int(
        raw.get("suspicious_rebind_throttle_s"),
        default=60,
        field_label="Mandates.suspicious_rebind_throttle_s",
    )
    if suspicious_rebind_throttle_s == 0:
        raise JudgePolicyInvalid(
            "judges.md ``Mandates.suspicious_rebind_throttle_s`` must be "
            "a positive integer (> 0); got 0. A zero throttle disables the "
            "re-bind defense entirely (spec/29 §'Suspicious-rebind throttle')."
        )

    # unextractable_target_action — spec/29 §"Validation steps" step 5
    # (line 366). Default "block" per spec/29.
    raw_uta = raw.get("unextractable_target_action")
    if raw_uta is None:
        unextractable_target_action: str = "block"
    else:
        if not isinstance(raw_uta, str):
            raise JudgePolicyInvalid(
                f"judges.md ``Mandates.unextractable_target_action`` must be "
                f"a string (\"block\" or \"escalate\"); got "
                f"{type(raw_uta).__name__}={raw_uta!r}"
            )
        unextractable_target_action = raw_uta.lower().strip()
        if unextractable_target_action not in ("block", "escalate"):
            raise JudgePolicyInvalid(
                f"judges.md ``Mandates.unextractable_target_action`` must be "
                f"\"block\" or \"escalate\" (spec/29 line 367); got "
                f"{raw_uta!r}"
            )

    # reservation_ttl_s — spec/29 §"Cost reservation pattern" (PR 3b).
    # Default 60s per spec/29 line 668. Stored by PR 3a; consumed by PR 3b.
    reservation_ttl_s = _coerce_int(
        raw.get("reservation_ttl_s"),
        default=60,
        field_label="Mandates.reservation_ttl_s",
    )
    if reservation_ttl_s == 0:
        raise JudgePolicyInvalid(
            "judges.md ``Mandates.reservation_ttl_s`` must be a positive "
            "integer (> 0); got 0. A zero TTL makes every reservation "
            "immediately expired (spec/29 §'Cost reservation pattern')."
        )

    # high_risk_lock_timeout_s — spec/29 §"High-risk lock specification"
    # (PR 4). Default 30s per spec/29 line 601. Stored here; consumed by PR 4.
    high_risk_lock_timeout_s = _coerce_int(
        raw.get("high_risk_lock_timeout_s"),
        default=30,
        field_label="Mandates.high_risk_lock_timeout_s",
    )
    if high_risk_lock_timeout_s == 0:
        raise JudgePolicyInvalid(
            "judges.md ``Mandates.high_risk_lock_timeout_s`` must be a "
            "positive integer (> 0); got 0. A zero timeout makes every "
            "high-risk lock acquisition immediately fail "
            "(spec/29 §'High-risk lock specification')."
        )

    # no_expiry_warning — spec/29 §"Doctor integration" (line 674).
    # Default True per spec/29. Doctor emits check_mandate_no_expiry when True.
    raw_new = raw.get("no_expiry_warning")
    if raw_new is None:
        no_expiry_warning = True
    elif not isinstance(raw_new, bool):
        raise JudgePolicyInvalid(
            f"judges.md ``Mandates.no_expiry_warning`` must be a boolean "
            f"(true/false); got {type(raw_new).__name__}={raw_new!r}"
        )
    else:
        no_expiry_warning = raw_new

    # cap_breach_action_class_default — spec/29 §"Validation steps" budget-
    # breach action defaults (PR 3b). Default per spec/29 line 669-672.
    # Stored by PR 3a; consumed by PR 3b.
    raw_cbac = raw.get("cap_breach_action_class_default")
    if raw_cbac is None:
        cap_breach_action_class_default = {
            "external_side_effect": "block",
            "high_risk": "escalate",
            "reversible_write": "block",
        }
    else:
        if not isinstance(raw_cbac, dict):
            raise JudgePolicyInvalid(
                f"judges.md ``Mandates.cap_breach_action_class_default`` "
                f"must be a mapping; got {type(raw_cbac).__name__}"
            )
        cap_breach_action_class_default = dict(raw_cbac)

    return MandateSettings(
        suspicious_rebind_throttle_s=suspicious_rebind_throttle_s,
        unextractable_target_action=unextractable_target_action,  # type: ignore[arg-type]
        reservation_ttl_s=reservation_ttl_s,
        high_risk_lock_timeout_s=high_risk_lock_timeout_s,
        no_expiry_warning=no_expiry_warning,
        cap_breach_action_class_default=cap_breach_action_class_default,
    )


def _merge_mandate_settings(
    own: MandateSettings,
    floor: MandateSettings,
) -> MandateSettings:
    """Cascade-merge two ``MandateSettings`` with floor-wins-where-stricter.

    Called from ``apply_project_floor`` when both delegate and project floor
    have a ``mandate_settings``. The merge direction per field is documented
    in the ``apply_project_floor`` comment block and in the inline rationale
    below. Spec/29 does not pin the cascade-merge direction; these choices
    default to "floor-wins where stricter" and are documented for PR 5 /
    spec/29 amendment input.

    Floor-wins rules:
    - ``suspicious_rebind_throttle_s``: max (longer throttle is safer).
    - ``unextractable_target_action``: "block" beats "escalate" (stricter).
    - ``reservation_ttl_s``: min (shorter TTL is safer; reduces budget-hold
      window; PR 5 should confirm this direction).
    - ``high_risk_lock_timeout_s``: max (more time to acquire lock;
      project floor's patience is a minimum guarantee).
    - ``no_expiry_warning``: True beats False (floor's opt-in to the
      doctor warning must not be silenced by a delegate).
    - ``cap_breach_action_class_default``: per-class floor-wins where
      floor is "block" and own is "escalate"; own wins otherwise.
    """
    # suspicious_rebind_throttle_s: floor's HIGHER value wins (longer = safer).
    merged_throttle = max(own.suspicious_rebind_throttle_s, floor.suspicious_rebind_throttle_s)

    # unextractable_target_action: "block" is stricter than "escalate";
    # floor's "block" overrides delegate's "escalate" (floor-wins where stricter).
    if floor.unextractable_target_action == "block":
        merged_uta = "block"
    else:
        merged_uta = own.unextractable_target_action

    # reservation_ttl_s: floor's LOWER value wins (shorter TTL frees budget
    # faster; conservative read per "floor-wins where stricter" discipline).
    # PR 5 to confirm direction per spec/29.
    merged_ttl = min(own.reservation_ttl_s, floor.reservation_ttl_s)

    # high_risk_lock_timeout_s: floor's HIGHER value wins (more lock patience
    # is the floor's guarantee; delegate cannot undercut it).
    merged_lock = max(own.high_risk_lock_timeout_s, floor.high_risk_lock_timeout_s)

    # no_expiry_warning: True beats False (floor's opt-in must not be silenced).
    merged_no_expiry = own.no_expiry_warning or floor.no_expiry_warning

    # cap_breach_action_class_default: per-class floor-wins where floor is "block".
    merged_cbac: dict[str, str] = dict(own.cap_breach_action_class_default)
    for class_name, floor_action in floor.cap_breach_action_class_default.items():
        own_action = merged_cbac.get(class_name)
        if floor_action == "block" and own_action == "escalate":
            # Floor's stricter "block" overrides delegate's "escalate".
            merged_cbac[class_name] = "block"
        elif class_name not in merged_cbac:
            # Delegate didn't specify this class; inherit from floor.
            merged_cbac[class_name] = floor_action

    return MandateSettings(
        suspicious_rebind_throttle_s=merged_throttle,
        unextractable_target_action=merged_uta,  # type: ignore[arg-type]
        reservation_ttl_s=merged_ttl,
        high_risk_lock_timeout_s=merged_lock,
        no_expiry_warning=merged_no_expiry,
        cap_breach_action_class_default=merged_cbac,
    )


def _parse_validation(raw: Any) -> tuple[str, str]:
    """Parse the top-level ``validation:`` field.

    Returns ``(value, source)`` where source is ``"default"`` when the
    field is omitted and ``"judges.md"`` when explicitly set. The
    distinction is load-bearing for cascade-floor strictness — a
    delegate that omits ``validation:`` must inherit the floor's value
    rather than trip a false-positive relax violation (PR 0-1a from
    the PR 5b plan-review re-round).

    Rejects:

    - ``audit`` with "not yet implemented; tracked at #176".
    - ``paranoid`` with "not yet implemented; tracked at #179" (the
      reserved-but-not-yet-implemented namespace is distinct from a
      generic operator typo per CLAUDE.md taste rule).
    - Any other unknown value with "validation must be one of
      {weakened, strict}; got <repr>".
    - Non-string values with the canonical "must be a string" message.

    On ``validation: strict``, probes ``import jsonschema`` and raises
    ``JudgePolicyInvalid`` with the install instruction when it fails.
    Operators who flip to strict without installing the extra see the
    failure LOUD at agent-load, not at the first amendment.
    """
    if raw is None:
        return "weakened", "default"
    if not isinstance(raw, str):
        raise JudgePolicyInvalid(
            f"judges.md ``validation`` must be a string "
            f"({list(_VALID_VALIDATION_VALUES)}); got "
            f"{type(raw).__name__}={raw!r}"
        )
    normalized = raw.lower().strip()
    if normalized in _RESERVED_VALIDATION_VALUES:
        raise JudgePolicyInvalid(_RESERVED_VALIDATION_VALUES[normalized])
    if normalized not in _VALID_VALIDATION_VALUES:
        raise JudgePolicyInvalid(
            f"judges.md ``validation`` must be one of "
            f"{list(_VALID_VALIDATION_VALUES)}; got {raw!r}"
        )
    if normalized == "strict":
        _check_jsonschema_importable()
    return normalized, "judges.md"
