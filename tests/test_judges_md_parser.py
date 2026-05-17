"""Tests for ``atomic_agents/judges_md.py`` — operator config parser
(spec/28, #112 PR 3a).

Covers:

- **Happy paths**: every recognized field parses correctly.
- **Default-fill**: omitted fields get spec/28 defaults.
- **Failure-policy shape detection**: flat (uniform across classes)
  vs nested (per-class override). Operator typos in either shape
  surface immediately.
- **Cascade-aware project-floor**: ``apply_project_floor`` raises on
  relax attempts; merges otherwise.
- **Malformation paths**: every JudgePolicyInvalid raise site has
  a regression test.
- **Atomic-snapshot semantics**: parser reads bytes once and hashes
  that exact snapshot.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest

from atomic_agents.exceptions import JudgePolicyInvalid
from atomic_agents.judge.types import (
    ActionClass,
    ClassPolicyValue,
)
from atomic_agents.judges_md import (
    JudgesConfig,
    MandateSettings,
    apply_project_floor,
    load_judges_config,
    parse_judges_md,
    parse_judges_md_text,
)


# ──────────────────────────────────────────────────────────────────
# Happy paths


class TestHappyPaths:
    def test_empty_text_returns_all_defaults(self):
        cfg = parse_judges_md_text("")
        assert cfg.default_backend == "rules"
        assert cfg.default_model is None
        assert cfg.timeout_ms == 5000
        assert cfg.budget.daily_usd is None
        assert cfg.budget.monthly_usd is None
        # Default class policy per spec/28
        assert cfg.class_policy.read_only == ClassPolicyValue.BYPASS
        assert cfg.class_policy.reversible_write == ClassPolicyValue.JUDGE_REQUIRED
        assert cfg.class_policy.external_side_effect == ClassPolicyValue.JUDGE_REQUIRED
        assert cfg.class_policy.high_risk == ClassPolicyValue.ESCALATE
        # Default failure_policy = block for every (class, exception)
        for cls in ActionClass:
            for exc in (
                "JudgeUnavailable", "JudgePolicyInvalid", "JudgeBudgetExhausted",
                "JudgeProposalInvalid", "JudgeAmendedProposalRejected",
            ):
                assert cfg.failure_policy[cls][exc] == "block"
        # Audit defaults
        assert cfg.judge_captures is False
        assert cfg.read_audit_mode is False

    def test_text_outside_yaml_blocks_ignored(self):
        cfg = parse_judges_md_text(
            "# Judges\n"
            "\n"
            "Some prose about the judge layer that should be ignored.\n"
            "Markdown bullets here that aren't in a yaml block too:\n"
            "- not_a_real_field: nope\n"
        )
        # Falls back to all defaults.
        assert cfg.class_policy.high_risk == ClassPolicyValue.ESCALATE

    def test_full_config_yaml_block(self):
        text = (
            "## Default\n"
            "```yaml\n"
            "backend: llm\n"
            "model: gpt-5-nano\n"
            "timeout_ms: 8000\n"
            "budget:\n"
            "  daily_usd: 1.50\n"
            "  monthly_usd: 30.0\n"
            "class_policy:\n"
            "  read_only: bypass\n"
            "  reversible_write: judge_required\n"
            "  external_side_effect: judge_required\n"
            "  high_risk: escalate\n"
            "escalation:\n"
            "  destination: vault\n"
            "  auto_decide_after_seconds: 86400\n"
            "  fallback_on_timeout: block\n"
            "judge_captures: true\n"
            "read_audit_mode: false\n"
            "specialist_composition:\n"
            "  - security\n"
            "  - mandate_check\n"
            "```\n"
        )
        cfg = parse_judges_md_text(text)
        assert cfg.default_backend == "llm"
        assert cfg.default_model == "gpt-5-nano"
        assert cfg.timeout_ms == 8000
        assert cfg.budget.daily_usd == 1.50
        assert cfg.budget.monthly_usd == 30.0
        assert cfg.class_policy.read_only == ClassPolicyValue.BYPASS
        assert cfg.class_policy.high_risk == ClassPolicyValue.ESCALATE
        assert cfg.escalation.destination == "vault"
        assert cfg.escalation.auto_decide_after_seconds == 86400
        # PR 5a: legacy string normalizes to {"default": "block"}.
        assert cfg.escalation.fallback_on_timeout == {"default": "block"}
        assert cfg.judge_captures is True
        assert cfg.specialist_axes == ["security", "mandate_check"]

    def test_class_policy_source_attribution(self):
        # Omitted classes get source="default"; specified get source="judges.md"
        text = (
            "```yaml\n"
            "class_policy:\n"
            "  high_risk: escalate\n"
            "```\n"
        )
        cfg = parse_judges_md_text(text)
        assert cfg.class_policy.source["high_risk"] == "judges.md"
        assert cfg.class_policy.source["read_only"] == "default"
        assert cfg.class_policy.source["reversible_write"] == "default"

    def test_multiple_yaml_blocks_merge_later_wins(self):
        text = (
            "```yaml\n"
            "backend: rules\n"
            "timeout_ms: 1000\n"
            "```\n"
            "```yaml\n"
            "backend: llm\n"
            "```\n"
        )
        cfg = parse_judges_md_text(text)
        assert cfg.default_backend == "llm"
        assert cfg.timeout_ms == 1000

    def test_empty_yaml_block_skipped(self):
        text = "```yaml\n```\n"
        cfg = parse_judges_md_text(text)
        assert cfg.default_backend == "rules"

    def test_specialist_composition_axes_mapping_shape(self):
        # Alternative shape: specialist_composition: {axes: [...]}
        text = (
            "```yaml\n"
            "specialist_composition:\n"
            "  axes:\n"
            "    - security\n"
            "    - performance\n"
            "```\n"
        )
        cfg = parse_judges_md_text(text)
        assert cfg.specialist_axes == ["security", "performance"]


# ──────────────────────────────────────────────────────────────────
# Failure-policy shape detection (Codex round-1 P2 #2)


class TestFailurePolicyShapes:
    def test_flat_shape_uniform_across_classes(self):
        text = (
            "```yaml\n"
            "failure_policy:\n"
            "  JudgeUnavailable: escalate\n"
            "  JudgeBudgetExhausted: block\n"
            "```\n"
        )
        cfg = parse_judges_md_text(text)
        # ``escalate`` for JudgeUnavailable applies to every class.
        for cls in ActionClass:
            assert cfg.failure_policy[cls]["JudgeUnavailable"] == "escalate"
            assert cfg.failure_policy[cls]["JudgeBudgetExhausted"] == "block"
            # Unspecified exceptions get the default-fill.
            assert cfg.failure_policy[cls]["JudgePolicyInvalid"] == "block"

    def test_nested_shape_per_class_override(self):
        text = (
            "```yaml\n"
            "failure_policy:\n"
            "  read_only:\n"
            "    JudgeUnavailable: allow\n"
            "  high_risk:\n"
            "    JudgeUnavailable: escalate\n"
            "    JudgeBudgetExhausted: escalate\n"
            "```\n"
        )
        cfg = parse_judges_md_text(text)
        assert cfg.failure_policy[ActionClass.READ_ONLY]["JudgeUnavailable"] == "allow"
        assert cfg.failure_policy[ActionClass.HIGH_RISK]["JudgeUnavailable"] == "escalate"
        assert cfg.failure_policy[ActionClass.HIGH_RISK]["JudgeBudgetExhausted"] == "escalate"
        # Unspecified classes keep defaults.
        assert (
            cfg.failure_policy[ActionClass.EXTERNAL_SIDE_EFFECT]["JudgeUnavailable"] == "block"
        )

    def test_failure_policy_for_helper(self):
        cfg = parse_judges_md_text(
            "```yaml\n"
            "failure_policy:\n"
            "  high_risk:\n"
            "    JudgeUnavailable: escalate\n"
            "```\n"
        )
        assert (
            cfg.failure_policy_for(ActionClass.HIGH_RISK, "JudgeUnavailable")
            == "escalate"
        )
        # Falls back to "block" for unspecified.
        assert (
            cfg.failure_policy_for(ActionClass.READ_ONLY, "JudgeUnavailable")
            == "block"
        )


# ──────────────────────────────────────────────────────────────────
# Malformation paths — every JudgePolicyInvalid raise


class TestMalformationPaths:
    def test_invalid_yaml_raises(self):
        text = "```yaml\nbackend: : invalid\n```\n"
        with pytest.raises(JudgePolicyInvalid, match="invalid YAML"):
            parse_judges_md_text(text)

    def test_yaml_block_not_a_mapping(self):
        text = "```yaml\n- just a list\n```\n"
        with pytest.raises(JudgePolicyInvalid, match="must be a mapping"):
            parse_judges_md_text(text)

    def test_invalid_class_policy_value(self):
        text = (
            "```yaml\n"
            "class_policy:\n"
            "  high_risk: not_a_valid_value\n"
            "```\n"
        )
        with pytest.raises(JudgePolicyInvalid, match="not a valid value"):
            parse_judges_md_text(text)

    def test_class_policy_extraneous_key(self):
        text = (
            "```yaml\n"
            "class_policy:\n"
            "  high_risk: escalate\n"
            "  bogus_class: bypass\n"
            "```\n"
        )
        with pytest.raises(JudgePolicyInvalid, match="unrecognized keys"):
            parse_judges_md_text(text)

    def test_timeout_ms_must_be_int(self):
        text = "```yaml\ntimeout_ms: \"5000\"\n```\n"
        with pytest.raises(JudgePolicyInvalid, match="timeout_ms"):
            parse_judges_md_text(text)

    def test_timeout_ms_must_be_nonneg(self):
        text = "```yaml\ntimeout_ms: -5\n```\n"
        with pytest.raises(JudgePolicyInvalid, match=">= 0"):
            parse_judges_md_text(text)

    def test_timeout_ms_rejects_bool(self):
        # bool is int subclass in Python — guard against `timeout_ms: true`
        text = "```yaml\ntimeout_ms: true\n```\n"
        with pytest.raises(JudgePolicyInvalid, match="timeout_ms"):
            parse_judges_md_text(text)

    def test_budget_must_be_mapping(self):
        text = "```yaml\nbudget: 1.50\n```\n"
        with pytest.raises(JudgePolicyInvalid, match="budget.*mapping"):
            parse_judges_md_text(text)

    def test_budget_negative_rejected(self):
        text = "```yaml\nbudget:\n  daily_usd: -1.50\n```\n"
        with pytest.raises(JudgePolicyInvalid, match=">= 0"):
            parse_judges_md_text(text)

    def test_failure_policy_unknown_exception_name_flat(self):
        text = (
            "```yaml\n"
            "failure_policy:\n"
            "  JudgeUnavailable: block\n"
            "  NotARealException: block\n"
            "```\n"
        )
        with pytest.raises(JudgePolicyInvalid, match="unrecognized exception names"):
            parse_judges_md_text(text)

    def test_failure_policy_invalid_outcome(self):
        text = (
            "```yaml\n"
            "failure_policy:\n"
            "  JudgeUnavailable: not_a_real_outcome\n"
            "```\n"
        )
        with pytest.raises(JudgePolicyInvalid, match="not a valid outcome"):
            parse_judges_md_text(text)

    def test_failure_policy_unknown_class_nested(self):
        text = (
            "```yaml\n"
            "failure_policy:\n"
            "  bogus_class:\n"
            "    JudgeUnavailable: block\n"
            "```\n"
        )
        with pytest.raises(JudgePolicyInvalid, match="unrecognized class names"):
            parse_judges_md_text(text)

    def test_failure_policy_per_class_unknown_exc(self):
        text = (
            "```yaml\n"
            "failure_policy:\n"
            "  high_risk:\n"
            "    NotAnException: block\n"
            "```\n"
        )
        with pytest.raises(JudgePolicyInvalid, match="unrecognized exception names"):
            parse_judges_md_text(text)

    def test_escalation_must_be_mapping(self):
        text = "```yaml\nescalation: not a mapping\n```\n"
        with pytest.raises(JudgePolicyInvalid, match="escalation.*mapping"):
            parse_judges_md_text(text)

    def test_escalation_invalid_fallback(self):
        text = (
            "```yaml\n"
            "escalation:\n"
            "  fallback_on_timeout: not_a_real_outcome\n"
            "```\n"
        )
        with pytest.raises(JudgePolicyInvalid, match="not a valid outcome"):
            parse_judges_md_text(text)

    def test_specialist_axes_invalid_shape(self):
        text = "```yaml\nspecialist_composition: 42\n```\n"
        with pytest.raises(JudgePolicyInvalid, match="specialist_composition"):
            parse_judges_md_text(text)

    def test_specialist_axes_non_string_item(self):
        text = (
            "```yaml\n"
            "specialist_composition:\n"
            "  - valid_axis\n"
            "  - 42\n"
            "```\n"
        )
        with pytest.raises(JudgePolicyInvalid, match="must be strings"):
            parse_judges_md_text(text)


# ──────────────────────────────────────────────────────────────────
# Per-class fallback_on_timeout (PR 5a of #112)


class TestPerClassFallbackOnTimeout:
    """``escalation.fallback_on_timeout`` accepts a legacy string OR a
    per-class dict. The dict form requires an explicit ``default`` key —
    there is no implicit fall-through. Per-class keys must be valid
    ``ActionClass.value`` strings; values must be valid outcomes.
    """

    def test_legacy_string_normalizes_to_default_dict(self):
        text = (
            "```yaml\n"
            "escalation:\n"
            "  fallback_on_timeout: block\n"
            "```\n"
        )
        cfg = parse_judges_md_text(text)
        assert cfg.escalation.fallback_on_timeout == {"default": "block"}

    def test_legacy_string_uppercase_normalized(self):
        # Backward-compat with the pre-PR-5a parser's case-insensitive
        # behavior. Value is lowercased on the way in.
        text = (
            "```yaml\n"
            "escalation:\n"
            "  fallback_on_timeout: BLOCK\n"
            "```\n"
        )
        cfg = parse_judges_md_text(text)
        assert cfg.escalation.fallback_on_timeout == {"default": "block"}

    def test_dict_shape_with_explicit_default(self):
        text = (
            "```yaml\n"
            "escalation:\n"
            "  fallback_on_timeout:\n"
            "    default: block\n"
            "    high_risk: block\n"
            "    reversible_write: allow\n"
            "```\n"
        )
        cfg = parse_judges_md_text(text)
        assert cfg.escalation.fallback_on_timeout == {
            "default": "block",
            "high_risk": "block",
            "reversible_write": "allow",
        }

    def test_dict_shape_default_only_equivalent_to_legacy_string(self):
        # Operators who want every class to share a policy may still
        # spell the dict form explicitly — the parser doesn't collapse
        # it back to a string; both shapes produce the canonical dict.
        text = (
            "```yaml\n"
            "escalation:\n"
            "  fallback_on_timeout:\n"
            "    default: block\n"
            "```\n"
        )
        cfg = parse_judges_md_text(text)
        assert cfg.escalation.fallback_on_timeout == {"default": "block"}

    def test_dict_shape_missing_default_raises(self):
        # P1 finding from PR 5a plan review (§3 of plan review):
        # ``default`` is mandatory in the dict form. No implicit
        # fall-through to "block" — operators always opt into a
        # default explicitly.
        text = (
            "```yaml\n"
            "escalation:\n"
            "  fallback_on_timeout:\n"
            "    high_risk: escalate\n"
            "    reversible_write: allow\n"
            "```\n"
        )
        with pytest.raises(JudgePolicyInvalid, match="missing required ``default:``"):
            parse_judges_md_text(text)

    def test_dict_shape_unknown_class_key_raises(self):
        # P0 finding from PR 5a plan review (§2): operator typo on
        # a class name fails LOUD at parse time rather than silently
        # falling through to default at auto-decide time. Names the
        # offending key.
        text = (
            "```yaml\n"
            "escalation:\n"
            "  fallback_on_timeout:\n"
            "    default: block\n"
            "    high_risc: escalate\n"  # operator typo
            "```\n"
        )
        with pytest.raises(JudgePolicyInvalid, match="high_risc.*not a recognised ActionClass"):
            parse_judges_md_text(text)

    def test_dict_shape_invalid_outcome_raises(self):
        text = (
            "```yaml\n"
            "escalation:\n"
            "  fallback_on_timeout:\n"
            "    default: block\n"
            "    high_risk: not_a_real_outcome\n"
            "```\n"
        )
        with pytest.raises(JudgePolicyInvalid, match="high_risk.*not a valid outcome"):
            parse_judges_md_text(text)

    def test_dict_shape_invalid_default_outcome_raises(self):
        text = (
            "```yaml\n"
            "escalation:\n"
            "  fallback_on_timeout:\n"
            "    default: nonsense\n"
            "```\n"
        )
        with pytest.raises(JudgePolicyInvalid, match="default.*not a valid outcome"):
            parse_judges_md_text(text)

    def test_legacy_string_revise_rejected(self):
        # /ship Step 9.1 adversarial finding (cross-confirmed by 3
        # specialists): the parser used to accept all four JudgmentOutcome
        # values, but ``_apply_auto_decide`` only branches on ``allow``
        # — ``revise``/``escalate`` silently collapsed to BLOCK at runtime
        # with audit text contradicting operator intent. Narrowed
        # acceptance to {allow, block} surfaces the gap at parse time.
        text = (
            "```yaml\n"
            "escalation:\n"
            "  fallback_on_timeout: revise\n"
            "```\n"
        )
        with pytest.raises(JudgePolicyInvalid, match="not a valid outcome"):
            parse_judges_md_text(text)

    def test_legacy_string_escalate_rejected(self):
        text = (
            "```yaml\n"
            "escalation:\n"
            "  fallback_on_timeout: escalate\n"
            "```\n"
        )
        with pytest.raises(JudgePolicyInvalid, match="not a valid outcome"):
            parse_judges_md_text(text)

    def test_dict_shape_revise_per_class_rejected(self):
        text = (
            "```yaml\n"
            "escalation:\n"
            "  fallback_on_timeout:\n"
            "    default: block\n"
            "    high_risk: revise\n"
            "```\n"
        )
        with pytest.raises(JudgePolicyInvalid, match="high_risk.*not a valid outcome"):
            parse_judges_md_text(text)

    def test_dict_shape_escalate_per_class_rejected(self):
        text = (
            "```yaml\n"
            "escalation:\n"
            "  fallback_on_timeout:\n"
            "    default: escalate\n"
            "```\n"
        )
        with pytest.raises(JudgePolicyInvalid, match="default.*not a valid outcome"):
            parse_judges_md_text(text)

    def test_non_string_non_dict_raises(self):
        text = (
            "```yaml\n"
            "escalation:\n"
            "  fallback_on_timeout: 42\n"
            "```\n"
        )
        with pytest.raises(JudgePolicyInvalid, match="must be a string or a mapping"):
            parse_judges_md_text(text)

    def test_dict_shape_non_string_value_raises(self):
        text = (
            "```yaml\n"
            "escalation:\n"
            "  fallback_on_timeout:\n"
            "    default: block\n"
            "    high_risk: true\n"  # YAML bool, not a string
            "```\n"
        )
        with pytest.raises(JudgePolicyInvalid, match="must be a string"):
            parse_judges_md_text(text)

    def test_dict_shape_non_string_key_raises(self):
        # PR 5a gap: the ``if not isinstance(key, str)`` branch in
        # ``_parse_fallback_on_timeout``. YAML always coerces mapping
        # keys to strings, so this branch is unreachable through
        # ``parse_judges_md_text``. Call the internal function directly
        # to pin it — protects against future callers (programmatic
        # construction, JSON5 parsers) that hand raw dicts to the function.
        from atomic_agents.judges_md import _parse_fallback_on_timeout

        with pytest.raises(JudgePolicyInvalid, match="keys must be strings"):
            _parse_fallback_on_timeout({42: "block", "default": "block"})

    def test_legacy_string_allow_normalizes_correctly(self):
        # ``"allow"`` is a valid legacy string outcome. Only ``"block"``
        # was tested in the existing suite. ``"revise"`` and
        # ``"escalate"`` are NOT valid for ``fallback_on_timeout`` per
        # the /ship Step 9.1 adversarial finding (the auto-decide path
        # only branches on ``allow``; the parser narrows to {allow,
        # block} to surface the gap at parse time).
        text = (
            "```yaml\n"
            "escalation:\n"
            "  fallback_on_timeout: allow\n"
            "```\n"
        )
        cfg = parse_judges_md_text(text)
        assert cfg.escalation.fallback_on_timeout == {"default": "allow"}

    def test_dict_shape_all_four_action_classes_as_keys(self):
        # All four ActionClass values are valid per-class keys in the
        # dict shape. ``test_dict_shape_with_explicit_default`` only
        # exercised ``high_risk`` + ``reversible_write``. Pin ``read_only``
        # + ``external_side_effect`` too so a future ActionClass rename
        # regresses loud here.
        text = (
            "```yaml\n"
            "escalation:\n"
            "  fallback_on_timeout:\n"
            "    default: block\n"
            "    read_only: allow\n"
            "    reversible_write: block\n"
            "    external_side_effect: block\n"
            "    high_risk: block\n"
            "```\n"
        )
        cfg = parse_judges_md_text(text)
        assert cfg.escalation.fallback_on_timeout == {
            "default": "block",
            "read_only": "allow",
            "reversible_write": "block",
            "external_side_effect": "block",
            "high_risk": "block",
        }


# ──────────────────────────────────────────────────────────────────
# Project-floor strictness (Codex round-1 P1 #2)


class TestProjectFloor:
    def test_floor_blocks_relax_attempt_per_class(self):
        floor_text = (
            "```yaml\n"
            "class_policy:\n"
            "  high_risk: escalate\n"
            "```\n"
        )
        delegate_text = (
            "```yaml\n"
            "class_policy:\n"
            "  high_risk: judge_required\n"  # relaxation — strictness drops
            "```\n"
        )
        floor = parse_judges_md_text(floor_text)
        delegate = parse_judges_md_text(delegate_text)
        with pytest.raises(JudgePolicyInvalid, match="relaxes the project floor"):
            apply_project_floor(delegate, floor)

    def test_delegate_may_strengthen(self):
        # Floor says judge_required; delegate strengthens to escalate.
        floor = parse_judges_md_text(
            "```yaml\nclass_policy:\n  high_risk: judge_required\n```\n"
        )
        delegate = parse_judges_md_text(
            "```yaml\nclass_policy:\n  high_risk: escalate\n```\n"
        )
        merged = apply_project_floor(delegate, floor)
        assert merged.class_policy.high_risk == ClassPolicyValue.ESCALATE

    def test_delegate_keeps_own_budget(self):
        floor = parse_judges_md_text(
            "```yaml\nbudget:\n  daily_usd: 5.0\n```\n"
        )
        delegate = parse_judges_md_text(
            "```yaml\nbudget:\n  daily_usd: 1.0\n```\n"
        )
        merged = apply_project_floor(delegate, floor)
        assert merged.budget.daily_usd == 1.0

    def test_floor_fills_in_missing_delegate_fields(self):
        floor = parse_judges_md_text(
            "```yaml\n"
            "model: gpt-5-nano\n"
            "class_policy:\n"
            "  high_risk: escalate\n"
            "```\n"
        )
        delegate = parse_judges_md_text("```yaml\nbackend: rules\n```\n")
        merged = apply_project_floor(delegate, floor)
        # Delegate didn't specify model; floor fills it in.
        assert merged.default_model == "gpt-5-nano"
        # Delegate's class_policy didn't override high_risk → floor's
        # escalate wins (delegate's default was escalate too — matches).
        assert merged.class_policy.high_risk == ClassPolicyValue.ESCALATE

    def test_no_floor_returns_own_unchanged(self):
        own = parse_judges_md_text("```yaml\nbackend: llm\n```\n")
        merged = apply_project_floor(own, None)
        assert merged.default_backend == "llm"

    def test_delegate_omits_class_inherits_floor_not_relax(self):
        # Codex round-2 P2: when floor sets external_side_effect:
        # escalate but delegate omits that class, the delegate's
        # default-fill produced judge_required at parse time —
        # apply_project_floor would falsely flag it as a relax
        # violation. Fix: only check strictness for classes the
        # delegate EXPLICITLY overrode (source="judges.md").
        floor = parse_judges_md_text(
            "```yaml\n"
            "class_policy:\n"
            "  external_side_effect: escalate\n"
            "  high_risk: escalate\n"
            "```\n"
        )
        delegate = parse_judges_md_text(
            # Delegate only sets reversible_write; floor's
            # external_side_effect + high_risk must be inherited
            # WITHOUT triggering relax violation.
            "```yaml\n"
            "class_policy:\n"
            "  reversible_write: escalate\n"
            "```\n"
        )
        merged = apply_project_floor(delegate, floor)
        assert merged.class_policy.external_side_effect == ClassPolicyValue.ESCALATE
        assert merged.class_policy.high_risk == ClassPolicyValue.ESCALATE
        assert merged.class_policy.reversible_write == ClassPolicyValue.ESCALATE


# ──────────────────────────────────────────────────────────────────
# Atomic-snapshot + cascade-aware load


class TestAtomicSnapshotLoad:
    def test_returns_none_when_file_absent(self, tmp_path):
        cfg = parse_judges_md(tmp_path / "nope.md")
        assert cfg is None

    def test_returns_none_for_none_path(self):
        assert parse_judges_md(None) is None

    def test_hashes_byte_snapshot(self, tmp_path):
        # Pin the hash semantic — content is hashed as bytes, in the
        # snapshot returned by read_bytes(), so torn reads can't
        # produce a stale-but-valid hash.
        path = tmp_path / "judges.md"
        content = "```yaml\nbackend: llm\n```\n"
        path.write_text(content)
        cfg = parse_judges_md(path)
        assert cfg is not None
        expected = hashlib.sha256(content.encode("utf-8")).hexdigest()
        assert cfg.judges_md_hash == expected

    def test_non_utf8_raises(self, tmp_path):
        path = tmp_path / "judges.md"
        path.write_bytes(b"\xff\xfe not valid utf-8")
        with pytest.raises(JudgePolicyInvalid, match="not valid UTF-8"):
            parse_judges_md(path)


class TestLoadJudgesConfig:
    def test_single_agent_layout(self, tmp_path):
        agent_root = tmp_path / "agent"
        agent_root.mkdir()
        (agent_root / "judges.md").write_text(
            "```yaml\n"
            "backend: llm\n"
            "class_policy:\n"
            "  high_risk: escalate\n"
            "```\n"
        )
        cfg = load_judges_config(agent_root, cascade=None, tools_md_text="x")
        assert cfg is not None
        assert cfg.default_backend == "llm"
        # tools_md_hash is stamped from the passed text.
        assert cfg.tools_md_hash == hashlib.sha256(b"x").hexdigest()

    def test_returns_none_when_no_judges_md(self, tmp_path):
        agent_root = tmp_path / "agent"
        agent_root.mkdir()
        cfg = load_judges_config(agent_root, cascade=None)
        assert cfg is None

    def test_cascade_project_floor_applied(self, tmp_path):
        # Simulate cascade with project_root containing judges.md
        from dataclasses import dataclass

        @dataclass
        class _StubCascade:
            project_root: Path

        agent_root = tmp_path / "instance"
        agent_root.mkdir()
        project_root = tmp_path / "project"
        project_root.mkdir()
        # Floor demands high_risk: escalate
        (project_root / "judges.md").write_text(
            "```yaml\nclass_policy:\n  high_risk: escalate\n```\n"
        )
        # Delegate tries to relax to judge_required — must raise.
        (agent_root / "judges.md").write_text(
            "```yaml\nclass_policy:\n  high_risk: judge_required\n```\n"
        )
        with pytest.raises(JudgePolicyInvalid, match="relaxes the project floor"):
            load_judges_config(
                agent_root, cascade=_StubCascade(project_root=project_root),
            )

    def test_cascade_floor_only_no_delegate(self, tmp_path):
        # Project floor present, no delegate judges.md — delegate
        # inherits floor wholesale.
        from dataclasses import dataclass

        @dataclass
        class _StubCascade:
            project_root: Path

        agent_root = tmp_path / "instance"
        agent_root.mkdir()
        project_root = tmp_path / "project"
        project_root.mkdir()
        (project_root / "judges.md").write_text(
            "```yaml\n"
            "backend: llm\n"
            "class_policy:\n"
            "  high_risk: escalate\n"
            "```\n"
        )
        cfg = load_judges_config(
            agent_root,
            cascade=_StubCascade(project_root=project_root),
        )
        assert cfg is not None
        assert cfg.default_backend == "llm"
        assert cfg.class_policy.high_risk == ClassPolicyValue.ESCALATE


# ──────────────────────────────────────────────────────────────────
# validation: field (PR 5b of #112)


class TestValidationField:
    """``validation:`` top-level field gates JSON-Schema validation of
    amended ``tool_arguments`` on REVISE. Default is ``weakened``
    (PR 3c behavior); ``strict`` opts into ``jsonschema.validate``
    against the registered tool's ``input_schema``.

    The gate is on the parsed config, NOT on transitive import
    availability of ``jsonschema`` — operators with the package
    pulled in by an unrelated dependency must not see strict
    validation kick in without explicit opt-in (plan review §8).
    """

    def test_omitted_defaults_to_weakened(self):
        cfg = parse_judges_md_text("```yaml\nbackend: rules\n```\n")
        assert cfg.validation == "weakened"
        assert cfg.validation_source == "default"

    def test_explicit_weakened_parses(self):
        cfg = parse_judges_md_text(
            "```yaml\nvalidation: weakened\n```\n"
        )
        assert cfg.validation == "weakened"
        assert cfg.validation_source == "judges.md"

    def test_strict_parses_when_jsonschema_importable(self):
        # The default test environment has jsonschema available via the
        # [validation] extra installed for CI. If a future env strips
        # it, this test fails LOUD — that's the contract working.
        cfg = parse_judges_md_text(
            "```yaml\nvalidation: strict\n```\n"
        )
        assert cfg.validation == "strict"
        assert cfg.validation_source == "judges.md"

    def test_strict_raises_when_jsonschema_missing(self, monkeypatch):
        # Monkeypatch the load-time probe — exercises the LOUD-at-load
        # contract operators see when they flip strict without first
        # installing the [validation] extra.
        from atomic_agents import judges_md

        def _fake_check():
            raise JudgePolicyInvalid(
                "judges.md sets ``validation: strict`` but the "
                "``jsonschema`` package is not importable. Install "
                "the ``[validation]`` extra BEFORE setting "
                "``validation: strict`` in judges.md: "
                "``pip install 'atomic-agents-stack[validation]'``."
            )

        monkeypatch.setattr(judges_md, "_check_jsonschema_importable", _fake_check)
        with pytest.raises(JudgePolicyInvalid, match="not importable"):
            parse_judges_md_text("```yaml\nvalidation: strict\n```\n")

    def test_audit_rejected_with_namespace_pointer(self):
        # Reserved namespace — operator reaching for a future feature
        # gets a distinct error from a typo. (#176 tracks the work.)
        with pytest.raises(JudgePolicyInvalid, match="issues/176"):
            parse_judges_md_text("```yaml\nvalidation: audit\n```\n")

    def test_paranoid_rejected_with_namespace_pointer(self):
        # Reserved namespace symmetric with audit. (#179 tracks the work.)
        with pytest.raises(JudgePolicyInvalid, match="issues/179"):
            parse_judges_md_text("```yaml\nvalidation: paranoid\n```\n")

    def test_unknown_value_rejected_with_allowed_set(self):
        with pytest.raises(
            JudgePolicyInvalid,
            match=r"validation`` must be one of \['weakened', 'strict'\]",
        ):
            parse_judges_md_text(
                "```yaml\nvalidation: super_strict\n```\n"
            )

    def test_non_string_rejected(self):
        with pytest.raises(JudgePolicyInvalid, match="must be a string"):
            parse_judges_md_text("```yaml\nvalidation: 42\n```\n")

    def test_uppercase_normalized(self):
        cfg = parse_judges_md_text("```yaml\nvalidation: STRICT\n```\n")
        assert cfg.validation == "strict"


class TestValidationFloor:
    """Cascade-floor strictness on ``validation``. Mirrors
    ``class_policy``: a delegate that explicitly sets the field must
    be at least as strict as the floor. A delegate that omits the
    field inherits the floor (no false-positive relax violation).
    """

    def test_floor_strict_delegate_weakened_explicit_raises(self):
        floor = parse_judges_md_text(
            "```yaml\nvalidation: strict\n```\n"
        )
        delegate = parse_judges_md_text(
            "```yaml\nvalidation: weakened\n```\n"
        )
        with pytest.raises(
            JudgePolicyInvalid,
            match="relaxes the project floor for ``validation``",
        ):
            apply_project_floor(delegate, floor)

    def test_floor_strict_delegate_strict_passes(self):
        floor = parse_judges_md_text(
            "```yaml\nvalidation: strict\n```\n"
        )
        delegate = parse_judges_md_text(
            "```yaml\nvalidation: strict\n```\n"
        )
        merged = apply_project_floor(delegate, floor)
        assert merged.validation == "strict"

    def test_floor_strict_delegate_omits_inherits_floor(self):
        # Delegate didn't specify validation — must inherit floor's
        # value rather than trip a false-positive relax violation
        # against the parser-default "weakened".
        floor = parse_judges_md_text(
            "```yaml\nvalidation: strict\n```\n"
        )
        delegate = parse_judges_md_text("```yaml\nbackend: rules\n```\n")
        merged = apply_project_floor(delegate, floor)
        assert merged.validation == "strict"

    def test_floor_weakened_delegate_strict_passes(self):
        # Strengthening is always allowed.
        floor = parse_judges_md_text(
            "```yaml\nvalidation: weakened\n```\n"
        )
        delegate = parse_judges_md_text(
            "```yaml\nvalidation: strict\n```\n"
        )
        merged = apply_project_floor(delegate, floor)
        assert merged.validation == "strict"

    def test_floor_strict_delegate_explicit_strict_source_is_delegate(self):
        # /ship Step 9.1 maintainability gap-fill: post-merge
        # validation_source should be "delegate" when the delegate
        # explicitly set strict. Regression guard for cascade-chain
        # checks that key on validation_source.
        floor = parse_judges_md_text(
            "```yaml\nvalidation: strict\n```\n"
        )
        delegate = parse_judges_md_text(
            "```yaml\nvalidation: strict\n```\n"
        )
        merged = apply_project_floor(delegate, floor)
        assert merged.validation_source == "delegate"

    def test_floor_strict_delegate_omits_source_is_floor(self):
        # Symmetric gap-fill: when the delegate omits and inherits the
        # floor, post-merge validation_source should be "floor"
        # (because the floor explicitly set it).
        floor = parse_judges_md_text(
            "```yaml\nvalidation: strict\n```\n"
        )
        delegate = parse_judges_md_text("```yaml\nbackend: rules\n```\n")
        merged = apply_project_floor(delegate, floor)
        assert merged.validation_source == "floor"


# ──────────────────────────────────────────────────────────────────
# ## Mandates section (spec/29 §"judges.md integration", #124 PR 3a)


class TestMandatesSection:
    """``## Mandates`` section in ``judges.md``. Inline YAML (no fenced
    block) per spec/29 §"judges.md integration" line 661. All fields
    default-fill from the ``MandateSettings`` dataclass defaults when the
    section is absent — zero behavior change for pre-#124 deployments.
    """

    def test_parse_judges_md_without_mandates_section_uses_defaults(self):
        # Backward-compat: no ## Mandates section → MandateSettings defaults.
        cfg = parse_judges_md_text(
            "```yaml\n"
            "backend: rules\n"
            "```\n"
        )
        assert cfg.mandate_settings == MandateSettings()
        assert cfg.mandate_settings.suspicious_rebind_throttle_s == 60
        assert cfg.mandate_settings.unextractable_target_action == "block"
        assert cfg.mandate_settings.reservation_ttl_s == 60
        assert cfg.mandate_settings.high_risk_lock_timeout_s == 30
        assert cfg.mandate_settings.no_expiry_warning is True
        assert cfg.mandate_settings.cap_breach_action_class_default == {
            "external_side_effect": "block",
            "high_risk": "escalate",
            "reversible_write": "block",
        }

    def test_parse_judges_md_with_mandates_section_all_fields(self):
        # All fields supplied → populated correctly.
        text = (
            "```yaml\n"
            "backend: rules\n"
            "```\n"
            "\n"
            "## Mandates\n"
            "\n"
            "reservation_ttl_s: 120\n"
            "suspicious_rebind_throttle_s: 90\n"
            "unextractable_target_action: escalate\n"
            "no_expiry_warning: false\n"
            "high_risk_lock_timeout_s: 45\n"
            "cap_breach_action_class_default:\n"
            "  external_side_effect: escalate\n"
            "  high_risk: block\n"
            "  reversible_write: escalate\n"
        )
        cfg = parse_judges_md_text(text)
        ms = cfg.mandate_settings
        assert ms.reservation_ttl_s == 120
        assert ms.suspicious_rebind_throttle_s == 90
        assert ms.unextractable_target_action == "escalate"
        assert ms.no_expiry_warning is False
        assert ms.high_risk_lock_timeout_s == 45
        assert ms.cap_breach_action_class_default == {
            "external_side_effect": "escalate",
            "high_risk": "block",
            "reversible_write": "escalate",
        }

    def test_parse_mandates_section_partial_fields_fallback_to_defaults(self):
        # Only some fields set → others fall back to MandateSettings defaults.
        text = (
            "## Mandates\n"
            "\n"
            "suspicious_rebind_throttle_s: 30\n"
        )
        cfg = parse_judges_md_text(text)
        ms = cfg.mandate_settings
        assert ms.suspicious_rebind_throttle_s == 30
        # Defaults for everything else.
        assert ms.reservation_ttl_s == 60
        assert ms.unextractable_target_action == "block"
        assert ms.high_risk_lock_timeout_s == 30
        assert ms.no_expiry_warning is True

    def test_parse_mandates_section_spec29_example_shape(self):
        # Reproduce the exact spec/29 §"judges.md integration" example
        # (line 666-674).
        text = (
            "## Mandates\n"
            "\n"
            "reservation_ttl_s: 60\n"
            "cap_breach_action_class_default:\n"
            "  external_side_effect: block\n"
            "  high_risk: escalate\n"
            "  reversible_write: block\n"
            "unextractable_target_action: block\n"
            "no_expiry_warning: true\n"
        )
        cfg = parse_judges_md_text(text)
        ms = cfg.mandate_settings
        assert ms.reservation_ttl_s == 60
        assert ms.unextractable_target_action == "block"
        assert ms.no_expiry_warning is True
        assert ms.cap_breach_action_class_default == {
            "external_side_effect": "block",
            "high_risk": "escalate",
            "reversible_write": "block",
        }

    def test_parse_mandates_section_invalid_unextractable_target_action(self):
        # Value outside {block, escalate} raises JudgePolicyInvalid.
        text = (
            "## Mandates\n"
            "\n"
            "unextractable_target_action: allow\n"
        )
        with pytest.raises(JudgePolicyInvalid, match="block.*escalate|escalate.*block"):
            parse_judges_md_text(text)

    def test_parse_mandates_section_invalid_throttle_zero(self):
        # Zero throttle disables the re-bind defense; refused at parse time.
        text = (
            "## Mandates\n"
            "\n"
            "suspicious_rebind_throttle_s: 0\n"
        )
        with pytest.raises(JudgePolicyInvalid, match="suspicious_rebind_throttle_s"):
            parse_judges_md_text(text)

    def test_parse_mandates_section_invalid_throttle_negative(self):
        # Negative value is refused by _coerce_int (>= 0 check).
        text = (
            "## Mandates\n"
            "\n"
            "suspicious_rebind_throttle_s: -5\n"
        )
        with pytest.raises(JudgePolicyInvalid, match=">= 0"):
            parse_judges_md_text(text)

    def test_parse_mandates_section_invalid_reservation_ttl_zero(self):
        text = (
            "## Mandates\n"
            "\n"
            "reservation_ttl_s: 0\n"
        )
        with pytest.raises(JudgePolicyInvalid, match="reservation_ttl_s"):
            parse_judges_md_text(text)

    def test_parse_mandates_section_no_expiry_warning_must_be_bool(self):
        text = (
            "## Mandates\n"
            "\n"
            "no_expiry_warning: yes_please\n"
        )
        with pytest.raises(JudgePolicyInvalid, match="no_expiry_warning.*bool"):
            parse_judges_md_text(text)

    def test_parse_mandates_section_cap_breach_must_be_mapping(self):
        text = (
            "## Mandates\n"
            "\n"
            "cap_breach_action_class_default: block\n"
        )
        with pytest.raises(JudgePolicyInvalid, match="cap_breach_action_class_default.*mapping"):
            parse_judges_md_text(text)

    def test_mandates_section_coexists_with_fenced_yaml_blocks(self):
        # Both config paths are active in the same file — fenced blocks
        # handle existing fields; ## Mandates handles mandate fields.
        text = (
            "```yaml\n"
            "backend: llm\n"
            "timeout_ms: 8000\n"
            "```\n"
            "\n"
            "## Mandates\n"
            "\n"
            "suspicious_rebind_throttle_s: 120\n"
            "unextractable_target_action: escalate\n"
        )
        cfg = parse_judges_md_text(text)
        assert cfg.default_backend == "llm"
        assert cfg.timeout_ms == 8000
        assert cfg.mandate_settings.suspicious_rebind_throttle_s == 120
        assert cfg.mandate_settings.unextractable_target_action == "escalate"

    def test_mandates_section_empty_body_uses_defaults(self):
        # Heading present but body empty → defaults.
        text = (
            "## Mandates\n"
            "\n"
            "## SomeOtherSection\n"
            "\n"
            "Prose here.\n"
        )
        cfg = parse_judges_md_text(text)
        assert cfg.mandate_settings == MandateSettings()

    def test_apply_project_floor_mandate_settings_cascade(self):
        # Floor-wins discipline: stricter values from the project floor
        # override the delegate's more permissive values.
        floor_text = (
            "## Mandates\n"
            "\n"
            "suspicious_rebind_throttle_s: 120\n"
            "unextractable_target_action: block\n"
            "reservation_ttl_s: 30\n"
            "high_risk_lock_timeout_s: 60\n"
            "no_expiry_warning: true\n"
            "cap_breach_action_class_default:\n"
            "  external_side_effect: block\n"
            "  high_risk: block\n"
            "  reversible_write: block\n"
        )
        delegate_text = (
            "## Mandates\n"
            "\n"
            "suspicious_rebind_throttle_s: 60\n"
            "unextractable_target_action: escalate\n"
            "reservation_ttl_s: 90\n"
            "high_risk_lock_timeout_s: 30\n"
            "no_expiry_warning: false\n"
            "cap_breach_action_class_default:\n"
            "  external_side_effect: escalate\n"
            "  high_risk: escalate\n"
            "  reversible_write: escalate\n"
        )
        floor = parse_judges_md_text(floor_text)
        delegate = parse_judges_md_text(delegate_text)
        merged = apply_project_floor(delegate, floor)
        ms = merged.mandate_settings

        # suspicious_rebind_throttle_s: max(60, 120) = 120 (floor's higher wins)
        assert ms.suspicious_rebind_throttle_s == 120
        # unextractable_target_action: floor's "block" overrides delegate's "escalate"
        assert ms.unextractable_target_action == "block"
        # reservation_ttl_s: min(90, 30) = 30 (floor's shorter wins)
        assert ms.reservation_ttl_s == 30
        # high_risk_lock_timeout_s: max(30, 60) = 60 (floor's higher wins)
        assert ms.high_risk_lock_timeout_s == 60
        # no_expiry_warning: True or False = True (floor's True wins)
        assert ms.no_expiry_warning is True
        # cap_breach_action_class_default: floor's "block" beats delegate's "escalate"
        assert ms.cap_breach_action_class_default["external_side_effect"] == "block"
        assert ms.cap_breach_action_class_default["high_risk"] == "block"
        assert ms.cap_breach_action_class_default["reversible_write"] == "block"

    def test_apply_project_floor_mandate_settings_own_wins_where_not_stricter(self):
        # When the delegate is already stricter or equal, own value is
        # preserved. Confirms floor-wins doesn't unconditionally clobber.
        floor_text = (
            "## Mandates\n"
            "\n"
            "suspicious_rebind_throttle_s: 30\n"
            "unextractable_target_action: escalate\n"
        )
        delegate_text = (
            "## Mandates\n"
            "\n"
            "suspicious_rebind_throttle_s: 90\n"
            "unextractable_target_action: block\n"
        )
        floor = parse_judges_md_text(floor_text)
        delegate = parse_judges_md_text(delegate_text)
        merged = apply_project_floor(delegate, floor)
        ms = merged.mandate_settings

        # Delegate's 90 is higher (stricter) → wins.
        assert ms.suspicious_rebind_throttle_s == 90
        # Delegate's "block" is stricter → wins (floor is "escalate").
        assert ms.unextractable_target_action == "block"
