"""``manage apply-rec <rec-id>`` verb tests (spec/55 #727 Unit 2 — the third verb).

Covers the verb-specific surface: rec-match against the CURRENT match universe
(``build_rec_match_universe``), the four apply-rec-introduced refusals
(``rec_no_longer_valid`` / ``rec_kind_not_applicable`` / ``rec_source_not_applicable``
/ ``rec_guard_failed``) each independently strip-tested, the delegation seam into
``apply_set_model_write()`` (delegation fidelity vs. a hand-typed ``set-model
--model``), the dedicated ``PRIMITIVE_MANAGE_APPLY_REC`` audit shape, and that
``set-model``'s own M9 composition chain still applies unchanged past all four
apply-rec gates.

``tests/test_manage_spine.py`` covers the verb-agnostic SPINE guarantees (lock/
agent_busy, exit-code ladder) — apply-rec's contribution there proves the
hoisted spine serves this third (delegating) verb too, not a re-test of
set-model's already-covered spine behavior (inherited via the shared
``apply_set_model_write`` write function apply-rec calls into).
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from atomic_agents.advisor.recommend import build_rec_match_universe, canonical_rec_id
from atomic_agents.logs.types import PRIMITIVE_MANAGE_APPLY_REC
from atomic_agents.manage.apply_rec import run_apply_rec
from atomic_agents.manage.set_model import run_set_model

from tests._manage_test_helpers import (
    collect_jsonl,
    get_fleet_log_dir,
    make_apply_rec_args,
    make_set_model_args,
)


# ── Fixture builder ──────────────────────────────────────────────────────────

# opus -> sonnet-dated is the baked-in default_same_family downgrade for
# claude-opus-4-8 (atomic_agents.advisor.targets._DEFAULT_SAME_FAMILY_DOWNGRADE).
DEFAULT_MODEL = "claude-opus-4-8"
DEFAULT_CANDIDATE = "claude-sonnet-4-6-20260101"


def _seed_apply_rec_agent(
    root: Path,
    agent: str,
    model: str,
    today: date,
    eval_overrides: list[dict] | None = None,
    run_cost_usd: float = 0.50,
    n_runs: int = 12,
) -> Path:
    """Seed one agent: a set-model-WRITABLE model.md (a real '## Default model'
    value span, unlike tests/advisor/test_advisor_recommend.py's bare
    ``model: <id>`` stub) + N primary runs + 12 evals.

    ``eval_overrides`` replaces the default 12-strong-pass eval fixture
    wholesale — used to inject a fresh hard-fail (guard-failing) window.
    No governance.md is written — the agent picks up a "governance" kind
    recommendation for free, which the kind-gate tests use.
    """
    agent_dir = root / agent
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "model.md").write_text(
        f"# MODEL: {agent}\n\n"
        "## Default model\n\n"
        f"**`{model}`**\n\n"
        "Chosen for: balanced reasoning and cost for day-to-day work.\n",
        encoding="utf-8",
    )

    runs = []
    for i in range(n_runs):
        ts = datetime.combine(
            today - timedelta(days=i + 1), datetime.min.time()
        ).replace(tzinfo=timezone.utc)
        runs.append(
            {
                "ts": ts.isoformat(),
                "trigger": "cron",
                "model": model,
                "input_tokens": 2000,
                "output_tokens": 1000,
                "cost_usd": run_cost_usd,
                "cache_hit_tokens": 0,
                "cache_miss_tokens": 2000,
                "latency_ms": 100,
                "status": "completed",
                "summary": "ok",
            }
        )
    for rec in runs:
        ts_d = datetime.fromisoformat(rec["ts"]).date()
        month_dir = agent_dir / "log" / ts_d.strftime("%Y-%m")
        month_dir.mkdir(parents=True, exist_ok=True)
        with (month_dir / f"{ts_d.isoformat()}.jsonl").open("a") as f:
            f.write(json.dumps(rec) + "\n")

    evals = eval_overrides or [
        {"ts": today.isoformat(), "verdict": "pass", "weighted_score": 4.6}
        for _ in range(12)
    ]
    evals_dir = agent_dir / "evals" / "runs"
    evals_dir.mkdir(parents=True, exist_ok=True)
    with (evals_dir / f"{today.isoformat()}.jsonl").open("w") as f:
        for rec in evals:
            f.write(json.dumps(rec) + "\n")

    return agent_dir


_OPERATOR_CONFIGURED_TARGETS_MD = """## Fleet Health Targets

```yaml
recommendations:
  work_type_allowed_models:
    coordinator:
      - claude-haiku-4-5-20251001
```
"""
# NOTE: keyed on "coordinator", not "general" — the fixture's synthetic runs
# use trigger="cron", which _classify_work_type's delegation-trigger ladder
# (atomic_agents/advisor/score.py) classifies as "coordinator", not "general".


def _model_md_text(agent_dir: Path) -> str:
    return (agent_dir / "model.md").read_text(encoding="utf-8")


def _find_rec_id(tmp_path: Path, agent: str, kind: str, today: date) -> str:
    universe = build_rec_match_universe(tmp_path, today=today)
    matches = [r for r in universe if r.agent == agent and r.kind == kind]
    assert matches, (
        f"fixture must yield a {kind!r} candidate for {agent!r}: {universe!r}"
    )
    rec = matches[0]
    return canonical_rec_id(rec.agent, rec.kind, rec.candidate_model)


# ── Group A: rec-match ──────────────────────────────────────────────────────


def test_rec_match_positive_applies(tmp_path):
    today = date.today()
    agent_dir = _seed_apply_rec_agent(tmp_path, "opus-agent", DEFAULT_MODEL, today)

    rec_id = _find_rec_id(tmp_path, "opus-agent", "savings_cost", today)

    exit_code = run_apply_rec(make_apply_rec_args(rec_id, tmp_path), tmp_path)

    assert exit_code == 0
    assert f"`{DEFAULT_CANDIDATE}`" in _model_md_text(agent_dir)
    assert len(list((agent_dir / ".config-snapshots" / "set-model").glob("*.md"))) == 1


def test_rec_no_longer_valid_refuses_no_write(tmp_path, capsys):
    today = date.today()
    agent_dir = _seed_apply_rec_agent(tmp_path, "opus-agent", DEFAULT_MODEL, today)
    before = _model_md_text(agent_dir)

    exit_code = run_apply_rec(
        make_apply_rec_args("0" * 12, tmp_path, use_json=True), tmp_path
    )

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error_type"] == "rec_no_longer_valid"
    assert _model_md_text(agent_dir) == before


# ── Group B: guard re-validation — three separately strip-tested controls ──


def test_rec_guard_failed_distinct_from_no_longer_valid_and_m9(tmp_path, capsys):
    """A guard-failing-but-floor-clearing candidate refuses rec_guard_failed —
    DISTINCT from rec_no_longer_valid (Group A) and from a set-model M9
    refusal (below), each independently strip-tested (per-invocation negative
    control discipline)."""
    today = date.today()
    guard_failing_evals = [
        {"ts": today.isoformat(), "verdict": "pass", "weighted_score": 4.6}
        for _ in range(11)
    ] + [
        {
            "ts": today.isoformat(),
            "verdict": "pass",
            "weighted_score": 4.6,
            "hard_fails": ["critical_format_error"],
        }
    ]
    agent_dir = _seed_apply_rec_agent(
        tmp_path,
        "guard-failing-agent",
        DEFAULT_MODEL,
        today,
        eval_overrides=guard_failing_evals,
    )
    before = _model_md_text(agent_dir)

    rec_id = _find_rec_id(tmp_path, "guard-failing-agent", "savings_cost", today)

    exit_code = run_apply_rec(
        make_apply_rec_args(rec_id, tmp_path, use_json=True), tmp_path
    )

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error_type"] == "rec_guard_failed"
    assert _model_md_text(agent_dir) == before


def test_m9_still_applies_distinct_from_rec_guard_failed(tmp_path, capsys, monkeypatch):
    """A passing, applicable rec whose candidate model has since been removed
    from PRICING refuses via set-model's OWN unpriced_model gate, unchanged —
    apply-rec does not shortcut it, and the error_type is DISTINCT from both
    rec_guard_failed and rec_no_longer_valid."""
    today = date.today()
    agent_dir = _seed_apply_rec_agent(tmp_path, "opus-agent", DEFAULT_MODEL, today)
    before = _model_md_text(agent_dir)

    rec_id = _find_rec_id(tmp_path, "opus-agent", "savings_cost", today)

    from atomic_agents.manage import set_model as set_model_mod

    monkeypatch.setattr(set_model_mod, "get_model_rates", lambda _model_id: None)

    exit_code = run_apply_rec(
        make_apply_rec_args(rec_id, tmp_path, use_json=True), tmp_path
    )

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error_type"] == "unpriced_model"
    assert _model_md_text(agent_dir) == before


# ── Group C: kind gate ───────────────────────────────────────────────────────


def test_rec_kind_not_applicable_refuses_before_delegation(tmp_path, capsys):
    today = date.today()
    agent_dir = _seed_apply_rec_agent(tmp_path, "opus-agent", DEFAULT_MODEL, today)
    before = _model_md_text(agent_dir)

    rec_id = _find_rec_id(tmp_path, "opus-agent", "governance", today)

    exit_code = run_apply_rec(
        make_apply_rec_args(rec_id, tmp_path, use_json=True), tmp_path
    )

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error_type"] == "rec_kind_not_applicable"
    # No delegation attempted — model.md untouched, no snapshot taken.
    assert _model_md_text(agent_dir) == before
    assert not (agent_dir / ".config-snapshots").exists()


def test_savings_cost_rec_id_proceeds_past_kind_gate(tmp_path):
    """Positive control for the kind gate: a savings_cost rec-id is never
    refused rec_kind_not_applicable."""
    today = date.today()
    _seed_apply_rec_agent(tmp_path, "opus-agent", DEFAULT_MODEL, today)
    rec_id = _find_rec_id(tmp_path, "opus-agent", "savings_cost", today)

    exit_code = run_apply_rec(make_apply_rec_args(rec_id, tmp_path), tmp_path)
    assert exit_code == 0


# ── Group D: source gate (the skeptic's guard) ──────────────────────────────


def test_rec_source_not_applicable_refuses(tmp_path, capsys):
    today = date.today()
    (tmp_path / "targets.md").write_text(
        _OPERATOR_CONFIGURED_TARGETS_MD, encoding="utf-8"
    )
    agent_dir = _seed_apply_rec_agent(tmp_path, "opus-agent", DEFAULT_MODEL, today)
    before = _model_md_text(agent_dir)

    universe = build_rec_match_universe(tmp_path, today=today)
    matches = [
        r for r in universe if r.agent == "opus-agent" and r.kind == "savings_cost"
    ]
    assert matches, f"fixture must yield a savings_cost candidate: {universe!r}"
    assert matches[0].source == "operator_configured", (
        f"fixture must select the operator_configured candidate; got {matches[0]!r}"
    )
    rec_id = canonical_rec_id(
        matches[0].agent, matches[0].kind, matches[0].candidate_model
    )

    exit_code = run_apply_rec(
        make_apply_rec_args(rec_id, tmp_path, use_json=True), tmp_path
    )

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error_type"] == "rec_source_not_applicable"
    assert _model_md_text(agent_dir) == before


# ── Group E: delegation fidelity ────────────────────────────────────────────


def test_delegation_fidelity_matches_hand_typed_set_model(tmp_path):
    """An applied apply-rec write is byte-identical in its model.md result to
    a hand-typed `set-model --model <candidate>` run against the identical
    starting state."""
    today = date.today()

    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()

    agent_dir_a = _seed_apply_rec_agent(root_a, "opus-agent", DEFAULT_MODEL, today)
    agent_dir_b = _seed_apply_rec_agent(root_b, "opus-agent", DEFAULT_MODEL, today)
    assert _model_md_text(agent_dir_a) == _model_md_text(agent_dir_b)

    rec_id = _find_rec_id(root_a, "opus-agent", "savings_cost", today)
    rc_a = run_apply_rec(make_apply_rec_args(rec_id, root_a), root_a)
    assert rc_a == 0

    rc_b = run_set_model(
        make_set_model_args("opus-agent", root_b, model=DEFAULT_CANDIDATE), root_b
    )
    assert rc_b == 0

    assert _model_md_text(agent_dir_a) == _model_md_text(agent_dir_b)

    snaps_a = list((agent_dir_a / ".config-snapshots" / "set-model").glob("*.md"))
    snaps_b = list((agent_dir_b / ".config-snapshots" / "set-model").glob("*.md"))
    assert len(snaps_a) == len(snaps_b) == 1
    assert snaps_a[0].read_text(encoding="utf-8") == snaps_b[0].read_text(
        encoding="utf-8"
    )


# ── Group F: audit primitive ────────────────────────────────────────────────


def test_audit_primitive_and_marker(tmp_path):
    today = date.today()
    agent_dir = _seed_apply_rec_agent(tmp_path, "opus-agent", DEFAULT_MODEL, today)
    rec_id = _find_rec_id(tmp_path, "opus-agent", "savings_cost", today)

    exit_code = run_apply_rec(make_apply_rec_args(rec_id, tmp_path), tmp_path)
    assert exit_code == 0

    records = collect_jsonl(agent_dir / "log")
    applied = [r for r in records if r.get("primitive") == PRIMITIVE_MANAGE_APPLY_REC]
    assert len(applied) == 1
    record = applied[0]

    # extra{} is flattened into top-level keys on JSONL serialization
    # (RunRecord.to_dict — see logs/types.py's docstring).
    assert record["status"] == "applied"
    assert record["model"] == "n/a"
    assert record["input_tokens"] == 0
    assert record["output_tokens"] == 0
    assert record["applied_from_rec"] == {
        "rec_id": rec_id,
        "kind": "savings_cost",
    }
    assert "principal_id" in record
    assert record["changed_fields"] == ["default_model"]
    assert record["before"] == {"default_model": DEFAULT_MODEL}
    assert record["after"] == {"default_model": DEFAULT_CANDIDATE}
    assert record["snapshot_path"] is not None

    # Also lands in the fleet-wide _manage log scope (M8 dual-scope append).
    fleet_records = collect_jsonl(get_fleet_log_dir(tmp_path))
    fleet_applied = [
        r for r in fleet_records if r.get("primitive") == PRIMITIVE_MANAGE_APPLY_REC
    ]
    assert len(fleet_applied) == 1


# ── Group G: --dry-run ───────────────────────────────────────────────────────


def test_dry_run_shows_swap_and_full_eval_headroom_no_write(tmp_path, capsys):
    today = date.today()
    agent_dir = _seed_apply_rec_agent(tmp_path, "opus-agent", DEFAULT_MODEL, today)
    before = _model_md_text(agent_dir)

    rec_id = _find_rec_id(tmp_path, "opus-agent", "savings_cost", today)

    exit_code = run_apply_rec(
        make_apply_rec_args(rec_id, tmp_path, dry_run=True, use_json=True), tmp_path
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["dry_run"] is True
    safety = payload["safety"]
    assert safety["passed"] is True
    for key in (
        "weighted_score_margin",
        "pass_rate_margin",
        "hard_fails",
        "sample_n",
        "rubric_threshold",
    ):
        assert key in safety
    assert payload["applied_from_rec"] == {"rec_id": rec_id, "kind": "savings_cost"}
    assert _model_md_text(agent_dir) == before


# ── Group H: --json refusal shape (all four apply-rec refusals) ────────────


def test_json_refusal_shape_all_four_apply_rec_refusals(tmp_path, capsys):
    today = date.today()
    (tmp_path / "targets.md").write_text(
        _OPERATOR_CONFIGURED_TARGETS_MD, encoding="utf-8"
    )
    _seed_apply_rec_agent(tmp_path, "opus-agent", DEFAULT_MODEL, today)

    # rec_no_longer_valid
    rc = run_apply_rec(make_apply_rec_args("f" * 12, tmp_path, use_json=True), tmp_path)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload == {
        "ok": False,
        "error_type": "rec_no_longer_valid",
        "reason": payload["reason"],
    }

    # rec_kind_not_applicable
    gov_rec_id = _find_rec_id(tmp_path, "opus-agent", "governance", today)
    rc = run_apply_rec(
        make_apply_rec_args(gov_rec_id, tmp_path, use_json=True), tmp_path
    )
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["ok"] is False
    assert payload["error_type"] == "rec_kind_not_applicable"

    # rec_source_not_applicable
    universe = build_rec_match_universe(tmp_path, today=today)
    savings = [
        r for r in universe if r.agent == "opus-agent" and r.kind == "savings_cost"
    ]
    assert savings and savings[0].source == "operator_configured"
    src_rec_id = canonical_rec_id(
        savings[0].agent, savings[0].kind, savings[0].candidate_model
    )
    rc = run_apply_rec(
        make_apply_rec_args(src_rec_id, tmp_path, use_json=True), tmp_path
    )
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["ok"] is False
    assert payload["error_type"] == "rec_source_not_applicable"


def test_json_refusal_shape_rec_guard_failed(tmp_path, capsys):
    today = date.today()
    guard_failing_evals = [
        {"ts": today.isoformat(), "verdict": "pass", "weighted_score": 4.6}
        for _ in range(11)
    ] + [
        {
            "ts": today.isoformat(),
            "verdict": "pass",
            "weighted_score": 4.6,
            "hard_fails": ["critical_format_error"],
        }
    ]
    _seed_apply_rec_agent(
        tmp_path,
        "guard-failing-agent",
        DEFAULT_MODEL,
        today,
        eval_overrides=guard_failing_evals,
    )
    rec_id = _find_rec_id(tmp_path, "guard-failing-agent", "savings_cost", today)

    rc = run_apply_rec(make_apply_rec_args(rec_id, tmp_path, use_json=True), tmp_path)
    payload = json.loads(capsys.readouterr().out)

    assert rc == 1
    assert payload["ok"] is False
    assert payload["error_type"] == "rec_guard_failed"
    assert payload["safety"]["passed"] is False
