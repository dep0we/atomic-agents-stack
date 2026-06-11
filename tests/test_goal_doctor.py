"""Tests for doctor.check_goal_backend (spec/41, issue #425).

Coverage (mirrors the sibling doctor-check test pattern —
test_corpus_doctor.py / test_doctor_check_mandate_backend.py):

  - PASS: goal.md absent (reactive agent) -> goal_md_present False
  - PASS: goal.md present and valid -> archived_count + capability flags in detail
  - FAIL: ATOMIC_AGENTS_GOAL_BACKEND names an unregistered backend (heavy
    instantiation branch) -> AND no raw credentials leak into the rendered output
  - FAIL: goal.md present but corrupted (heavy load_goal probe)

The dual-probe pattern (MEMORY.md feedback_doctor_dual_probe_pattern) requires
exercising BOTH the lightweight list_archived() probe and the heavy load_goal()
probe; this file pins both, plus the credential-redaction guarantee for a
URL-shaped ATOMIC_AGENTS_GOAL_BACKEND value.

Filesystem isolation: every test uses tmp_path. No writes outside the temp dir.
Env-var isolation: monkeypatch.setenv / delenv; all env mutations auto-revert.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from atomic_agents.doctor import (
    FAIL,
    PASS,
    check_goal_backend,
    run_doctor,
)
from atomic_agents._goal_impl import CURRENT_GOAL_SCHEMA_VERSION


# ──────────────────────────────────────────────────────────────────────────────
# Helpers


def _clear_goal_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATOMIC_AGENTS_GOAL_BACKEND", raising=False)


def _write_goal_md(agent_root: Path, *, intent: str = "Doctor test goal") -> None:
    agent_root.mkdir(parents=True, exist_ok=True)
    content = f"""---
schema_version: {CURRENT_GOAL_SCHEMA_VERSION}
active: true
intent: {intent}
priority: high
created: 2026-06-11
last_progress_check: 2026-06-11
success_criteria:
  - Criterion one
sub_goals:
  - id: sg1
    label: First sub-goal
    status: pending
---

## Overview

Goal body.

## History (auto-appended)
- 2026-06-11 — goal created
"""
    (agent_root / "goal.md").write_text(content, encoding="utf-8")


# ──────────────────────────────────────────────────────────────────────────────
# PASS


def test_pass_when_goal_md_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reactive agent with no goal.md PASSes (not a failure) with goal_md_present False."""
    _clear_goal_env(monkeypatch)
    agent_root = tmp_path / "reactive-agent"
    agent_root.mkdir(parents=True, exist_ok=True)

    result = check_goal_backend(agent_root)

    assert result.status == PASS
    assert result.detail["goal_md_present"] is False
    assert result.message.endswith("(no goal.md for this agent)")


def test_pass_when_goal_md_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A present, valid goal.md PASSes with capability flags + archived_count in detail."""
    _clear_goal_env(monkeypatch)
    agent_root = tmp_path / "goal-agent"
    _write_goal_md(agent_root)

    result = check_goal_backend(agent_root)

    assert result.status == PASS
    assert result.detail["goal_md_present"] is True
    assert result.detail["archived_count"] == 0
    assert result.detail["backend_id"] == "filesystem"
    # Capability snapshot present and honest for the filesystem reference impl.
    assert result.detail["supports_canonical_export"] is True
    assert result.detail["supports_archive"] is True
    assert result.detail["supports_history_query"] is True


# ──────────────────────────────────────────────────────────────────────────────
# FAIL — unregistered backend (heavy instantiation branch)


def test_fail_when_backend_unregistered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown ATOMIC_AGENTS_GOAL_BACKEND id FAILs at instantiation."""
    agent_root = tmp_path / "agent"
    agent_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ATOMIC_AGENTS_GOAL_BACKEND", "nonexistent-backend")

    result = check_goal_backend(agent_root)

    assert result.status == FAIL
    assert "nonexistent-backend" in result.message
    assert result.detail["backend_id"] == "nonexistent-backend"


def test_fail_does_not_leak_url_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A URL-shaped ATOMIC_AGENTS_GOAL_BACKEND must NOT leak embedded credentials.

    Credential-leak regression (the doctor recomputes raw_backend_id from
    os.environ and must redact it independently of the factory). The full
    user:pass@host value must appear nowhere in the rendered message or detail.
    """
    agent_root = tmp_path / "agent"
    agent_root.mkdir(parents=True, exist_ok=True)
    secret_url = "postgres://user:hunter2@db.internal:5432/goals"
    monkeypatch.setenv("ATOMIC_AGENTS_GOAL_BACKEND", secret_url)

    result = check_goal_backend(agent_root)

    assert result.status == FAIL
    rendered = result.message + " " + str(result.detail)
    assert "user:hunter2" not in rendered, "credentials leaked into doctor output"
    assert "hunter2" not in rendered, "password leaked into doctor output"
    assert "db.internal" not in rendered, "host leaked into doctor output"
    # The redacted scheme is what should surface.
    assert "postgres://..." in result.detail["backend_id"]


# ──────────────────────────────────────────────────────────────────────────────
# FAIL — corrupted goal.md (heavy load_goal probe)


def test_fail_when_goal_md_corrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A present-but-corrupt goal.md FAILs via the heavy load_goal() probe.

    The light list_archived() probe passes (goal_archive/ absent -> []), so this
    asserts the dual-probe pattern's heavy leg actually fires — a single-probe
    check would false-PASS here.
    """
    _clear_goal_env(monkeypatch)
    agent_root = tmp_path / "corrupt-agent"
    agent_root.mkdir(parents=True, exist_ok=True)
    # Wrong schema_version type + unknown sub-goal status -> SchemaValidationError.
    (agent_root / "goal.md").write_text(
        "---\n"
        "schema_version: 999\n"
        "active: true\n"
        "intent: Corrupt goal\n"
        "priority: high\n"
        "created: 2026-06-11\n"
        "last_progress_check: 2026-06-11\n"
        "success_criteria:\n  - done\n"
        "sub_goals:\n  - id: sg1\n    label: x\n    status: frobnicate\n"
        "---\n\nbody\n",
        encoding="utf-8",
    )

    result = check_goal_backend(agent_root)

    assert result.status == FAIL
    assert result.detail["goal_md_present"] is True
    assert "load_goal()" in result.message


# ──────────────────────────────────────────────────────────────────────────────
# run_doctor wiring


def test_check_goal_backend_appears_in_run_doctor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_doctor() includes a 'goal-backend' result for an agent root."""
    _clear_goal_env(monkeypatch)
    agents_root = tmp_path / "agents"
    agent_root = agents_root / "agent"
    _write_goal_md(agent_root)

    results = run_doctor("agent", agents_root, skip_mcp=True)
    names = {r.name for r in results}
    assert "goal-backend" in names


# ──────────────────────────────────────────────────────────────────────────────
# FAIL — lightweight list_archived() probe raises


def test_fail_when_list_archived_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """check_goal_backend() FAILs when the lightweight list_archived() probe raises.

    This exercises the dual-probe step-1 error branch (doctor.py:3743-3753).
    The heavy load_goal() leg is NOT reached; this is a distinct FAIL path from
    the corrupted-goal.md test.

    Strategy: register a stub backend whose list_archived() always raises a
    RuntimeError, set ATOMIC_AGENTS_GOAL_BACKEND to point at it, then confirm
    the doctor renders FAIL with the error type in the message.
    """
    from atomic_agents.goal import (
        register_goal_backend,
        unregister_goal_backend,
    )
    from atomic_agents.goal.filesystem import FilesystemGoalBackend

    agent_root = tmp_path / "agent"
    agent_root.mkdir(parents=True, exist_ok=True)

    class _BrokenListBackend(FilesystemGoalBackend):
        @property
        def backend_id(self) -> str:
            return "broken-list"

        def list_archived(self, agent_id: str) -> list:
            raise RuntimeError("simulated list_archived failure")

    register_goal_backend("broken-list", _BrokenListBackend)
    try:
        monkeypatch.setenv("ATOMIC_AGENTS_GOAL_BACKEND", "broken-list")

        result = check_goal_backend(agent_root)

        assert result.status == FAIL
        assert "list_archived()" in result.message
        assert "RuntimeError" in result.message
        assert result.detail["error_type"] == "RuntimeError"
        assert result.detail["backend_id"] == "broken-list"
    finally:
        unregister_goal_backend("broken-list")
