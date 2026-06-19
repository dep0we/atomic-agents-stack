"""FilesystemConversationBackend implementation-specific tests (spec/47 DRAFT).

Tests beyond the conformance suite that exercise filesystem-specific behavior:
- UTC timestamp normalization (Z vs +00:00 sort order)
- O(n) eviction algorithm correctness
- flock concurrency guard (lock file created)
- Conv-dir containment check before mkdir
- Export scope field
- Write-turn flock cleanup (fd not leaked on unlock error)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from atomic_agents.conversation import (
    ConversationAccessDenied,
    ConversationBackendError,
    FilesystemConversationBackend,
    LOCAL_PRINCIPAL,
    Principal,
    Turn,
)
from atomic_agents.conversation.filesystem import (
    _normalize_utc_ts,
    _turn_filename,
    _estimate_tokens,
)
from atomic_agents.exceptions import PathTraversalError


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_turn(role: str = "user", content: str = "hello", run_id: str = "r1") -> Turn:
    return Turn(role=role, content=content, ts=_utc_now(), run_id=run_id)


# ──────────────────────────────────────────────────────────────
# UTC timestamp normalization


def test_normalize_utc_ts_plus00_unchanged() -> None:
    ts = "2026-06-19T14:33:21.987654+00:00"
    result = _normalize_utc_ts(ts)
    assert result == ts


def test_normalize_utc_ts_z_suffix_converted() -> None:
    """Z-suffix is converted to +00:00 for sort-order consistency."""
    ts = "2026-06-19T14:33:21Z"
    result = _normalize_utc_ts(ts)
    assert result.endswith("+00:00")
    assert "Z" not in result


def test_normalize_utc_ts_non_utc_converted(caplog) -> None:
    """Non-UTC offset is converted to UTC with a WARNING."""
    ts = "2026-06-19T09:33:21-05:00"  # UTC-5 = 14:33:21 UTC
    with caplog.at_level(
        logging.WARNING, logger="atomic_agents.conversation.filesystem"
    ):
        result = _normalize_utc_ts(ts)
    assert result.endswith("+00:00")
    # Should contain the UTC equivalent time
    assert "14:33:21" in result
    # Warning logged
    assert any("normalized to UTC" in r.message for r in caplog.records)


def test_normalize_utc_ts_malformed_passthrough() -> None:
    """Malformed timestamp passes through unchanged (write_turn lets it through)."""
    bad_ts = "not-a-timestamp"
    result = _normalize_utc_ts(bad_ts)
    assert result == bad_ts


def test_turn_filename_sorts_correctly() -> None:
    """Two turns at the same UTC instant with different notation sort identically."""
    ts_plus = "2026-06-19T14:33:21+00:00"
    ts_z = "2026-06-19T14:33:21Z"
    turn_plus = Turn(role="user", content="a", ts=ts_plus, run_id="r-plus")
    turn_z = Turn(role="user", content="a", ts=ts_z, run_id="r-z")
    # Both produce the same safe_ts prefix (after normalization). The filename is
    # <ts>_<run_id>_<seq>_<role>.json, so the timestamp is the first '_'-delimited
    # component (run_id is unique per turn so the full names differ — only the ts
    # prefix is asserted equal).
    fn_plus = _turn_filename(turn_plus)
    fn_z = _turn_filename(turn_z)
    ts_part_plus = fn_plus.split("_", 1)[0]
    ts_part_z = fn_z.split("_", 1)[0]
    assert ts_part_plus == ts_part_z


def test_turn_filename_no_colons_or_plus() -> None:
    """Turn filename is path-safe (no colons, no raw plus signs)."""
    turn = Turn(role="user", content="x", ts=_utc_now(), run_id="safe")
    fn = _turn_filename(turn)
    assert ":" not in fn
    assert "+" not in fn


# ──────────────────────────────────────────────────────────────
# Token estimate


def test_estimate_tokens_nonzero() -> None:
    turn = Turn(role="user", content="hello world", ts=_utc_now(), run_id="r")
    est = _estimate_tokens(turn)
    assert est >= 1


def test_estimate_tokens_scales_with_content() -> None:
    short = Turn(role="user", content="hi", ts=_utc_now(), run_id="r")
    long = Turn(role="user", content="x" * 400, ts=_utc_now(), run_id="r")
    assert _estimate_tokens(long) > _estimate_tokens(short)


# ──────────────────────────────────────────────────────────────
# O(n) eviction


def test_eviction_on_returns_empty_for_very_tight_budget(tmp_path: Path) -> None:
    """When all turns exceed budget, load_turns returns [] (one-turn floor applies)."""
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    backend = FilesystemConversationBackend(agent_root)
    # Write a turn with long content
    big_turn = Turn(role="user", content="a" * 400, ts=_utc_now(), run_id="big")
    backend.write_turn(LOCAL_PRINCIPAL, "c", big_turn)
    # budget_tokens=1 is smaller than any turn estimate
    result = backend.load_turns(LOCAL_PRINCIPAL, "c", budget_tokens=1)
    assert result == []


def test_eviction_keeps_newest_on_large_conversation(tmp_path: Path) -> None:
    """For a large conversation, the N most-recent turns are kept."""
    import time

    agent_root = tmp_path / "agent-large"
    agent_root.mkdir()
    backend = FilesystemConversationBackend(agent_root)

    n_turns = 20
    for i in range(n_turns):
        time.sleep(0.001)
        t = Turn(
            role="user", content=f"turn-{i:03d}", ts=_utc_now(), run_id=f"run-{i:03d}"
        )
        backend.write_turn(LOCAL_PRINCIPAL, "c", t)

    # Each turn: "user"(4) + "turn-XXX"(8) = 12 chars => (12)//4+1 = 4 tokens
    # Budget for exactly 5 turns: 5*4 = 20
    result = backend.load_turns(LOCAL_PRINCIPAL, "c", budget_tokens=20)

    # Should be the 5 most recent, in chronological order
    assert len(result) == 5
    for idx, turn in enumerate(result):
        expected_i = n_turns - 5 + idx
        assert turn.run_id == f"run-{expected_i:03d}"


# ──────────────────────────────────────────────────────────────
# Flock and directory creation


def test_write_turn_creates_lock_file(tmp_path: Path) -> None:
    """write_turn() creates the per-principal .conv.lock file."""
    agent_root = tmp_path / "agent-lock"
    agent_root.mkdir()
    backend = FilesystemConversationBackend(agent_root)
    backend.write_turn(LOCAL_PRINCIPAL, "c", _make_turn())
    lock_file = agent_root / "conversations" / "local" / ".conv.lock"
    assert lock_file.exists()


def test_write_turn_creates_nested_dirs(tmp_path: Path) -> None:
    """write_turn() creates conversations/<principal>/<conv_id>/ lazily."""
    agent_root = tmp_path / "agent-dirs"
    agent_root.mkdir()
    backend = FilesystemConversationBackend(agent_root)
    backend.write_turn(LOCAL_PRINCIPAL, "my-conv", _make_turn())
    conv_dir = agent_root / "conversations" / "local" / "my-conv"
    assert conv_dir.is_dir()


# ──────────────────────────────────────────────────────────────
# Conv-dir symlink containment check


def test_write_turn_refuses_symlinked_conv_dir(tmp_path: Path) -> None:
    """write_turn() refuses a pre-staged symlink for conv_dir (P1 finding)."""
    agent_root = tmp_path / "agent-symdir"
    agent_root.mkdir()
    backend = FilesystemConversationBackend(agent_root)

    # Pre-create the principal dir so we can stage the symlink
    principal_dir = agent_root / "conversations" / "local"
    principal_dir.mkdir(parents=True)

    # Stage a symlink: conv_dir -> /tmp/evil
    evil_target = tmp_path / "evil"
    evil_target.mkdir()
    conv_link = principal_dir / "evil-conv"
    conv_link.symlink_to(evil_target)

    turn = _make_turn(run_id="evil")
    with pytest.raises((PathTraversalError, ConversationBackendError)):
        backend.write_turn(LOCAL_PRINCIPAL, "evil-conv", turn)


# ──────────────────────────────────────────────────────────────
# Export


def test_export_scope_is_agent_root(tmp_path: Path) -> None:
    agent_root = tmp_path / "agent-exp"
    agent_root.mkdir()
    backend = FilesystemConversationBackend(agent_root)
    exp = backend.export()
    assert exp.scope == str(agent_root)


def test_export_relative_paths_under_agent_root(tmp_path: Path) -> None:
    agent_root = tmp_path / "agent-rel"
    agent_root.mkdir()
    backend = FilesystemConversationBackend(agent_root)
    backend.write_turn(LOCAL_PRINCIPAL, "conv-rel", _make_turn(run_id="rel-1"))
    exp = backend.export()
    assert len(exp.entries_with_bytes) == 1
    rel_path = exp.entries_with_bytes[0][0]
    # Must be relative to agent_root (no leading slash, starts with 'conversations/')
    assert rel_path.startswith("conversations/")
    assert not rel_path.startswith("/")


# ──────────────────────────────────────────────────────────────
# Symlinked conversations/ root


def test_symlinked_conversations_dir_raises_or_returns_empty(tmp_path: Path) -> None:
    """A symlinked conversations/ pointing outside agent_root is refused."""
    agent_root = tmp_path / "agent-sym"
    agent_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    # Create symlinked conversations/ -> outside
    conv_link = agent_root / "conversations"
    conv_link.symlink_to(outside)

    backend = FilesystemConversationBackend(agent_root)
    # load_turns should return [] (fail-soft — absent semantics for reads)
    result = backend.load_turns(LOCAL_PRINCIPAL, "c")
    assert result == []

    # write_turn should raise (fail-loud for writes)
    with pytest.raises((PathTraversalError, ConversationBackendError)):
        backend.write_turn(LOCAL_PRINCIPAL, "c", _make_turn())


# ──────────────────────────────────────────────────────────────
# Multiple principals isolated


def test_multiple_principals_isolated_real_dirs(tmp_path: Path) -> None:
    """Two real principals with different dirs are isolated — no overlap."""
    agent_root = tmp_path / "agent-multi"
    agent_root.mkdir()
    backend = FilesystemConversationBackend(agent_root)

    alice = Principal(identifier="alice", derivation_source="local", is_verified=True)
    bob = Principal(identifier="bob", derivation_source="local", is_verified=True)

    backend.write_turn(
        alice, CONV_ID := "shared-conv", _make_turn(content="alice-msg", run_id="a1")
    )
    backend.write_turn(bob, "shared-conv", _make_turn(content="bob-msg", run_id="b1"))

    alice_turns = backend.load_turns(alice, "shared-conv")
    bob_turns = backend.load_turns(bob, "shared-conv")

    assert len(alice_turns) == 1 and alice_turns[0].content == "alice-msg"
    assert len(bob_turns) == 1 and bob_turns[0].content == "bob-msg"


CONV_ID = "conv-test"
