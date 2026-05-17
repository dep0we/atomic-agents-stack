"""SQLite-specific tests for ``SQLiteToolRegistryBackend`` (#64 PR 3).

Conformance tests in ``test_tool_registry_protocol_conformance.py``
exercise the Protocol contract across BOTH backends. THIS module
covers the SQLite-specific behavior: schema initialization (cold-
start race), URL parsing, multi-scope isolation, install + uninstall
semantics, install-time validation (rejection of malformed
descriptors / handlers), :memory: mode, doctor coherence check.

The conformance suite covers happy-path Protocol behavior; SQLite-
specific deviations live here. Mirrors the
``test_profile_sqlite_backend.py`` shape from the AgentProfile arc.
"""

from __future__ import annotations

import sqlite3
import warnings
from pathlib import Path

import pytest

from atomic_agents.exceptions import (
    ToolAlreadyInstalled,
    ToolDescriptorInvalid,
    ToolHandlerImportFailed,
    ToolNotInRegistry,
)
from atomic_agents.registry import (
    SQLiteToolRegistryBackend,
    ToolRegistryBackend,
    get_default_tool_registry_backend,
    get_tool_registry_backend,
    list_tool_registry_backends,
    make_sqlite_tool_registry_backend_from_url,
)


# Reuse the descriptor + handler fixtures from the conformance suite.
from tests.test_tool_registry_protocol_conformance import (
    _GOOD_DESCRIPTOR,
    _GOOD_HANDLER,
)


def _make_source(tmp_path: Path, name: str = "query_database") -> Path:
    """Stage a descriptor + handler under ``tmp_path/staging/<name>/``."""
    staging = tmp_path / "staging" / name
    staging.mkdir(parents=True, exist_ok=True)
    (staging / f"{name}.md").write_text(
        _GOOD_DESCRIPTOR.replace("query_database", name), encoding="utf-8"
    )
    (staging / f"{name}.py").write_text(_GOOD_HANDLER, encoding="utf-8")
    return staging


# ──────────────────────────────────────────────────────────────────
# Constructor + scope validation


def test_constructor_creates_db_parent_dir(tmp_path):
    """sqlite3.connect raises OperationalError if parent dir missing —
    constructor MUST mkdir(parents=True, exist_ok=True) lazily."""
    db_path = tmp_path / "deep" / "nested" / "tools.db"
    backend = SQLiteToolRegistryBackend(db_path, agent_scope="test")
    # Trigger connection on first list_tools — proves construction
    # didn't fail on the missing parent dir.
    assert backend.list_tools() == []
    assert db_path.exists()


def test_constructor_refuses_empty_agent_scope(tmp_path):
    """Empty agent_scope is operator error — refused at construction."""
    with pytest.raises(ValueError, match="non-empty string"):
        SQLiteToolRegistryBackend(tmp_path / "tools.db", agent_scope="")


def test_constructor_refuses_agent_scope_with_path_separator(tmp_path):
    """agent_scope flows into handlers_root subdir name — separators refused."""
    with pytest.raises(ValueError, match="path separator"):
        SQLiteToolRegistryBackend(
            tmp_path / "tools.db", agent_scope="evil/scope"
        )
    with pytest.raises(ValueError, match="path separator"):
        SQLiteToolRegistryBackend(
            tmp_path / "tools.db", agent_scope="evil\\scope"
        )


def test_constructor_refuses_agent_scope_with_dotdot(tmp_path):
    """``..`` in agent_scope would escape handlers_root layout."""
    with pytest.raises(ValueError, match=r"\.\."):
        SQLiteToolRegistryBackend(
            tmp_path / "tools.db", agent_scope="../escape"
        )


def test_constructor_refuses_agent_scope_with_control_char(tmp_path):
    """Control chars in agent_scope refused (defense-in-depth + log-injection)."""
    with pytest.raises(ValueError, match="control character"):
        SQLiteToolRegistryBackend(
            tmp_path / "tools.db", agent_scope="scope\nwith\nnewlines"
        )


def test_default_handlers_root_under_db_parent(tmp_path):
    """When ``handlers_root`` is unset, it defaults to ``<db_path>.parent / handlers``."""
    db_path = tmp_path / "subdir" / "tools.db"
    backend = SQLiteToolRegistryBackend(db_path, agent_scope="test")
    assert backend.handlers_root == tmp_path / "subdir" / "handlers"


# ──────────────────────────────────────────────────────────────────
# Schema initialization + cold-start race


def test_schema_init_creates_tables(tmp_path):
    db_path = tmp_path / "tools.db"
    backend = SQLiteToolRegistryBackend(db_path, agent_scope="test")
    # Trigger connection
    backend.list_tools()
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    table_names = {row[0] for row in rows}
    assert "tools" in table_names
    assert "meta" in table_names
    conn.close()


def test_schema_version_row_present(tmp_path):
    """schema_version=1 row inserted at first connection."""
    db_path = tmp_path / "tools.db"
    backend = SQLiteToolRegistryBackend(db_path, agent_scope="test")
    backend.list_tools()
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()
    assert row is not None
    assert row[0] == "1"
    conn.close()


def test_schema_version_mismatch_raises(tmp_path):
    """A db with the wrong schema_version refuses to open."""
    db_path = tmp_path / "tools.db"
    # Pre-seed a stale schema_version
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', '999')"
    )
    conn.commit()
    conn.close()

    backend = SQLiteToolRegistryBackend(db_path, agent_scope="test")
    with pytest.raises(RuntimeError, match="schema version mismatch"):
        backend.list_tools()  # triggers _ensure_schema


def test_concurrent_schema_init_no_race(tmp_path):
    """Plan-subagent Risk F: multi-process schema init MUST be idempotent.

    Spawns 16 threads each constructing a fresh backend against the same
    db file. INSERT OR IGNORE in _ensure_schema prevents UNIQUE constraint
    races on the meta('schema_version', '1') row.
    """
    from concurrent.futures import ThreadPoolExecutor

    db_path = tmp_path / "tools.db"

    def init_backend(i: int):
        # Each thread gets a separate connection via threading.local
        b = SQLiteToolRegistryBackend(db_path, agent_scope=f"thread-{i}")
        return b.list_tools()

    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = [pool.submit(init_backend, i) for i in range(16)]
        results = [f.result() for f in futures]

    assert all(r == [] for r in results)
    # Final schema_version still '1'
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()
    assert row[0] == "1"
    conn.close()


def test_busy_timeout_set_before_wal_pragma(tmp_path):
    """#215: ``PRAGMA busy_timeout=5000`` MUST be set before the WAL
    pragma so the cold-start WAL-transition race waits instead of
    raising ``OperationalError: database is locked``. Same shape as
    the #208 fix in ``atomic_agents/logs/sqlite.py``; the registry
    SQLite backend now mirrors the same primitive + retry loop to
    meet spec/25 MUST #5 (parity with spec/22 MUST #4).
    """
    backend = SQLiteToolRegistryBackend(tmp_path / "tools.db", agent_scope="test")
    backend.list_tools()  # trigger _get_conn
    conn = backend._get_conn()
    timeout_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert timeout_ms == 5000, (
        f"busy_timeout must be 5000ms (got {timeout_ms}) — without it, "
        f"concurrent threads/processes opening a fresh db race the WAL "
        f"transition and the losers raise OperationalError"
    )


def test_concurrent_schema_init_no_race_under_repeated_runs(tmp_path):
    """#215 regression: repeat the 16-thread concurrent-init scenario 10
    times against a fresh db each iteration. With busy_timeout=5000 +
    retry-on-SQLITE_BUSY/LOCKED on the WAL pragma, all 10 iterations
    MUST succeed.

    Pre-fix: CI Python 3.11 flaked at this exact scenario. Post-fix:
    10/10 success matching the #208 SQLiteLogBackend regression test.
    """
    from concurrent.futures import ThreadPoolExecutor

    for iteration in range(10):
        db_path = tmp_path / f"tools_{iteration}.db"

        def init_backend(i: int):
            b = SQLiteToolRegistryBackend(db_path, agent_scope=f"thread-{i}")
            return b.list_tools()

        with ThreadPoolExecutor(max_workers=16) as pool:
            futures = [pool.submit(init_backend, i) for i in range(16)]
            results = [f.result() for f in futures]

        assert all(r == [] for r in results), (
            f"iteration {iteration}: not all threads returned []; "
            f"WAL race likely surfaced again"
        )


def test_wal_journal_mode_active(tmp_path):
    """File-backed backend MUST enable WAL — verified via PRAGMA probe."""
    db_path = tmp_path / "tools.db"
    backend = SQLiteToolRegistryBackend(db_path, agent_scope="test")
    backend.list_tools()  # trigger connection
    conn = sqlite3.connect(str(db_path))
    row = conn.execute("PRAGMA journal_mode").fetchone()
    assert row[0].lower() == "wal"
    conn.close()


# ──────────────────────────────────────────────────────────────────
# install / uninstall round-trip


def test_install_round_trip(tmp_path):
    """install() + list_tools() + load_tool() — happy path."""
    backend = SQLiteToolRegistryBackend(tmp_path / "tools.db", agent_scope="test")
    src = _make_source(tmp_path, "my_tool")
    ref = backend.install(source=str(src))

    assert ref.name == "my_tool"
    assert ref.classification == "read_only"
    assert ref.version is None

    refs = backend.list_tools()
    assert [r.name for r in refs] == ["my_tool"]

    td = backend.load_tool("my_tool")
    result = td.handler({"query": "SELECT 1"})
    assert "SELECT 1" in result


def test_install_copies_handler_to_handlers_root(tmp_path):
    """install() copies the handler file to ``<handlers_root>/<scope>/<name>.py``."""
    handlers_root = tmp_path / "custom_handlers"
    backend = SQLiteToolRegistryBackend(
        tmp_path / "tools.db",
        agent_scope="alice",
        handlers_root=handlers_root,
    )
    src = _make_source(tmp_path, "my_tool")
    backend.install(source=str(src))

    handler_dest = handlers_root / "alice" / "my_tool.py"
    assert handler_dest.is_file()
    assert "def handler" in handler_dest.read_text(encoding="utf-8")


def test_install_collision_raises_tool_already_installed(tmp_path):
    """Plan-subagent Risk D: install() with same name → ToolAlreadyInstalled."""
    backend = SQLiteToolRegistryBackend(tmp_path / "tools.db", agent_scope="test")
    src = _make_source(tmp_path, "my_tool")
    backend.install(source=str(src))
    with pytest.raises(ToolAlreadyInstalled, match="my_tool"):
        backend.install(source=str(src))


def test_install_rejects_non_directory_source(tmp_path):
    backend = SQLiteToolRegistryBackend(tmp_path / "tools.db", agent_scope="test")
    with pytest.raises(ValueError, match="not a directory"):
        backend.install(source=str(tmp_path / "nonexistent"))


def test_install_rejects_empty_source_dir(tmp_path):
    backend = SQLiteToolRegistryBackend(tmp_path / "tools.db", agent_scope="test")
    empty = tmp_path / "empty_source"
    empty.mkdir()
    with pytest.raises(ValueError, match="no descriptor"):
        backend.install(source=str(empty))


def test_install_rejects_malformed_descriptor(tmp_path):
    """Plan-subagent feature: install() validates descriptor up-front."""
    backend = SQLiteToolRegistryBackend(tmp_path / "tools.db", agent_scope="test")
    src = tmp_path / "bad_source"
    src.mkdir()
    (src / "bad.md").write_text("not frontmatter\nplain markdown\n", encoding="utf-8")
    (src / "bad.py").write_text(_GOOD_HANDLER, encoding="utf-8")
    with pytest.raises(ToolDescriptorInvalid):
        backend.install(source=str(src))


def test_install_rejects_broken_handler_module(tmp_path):
    """Plan-subagent feature: install() imports the handler up-front."""
    backend = SQLiteToolRegistryBackend(tmp_path / "tools.db", agent_scope="test")
    src = tmp_path / "broken_source"
    src.mkdir()
    (src / "broken.md").write_text(
        _GOOD_DESCRIPTOR.replace("query_database", "broken"), encoding="utf-8"
    )
    (src / "broken.py").write_text(
        "raise RuntimeError('broken at import')\n", encoding="utf-8"
    )
    with pytest.raises(ToolHandlerImportFailed):
        backend.install(source=str(src))


def test_install_rejects_handler_without_handler_symbol(tmp_path):
    """install() catches handler module without ``handler`` symbol up-front."""
    backend = SQLiteToolRegistryBackend(tmp_path / "tools.db", agent_scope="test")
    src = tmp_path / "no_handler_source"
    src.mkdir()
    (src / "no_handler.md").write_text(
        _GOOD_DESCRIPTOR.replace("query_database", "no_handler"), encoding="utf-8"
    )
    (src / "no_handler.py").write_text(
        "# no handler symbol\nx = 1\n", encoding="utf-8"
    )
    with pytest.raises(ToolHandlerImportFailed):
        backend.install(source=str(src))


def test_install_rejects_name_mismatch(tmp_path):
    """Descriptor's frontmatter ``name`` MUST match the file stem."""
    backend = SQLiteToolRegistryBackend(tmp_path / "tools.db", agent_scope="test")
    src = tmp_path / "mismatch_source"
    src.mkdir()
    bad_descriptor = """\
---
name: typo_name
description: x
classification: read_only
---
"""
    (src / "correct_name.md").write_text(bad_descriptor, encoding="utf-8")
    (src / "correct_name.py").write_text(_GOOD_HANDLER, encoding="utf-8")
    with pytest.raises(ToolDescriptorInvalid, match="name"):
        backend.install(source=str(src))


def test_install_rejects_version_when_versioning_unsupported(tmp_path):
    """Plan-subagent Risk L: capability honesty.

    PR 3 declares ``supports_versioning=False``; install() MUST reject
    non-None ``version`` rather than silently storing it.
    """
    backend = SQLiteToolRegistryBackend(tmp_path / "tools.db", agent_scope="test")
    src = _make_source(tmp_path, "my_tool")
    with pytest.raises(ValueError, match="supports_versioning=False"):
        backend.install(source=str(src), version="1.2.3")


def test_uninstall_removes_from_catalog_and_disk(tmp_path):
    backend = SQLiteToolRegistryBackend(tmp_path / "tools.db", agent_scope="test")
    src = _make_source(tmp_path, "my_tool")
    backend.install(source=str(src))
    assert "my_tool" in [r.name for r in backend.list_tools()]
    handler_path = backend.handlers_root / "test" / "my_tool.py"
    assert handler_path.is_file()

    backend.uninstall("my_tool")
    assert backend.list_tools() == []
    assert not handler_path.is_file()


def test_uninstall_unknown_name_is_idempotent(tmp_path):
    """Spec/25: uninstall is a no-op for unknown names."""
    backend = SQLiteToolRegistryBackend(tmp_path / "tools.db", agent_scope="test")
    # Should not raise
    backend.uninstall("nothing_to_remove")
    assert backend.list_tools() == []


def test_uninstall_refuses_path_traversal(tmp_path):
    backend = SQLiteToolRegistryBackend(tmp_path / "tools.db", agent_scope="test")
    with pytest.raises(ValueError):
        backend.uninstall("../escape")


# ──────────────────────────────────────────────────────────────────
# Cross-scope isolation — the security primitive


def test_cross_scope_list_tools_isolation(tmp_path):
    """Plan-subagent Risk B: scope A's tools MUST NOT surface in scope B's list_tools."""
    db = tmp_path / "tools.db"
    alice = SQLiteToolRegistryBackend(db, agent_scope="alice")
    bob = SQLiteToolRegistryBackend(db, agent_scope="bob")

    alice.install(source=str(_make_source(tmp_path, "alice_tool")))
    bob.install(source=str(_make_source(tmp_path, "bob_tool")))

    assert [r.name for r in alice.list_tools()] == ["alice_tool"]
    assert [r.name for r in bob.list_tools()] == ["bob_tool"]


def test_cross_scope_load_tool_isolation(tmp_path):
    """Scope A cannot load_tool a tool installed under scope B."""
    db = tmp_path / "tools.db"
    alice = SQLiteToolRegistryBackend(db, agent_scope="alice")
    bob = SQLiteToolRegistryBackend(db, agent_scope="bob")

    alice.install(source=str(_make_source(tmp_path, "alice_only")))

    # Bob's view does NOT see alice_only
    with pytest.raises(ToolNotInRegistry):
        bob.load_tool("alice_only")


def test_cross_scope_uninstall_isolation(tmp_path):
    """uninstall() filtered by scope — Bob cannot remove Alice's tool."""
    db = tmp_path / "tools.db"
    alice = SQLiteToolRegistryBackend(db, agent_scope="alice")
    bob = SQLiteToolRegistryBackend(db, agent_scope="bob")

    alice.install(source=str(_make_source(tmp_path, "alice_only")))
    # Bob's uninstall is a no-op (the row isn't in bob's scope).
    bob.uninstall("alice_only")
    # Alice's tool still there.
    assert "alice_only" in [r.name for r in alice.list_tools()]


def test_cross_scope_same_name_coexist(tmp_path):
    """Composite PK (agent_scope, name) — two scopes can both have ``foo``."""
    db = tmp_path / "tools.db"
    alice = SQLiteToolRegistryBackend(db, agent_scope="alice")
    bob = SQLiteToolRegistryBackend(db, agent_scope="bob")

    alice.install(source=str(_make_source(tmp_path, "shared_name")))
    bob.install(source=str(_make_source(tmp_path, "shared_name")))

    assert "shared_name" in [r.name for r in alice.list_tools()]
    assert "shared_name" in [r.name for r in bob.list_tools()]


# ──────────────────────────────────────────────────────────────────
# :memory: mode


def test_memory_mode_emits_runtime_warning():
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        SQLiteToolRegistryBackend(":memory:", agent_scope="test")
    assert any(
        issubclass(w.category, RuntimeWarning) and "non-persistent" in str(w.message)
        for w in captured
    )


def test_memory_mode_durable_is_false():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        backend = SQLiteToolRegistryBackend(":memory:", agent_scope="test")
    assert backend.capabilities().durable is False


def test_file_backed_durable_is_true(tmp_path):
    backend = SQLiteToolRegistryBackend(tmp_path / "tools.db", agent_scope="test")
    assert backend.capabilities().durable is True


# ──────────────────────────────────────────────────────────────────
# URL factory


def test_url_factory_default_scope(tmp_path):
    backend = make_sqlite_tool_registry_backend_from_url(
        f"sqlite:///{tmp_path}/tools.db"
    )
    assert isinstance(backend, SQLiteToolRegistryBackend)
    assert backend.agent_scope == "default"


def test_url_factory_with_agent_scope_query(tmp_path):
    backend = make_sqlite_tool_registry_backend_from_url(
        f"sqlite:///{tmp_path}/tools.db?agent_scope=alice"
    )
    assert backend.agent_scope == "alice"


def test_url_factory_memory_shorthand():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        backend = make_sqlite_tool_registry_backend_from_url("sqlite::memory:")
    assert isinstance(backend, SQLiteToolRegistryBackend)
    assert backend.db_path == ":memory:"


def test_url_factory_three_slash_memory():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        backend = make_sqlite_tool_registry_backend_from_url("sqlite:///:memory:")
    assert backend.db_path == ":memory:"


def test_url_factory_rejects_wrong_scheme(tmp_path):
    with pytest.raises(ValueError, match="scheme"):
        make_sqlite_tool_registry_backend_from_url(f"postgres://{tmp_path}/db")


def test_url_factory_rejects_netloc(tmp_path):
    with pytest.raises(ValueError, match="netloc"):
        make_sqlite_tool_registry_backend_from_url(
            f"sqlite://host/{tmp_path}/db"
        )


def test_url_factory_rejects_unknown_query_param(tmp_path):
    """Plan-subagent F-5 shape: silent drops of unknown query params let
    operator intent slip through."""
    with pytest.raises(ValueError, match="unsupported query parameters"):
        make_sqlite_tool_registry_backend_from_url(
            f"sqlite:///{tmp_path}/tools.db?mode=ro"
        )


def test_url_factory_rejects_fragment(tmp_path):
    with pytest.raises(ValueError, match="fragment"):
        make_sqlite_tool_registry_backend_from_url(
            f"sqlite:///{tmp_path}/tools.db#test"
        )


# ──────────────────────────────────────────────────────────────────
# Registry resolution + env-var dispatch


def test_registry_resolves_sqlite():
    cls = get_tool_registry_backend("sqlite")
    assert cls is SQLiteToolRegistryBackend


def test_registry_lists_sqlite():
    assert "sqlite" in list_tool_registry_backends()


def test_get_default_with_sqlite_env_var(tmp_path, monkeypatch):
    """``ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND=sqlite`` without URL →
    defaults to ``<agent_root>/.tools.db`` + ``agent_scope=<agent_root.name>``."""
    monkeypatch.setenv("ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND", "sqlite")
    monkeypatch.delenv("ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND_URL", raising=False)
    agent_root = tmp_path / "scout"
    agent_root.mkdir()
    backend = get_default_tool_registry_backend(agent_root)
    assert isinstance(backend, SQLiteToolRegistryBackend)
    assert backend.db_path == str(agent_root / ".tools.db")
    assert backend.agent_scope == "scout"


def test_get_default_with_sqlite_url_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND", "sqlite")
    monkeypatch.setenv(
        "ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND_URL",
        f"sqlite:///{tmp_path}/shared.db?agent_scope=multi",
    )
    backend = get_default_tool_registry_backend(tmp_path / "scout")
    assert isinstance(backend, SQLiteToolRegistryBackend)
    assert backend.agent_scope == "multi"


# ──────────────────────────────────────────────────────────────────
# Handler ergonomics — closures, imports, module-level setup


def test_concurrent_install_same_name_winner_handler_survives(tmp_path):
    """Step 11 adversarial P1 REPRODUCED (50/50 pre-fix): concurrent
    install() of the same name used to clobber the winner's handler
    file via the loser's rollback unlink().

    Fix: INSERT-first, atomic_write-on-success only. The loser sees
    rowcount=0, raises ToolAlreadyInstalled WITHOUT touching disk.
    The winner's file is materialized atomically once SQL says it's
    the winner.

    Asserts: exactly one install succeeds, all others raise
    ToolAlreadyInstalled, AND the surviving handler file at
    ``<handlers_root>/<scope>/<name>.py`` is readable + importable
    after the race.
    """
    from concurrent.futures import ThreadPoolExecutor

    backend = SQLiteToolRegistryBackend(tmp_path / "tools.db", agent_scope="t")
    # Pre-stage 8 distinct sources for the same name — each thread has
    # its own source so the file-on-disk identity diverges per-thread.
    sources = [_make_source(tmp_path / f"src_{i}", "raced") for i in range(8)]

    results = {"success": 0, "collision": 0, "transient": 0}
    transient_errors: list[Exception] = []

    def install_one(src: Path):
        try:
            backend.install(source=str(src))
            results["success"] += 1
        except ToolAlreadyInstalled:
            results["collision"] += 1
        except sqlite3.OperationalError as exc:
            # Transient lock contention even with busy_timeout=5000ms
            # under heavy 8-way race — acceptable as long as the
            # winner's file is still readable downstream. The fix's
            # core invariant (winner's file survives) is what this
            # test guards; transient losers can retry.
            results["transient"] += 1
            transient_errors.append(exc)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(install_one, sources))

    # Core invariants:
    # 1. Exactly one install succeeded (composite PK + ON CONFLICT).
    assert results["success"] == 1, f"expected exactly one success; {results}"
    # 2. All other threads either collided cleanly OR hit transient
    #    contention — never silently dropped state.
    assert (
        results["success"] + results["collision"] + results["transient"] == 8
    ), f"accounted-for threads: {results}"
    # 3. The KEY invariant — winner's handler file SURVIVED the race.
    #    Pre-fix the losers' rollback unlinks destroyed it; load_tool
    #    afterwards raised ToolHandlerImportFailed permanently.
    td = backend.load_tool("raced")
    assert callable(td.handler)
    result = td.handler({"query": "post-race"})
    assert "post-race" in result


def test_url_factory_redacts_credentials_in_error_messages():
    """Step 11 adversarial P1 REPRODUCED: URL factory used to echo
    raw URLs (including passwords) in its ValueError messages. Now
    redacted via ``_redact_url``."""
    with pytest.raises(ValueError) as exc_info:
        make_sqlite_tool_registry_backend_from_url(
            "postgres://user:supersecret@host:5432/db"
        )
    err_text = str(exc_info.value)
    assert "supersecret" not in err_text
    assert "user:supersecret" not in err_text
    # Scheme + host still surface — diagnostic without leaking the secret.
    assert "postgres" in err_text


def test_url_factory_rejects_duplicate_query_param(tmp_path):
    """Step 11 spirit: silent drops of duplicate keys are operator-
    intent footguns (which scope wins — alice or bob?)."""
    with pytest.raises(ValueError, match="duplicate query parameter"):
        make_sqlite_tool_registry_backend_from_url(
            f"sqlite:///{tmp_path}/tools.db?agent_scope=alice&agent_scope=bob"
        )


def test_url_factory_rejects_empty_path():
    """Empty path → ``sqlite://`` — refused."""
    with pytest.raises(ValueError, match="empty path"):
        make_sqlite_tool_registry_backend_from_url("sqlite://")


def test_url_factory_rejects_root_slash_only():
    """Path-only ``/`` → refused (same shape as empty path)."""
    with pytest.raises(ValueError, match="empty path"):
        make_sqlite_tool_registry_backend_from_url("sqlite:///")


def test_constructor_rejects_root_handlers_root(tmp_path):
    """Step 11 P2: handlers_root must have >1 path component after
    resolution to prevent filesystem-root scoping."""
    with pytest.raises(ValueError, match="<= 1 path component"):
        SQLiteToolRegistryBackend(
            tmp_path / "tools.db",
            agent_scope="t",
            handlers_root=Path("/"),
        )


def test_install_rejects_non_callable_handler(tmp_path):
    """Step 11 specialist finding: install() now catches
    ``handler = 42`` (non-callable) at install time via the
    strengthened ``_import_handler`` callable check."""
    backend = SQLiteToolRegistryBackend(tmp_path / "tools.db", agent_scope="t")
    src = tmp_path / "src"
    src.mkdir()
    (src / "non_callable.md").write_text(
        _GOOD_DESCRIPTOR.replace("query_database", "non_callable"),
        encoding="utf-8",
    )
    (src / "non_callable.py").write_text(
        "# handler is not callable\nhandler = 42\n", encoding="utf-8"
    )
    with pytest.raises(ToolHandlerImportFailed, match="not callable"):
        backend.install(source=str(src))


def test_memory_mode_handlers_root_defaults_to_tempdir(tmp_path, monkeypatch):
    """Step 11 P2 REPRODUCED pre-fix: :memory: defaulted handlers_root
    to CWD/.handlers, writing to disk despite the non-persistent
    promise. Two :memory: instances would clobber each other's
    handlers via the shared on-disk path. Fix: per-instance tempdir.
    """
    monkeypatch.chdir(tmp_path)  # make the CWD predictable
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        b1 = SQLiteToolRegistryBackend(":memory:", agent_scope="t")
        b2 = SQLiteToolRegistryBackend(":memory:", agent_scope="t")
    # The two :memory: instances get DIFFERENT handlers_root dirs.
    assert b1.handlers_root != b2.handlers_root
    # CWD-based default is GONE.
    assert b1.handlers_root != tmp_path / ".handlers"


def test_handler_with_module_level_import_works(tmp_path):
    """Plan-subagent Risk A: handler module's top-level imports MUST
    work — closures over module-globals are the canonical pattern.

    This is the test that base64-exec would have FAILED. The handler
    imports json at module top-level and uses it inside `handler` —
    works because the handler module exists as a real .py file and
    importlib's spec_from_file_location preserves the module's
    __globals__.
    """
    backend = SQLiteToolRegistryBackend(tmp_path / "tools.db", agent_scope="test")
    src = tmp_path / "json_source"
    src.mkdir()
    (src / "json_tool.md").write_text(
        _GOOD_DESCRIPTOR.replace("query_database", "json_tool"),
        encoding="utf-8",
    )
    (src / "json_tool.py").write_text(
        "import json\n"
        "_PREFIX = 'serialized: '\n"
        "def handler(input):\n"
        "    return _PREFIX + json.dumps(input)\n",
        encoding="utf-8",
    )
    backend.install(source=str(src))
    td = backend.load_tool("json_tool")
    result = td.handler({"query": "x"})
    assert result.startswith("serialized: ")
    assert '"query": "x"' in result
