"""Tests for corpus backend env var resolution, per-runner kwarg threading, and CLI activation.

Covers:
- get_default_corpus_backend env var resolution (8 tests)
- OutcomeRunner / EvalRunner / DreamRunner kwarg storage (4 tests)
- CLI _cmd_corpus uses get_default_corpus_backend (1 test)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from atomic_agents.corpus import (
    FilesystemCorpusBackend,
    SQLiteCorpusBackend,
    get_default_corpus_backend,
)
from atomic_agents.exceptions import CorpusBackendNotRegistered


# ──────────────────────────────────────────────────────────────────────────────
# Helpers


def _clear_corpus_env(monkeypatch):
    """Remove both corpus env vars to avoid leakage between tests."""
    monkeypatch.delenv("ATOMIC_AGENTS_CORPUS_BACKEND", raising=False)
    monkeypatch.delenv("ATOMIC_AGENTS_CORPUS_BACKEND_URL", raising=False)


# ──────────────────────────────────────────────────────────────────────────────
# Env var resolution tests


def test_default_corpus_backend_resolves_filesystem_when_unset(tmp_path, monkeypatch):
    """No env var set + agent_root supplied -> FilesystemCorpusBackend."""
    _clear_corpus_env(monkeypatch)
    agent_root = tmp_path / "my-agent"
    agent_root.mkdir()

    backend = get_default_corpus_backend(agent_root)

    assert isinstance(backend, FilesystemCorpusBackend)


def test_default_corpus_backend_resolves_sqlite_when_env_var_set(tmp_path, monkeypatch):
    """ATOMIC_AGENTS_CORPUS_BACKEND=sqlite (no URL) -> SQLiteCorpusBackend at default path."""
    _clear_corpus_env(monkeypatch)
    agent_root = tmp_path / "my-agent"
    agent_root.mkdir()
    monkeypatch.setenv("ATOMIC_AGENTS_CORPUS_BACKEND", "sqlite")

    backend = get_default_corpus_backend(agent_root)

    assert isinstance(backend, SQLiteCorpusBackend)
    # The default db path should be <agent_root>/.corpus.db
    # Use .resolve() on both sides: on macOS, tmp_path may be /private/tmp/...
    # but the URL parser produces //private/tmp/... (double slash from sqlite:///).
    assert backend._db_path.resolve() == (agent_root / ".corpus.db").resolve()
    # The agent_scope should match the directory name
    assert backend._agent_scope == agent_root.name


def test_default_corpus_backend_url_overrides_default_path(tmp_path, monkeypatch):
    """ATOMIC_AGENTS_CORPUS_BACKEND=sqlite + explicit URL -> URL used, not default path."""
    _clear_corpus_env(monkeypatch)
    agent_root = tmp_path / "my-agent"
    agent_root.mkdir()
    custom_db = tmp_path / "custom.db"
    monkeypatch.setenv("ATOMIC_AGENTS_CORPUS_BACKEND", "sqlite")
    monkeypatch.setenv(
        "ATOMIC_AGENTS_CORPUS_BACKEND_URL",
        f"sqlite:///{custom_db}?agent_scope=test",
    )

    backend = get_default_corpus_backend(agent_root)

    assert isinstance(backend, SQLiteCorpusBackend)
    assert backend._db_path.resolve() == custom_db.resolve()
    assert backend._agent_scope == "test"


def test_default_corpus_backend_url_empty_string_falls_back_to_default(
    tmp_path, monkeypatch
):
    """ATOMIC_AGENTS_CORPUS_BACKEND=sqlite + ATOMIC_AGENTS_CORPUS_BACKEND_URL="" -> uses default path."""
    _clear_corpus_env(monkeypatch)
    agent_root = tmp_path / "my-agent"
    agent_root.mkdir()
    monkeypatch.setenv("ATOMIC_AGENTS_CORPUS_BACKEND", "sqlite")
    monkeypatch.setenv("ATOMIC_AGENTS_CORPUS_BACKEND_URL", "")

    backend = get_default_corpus_backend(agent_root)

    assert isinstance(backend, SQLiteCorpusBackend)
    assert backend._db_path.resolve() == (agent_root / ".corpus.db").resolve()
    assert backend._agent_scope == agent_root.name


def test_default_corpus_backend_empty_backend_env_var_treated_as_unset(
    tmp_path, monkeypatch
):
    """ATOMIC_AGENTS_CORPUS_BACKEND="" (explicitly empty) -> FilesystemCorpusBackend (treated as unset)."""
    _clear_corpus_env(monkeypatch)
    agent_root = tmp_path / "my-agent"
    agent_root.mkdir()
    monkeypatch.setenv("ATOMIC_AGENTS_CORPUS_BACKEND", "")

    backend = get_default_corpus_backend(agent_root)

    assert isinstance(backend, FilesystemCorpusBackend)


def test_default_corpus_backend_whitespace_padded_env_var_works(tmp_path, monkeypatch):
    """ATOMIC_AGENTS_CORPUS_BACKEND=" sqlite " (whitespace padding) -> resolves to SQLiteCorpusBackend."""
    _clear_corpus_env(monkeypatch)
    agent_root = tmp_path / "my-agent"
    agent_root.mkdir()
    monkeypatch.setenv("ATOMIC_AGENTS_CORPUS_BACKEND", "  sqlite  ")

    backend = get_default_corpus_backend(agent_root)

    assert isinstance(backend, SQLiteCorpusBackend)


def test_default_corpus_backend_filesystem_url_supported(tmp_path, monkeypatch):
    """ATOMIC_AGENTS_CORPUS_BACKEND=filesystem + URL -> FilesystemCorpusBackend via URL factory."""
    _clear_corpus_env(monkeypatch)
    agent_root = tmp_path / "my-agent"
    agent_root.mkdir()
    custom_root = tmp_path / "custom-root"
    monkeypatch.setenv("ATOMIC_AGENTS_CORPUS_BACKEND", "filesystem")
    monkeypatch.setenv(
        "ATOMIC_AGENTS_CORPUS_BACKEND_URL",
        f"filesystem:///{custom_root}",
    )

    backend = get_default_corpus_backend(agent_root)

    assert isinstance(backend, FilesystemCorpusBackend)
    # The backend should use the URL-supplied path, not agent_root
    assert backend._agent_root.resolve() == custom_root.resolve()


def test_default_corpus_backend_agent_root_empty_name_raises(tmp_path, monkeypatch):
    """Path('/') as agent_root with sqlite -> raises CorpusBackendNotRegistered naming URL remedy."""
    _clear_corpus_env(monkeypatch)
    monkeypatch.setenv("ATOMIC_AGENTS_CORPUS_BACKEND", "sqlite")

    with pytest.raises(CorpusBackendNotRegistered) as exc_info:
        get_default_corpus_backend(Path("/"))

    # The error message must name the URL env var as the fix
    assert "ATOMIC_AGENTS_CORPUS_BACKEND_URL" in str(exc_info.value)


# ──────────────────────────────────────────────────────────────────────────────
# Per-runner kwarg threading tests


def test_outcome_runner_stores_corpus_backend_kwarg(tmp_path):
    """OutcomeRunner stores the passed corpus_backend on self._corpus_backend."""
    from atomic_agents.outcome import OutcomeRunner

    agent_root = tmp_path / "agents" / "testagent"
    agent_root.mkdir(parents=True)
    _make_minimal_agent(agent_root)

    corpus_backend = FilesystemCorpusBackend(agent_root)
    runner = OutcomeRunner(
        agents_root=tmp_path / "agents",
        agent_name="testagent",
        corpus_backend=corpus_backend,
    )

    assert runner._corpus_backend is corpus_backend


def test_eval_runner_stores_corpus_backend_kwarg(tmp_path):
    """EvalRunner stores the passed corpus_backend on self._corpus_backend."""
    from atomic_agents.eval import EvalRunner

    agent_root = tmp_path / "agents" / "testagent"
    agent_root.mkdir(parents=True)
    _make_minimal_agent(agent_root)
    _make_minimal_evals(agent_root)

    corpus_backend = FilesystemCorpusBackend(agent_root)
    runner = EvalRunner(
        agents_root=tmp_path / "agents",
        agent_name="testagent",
        corpus_backend=corpus_backend,
    )

    assert runner._corpus_backend is corpus_backend


def test_dream_runner_stores_corpus_backend_kwarg(tmp_path):
    """DreamRunner stores the passed corpus_backend on self._corpus_backend."""
    from atomic_agents.dream import DreamRunner

    agent_root = tmp_path / "agents" / "testagent"
    agent_root.mkdir(parents=True)
    _make_minimal_agent(agent_root)

    corpus_backend = FilesystemCorpusBackend(agent_root)
    runner = DreamRunner(
        agents_root=tmp_path / "agents",
        agent_name="testagent",
        corpus_backend=corpus_backend,
    )

    assert runner._corpus_backend is corpus_backend


def test_outcome_runner_threads_corpus_backend_to_atomic_agent(tmp_path):
    """OutcomeRunner threads corpus_backend to the internal AtomicAgent construction."""
    from atomic_agents.outcome import OutcomeRunner
    from atomic_agents.agent import AtomicAgent

    agent_root = tmp_path / "agents" / "testagent"
    agent_root.mkdir(parents=True)
    _make_minimal_agent(agent_root)

    corpus_backend = FilesystemCorpusBackend(agent_root)

    constructed_kwargs: list[dict] = []

    original_init = AtomicAgent.__init__

    def capturing_init(self_inner, *args, **kwargs):
        constructed_kwargs.append(dict(kwargs))
        original_init(self_inner, *args, **kwargs)

    runner = OutcomeRunner(
        agents_root=tmp_path / "agents",
        agent_name="testagent",
        corpus_backend=corpus_backend,
    )

    with patch.object(AtomicAgent, "__init__", capturing_init):
        # Trigger the internal agent construction by calling a method that
        # creates AtomicAgent. Use a rubric-less run call attempt; we only
        # care about the kwarg capture, so mock the LLM + judge layers.
        try:
            runner.run(description="test", rubric="## quality\nGood.", max_iterations=1)
        except Exception:
            pass  # expected -- no LLM keys in test env

    # At least one AtomicAgent construction must have included the corpus_backend
    corpus_kwargs = [kw.get("corpus_backend") for kw in constructed_kwargs]
    assert any(cb is corpus_backend for cb in corpus_kwargs), (
        f"corpus_backend not threaded to AtomicAgent. "
        f"Saw kwarg corpus_backend values: {corpus_kwargs}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# CLI activation test


def test_cli_corpus_subcommand_uses_default_resolver(tmp_path, monkeypatch):
    """_cmd_corpus uses get_default_corpus_backend, resolving to SQLiteCorpusBackend when env var is set."""
    from atomic_agents.cli import _cmd_corpus

    _clear_corpus_env_monkeypatch(monkeypatch)
    agent_root = tmp_path / "my-agent"
    agent_root.mkdir()
    custom_db = tmp_path / "cli-test.db"
    monkeypatch.setenv("ATOMIC_AGENTS_CORPUS_BACKEND", "sqlite")
    monkeypatch.setenv(
        "ATOMIC_AGENTS_CORPUS_BACKEND_URL",
        f"sqlite:///{custom_db}?agent_scope=clitest",
    )

    # Capture the backend instance the CLI resolves
    resolved_backends: list = []
    original_get_default = get_default_corpus_backend

    def capturing_get_default(agent_root_arg):
        backend = original_get_default(agent_root_arg)
        resolved_backends.append(backend)
        return backend

    args = MagicMock()
    args.agent_root = str(agent_root)
    args.corpus_cmd = "list"
    args.corpus = "wiki"

    with patch(
        "atomic_agents.cli.get_default_corpus_backend"
        if _cli_imports_directly()
        else "atomic_agents.corpus.get_default_corpus_backend",
        side_effect=capturing_get_default,
    ):
        try:
            _cmd_corpus(args)
        except Exception:
            pass  # list on empty corpus is fine; we just need the backend resolution

    # The captured backend must be SQLiteCorpusBackend, not FilesystemCorpusBackend
    assert resolved_backends, (
        "get_default_corpus_backend was never called by _cmd_corpus"
    )
    assert isinstance(resolved_backends[0], SQLiteCorpusBackend), (
        f"Expected SQLiteCorpusBackend from CLI, got {type(resolved_backends[0])!r}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers


def _clear_corpus_env_monkeypatch(monkeypatch):
    """Remove both corpus env vars (named to avoid shadowing the module-level helper)."""
    monkeypatch.delenv("ATOMIC_AGENTS_CORPUS_BACKEND", raising=False)
    monkeypatch.delenv("ATOMIC_AGENTS_CORPUS_BACKEND_URL", raising=False)


def _cli_imports_directly() -> bool:
    """Return True when cli.py imports get_default_corpus_backend into its own namespace."""
    from atomic_agents import cli as cli_module

    # cli.py does a deferred import inside _cmd_corpus; the symbol is not at
    # module level, so the patch target is the corpus package's namespace.
    return hasattr(cli_module, "get_default_corpus_backend")


def _make_minimal_agent(agent_root: Path) -> None:
    """Create the minimal directory layout required by AtomicAgent / runners."""
    persona = agent_root / "persona"
    persona.mkdir(parents=True, exist_ok=True)
    (persona / "IDENTITY.md").write_text("# IDENTITY\n\nI am a test agent.")
    (persona / "SOUL.md").write_text("# SOUL\n\nBrief.")
    (agent_root / "tools.md").write_text(
        "# TOOLS\n\n## Read paths\n- "
        + str(agent_root)
        + "\n\n## Write paths\n- "
        + str(agent_root)
        + "\n"
    )
    (agent_root / "model.md").write_text(
        "# MODEL\n\n## Default model\n\nclaude-sonnet-4-6-20260101\n\n"
        "## Fallback\n\nclaude-haiku-4-5-20251001\n"
    )


def _make_minimal_evals(agent_root: Path) -> None:
    """Create the minimal evals/ layout required by EvalRunner."""
    evals_dir = agent_root / "evals"
    evals_dir.mkdir(exist_ok=True)
    (evals_dir / "rubric.md").write_text(
        "---\nweights:\n  quality: 100\nthreshold_pass: 3.0\n---\n\n# Rubric\n\n## quality\nGood output.\n"
    )
    (evals_dir / "judge.md").write_text(
        "---\nrecommended_judge:\n  cross_family: []\n  same_family_fallback: []\n"
        "strict_mode: false\naudit_sample_pct: 0.0\n---\n\n# Judge\n"
    )
    golden = evals_dir / "golden"
    golden.mkdir(exist_ok=True)
