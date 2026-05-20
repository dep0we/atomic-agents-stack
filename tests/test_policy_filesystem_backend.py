"""Filesystem-specific tests for ``FilesystemPolicyBackend``.

Conformance tests in ``test_policy_protocol_conformance.py`` exercise the
Protocol contract that every backend must satisfy.  THIS module exercises
filesystem-specific behavior: lazy parsing on first call, mtime+size-based
cache invalidation, concurrent-parse idempotence, the URL factory, and the
malformed-YAML lazy-raise guarantee.

The conformance suite already covers Protocol-shaped invariants (agent_name
validation, default-open behavior, capability surface) across both backends.
This module covers what is filesystem-only:

* Construction is side-effect-free — ``os.stat`` is never called during
  ``__init__`` (F4 / spec/32 MUST #4).
* Lazy parse — ``policy.md`` written AFTER construction is still seen on
  the first method call.
* mtime-cache hit — identical mtime+size tuple skips re-parse.
* mtime-cache invalidation — mtime bump triggers re-parse.
* size-change invalidation — same mtime, different size invalidates (F8).
* Concurrent parse idempotence — 5 threads calling ``get_effective_caps``
  simultaneously after an mtime bump return identical results, no exceptions
  (F10).
* Malformed YAML raises ``PolicyInvalid`` on the FIRST method call (not at
  construction time) — construction succeeds, lazy parse raises.
* URL factory — ``filesystem:///path`` resolves correctly; other schemes
  raise ``ValueError`` with credentials redacted from the error message.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from atomic_agents.policy.types import (
    CostCaps,
    PolicyInvalid,
)

# FilesystemPolicyBackend and make_filesystem_policy_backend_from_url are
# imported inside each test so the test file can be collected even if
# filesystem.py has not yet been written by sibling Lane B.  The ImportError
# would only surface at runtime (when the test actually runs), not at
# collection time.


# ─────────────────────────────────────────────────────────────────────────────
# Helpers


def _write_policy(directory: Path, content: str) -> None:
    """Write ``policy.md`` in ``directory`` with ``content``."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "policy.md").write_text(content, encoding="utf-8")


_SIMPLE_CAPS_POLICY = """\
cost_caps:
  daily_usd: 42.0
"""

_UPDATED_CAPS_POLICY = """\
cost_caps:
  daily_usd: 99.0
"""

_MALFORMED_YAML_POLICY = """\
cost_caps:
  daily_usd: [this is not a scalar
  monthly_usd: also broken::: yaml
"""

_EMPTY_AGENT_BODY_POLICY = """\
cost_caps:
  daily_usd: 77.0

agents:
  foo: {}
"""

_MERGE_SEMANTICS_POLICY = """\
cost_caps:
  daily_usd: 50.0

tools:
  allow:
    - tool_a
  deny:
    - tool_b

mcp_servers:
  allow:
    - server_x

model: claude-opus-4-5

agents:
  my_agent:
    cost_caps:
      daily_usd: 30.0
    tools:
      allow:
        - tool_c
      deny:
        - tool_d
    mcp_servers:
      allow:
        - server_y
    model: gpt-4o
"""


# ─────────────────────────────────────────────────────────────────────────────
# Test 1 — Constructor accepts non-existent path (F4)


def test_constructor_accepts_nonexistent_path(tmp_path: Path) -> None:
    """``FilesystemPolicyBackend(non_existent_path)`` succeeds.

    The directory does not need to exist at construction time.  Side-effect-
    free construction is load-bearing for test-fixture ergonomics (tests can
    arrange the filesystem AFTER construction, per the lazy-parse guarantee).
    """
    from atomic_agents.policy.filesystem import FilesystemPolicyBackend

    non_existent = tmp_path / "does-not-exist"
    assert not non_existent.exists()
    backend = FilesystemPolicyBackend(non_existent)
    assert backend is not None


# ─────────────────────────────────────────────────────────────────────────────
# Test 2 — Constructor does NOT call os.stat


def test_constructor_does_not_stat_filesystem(tmp_path: Path) -> None:
    """``FilesystemPolicyBackend.__init__`` MUST NOT call ``os.stat``.

    Monkeypatching ``os.stat`` to raise ensures that any stat call during
    construction is caught.  If the constructor is truly side-effect-free
    (F4 / spec/32 MUST #4) the patch never triggers and no exception is
    raised.
    """
    from atomic_agents.policy.filesystem import FilesystemPolicyBackend

    def _stat_must_not_be_called(*args, **kwargs):
        raise AssertionError(
            "FilesystemPolicyBackend.__init__ called os.stat — "
            "construction must be side-effect-free (spec/32 MUST #4)"
        )

    with patch("os.stat", side_effect=_stat_must_not_be_called):
        # Must not raise — even with a stat-refusing patch in place
        backend = FilesystemPolicyBackend(tmp_path / "any-path")

    assert backend is not None


# ─────────────────────────────────────────────────────────────────────────────
# Test 3 — First method call lazy-parses policy.md written after construction


def test_first_method_call_lazy_parses(tmp_path: Path) -> None:
    """``policy.md`` written AFTER backend construction is seen on the first
    call to any query method.

    This confirms the lazy-parse guarantee: the backend does not snapshot the
    filesystem state at construction time, but instead reads ``policy.md`` on
    demand.
    """
    from atomic_agents.policy.filesystem import FilesystemPolicyBackend

    project_root = tmp_path / "project"
    project_root.mkdir()
    # Construct BEFORE writing policy.md
    backend = FilesystemPolicyBackend(project_root)

    # Write policy.md AFTER construction
    _write_policy(project_root, _SIMPLE_CAPS_POLICY)

    # First call must parse and return the written value
    caps = backend.get_effective_caps("any_agent")
    assert caps.daily_usd == 42.0


# ─────────────────────────────────────────────────────────────────────────────
# Test 4 — mtime-cache HIT skips re-parse


def test_mtime_cache_hit_skips_parse(tmp_path: Path, monkeypatch) -> None:
    """Identical mtime+size tuple → cached snapshot is returned without
    re-parsing ``policy.md``.

    After the first successful parse the backend caches the snapshot keyed on
    ``(mtime_ns, st_size)``.  A second call with the same on-disk state MUST
    return the cached value.  We verify this by monkeypatching the
    ``parse_policy_md`` function to raise after the first successful call —
    any re-parse attempt would blow up, proving the cache was used.
    """
    # parse_policy_md is imported lazily inside _get_snapshot (avoids the
    # policy_md → policy package init → backend bootstrap → filesystem →
    # policy_md cycle), so the monkeypatch target is the policy_md module
    # itself, not the filesystem module.
    from atomic_agents import policy_md as policy_md_module
    from atomic_agents.policy.filesystem import FilesystemPolicyBackend

    project_root = tmp_path / "project"
    _write_policy(project_root, _SIMPLE_CAPS_POLICY)

    backend = FilesystemPolicyBackend(project_root)

    # First call — cold parse
    caps_first = backend.get_effective_caps("any_agent")
    assert caps_first.daily_usd == 42.0

    # Poison the parser so any re-parse would raise
    def _raise_on_parse(*args, **kwargs):
        raise AssertionError("parse_policy_md called on cache HIT — should use cache")

    monkeypatch.setattr(policy_md_module, "parse_policy_md", _raise_on_parse)

    # Second call — same mtime+size, MUST use cache (no re-parse)
    caps_second = backend.get_effective_caps("any_agent")
    assert caps_second.daily_usd == 42.0


# ─────────────────────────────────────────────────────────────────────────────
# Test 5 — mtime-cache INVALIDATES on mtime change


def test_mtime_cache_invalidates_on_mtime_change(tmp_path: Path) -> None:
    """Editing ``policy.md`` (which bumps mtime) invalidates the cache; the
    second call returns the updated value."""
    from atomic_agents.policy.filesystem import FilesystemPolicyBackend

    project_root = tmp_path / "project"
    _write_policy(project_root, _SIMPLE_CAPS_POLICY)
    backend = FilesystemPolicyBackend(project_root)

    caps_before = backend.get_effective_caps("any_agent")
    assert caps_before.daily_usd == 42.0

    # Small sleep to guarantee mtime advances on filesystems with 1-second
    # granularity.  On modern macOS/Linux mtime_ns has sub-second resolution,
    # but we sleep 10 ms defensively.
    time.sleep(0.01)
    _write_policy(project_root, _UPDATED_CAPS_POLICY)

    caps_after = backend.get_effective_caps("any_agent")
    assert caps_after.daily_usd == 99.0


# ─────────────────────────────────────────────────────────────────────────────
# Test 6 — size-change invalidates even when mtime is same (F8)


def test_size_change_invalidates_even_when_mtime_same(tmp_path: Path) -> None:
    """Same mtime but different file size → cache is invalidated.

    The cache key is ``(mtime_ns, st_size)`` (F8).  We force the mtime back
    to the original value via ``os.utime`` after writing new content, leaving
    only the size difference to trigger the invalidation.
    """
    from atomic_agents.policy.filesystem import FilesystemPolicyBackend

    project_root = tmp_path / "project"
    policy_path = project_root / "policy.md"
    _write_policy(project_root, _SIMPLE_CAPS_POLICY)

    backend = FilesystemPolicyBackend(project_root)
    caps_before = backend.get_effective_caps("any_agent")
    assert caps_before.daily_usd == 42.0

    # Capture the original mtime
    original_stat = os.stat(policy_path)
    original_mtime = (original_stat.st_atime, original_stat.st_mtime)

    # Write new content — different size
    policy_path.write_text(_UPDATED_CAPS_POLICY, encoding="utf-8")

    # Force mtime BACK to original value so only size differs
    os.utime(policy_path, original_mtime)

    # Cache key = (mtime_ns, st_size); size changed → cache MUST invalidate
    caps_after = backend.get_effective_caps("any_agent")
    assert caps_after.daily_usd == 99.0, (
        "Cache was not invalidated when size changed with same mtime — "
        "cache key must include st_size (F8)"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test 7 — Concurrent parse after mtime bump is idempotent (F10)


def test_concurrent_parse_after_mtime_bump_is_idempotent(tmp_path: Path) -> None:
    """5 threads calling ``get_effective_caps`` simultaneously after an mtime
    bump all return identical ``CostCaps``; no exceptions are raised (F10).

    This verifies that the lazy-parse + cache-update path is thread-safe:
    either a parse lock serialises the parse (one thread parses, the rest wait
    and read the cached result) or the parse is cheap and idempotent enough
    that concurrent parses produce the same snapshot.  Either implementation
    is acceptable — the test asserts only on observable outcomes.
    """
    from atomic_agents.policy.filesystem import FilesystemPolicyBackend

    project_root = tmp_path / "project"
    _write_policy(project_root, _SIMPLE_CAPS_POLICY)
    backend = FilesystemPolicyBackend(project_root)

    # Warm the cache with the original content
    _ = backend.get_effective_caps("any_agent")

    # Update policy.md to bump mtime → all threads will see a stale cache
    time.sleep(0.01)
    _write_policy(project_root, _UPDATED_CAPS_POLICY)

    results: list[CostCaps] = []
    errors: list[Exception] = []

    def _call_backend() -> None:
        try:
            caps = backend.get_effective_caps("any_agent")
            results.append(caps)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_call_backend) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    assert not errors, f"Concurrent parse raised exceptions: {errors}"
    assert len(results) == 5, "Not all threads completed"

    # All results must be identical
    daily_values = {c.daily_usd for c in results}
    assert len(daily_values) == 1, (
        f"Concurrent parse returned non-identical CostCaps: {daily_values}"
    )
    assert results[0].daily_usd == 99.0


# ─────────────────────────────────────────────────────────────────────────────
# Test 8 — Malformed policy.md raises PolicyInvalid on first call


def test_malformed_policy_md_raises_PolicyInvalid_on_first_call(
    tmp_path: Path,
) -> None:
    """Malformed YAML in ``policy.md`` → construction succeeds (lazy-parse),
    first method call raises ``PolicyInvalid``.

    This verifies both sides of the lazy-parse guarantee:
    1. Construction must NOT parse — it must succeed even with bad YAML on disk.
    2. The first method call MUST raise ``PolicyInvalid`` (not a raw
       YAML parse error or an unhandled exception).
    """
    from atomic_agents.policy.filesystem import FilesystemPolicyBackend

    project_root = tmp_path / "project"
    _write_policy(project_root, _MALFORMED_YAML_POLICY)

    # Construction must succeed — lazy parse means bad YAML is not read yet
    backend = FilesystemPolicyBackend(project_root)

    # First call must raise PolicyInvalid (not raw YAMLError or any other)
    with pytest.raises(PolicyInvalid):
        backend.get_effective_caps("any_agent")


# ─────────────────────────────────────────────────────────────────────────────
# Test 9 — URL factory accepts filesystem:// scheme


def test_url_factory_filesystem_scheme(tmp_path: Path) -> None:
    """``make_filesystem_policy_backend_from_url("filesystem:///path")``
    returns a ``FilesystemPolicyBackend`` instance.  A non-existent path is
    accepted (construction is side-effect-free)."""
    from atomic_agents.policy.filesystem import (
        FilesystemPolicyBackend,
        make_filesystem_policy_backend_from_url,
    )

    non_existent = tmp_path / "ghost-project"
    assert not non_existent.exists()

    url = f"filesystem://{non_existent}"
    backend = make_filesystem_policy_backend_from_url(url)

    assert isinstance(backend, FilesystemPolicyBackend)


# ─────────────────────────────────────────────────────────────────────────────
# Test 10 — URL factory refuses non-filesystem scheme; credential is redacted


def test_url_factory_refuses_other_scheme_with_credential_redaction(
    tmp_path: Path,
) -> None:
    """``make_filesystem_policy_backend_from_url("postgres://user:pass@host/db")``
    raises ``ValueError``.

    The error message MUST NOT contain the literal ``"pass"`` string —
    credentials embedded in accidentally-pasted database URLs must not appear
    in error output (spec/32 MUST #6 — URL credential redaction at factory
    ``ValueError`` sites).
    """
    from atomic_agents.policy.filesystem import make_filesystem_policy_backend_from_url

    bad_url = "postgres://user:pass@host/db"

    with pytest.raises(ValueError) as exc_info:
        make_filesystem_policy_backend_from_url(bad_url)

    error_message = str(exc_info.value)
    assert "pass" not in error_message, (
        f"Credential 'pass' leaked into the error message: {error_message!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Bonus: per-agent merge semantics (filesystem-only, verifying F7 parse output)


def test_per_agent_merge_semantics_cost_cap_min(tmp_path: Path) -> None:
    """Fleet daily=50, agent daily=30 → effective daily=30 (agent stricter).
    Fleet daily=50, agent daily=80 → effective daily=50 (fleet stricter).
    Verifies the filesystem parser wires the MIN composition correctly.
    """
    from atomic_agents.policy.filesystem import FilesystemPolicyBackend

    project_root = tmp_path / "project"
    _write_policy(project_root, _MERGE_SEMANTICS_POLICY)
    backend = FilesystemPolicyBackend(project_root)

    # my_agent has cost_caps.daily_usd=30 in the policy; fleet is 50 → MIN=30
    caps = backend.get_effective_caps("my_agent")
    assert caps.daily_usd == 30.0

    # unmentioned_agent inherits fleet daily=50
    caps_fleet = backend.get_effective_caps("unmentioned_agent")
    assert caps_fleet.daily_usd == 50.0


def test_per_agent_tool_merge_deny_takes_precedence(tmp_path: Path) -> None:
    """Fleet allow=[tool_a], deny=[tool_b] + agent allow=[tool_c], deny=[tool_d].
    Effective allow for my_agent = {tool_a, tool_c}.
    Effective deny for my_agent  = {tool_b, tool_d}.
    tool_a → allowed (in merged allow).
    tool_b → denied  (in merged deny).
    tool_c → allowed (in merged allow via agent).
    tool_d → denied  (in merged deny via agent).
    tool_e → denied  (effective_allow is non-empty; tool_e not in it).
    """
    from atomic_agents.policy.filesystem import FilesystemPolicyBackend

    project_root = tmp_path / "project"
    _write_policy(project_root, _MERGE_SEMANTICS_POLICY)
    backend = FilesystemPolicyBackend(project_root)

    assert backend.is_tool_allowed("my_agent", "tool_a") is True
    assert backend.is_tool_allowed("my_agent", "tool_b") is False
    assert backend.is_tool_allowed("my_agent", "tool_c") is True
    assert backend.is_tool_allowed("my_agent", "tool_d") is False
    assert backend.is_tool_allowed("my_agent", "tool_e") is False


def test_per_agent_mcp_merge(tmp_path: Path) -> None:
    """Fleet mcp allow=[server_x] + agent allow=[server_y] →
    both allowed for my_agent; server_z is denied (non-empty merged allow)."""
    from atomic_agents.policy.filesystem import FilesystemPolicyBackend

    project_root = tmp_path / "project"
    _write_policy(project_root, _MERGE_SEMANTICS_POLICY)
    backend = FilesystemPolicyBackend(project_root)

    assert backend.is_mcp_server_allowed("my_agent", "server_x") is True
    assert backend.is_mcp_server_allowed("my_agent", "server_y") is True
    assert backend.is_mcp_server_allowed("my_agent", "server_z") is False


def test_per_agent_model_replace(tmp_path: Path) -> None:
    """Fleet model=claude-opus-4-5; my_agent model=gpt-4o.
    Model is REPLACE — agent value entirely replaces fleet value."""
    from atomic_agents.policy.filesystem import FilesystemPolicyBackend

    project_root = tmp_path / "project"
    _write_policy(project_root, _MERGE_SEMANTICS_POLICY)
    backend = FilesystemPolicyBackend(project_root)

    assert backend.get_effective_model("my_agent") == "gpt-4o"
    assert backend.get_effective_model("unmentioned_agent") == "claude-opus-4-5"


def test_oversized_policy_md_raises_PolicyInvalid(tmp_path: Path) -> None:
    """policy.md exceeding ``MAX_POLICY_MD_BYTES`` raises ``PolicyInvalid``.

    Defends against YAML alias-bomb / billion-laughs DoS — a small policy.md
    can expand to multi-GB RSS under ``yaml.safe_load`` without a size bound.
    Mirror of the spec/25 PR 1 lesson (ToolRegistry hit 33 GB RSS pre-fix).
    Round 2 of Step 11 Opus adversarial caught the missing regression test.
    """
    from atomic_agents.policy.filesystem import FilesystemPolicyBackend
    from atomic_agents.policy.types import PolicyInvalid
    from atomic_agents.policy_md import MAX_POLICY_MD_BYTES

    project_root = tmp_path / "project"
    project_root.mkdir()
    # cap + 1 byte — must raise
    (project_root / "policy.md").write_bytes(b"a" * (MAX_POLICY_MD_BYTES + 1))
    backend = FilesystemPolicyBackend(project_root)
    with pytest.raises(PolicyInvalid, match=r"exceeds the \d+-byte"):
        backend.get_effective_caps("any_agent")


def test_policy_md_at_exactly_cap_does_not_raise(tmp_path: Path) -> None:
    """policy.md at exactly ``MAX_POLICY_MD_BYTES`` parses normally.

    Pins the boundary condition: the cap check is strict ``>``, not ``>=``.
    Round 2 of Step 11 Opus adversarial flagged the boundary as needing a
    regression test so a future refactor swapping ``>`` for ``>=`` would
    fail CI loudly.
    """
    from atomic_agents.policy.filesystem import FilesystemPolicyBackend
    from atomic_agents.policy_md import MAX_POLICY_MD_BYTES

    project_root = tmp_path / "project"
    project_root.mkdir()
    # Build a valid YAML document that totals exactly MAX_POLICY_MD_BYTES
    header = b"cost_caps:\n  daily_usd: 5.0\n# pad: "
    padding = b"x" * (MAX_POLICY_MD_BYTES - len(header) - 1) + b"\n"
    content = header + padding
    assert len(content) == MAX_POLICY_MD_BYTES, "test fixture must be exactly at cap"
    (project_root / "policy.md").write_bytes(content)
    backend = FilesystemPolicyBackend(project_root)
    caps = backend.get_effective_caps("any_agent")
    assert caps.daily_usd == 5.0


def test_empty_agents_body_means_no_override(tmp_path: Path) -> None:
    """``agents: { foo: {} }`` → fleet defaults apply to agent ``foo`` unchanged.

    An empty per-agent dict must not silently zero out fleet caps.  This is
    the F12 requirement pinned to the filesystem parser specifically.
    """
    from atomic_agents.policy.filesystem import FilesystemPolicyBackend

    project_root = tmp_path / "project"
    _write_policy(project_root, _EMPTY_AGENT_BODY_POLICY)
    backend = FilesystemPolicyBackend(project_root)

    caps = backend.get_effective_caps("foo")
    assert caps.daily_usd == 77.0, (
        "Empty per-agent body zeroed out fleet caps — must return fleet default (F12)"
    )
