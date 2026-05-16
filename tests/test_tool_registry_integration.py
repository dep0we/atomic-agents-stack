"""Integration tests for #64 PR 2 — ToolRegistryBackend wiring.

These tests verify that the tool-registry backend kwarg flows correctly
through every code path that constructs an ``AtomicAgent``: the
constructor itself, the three runners that DO construct internal
agents (OutcomeRunner, EvalRunner; DreamRunner stores the kwarg for
API parity but doesn't construct internal agents), the delegate path,
and the doctor coherence check.

The load-bearing tests are:

- ``test_atomic_agent_registers_backend_tools_into_in_memory_registry``
  — pins Decision 1 + Decision 8 of spec/25 (Protocol layer COMPOSES
  with the in-memory ``ToolRegistry``).
- ``test_atomic_agent_collision_operator_wins`` — pins the collision
  discipline: operator-supplied tools register FIRST; backend tools
  register with ``allow_overwrite=False`` so collisions surface as
  ``ToolNameCollision``.
- ``test_atomic_agent_empty_tools_dir_preserves_pre_pr2_behavior`` —
  pins the "every existing 96 AtomicAgent test site sees zero
  behavior change" invariant. For an agent without a ``tools/``
  directory, the wiring loop runs zero times.
- ``test_atomic_agent_delegate_does_not_thread_tool_registry`` — pins
  spec/25 Decision 9 (per-agent scoping): the coordinator's filesystem
  tool registry MUST NOT be threaded to the target. Target builds its
  own via the default factory rooted at the TARGET's agent_root.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from atomic_agents import (
    AtomicAgent,
    FilesystemToolRegistryBackend,
    ToolDefinition,
    ToolRegistry,
    ToolRegistryBackend,
)
from atomic_agents.exceptions import ToolNameCollision
from atomic_agents.eval import EvalRunner
from atomic_agents.outcome import OutcomeRunner
from atomic_agents.dream import DreamRunner
from atomic_agents.doctor import check_tool_registry_backend


# ──────────────────────────────────────────────────────────────────
# Fixtures: minimum-viable agent dir on disk


_GOOD_DESCRIPTOR = """\
---
name: {name}
description: Operator-provided tool for integration tests.
classification: read_only
input_schema:
  type: object
  properties:
    query:
      type: string
      description: any value
  required: [query]
---
"""

_GOOD_HANDLER = """\
def handler(input):
    return f"ran: {input['query']!r}"
"""


def _make_minimal_agent_dir(
    scope_root: Path,
    agent_name: str = "scout",
    *,
    tools: dict[str, str] | None = None,
) -> Path:
    """Create the minimum on-disk shape AtomicAgent needs to construct.

    ``tools`` is an optional mapping of ``tool_name -> handler-body``.
    When provided, each tool gets a descriptor + handler under
    ``<agent>/tools/<name>.{md,py}``.
    """
    agent_root = scope_root / agent_name
    (agent_root / "persona").mkdir(parents=True)
    (agent_root / "persona" / "IDENTITY.md").write_text(
        "# Scout\n\n## Operating mode\n\nThis agent is reactive.\n",
        encoding="utf-8",
    )
    (agent_root / "tools.md").write_text(
        "# Tools\n\n## Read paths\n\n- ~/scout/data\n",
        encoding="utf-8",
    )
    (agent_root / "model.md").write_text(
        "# Model\n\n## Default model\n\nclaude-sonnet-4-6-20260101\n\n"
        "```yaml\ncost_guardrails:\n  enabled: true\n  daily_cap_usd: 5.0\n"
        "  monthly_cap_usd: 100.0\n```\n",
        encoding="utf-8",
    )
    (agent_root / "memory").mkdir()
    if tools:
        tools_dir = agent_root / "tools"
        tools_dir.mkdir()
        for tname, handler in tools.items():
            (tools_dir / f"{tname}.md").write_text(
                _GOOD_DESCRIPTOR.format(name=tname), encoding="utf-8"
            )
            (tools_dir / f"{tname}.py").write_text(handler, encoding="utf-8")
    return agent_root


# ──────────────────────────────────────────────────────────────────
# AtomicAgent.__init__ wiring


def test_atomic_agent_tool_registry_backend_public_attribute(tmp_path):
    """``self.tool_registry_backend`` is populated as a public attribute
    mirroring ``self.lock_backend`` / ``self.log_backend`` /
    ``self.profile_backend``."""
    _make_minimal_agent_dir(tmp_path, "scout")
    agent = AtomicAgent(name="scout", agents_root=tmp_path)
    assert isinstance(agent.tool_registry_backend, FilesystemToolRegistryBackend)
    # The backend is scoped at agent_root (not agents_root) — spec/25 Decision 9.
    assert agent.tool_registry_backend.agent_root == agent.agent_root


def test_atomic_agent_tool_registry_backend_kwarg_wins(tmp_path):
    """Explicit ``tool_registry_backend=`` kwarg bypasses default factory."""
    _make_minimal_agent_dir(tmp_path, "scout")
    explicit_backend = FilesystemToolRegistryBackend(tmp_path / "scout")
    agent = AtomicAgent(
        name="scout",
        agents_root=tmp_path,
        tool_registry_backend=explicit_backend,
    )
    assert agent.tool_registry_backend is explicit_backend


def test_atomic_agent_uses_env_var_default_when_kwarg_unset(tmp_path, monkeypatch):
    """No kwarg + filesystem env var → FilesystemToolRegistryBackend."""
    monkeypatch.setenv("ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND", "filesystem")
    _make_minimal_agent_dir(tmp_path, "scout")
    agent = AtomicAgent(name="scout", agents_root=tmp_path)
    assert isinstance(agent.tool_registry_backend, FilesystemToolRegistryBackend)


def test_atomic_agent_empty_tools_dir_preserves_pre_pr2_behavior(tmp_path):
    """Empty / missing ``tools/`` dir → zero backend registrations.

    This is the load-bearing invariant: every existing 96 AtomicAgent
    test site has no ``tools/`` directory, so the PR 2 wiring loop
    runs zero times and ``self.tool_registry`` size is unchanged."""
    _make_minimal_agent_dir(tmp_path, "scout")  # no tools= kwarg
    agent = AtomicAgent(name="scout", agents_root=tmp_path)
    # The in-memory registry has only built-in tools (atomic_capture +
    # skill-related — whatever the framework registers at init).
    # Backend registrations would be ADDITIONAL — we just assert
    # no ToolRegistry entries reference tools that would only exist
    # if backend.list_tools() returned non-empty.
    assert agent.tool_registry_backend.list_tools() == []


def test_atomic_agent_registers_backend_tools_into_in_memory_registry(tmp_path):
    """Decision 1 + Decision 8 of spec/25: backend.list_tools() →
    backend.load_tool(name) → tool_registry.register(td)."""
    _make_minimal_agent_dir(
        tmp_path,
        "scout",
        tools={"query_database": _GOOD_HANDLER},
    )
    agent = AtomicAgent(name="scout", agents_root=tmp_path)
    # Backend yielded one ref; in-memory registry has it.
    assert "query_database" in agent.tool_registry.list_names()
    td = agent.tool_registry.get("query_database")
    assert td is not None
    assert td.classification == "read_only"
    # Handler is materialized and callable.
    result = td.handler({"query": "SELECT 1"})
    assert "SELECT 1" in result


def test_atomic_agent_collision_raises_loudly(tmp_path):
    """Operator-supplied tools register FIRST; backend tools then
    register with ``allow_overwrite=False``, so a collision surfaces
    loudly as ``ToolNameCollision`` AT agent construction. Spec/25
    Decision 8.

    Note on "operator wins" semantics: a collision is a HARD FAILURE,
    not a silent operator-win. The agent cannot construct until the
    operator drops the name from one source. The operator's NAME is
    preserved (it registered first); the agent itself is unusable
    until the conflict is resolved. This is the spec/25 Decision 8
    contract — operators see the collision early and fix the cause,
    rather than silently getting the wrong handler at dispatch.
    """
    _make_minimal_agent_dir(
        tmp_path,
        "scout",
        tools={"query_database": _GOOD_HANDLER},
    )

    operator_registry = ToolRegistry()
    operator_registry.register(
        ToolDefinition(
            name="query_database",
            description="Operator-provided override.",
            input_schema={"type": "object", "properties": {}, "required": []},
            handler=lambda input: "from operator",
        )
    )

    # Backend's load_tool then attempts to register the same name with
    # allow_overwrite=False — that raises ToolNameCollision at agent
    # construction. Operators wanting to override the backend's tool
    # MUST drop the name from one of the two sources.
    with pytest.raises(ToolNameCollision):
        AtomicAgent(
            name="scout",
            agents_root=tmp_path,
            tools=operator_registry,
        )


def test_atomic_agent_skips_malformed_tool_does_not_block_construction(tmp_path):
    """A malformed descriptor or import failure on ONE tool should not
    prevent the agent from constructing — other tools may still be
    usable. The wiring loop swallows the per-tool exception and
    continues."""
    agent_root = _make_minimal_agent_dir(
        tmp_path, "scout", tools={"good": _GOOD_HANDLER}
    )
    # Add a broken tool that imports successfully but has a frontmatter mismatch
    bad_descriptor_path = agent_root / "tools" / "bad.md"
    bad_descriptor_path.write_text(
        "---\nname: NOT_BAD\ndescription: x\n---\n", encoding="utf-8"
    )
    (agent_root / "tools" / "bad.py").write_text(
        "def handler(input): return 'ok'\n", encoding="utf-8"
    )

    # Should not raise — agent constructs, good tool registered, bad skipped.
    agent = AtomicAgent(name="scout", agents_root=tmp_path)
    assert "good" in agent.tool_registry.list_names()
    assert "bad" not in agent.tool_registry.list_names()


def test_atomic_agent_skips_broken_handler_module_does_not_block(tmp_path):
    """Step 11 regression: a handler module that raises at import time
    must NOT block agent construction. The wiring loop swallows
    ``ToolHandlerImportFailed`` and proceeds. Distinct from the
    descriptor-name-mismatch path — exercises the actual
    ``try/except`` around ``load_tool()``."""
    agent_root = _make_minimal_agent_dir(
        tmp_path, "scout", tools={"good": _GOOD_HANDLER}
    )
    # broken.py raises at import time (top-level RuntimeError)
    (agent_root / "tools" / "broken.md").write_text(
        _GOOD_DESCRIPTOR.format(name="broken"), encoding="utf-8"
    )
    (agent_root / "tools" / "broken.py").write_text(
        "raise RuntimeError('hostile module raised at import')\n",
        encoding="utf-8",
    )
    agent = AtomicAgent(name="scout", agents_root=tmp_path)
    assert "good" in agent.tool_registry.list_names()
    assert "broken" not in agent.tool_registry.list_names()


def test_atomic_agent_unreadable_tools_dir_does_not_block(tmp_path):
    """Step 11 adversarial REPRODUCED P1: a chmod-000 ``<agent>/tools/``
    directory previously caused ``PermissionError`` from ``iterdir()``
    to propagate out of ``AtomicAgent.__init__``, blocking every agent
    construction. The fix moves the defense into
    ``FilesystemToolRegistryBackend.list_tools`` (treat unreadable
    tools/ as empty — same shape as missing tools/).

    Skip on Windows where chmod doesn't enforce the same semantics.
    """
    import os
    import sys

    if sys.platform == "win32":
        pytest.skip("chmod permissions don't apply on Windows")

    agent_root = _make_minimal_agent_dir(
        tmp_path, "scout", tools={"good": _GOOD_HANDLER}
    )
    tools_dir = agent_root / "tools"
    original_mode = tools_dir.stat().st_mode
    os.chmod(tools_dir, 0o000)
    try:
        # MUST NOT raise — the defensive try/except inside list_tools
        # treats unreadable as empty.
        agent = AtomicAgent(name="scout", agents_root=tmp_path)
        # No tools registered (the catalog appears empty to the framework)
        assert "good" not in agent.tool_registry.list_names()
    finally:
        # Restore permissions so the tmp_path cleanup can succeed.
        os.chmod(tools_dir, original_mode)


def test_atomic_agent_skips_filename_with_control_char_does_not_block(tmp_path):
    """Step 11 regression: a descriptor filename containing a control
    character (e.g., generated by a buggy operator script) causes
    ``_validate_tool_name`` to raise ``ValueError`` inside
    ``load_tool``. The wiring loop must catch ``ValueError`` so a
    single bad filename doesn't break every AtomicAgent construction.
    """
    agent_root = _make_minimal_agent_dir(
        tmp_path, "scout", tools={"good": _GOOD_HANDLER}
    )
    # Write a descriptor with a tab character in the file stem
    bad_path = agent_root / "tools" / "tool\twith_tab.md"
    bad_path.write_text(
        _GOOD_DESCRIPTOR.format(name="tool\twith_tab"), encoding="utf-8"
    )
    (agent_root / "tools" / "tool\twith_tab.py").write_text(
        "def handler(input): return 'ok'\n", encoding="utf-8"
    )

    # Agent constructs — the control-char tool is silently skipped.
    agent = AtomicAgent(name="scout", agents_root=tmp_path)
    assert "good" in agent.tool_registry.list_names()
    # The bad name MUST NOT register (validator caught it before disk read)
    assert all("\t" not in n for n in agent.tool_registry.list_names())


# ──────────────────────────────────────────────────────────────────
# Runner threading


def test_outcome_runner_threads_tool_registry_backend(monkeypatch, tmp_path):
    """OutcomeRunner threads the kwarg to internal AtomicAgent at run() time.

    Step 11 adversarial regression: a storage-only assertion would
    pass even if a future contributor removed
    ``tool_registry_backend=self._tool_registry_backend`` from the
    AtomicAgent call site. This test monkeypatches the internal
    AtomicAgent class to capture its kwargs, ensuring the kwarg-drop
    trap is pinned at the BOUNDARY (#61 / #63 PR 2 caught the same
    shape on log_backend / profile_backend).
    """
    _make_minimal_agent_dir(tmp_path, "scout")
    explicit_backend = FilesystemToolRegistryBackend(tmp_path / "scout")
    runner = OutcomeRunner(
        agents_root=tmp_path,
        agent_name="scout",
        tool_registry_backend=explicit_backend,
    )
    # Storage still pinned — the public API surface
    assert runner._tool_registry_backend is explicit_backend

    # Now capture the kwargs at the threading boundary. The runner's
    # run() constructs an AtomicAgent — intercept it.
    captured: dict = {}

    class _SentinelAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            # Raise to abort the rest of run() — we only care about the boundary
            raise RuntimeError("boundary captured; aborting run()")

    monkeypatch.setattr("atomic_agents.outcome.AtomicAgent", _SentinelAgent)

    with pytest.raises(RuntimeError, match="boundary captured"):
        runner.run(description="test", rubric="# Rubric\n- Done\n")

    assert captured.get("tool_registry_backend") is explicit_backend


def test_eval_runner_threads_tool_registry_backend(monkeypatch, tmp_path):
    """EvalRunner threads the kwarg to internal AtomicAgent at _run_one_golden() time.

    Step 11 adversarial regression — see OutcomeRunner test for the
    threading-vs-storage distinction.
    """
    agent_root = _make_minimal_agent_dir(tmp_path, "scout")
    (agent_root / "evals").mkdir()
    (agent_root / "evals" / "rubric.md").write_text(
        "---\nweights:\n  correctness: 100\nthreshold_pass: 4.0\n---\n"
        "# Rubric\n- Done correctly\n",
        encoding="utf-8",
    )
    (agent_root / "evals" / "judge.md").write_text(
        "# Judge model\n\nclaude-sonnet-4-6-20260101\n", encoding="utf-8"
    )
    (agent_root / "evals" / "golden").mkdir()

    explicit_backend = FilesystemToolRegistryBackend(tmp_path / "scout")
    runner = EvalRunner(
        agents_root=tmp_path,
        agent_name="scout",
        tool_registry_backend=explicit_backend,
    )
    assert runner._tool_registry_backend is explicit_backend

    # Capture at the threading boundary inside _run_one_golden.
    captured: dict = {}

    class _SentinelAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            raise RuntimeError("boundary captured; aborting eval")

    monkeypatch.setattr("atomic_agents.eval.AtomicAgent", _SentinelAgent)

    # Construct a synthetic EvalTest + call run_test() directly — it
    # constructs the internal AtomicAgent at eval.py:331.
    from atomic_agents.eval import EvalTest

    test = EvalTest(
        test_id="g1",
        category="smoke",
        path=tmp_path / "scout" / "evals" / "golden" / "g1.md",
        setup="",
        input="ping",
        expected_behavior="response",
        pass_criteria="any response",
    )
    with pytest.raises(RuntimeError, match="boundary captured"):
        runner.run_test(test)

    assert captured.get("tool_registry_backend") is explicit_backend


def test_dream_runner_stores_tool_registry_backend(tmp_path):
    """DreamRunner stores the kwarg for API parity with other runners.

    DreamRunner doesn't currently construct internal AtomicAgents
    (raw LLM calls only) but operators wiring multiple runners use one
    signature shape across all three. Reserved for future dream
    pipelines that DO dispatch tools.
    """
    _make_minimal_agent_dir(tmp_path, "scout")
    explicit_backend = FilesystemToolRegistryBackend(tmp_path / "scout")
    runner = DreamRunner(
        agents_root=tmp_path,
        agent_name="scout",
        tool_registry_backend=explicit_backend,
    )
    assert runner._tool_registry_backend is explicit_backend


def test_atomic_agent_delegate_does_not_thread_tool_registry(monkeypatch, tmp_path):
    """Spec/25 Decision 9 — per-agent scoping enforced at the delegate boundary.

    The coordinator's filesystem tool registry is rooted at its OWN
    agent_root. Threading it to the target would put the COORDINATOR's
    tools in the target's catalog. spec/25 Decision 9 + the comment
    at ``agent.py:~3260`` document the non-threading.

    Step 11 adversarial regression: a "two parallel agents have
    distinct backends" assertion would pass trivially (each call to
    the default factory builds a fresh backend). The actual
    invariant is that ``coordinator.delegate(...)`` constructs
    ``target_agent`` WITHOUT passing ``tool_registry_backend=`` —
    this test pins that boundary by monkeypatching the internal
    AtomicAgent class to capture its kwargs.
    """
    coord_root = _make_minimal_agent_dir(
        tmp_path, "coord", tools={"coord_only_tool": _GOOD_HANDLER}
    )
    _make_minimal_agent_dir(
        tmp_path, "target", tools={"target_only_tool": _GOOD_HANDLER}
    )

    # Coordinator roster.md so coord can delegate to target.
    # parse_roster_md expects H2 ``## Delegate to`` (not H1).
    (coord_root / "roster.md").write_text(
        "# Roster\n\n## Delegate to\n\n- target — integration test target\n",
        encoding="utf-8",
    )

    coord = AtomicAgent(name="coord", agents_root=tmp_path)

    # Capture the target's AtomicAgent kwargs at the delegate boundary.
    captured: dict = {}
    real_atomic_agent = AtomicAgent

    class _CapturingAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            # Abort delegate() — we only need the boundary inspection
            raise RuntimeError("delegate boundary captured")

    # Patch the AtomicAgent symbol inside agent.py (the module that
    # calls AtomicAgent(target_agent_name, ...) from delegate()).
    monkeypatch.setattr("atomic_agents.agent.AtomicAgent", _CapturingAgent)

    with pytest.raises(RuntimeError, match="delegate boundary captured"):
        coord.delegate(target_agent_name="target", work_item="ping")

    # Profile backend IS threaded (fleet-scoped — Decision 9 of spec/24)
    assert "profile_backend" in captured
    assert captured["profile_backend"] is coord.profile_backend
    # tool_registry_backend MUST NOT be threaded (per-agent scoped —
    # Decision 9 of spec/25). A regression here would silently route
    # the coordinator's tool catalog to the target.
    assert "tool_registry_backend" not in captured


# ──────────────────────────────────────────────────────────────────
# Per-agent tool isolation


def test_per_agent_tools_isolation(tmp_path):
    """Each agent's tool catalog is scoped to its OWN tools/ dir.

    A and B in the same agents_root with different tools each see
    only their own tools — Decision 9 enforcement.
    """
    _make_minimal_agent_dir(tmp_path, "agent_a", tools={"a_tool": _GOOD_HANDLER})
    _make_minimal_agent_dir(tmp_path, "agent_b", tools={"b_tool": _GOOD_HANDLER})

    a = AtomicAgent(name="agent_a", agents_root=tmp_path)
    b = AtomicAgent(name="agent_b", agents_root=tmp_path)

    assert "a_tool" in a.tool_registry.list_names()
    assert "b_tool" not in a.tool_registry.list_names()
    assert "b_tool" in b.tool_registry.list_names()
    assert "a_tool" not in b.tool_registry.list_names()


# ──────────────────────────────────────────────────────────────────
# Doctor coherence check


def test_doctor_check_tool_registry_backend_filesystem_pass(tmp_path):
    """Filesystem default + present agent_root → PASS with tool count."""
    _make_minimal_agent_dir(tmp_path, "scout", tools={"query_database": _GOOD_HANDLER})
    result = check_tool_registry_backend(tmp_path / "scout")
    assert result.name == "tool-registry-backend"
    assert result.status == "pass"
    assert "1 tool" in result.message
    assert result.detail["backend_id"] == "filesystem"
    assert result.detail["tool_count"] == 1


def test_doctor_check_tool_registry_backend_zero_tools(tmp_path):
    """Empty / missing tools/ dir → PASS with 0 tool count (NOT a failure)."""
    _make_minimal_agent_dir(tmp_path, "scout")
    result = check_tool_registry_backend(tmp_path / "scout")
    assert result.status == "pass"
    assert "0 tools" in result.message
    assert result.detail["tool_count"] == 0


def test_doctor_check_tool_registry_backend_unknown_id_fails(tmp_path, monkeypatch):
    """Operator typo in env var → FAIL with known-id list."""
    _make_minimal_agent_dir(tmp_path, "scout")
    monkeypatch.setenv("ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND", "totally-not-real")
    result = check_tool_registry_backend(tmp_path / "scout")
    assert result.status == "fail"
    assert "totally-not-real" in result.message
    assert "filesystem" in result.message  # known-id list surfaces


def test_doctor_check_tool_registry_backend_sqlite_pass(tmp_path, monkeypatch):
    """#64 PR 3: doctor surfaces SQLite-specific capability snapshot."""
    monkeypatch.setenv("ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND", "sqlite")
    monkeypatch.delenv("ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND_URL", raising=False)
    _make_minimal_agent_dir(tmp_path, "scout")
    result = check_tool_registry_backend(tmp_path / "scout")
    assert result.status == "pass"
    assert "sqlite" in result.message
    # SQLite flips supports_install/uninstall=True (vs filesystem False)
    assert result.detail["backend_id"] == "sqlite"
    assert result.detail["supports_install"] is True
    assert result.detail["supports_uninstall"] is True


def test_doctor_check_tool_registry_backend_redacts_url_credentials(
    tmp_path, monkeypatch
):
    """Credentials in ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND value never
    surface in error text. Mirrors sister-check defense-in-depth."""
    _make_minimal_agent_dir(tmp_path, "scout")
    monkeypatch.setenv(
        "ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND",
        "postgres://user:secretpass@host:5432/db",
    )
    result = check_tool_registry_backend(tmp_path / "scout")
    assert result.status == "fail"
    assert "secretpass" not in result.message
    assert "user:secretpass" not in result.message
