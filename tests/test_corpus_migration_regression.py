"""IRON RULE regression suite for CorpusBackend wiring (#65 PR 3).

Assertions 1-4 verify that both the legacy direct-read path and the
FilesystemCorpusBackend Protocol path produce byte-identical results.
Assertion 5 (full pre-#65 suite passes unchanged) is a CI criterion
documented in the PR body, not a unit test.

Decision 1 Option B: the legacy direct-read path in _load_indexes now
catches OSError and returns "", matching the Protocol path's behavior
(corpus/filesystem.py:699-702). Test 5 in this file covers that
new behaviour.

Fixture shape
-------------
Each test creates:
  <agent_root>/wiki/INDEX.md      -- wiki index content
  <agent_root>/wiki/notes-on-vienna.md  -- sibling page

The sibling page exercises the list_pages() shape even though the
IRON RULE focuses on the INDEX read path.
"""

from __future__ import annotations

import logging
import pathlib

import pytest

from atomic_agents import AtomicAgent
from atomic_agents.bundle import _render_memory_breakpoint
from atomic_agents.corpus.filesystem import FilesystemCorpusBackend


# ── Fixtures ──────────────────────────────────────────────────────────────────

_INDEX_CONTENT = "# Wiki Index\n\nSee [[notes-on-vienna]] for background.\n"
_SIBLING_CONTENT = "# Notes on Vienna\n\nThe city in the 1920s.\n"


def _make_wiki_fixture(agent_root: pathlib.Path) -> pathlib.Path:
    """Write wiki/INDEX.md and wiki/notes-on-vienna.md under agent_root.

    Returns ``agent_root`` for convenience.
    """
    wiki_dir = agent_root / "wiki"
    wiki_dir.mkdir(parents=True, exist_ok=True)
    (wiki_dir / "INDEX.md").write_text(_INDEX_CONTENT, encoding="utf-8")
    (wiki_dir / "notes-on-vienna.md").write_text(_SIBLING_CONTENT, encoding="utf-8")
    return agent_root


def _make_agent_root(tmp_path: pathlib.Path, name: str = "test-agent") -> pathlib.Path:
    """Create a minimal agent directory structure under tmp_path.

    AtomicAgent construction requires either ``persona/IDENTITY.md`` or
    ``persona.link.md`` to satisfy the AgentProfileBackend sentinel check.
    A minimal ``persona/IDENTITY.md`` and ``memory/`` dir are written here.
    """
    agent_root = tmp_path / "agents" / name
    agent_root.mkdir(parents=True, exist_ok=True)
    (agent_root / "memory").mkdir(exist_ok=True)
    persona_dir = agent_root / "persona"
    persona_dir.mkdir(exist_ok=True)
    (persona_dir / "IDENTITY.md").write_text(
        "# Test Agent\n\nA minimal test persona.\n", encoding="utf-8"
    )
    return agent_root


# ── Test 1 ────────────────────────────────────────────────────────────────────


def test_agent_load_indexes_none_fallback_byte_identical(
    tmp_path: pathlib.Path,
) -> None:
    """Legacy direct-read path produces content byte-identical to the on-disk file.

    Forces the else-branch in _load_indexes (corpus_backend=None) and asserts
    that agent._wiki_index_text matches the INDEX.md content exactly.
    """
    agents_root = tmp_path / "agents"
    agent_root = _make_agent_root(tmp_path)
    _make_wiki_fixture(agent_root)

    agent = AtomicAgent(name="test-agent", agents_root=agents_root)

    # Force the legacy direct-read path by nulling the backend, then
    # re-run _load_indexes so it exercises the else-branch.
    agent._wiki_index_text = ""
    agent.corpus_backend = None  # type: ignore[assignment]
    agent._load_indexes()

    assert agent._wiki_index_text == _INDEX_CONTENT


# ── Test 2 ────────────────────────────────────────────────────────────────────


def test_agent_load_indexes_explicit_filesystem_matches_none(
    tmp_path: pathlib.Path,
) -> None:
    """Protocol path and legacy direct-read path agree on _wiki_index_text.

    Constructs two agents over the same fixture:
    - agent_none: legacy direct-read (corpus_backend forced to None)
    - agent_fs: explicit FilesystemCorpusBackend

    Both must produce byte-identical _wiki_index_text values.
    """
    agents_root = tmp_path / "agents"
    agent_root = _make_agent_root(tmp_path)
    _make_wiki_fixture(agent_root)

    # Agent via legacy path (corpus_backend forced to None).
    agent_none = AtomicAgent(name="test-agent", agents_root=agents_root)
    agent_none._wiki_index_text = ""
    agent_none.corpus_backend = None  # type: ignore[assignment]
    agent_none._load_indexes()

    # Agent via explicit FilesystemCorpusBackend Protocol path.
    # _load_indexes is called lazily inside load() / call(); invoke it
    # directly here so _wiki_index_text is populated without an LLM call.
    agent_fs = AtomicAgent(
        name="test-agent",
        agents_root=agents_root,
        corpus_backend=FilesystemCorpusBackend(agent_root),
    )
    agent_fs._load_indexes()

    assert agent_none._wiki_index_text == agent_fs._wiki_index_text


# ── Test 3 ────────────────────────────────────────────────────────────────────


def test_bundle_render_memory_breakpoint_none_fallback_byte_identical(
    tmp_path: pathlib.Path,
) -> None:
    """_render_memory_breakpoint with corpus_backend=None outputs the wiki section.

    Asserts:
    - The output list contains a wiki section (non-empty return value).
    - The wiki section matches the expected shape:
        ## Wiki * INDEX.md
        `<path>`

        <content>
    """
    agent_root = _make_agent_root(tmp_path)
    _make_wiki_fixture(agent_root)

    output = _render_memory_breakpoint(agent_root, corpus_backend=None)

    # Output should be non-empty (breakpoint header + wiki section present).
    assert output, "Expected non-empty output from _render_memory_breakpoint"

    # Find the wiki section in the output list.
    wiki_section = next(
        (s for s in output if "Wiki" in s and "INDEX.md" in s),
        None,
    )
    assert wiki_section is not None, "Expected a wiki INDEX section in the output"

    # Verify the expected bundle format shape.
    wiki_path = agent_root / "wiki" / "INDEX.md"
    expected_content = _INDEX_CONTENT.strip()
    assert "## Wiki · INDEX.md" in wiki_section
    assert f"`{wiki_path}`" in wiki_section
    assert expected_content in wiki_section


# ── Test 4 ────────────────────────────────────────────────────────────────────


def test_bundle_render_memory_breakpoint_explicit_filesystem_matches_none(
    tmp_path: pathlib.Path,
) -> None:
    """IRON RULE: both _render_memory_breakpoint call paths are byte-identical.

    This is the load-bearing IRON RULE assertion -- silent corruption
    prevention guard. If the Protocol path and the legacy path diverge,
    bundle output changes depending on how the operator configured the
    backend, which is a correctness regression.
    """
    agent_root = _make_agent_root(tmp_path)
    _make_wiki_fixture(agent_root)

    fallback_output = _render_memory_breakpoint(agent_root, corpus_backend=None)
    protocol_output = _render_memory_breakpoint(
        agent_root,
        corpus_backend=FilesystemCorpusBackend(agent_root),
    )

    assert fallback_output == protocol_output


# ── Test 5 ────────────────────────────────────────────────────────────────────


def test_agent_load_indexes_oserror_returns_empty_with_warning(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Decision 1 Option B: unreadable INDEX.md returns empty string and logs a warning.

    When wiki/INDEX.md exists but raises OSError on read, the legacy
    direct-read path (corpus_backend=None) soft-degrades: _wiki_index_text
    becomes "" and a warning with the "wiki_index_unreadable" marker is logged.
    This matches the Protocol path's behavior (corpus/filesystem.py:699-702).
    """
    agents_root = tmp_path / "agents"
    agent_root = _make_agent_root(tmp_path)
    _make_wiki_fixture(agent_root)

    # Monkeypatch pathlib.Path.read_text to raise PermissionError for INDEX.md.
    # Using monkeypatch ensures auto-revert after the test regardless of outcome.
    original_read_text = pathlib.Path.read_text

    def patched_read_text(self: pathlib.Path, *args, **kwargs) -> str:  # type: ignore[override]
        if self.name == "INDEX.md":
            raise PermissionError("simulated permission denied")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_text", patched_read_text)

    agent = AtomicAgent(name="test-agent", agents_root=agents_root)

    # Force legacy path by nulling corpus_backend and re-running _load_indexes.
    # (The constructor always sets a default FilesystemCorpusBackend, so we
    # override it to reach the else-branch that holds the OSError catch.)
    agent._wiki_index_text = ""
    agent.corpus_backend = None  # type: ignore[assignment]

    with caplog.at_level(logging.WARNING):
        agent._load_indexes()

    assert agent._wiki_index_text == ""
    assert any(
        "wiki_index_unreadable" in record.message for record in caplog.records
    ), (
        "Expected a log record containing 'wiki_index_unreadable'; "
        f"got records: {[r.message for r in caplog.records]}"
    )


# ── Test 6 ────────────────────────────────────────────────────────────────────


def test_agent_load_indexes_protocol_path_exception_soft_degrades(
    tmp_path: pathlib.Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Round 2 finding F6: Protocol-path broad except branch coverage.

    When a custom CorpusBackend's render_index_summary raises an unexpected
    exception (sqlite3.OperationalError on db lock, CorpusError from a buggy
    implementer, AttributeError from a typo, etc.), agent.py:_load_indexes
    must soft-degrade to "" with a logged warning rather than crash agent
    construction. Mirrors the Test 5 OSError soft-degrade on the legacy
    direct-read path; this test exercises the corresponding Protocol-path
    boundary added in the Round 1 fix commit.
    """
    import sqlite3

    agents_root = tmp_path / "agents"
    agent_root = _make_agent_root(tmp_path)
    _make_wiki_fixture(agent_root)

    class _RaisingCorpusBackend:
        """Minimal CorpusBackend stub whose render_index_summary raises."""

        backend_id = "test-raising"

        def render_index_summary(self, corpus: str) -> str:
            raise sqlite3.OperationalError("simulated db locked")

    agent = AtomicAgent(name="test-agent", agents_root=agents_root)

    # Force Protocol path with a backend that raises on render_index_summary.
    agent._wiki_index_text = ""
    agent.corpus_backend = _RaisingCorpusBackend()  # type: ignore[assignment]

    with caplog.at_level(logging.WARNING):
        agent._load_indexes()

    assert agent._wiki_index_text == ""
    assert any(
        "wiki_index_unreadable" in record.message for record in caplog.records
    ), (
        "Expected a log record containing 'wiki_index_unreadable' on the "
        f"Protocol path; got records: {[r.message for r in caplog.records]}"
    )
    # The log warning must name the backend class so operators know which
    # custom backend produced the failure.
    assert any(
        "_RaisingCorpusBackend" in record.message for record in caplog.records
    ), (
        "Expected a log record naming the offending backend class; "
        f"got records: {[r.message for r in caplog.records]}"
    )
