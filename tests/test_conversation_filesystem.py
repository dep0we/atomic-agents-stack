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


# ──────────────────────────────────────────────────────────────
# Issue #557 backfill: _normalize_utc_ts tz-naive WARNING branch


def test_normalize_utc_ts_naive_warns_and_treats_as_utc(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A tz-naive-but-valid ISO timestamp (no offset, no Z) triggers the
    'no timezone; treating as UTC' WARNING and returns +00:00 form.

    This is a DISTINCT branch from test_normalize_utc_ts_non_utc_converted
    which tests a tz-aware non-UTC timestamp (-05:00). The naive branch fires
    at filesystem.py:220-224 when dt.tzinfo is None.

    Negative control: the return value still preserves the time component
    (14:33:21) — naive is not shifted, just pinned to UTC.
    """
    naive_ts = "2026-06-19T14:33:21"  # no offset, no Z

    with caplog.at_level(
        logging.WARNING, logger="atomic_agents.conversation.filesystem"
    ):
        result = _normalize_utc_ts(naive_ts)

    # The result must be a valid UTC +00:00 string.
    assert result.endswith("+00:00"), f"expected +00:00 suffix, got {result!r}"
    # The time component must be preserved (naive treated as-is-in-UTC, not shifted).
    assert "14:33:21" in result, f"time component must be unchanged: {result!r}"
    # The branch-distinctive WARNING must have been emitted.
    assert any("no timezone" in r.message for r in caplog.records), (
        "expected 'no timezone' WARNING for a tz-naive timestamp; got: "
        + str([r.message for r in caplog.records])
    )
    # Ensure the result is parseable and has tzinfo set.
    from datetime import datetime

    parsed = datetime.fromisoformat(result)
    assert parsed.tzinfo is not None, "parsed result must have tzinfo (UTC)"


def test_normalize_utc_ts_naive_negative_utc_aware_no_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Negative control: a UTC-AWARE timestamp must NOT emit the 'no timezone'
    WARNING (branch is conditional on tzinfo is None)."""
    aware_utc = "2026-06-19T14:33:21+00:00"

    with caplog.at_level(
        logging.WARNING, logger="atomic_agents.conversation.filesystem"
    ):
        result = _normalize_utc_ts(aware_utc)

    assert result == aware_utc  # UTC +00:00 passes through unchanged
    assert not any("no timezone" in r.message for r in caplog.records), (
        "UTC-aware timestamp must NOT trigger the naive-ts warning"
    )


# ──────────────────────────────────────────────────────────────
# Issue #557 backfill: ConversationBackendError I/O-error mappings


def test_load_turns_glob_oserror_raises_backend_error(tmp_path: Path) -> None:
    """OSError from conv_dir.glob() → ConversationBackendError (load_turns path).

    Targets filesystem.py line ~588: turn_files = sorted(conv_dir.glob("*.json"))
    Patch glob AFTER the directory exists so the code reaches the glob call.
    """
    agent_root = tmp_path / "agent-glob"
    agent_root.mkdir()
    backend = FilesystemConversationBackend(agent_root)
    # Write a turn so the directory exists (glob is only called on existing dirs).
    backend.write_turn(LOCAL_PRINCIPAL, "c", _make_turn(run_id="seed"))

    # Selective patch: only the conv_dir glob (the named sink, filesystem.py:588)
    # raises; any other Path.glob call passes through to the real method. A
    # blanket side_effect would also go green if the code path grew an EARLIER
    # glob, silently stopping coverage of the intended sink
    # (feedback_layered_except_typed_branch_false_green / Principle #12).
    original_glob = Path.glob

    def _selective_glob(self, *args, **kwargs):
        # conv_dir is conversations/local/c — its name is the conversation_id.
        if self.name == "c" and self.parent.name == LOCAL_PRINCIPAL.identifier:
            raise OSError("glob injected error")
        return original_glob(self, *args, **kwargs)

    with patch.object(Path, "glob", _selective_glob):
        with pytest.raises(ConversationBackendError, match="Failed to list turns"):
            backend.load_turns(LOCAL_PRINCIPAL, "c")


def test_load_turns_enoent_read_text_skips_turn(tmp_path: Path) -> None:
    """TOCTOU: FileNotFoundError(errno=2) from turn_file.read_text() → turn is
    SKIPPED (not raised). The remaining valid turn is still returned.

    This is the ENOENT branch at filesystem.py:663-665 (errno == 2 → continue).
    The non-ENOENT OSError test below has a DISTINCT assertion (pytest.raises)
    to prevent a false-green that swallows the wrong branch.
    """
    import time

    agent_root = tmp_path / "agent-enoent"
    agent_root.mkdir()
    backend = FilesystemConversationBackend(agent_root)
    # Write two turns so there are two turn files to glob.
    backend.write_turn(
        LOCAL_PRINCIPAL,
        "c",
        Turn(role="user", content="t1", ts=_utc_now(), run_id="r1"),
    )
    time.sleep(0.01)
    backend.write_turn(
        LOCAL_PRINCIPAL,
        "c",
        Turn(role="user", content="t2", ts=_utc_now(), run_id="r2"),
    )

    call_count = {"n": 0}
    original_read_text = Path.read_text

    def _selective_fail(self, *args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # Simulate TOCTOU on the first turn file.
            raise FileNotFoundError(2, "No such file or directory")
        return original_read_text(self, *args, **kwargs)

    with patch.object(Path, "read_text", _selective_fail):
        result = backend.load_turns(LOCAL_PRINCIPAL, "c")

    # The ENOENT-failed turn is SKIPPED; the second turn is still returned.
    assert len(result) == 1, (
        f"ENOENT turn must be skipped; expected 1 turn, got {len(result)}"
    )
    assert result[0].content == "t2"


def test_load_turns_non_enoent_oserror_raises_backend_error(tmp_path: Path) -> None:
    """Non-ENOENT OSError from turn_file.read_text() → ConversationBackendError
    raised (the non-ENOENT branch at filesystem.py:666-668).

    Distinct from the ENOENT test above: this asserts pytest.raises, not a list.
    Per feedback_layered_except_typed_branch_false_green: the two tests have
    non-overlapping observable outcomes to prevent false-greens.
    """
    agent_root = tmp_path / "agent-perm"
    agent_root.mkdir()
    backend = FilesystemConversationBackend(agent_root)
    backend.write_turn(
        LOCAL_PRINCIPAL,
        "c",
        Turn(role="user", content="t1", ts=_utc_now(), run_id="r1"),
    )

    with patch.object(
        Path,
        "read_text",
        side_effect=OSError(13, "Permission denied"),  # errno=13, not ENOENT
    ):
        with pytest.raises(ConversationBackendError, match="Failed to read turn file"):
            backend.load_turns(LOCAL_PRINCIPAL, "c")


def test_write_turn_principal_mkdir_oserror_raises_backend_error(
    tmp_path: Path,
) -> None:
    """OSError from principal_dir.mkdir() → ConversationBackendError.

    Targets filesystem.py:750: principal_dir.mkdir(parents=True, exist_ok=True).
    """
    agent_root = tmp_path / "agent-mkdir"
    agent_root.mkdir()
    backend = FilesystemConversationBackend(agent_root)

    original_mkdir = Path.mkdir
    call_count = {"n": 0}

    def _fail_on_first_mkdir(self, *args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise OSError("injected mkdir failure")
        return original_mkdir(self, *args, **kwargs)

    with patch.object(Path, "mkdir", _fail_on_first_mkdir):
        with pytest.raises(
            ConversationBackendError, match="Failed to create principal"
        ):
            backend.write_turn(LOCAL_PRINCIPAL, "c", _make_turn(run_id="r1"))


def test_write_turn_lock_file_open_oserror_raises_backend_error(tmp_path: Path) -> None:
    """OSError from open(lock_file, 'w') → ConversationBackendError.

    Targets filesystem.py:757: lock_fd = open(lock_file, 'w').
    The principal_dir must already exist so that mkdir succeeds and we
    reach the lock-file open.
    """
    import builtins

    agent_root = tmp_path / "agent-lockopen"
    agent_root.mkdir()
    backend = FilesystemConversationBackend(agent_root)
    # Pre-create the principal directory so mkdir is a no-op and we reach open().
    principal_dir = agent_root / "conversations" / "local"
    principal_dir.mkdir(parents=True, exist_ok=True)

    original_open = builtins.open
    call_count = {"n": 0}

    def _fail_lock_open(file, mode="r", *args, **kwargs):
        if mode == "w" and str(file).endswith(".conv.lock"):
            call_count["n"] += 1
            raise OSError("injected lock-open failure")
        return original_open(file, mode, *args, **kwargs)

    with patch("builtins.open", side_effect=_fail_lock_open):
        with pytest.raises(
            ConversationBackendError, match="Failed to open per-principal lock"
        ):
            backend.write_turn(LOCAL_PRINCIPAL, "c", _make_turn(run_id="r1"))

    assert call_count["n"] >= 1, "the lock-open path must have been exercised"


def test_write_turn_atomic_write_oserror_raises_backend_error(tmp_path: Path) -> None:
    """OSError from atomic_write() → ConversationBackendError.

    Patches atomic_agents.conversation.filesystem.atomic_write (the imported
    name in the module), NOT atomic_agents._io.atomic_write (the definition
    site). Patching the wrong target leaves the real atomic_write running and
    the error is never injected.
    """
    agent_root = tmp_path / "agent-atomicwrite"
    agent_root.mkdir()
    backend = FilesystemConversationBackend(agent_root)

    with patch(
        "atomic_agents.conversation.filesystem.atomic_write",
        side_effect=OSError("injected disk full"),
    ):
        with pytest.raises(ConversationBackendError, match="Failed to write turn"):
            backend.write_turn(LOCAL_PRINCIPAL, "c", _make_turn(run_id="r1"))


def test_export_iterdir_oserror_raises_backend_error(tmp_path: Path) -> None:
    """OSError from conv_root.iterdir() during export() → ConversationBackendError.

    Targets filesystem.py:874: principal_dirs = sorted(p for p in conv_root.iterdir() ...).
    The conversations/ dir must exist so the code reaches iterdir().

    Note: the issue brief listed 'os.scandir' as a sink. However,
    os.scandir in this file is called inside _ondisk_principal_name()
    (line 493) and maps to PathTraversalError, NOT ConversationBackendError.
    The ConversationBackendError-raising iterdir sink is in export() at
    line 874. That distinction is documented here per prep finding P1.
    """
    agent_root = tmp_path / "agent-export"
    agent_root.mkdir()
    backend = FilesystemConversationBackend(agent_root)
    # Write a turn so conversations/ exists (iterdir is skipped on absent root).
    backend.write_turn(LOCAL_PRINCIPAL, "c", _make_turn(run_id="r1"))

    # Selective patch: only the conv_root iterdir (the named sink,
    # filesystem.py:874) raises; other Path.iterdir calls pass through. A
    # blanket side_effect would go green for an unrelated earlier iterdir
    # (feedback_layered_except_typed_branch_false_green / Principle #12).
    original_iterdir = Path.iterdir

    def _selective_iterdir(self, *args, **kwargs):
        if self.name == "conversations":
            raise OSError("injected iterdir failure")
        return original_iterdir(self, *args, **kwargs)

    with patch.object(Path, "iterdir", _selective_iterdir):
        with pytest.raises(
            ConversationBackendError, match="Failed to enumerate conversations"
        ):
            backend.export()


def test_ondisk_principal_name_scandir_oserror_raises_path_traversal(
    tmp_path: Path,
) -> None:
    """OSError from os.scandir() inside _ondisk_principal_name() → PathTraversalError.

    Per prep finding P1: os.scandir in this module maps to PathTraversalError
    (not ConversationBackendError). This test pins the CORRECT exception type
    for that sink so a refactor that swaps the exception type goes RED.
    """
    agent_root = tmp_path / "agent-scandir"
    agent_root.mkdir()
    backend = FilesystemConversationBackend(agent_root)
    # Pre-create conversations/local so the stat() call in _ondisk_principal_name
    # succeeds (the scandir call is what we are patching).
    conversations_root = agent_root / "conversations"
    conversations_root.mkdir()
    principal_dir = conversations_root / "local"
    principal_dir.mkdir()

    with patch("os.scandir", side_effect=OSError("injected scandir failure")):
        with pytest.raises(PathTraversalError):
            backend._ondisk_principal_name(conversations_root, "local")


# ──────────────────────────────────────────────────────────────
# Issue #557 backfill: conversation/__init__.py registry helpers


@pytest.fixture()
def _isolated_conv_registry():
    """Snapshot and restore atomic_agents.conversation._registry AND the
    ATOMIC_AGENTS_CONVERSATION_BACKEND env var around each registry-helper test
    so register/unregister/list calls and env-driven dispatch don't leak between
    tests (mirrors test_judge_types_and_registry._isolate_registry pattern).

    'filesystem' is registered at import time; without this fixture a test
    that calls unregister_conversation_backend('filesystem') would corrupt
    the registry for all subsequent tests in the session.

    The env var is owned here too (Principle #8 — the fixture isolates ALL the
    shared process state it touches, not just the registry dict): the
    get_default_* tests set it, and a leak would silently change
    get_default_conversation_backend() for every later agent construction.
    """
    import atomic_agents.conversation as _conv_module

    saved = dict(_conv_module._registry)
    _saved_env = os.environ.get("ATOMIC_AGENTS_CONVERSATION_BACKEND")
    try:
        yield
    finally:
        _conv_module._registry.clear()
        _conv_module._registry.update(saved)
        if _saved_env is None:
            os.environ.pop("ATOMIC_AGENTS_CONVERSATION_BACKEND", None)
        else:
            os.environ["ATOMIC_AGENTS_CONVERSATION_BACKEND"] = _saved_env


def test_list_conversation_backends_returns_lexicographic(
    _isolated_conv_registry,
) -> None:
    """list_conversation_backends() returns ids in sorted order."""
    from atomic_agents.conversation import (
        list_conversation_backends,
        register_conversation_backend,
    )

    register_conversation_backend("zzz-last", FilesystemConversationBackend)
    register_conversation_backend("aaa-first", FilesystemConversationBackend)
    ids = list_conversation_backends()
    assert ids == sorted(ids), "registry list must be lexicographically sorted"
    assert "filesystem" in ids
    assert "aaa-first" in ids
    assert "zzz-last" in ids


def test_unregister_conversation_backend_removes_entry(
    _isolated_conv_registry,
) -> None:
    """unregister_conversation_backend() removes the entry; subsequent call to
    get_conversation_backend raises BackendNotRegistered."""
    from atomic_agents.exceptions import BackendNotRegistered
    from atomic_agents.conversation import (
        get_conversation_backend,
        register_conversation_backend,
        unregister_conversation_backend,
    )

    register_conversation_backend("tmp-backend", FilesystemConversationBackend)
    assert get_conversation_backend("tmp-backend") is FilesystemConversationBackend

    unregister_conversation_backend("tmp-backend")
    with pytest.raises(BackendNotRegistered):
        get_conversation_backend("tmp-backend")


def test_unregister_conversation_backend_noop_when_absent(
    _isolated_conv_registry,
) -> None:
    """unregister_conversation_backend() is a no-op when the id is not registered."""
    from atomic_agents.conversation import unregister_conversation_backend

    # Must not raise.
    unregister_conversation_backend("nonexistent-backend-id")


def test_get_conversation_backend_raises_backend_not_registered(
    _isolated_conv_registry,
) -> None:
    """get_conversation_backend() raises BackendNotRegistered for an unknown id."""
    from atomic_agents.exceptions import BackendNotRegistered
    from atomic_agents.conversation import get_conversation_backend

    with pytest.raises(BackendNotRegistered, match="unknown-backend-xyz"):
        get_conversation_backend("unknown-backend-xyz")


def test_register_conversation_backend_reregister_logs_debug(
    _isolated_conv_registry, caplog: pytest.LogCaptureFixture
) -> None:
    """Re-registering an existing backend_id logs at DEBUG (re-register is
    intentional for operator wrapper swaps)."""
    from atomic_agents.conversation import register_conversation_backend

    register_conversation_backend("filesystem", FilesystemConversationBackend)
    # Re-register the same id.
    with caplog.at_level(logging.DEBUG, logger="atomic_agents.conversation"):
        register_conversation_backend("filesystem", FilesystemConversationBackend)

    assert any(
        "replacing registered conversation backend" in r.message
        and "filesystem" in r.message
        for r in caplog.records
    ), "expected DEBUG log on re-registration; got: " + str(
        [r.message for r in caplog.records]
    )


def test_get_default_conversation_backend_registry_dispatch(
    tmp_path: Path,
    _isolated_conv_registry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_default_conversation_backend() dispatches through the registry for
    a custom (non-'filesystem') backend_id set via env var.

    This tests the registry-dispatch path (NOT the 'filesystem' fast-path that
    a test using 'filesystem' would silently exercise only — prep finding P2).
    monkeypatch.setenv auto-restores the env var on teardown even if the body
    raises, so there is no manual del and no dead try/except wrapper.
    """
    from atomic_agents.conversation import (
        get_default_conversation_backend,
        register_conversation_backend,
    )

    class _StubBackend(FilesystemConversationBackend):
        backend_id = "_test_custom"

    register_conversation_backend("_test_custom", _StubBackend)

    monkeypatch.setenv("ATOMIC_AGENTS_CONVERSATION_BACKEND", "_test_custom")
    result = get_default_conversation_backend(tmp_path)

    assert isinstance(result, _StubBackend), (
        "registry-dispatch path must instantiate the registered class"
    )


def test_get_default_conversation_backend_failfast_unknown_id(
    tmp_path: Path,
    _isolated_conv_registry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_default_conversation_backend() raises BackendNotRegistered for an
    unknown env var value (fail-fast path)."""
    from atomic_agents.exceptions import BackendNotRegistered
    from atomic_agents.conversation import get_default_conversation_backend

    monkeypatch.setenv(
        "ATOMIC_AGENTS_CONVERSATION_BACKEND", "completely-unknown-backend"
    )
    with pytest.raises(BackendNotRegistered):
        get_default_conversation_backend(tmp_path)


def test_redact_for_error_message_url_scheme() -> None:
    """URL-scheme value is redacted to 'scheme://...'."""
    from atomic_agents.conversation import _redact_for_error_message

    result = _redact_for_error_message("postgres://user:pw@host/db")
    assert result == "postgres://..."


def test_redact_for_error_message_schemeless_dsn() -> None:
    """Schemeless DSN (user:pass@host/db) is fully redacted."""
    from atomic_agents.conversation import _redact_for_error_message

    result = _redact_for_error_message("user:pw@host/db")
    assert result == "[redacted-connection-string]"


def test_redact_for_error_message_truncation() -> None:
    """Value exceeding max_len is truncated with '...'."""
    from atomic_agents.conversation import _redact_for_error_message

    long_val = "x" * 40
    result = _redact_for_error_message(long_val)
    assert result.endswith("...")
    # Default max_len=32 → result is 32 chars + '...' = 35 chars.
    assert len(result) <= 35
    # Negative control: pin that the TRUNCATION branch produced this — NOT the
    # schemeless-DSN redaction branch. A regression that fired DSN redaction too
    # eagerly on a long plain value would still endswith('...'), so assert the
    # output is the truncated original prefix, not the redaction sentinel.
    assert result != "[redacted-connection-string]"
    assert result.startswith("xxxx")


def test_redact_for_error_message_short_safe_passthrough() -> None:
    """Short, safe value passes through unchanged."""
    from atomic_agents.conversation import _redact_for_error_message

    result = _redact_for_error_message("filesystem")
    assert result == "filesystem"
