"""Tests for doctor.check_corpus_backend (spec/34 PR 3, issue #65).

Coverage:
  - PASS: empty filesystem, populated below threshold, sqlite pin
  - WARN: page-count cliff (wiki), page-count cliff (raw), URL without backend id
  - FAIL: malformed env var (unregistered backend), unwritable sqlite path
  - Capability snapshot fields in detail dict
  - URL credential redaction
  - check_corpus_backend appears in run_doctor results

Filesystem isolation: every test uses tmp_path. No writes outside the temp dir.
Env-var isolation: monkeypatch.setenv / delenv; all env mutations auto-revert.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from atomic_agents.doctor import (
    FAIL,
    PASS,
    WARN,
    check_corpus_backend,
    run_doctor,
)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers


def _clear_corpus_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove corpus env vars so tests start from a known clean state."""
    monkeypatch.delenv("ATOMIC_AGENTS_CORPUS_BACKEND", raising=False)
    monkeypatch.delenv("ATOMIC_AGENTS_CORPUS_BACKEND_URL", raising=False)


def _write_wiki_pages(agent_root: Path, count: int) -> None:
    """Write ``count`` minimal wiki pages under <agent_root>/wiki/."""
    wiki = agent_root / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        (wiki / f"page-{i:04d}.md").write_text(
            f"# Page {i}\n\nContent for page {i}.\n", encoding="utf-8"
        )


def _write_raw_pages(agent_root: Path, count: int) -> None:
    """Write ``count`` minimal raw documents under <agent_root>/raw/."""
    raw = agent_root / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        (raw / f"doc-{i:04d}.txt").write_text(f"Raw document {i}.\n", encoding="utf-8")


# ──────────────────────────────────────────────────────────────────────────────
# PASS tests


def test_check_corpus_backend_passes_on_empty_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fresh tmp agent_root with no wiki/raw dirs and no env vars returns PASS.

    Detail dict must report wiki_page_count == 0 and raw_page_count == 0.
    """
    _clear_corpus_env(monkeypatch)

    result = check_corpus_backend(tmp_path)

    assert result.status == PASS
    assert result.detail["wiki_page_count"] == 0
    assert result.detail["raw_page_count"] == 0


def test_check_corpus_backend_passes_on_populated_filesystem_below_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """5 wiki pages + 3 raw pages (well below the 1000-page cliff) returns PASS.

    Detail dict must report the actual counts.
    """
    _clear_corpus_env(monkeypatch)
    _write_wiki_pages(tmp_path, 5)
    _write_raw_pages(tmp_path, 3)

    result = check_corpus_backend(tmp_path)

    assert result.status == PASS
    assert result.detail["wiki_page_count"] == 5
    assert result.detail["raw_page_count"] == 3


def test_check_corpus_backend_passes_on_sqlite_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ATOMIC_AGENTS_CORPUS_BACKEND=sqlite with a populated tmp dir returns PASS.

    Detail dict must show backend_id == 'sqlite' and
    supports_full_text_search == True (SQLite has FTS5 indexing).
    """
    _clear_corpus_env(monkeypatch)
    monkeypatch.setenv("ATOMIC_AGENTS_CORPUS_BACKEND", "sqlite")
    # Provide an explicit URL so we control the db path.
    db_url = f"sqlite:///{tmp_path / '.corpus.db'}?agent_scope=test-agent"
    monkeypatch.setenv("ATOMIC_AGENTS_CORPUS_BACKEND_URL", db_url)

    result = check_corpus_backend(tmp_path)

    assert result.status == PASS
    assert result.detail["backend_id"] == "sqlite"
    assert result.detail["supports_full_text_search"] is True


# ──────────────────────────────────────────────────────────────────────────────
# WARN tests


def test_check_corpus_backend_warns_on_page_count_cliff_wiki(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Filesystem backend (supports_full_text_search=False) with > 1000 wiki pages
    returns WARN.

    We monkeypatch FilesystemCorpusBackend.stats to return a high page_count for
    the 'wiki' corpus so the test does not need to write 1001 real files.
    The hint message must mention the sqlite recommendation.
    """
    from atomic_agents.corpus.filesystem import FilesystemCorpusBackend
    from atomic_agents.corpus.types import CorpusStats

    _clear_corpus_env(monkeypatch)

    def _fake_stats(self, corpus: str) -> CorpusStats:  # noqa: ANN001
        page_count = 1001 if corpus == "wiki" else 0
        return CorpusStats(
            page_count=page_count,
            total_bytes=page_count * 512,
            last_update=None,
            most_recent=[],
        )

    monkeypatch.setattr(FilesystemCorpusBackend, "stats", _fake_stats)

    result = check_corpus_backend(tmp_path)

    assert result.status == WARN
    assert "Set ATOMIC_AGENTS_CORPUS_BACKEND=sqlite" in result.message
    assert "1001" in result.message


def test_check_corpus_backend_warns_on_page_count_cliff_raw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Filesystem backend with > 1000 raw pages (wiki == 0) returns WARN.

    The hint message must mention the sqlite recommendation and the raw count.
    """
    from atomic_agents.corpus.filesystem import FilesystemCorpusBackend
    from atomic_agents.corpus.types import CorpusStats

    _clear_corpus_env(monkeypatch)

    def _fake_stats(self, corpus: str) -> CorpusStats:  # noqa: ANN001
        page_count = 1001 if corpus == "raw" else 0
        return CorpusStats(
            page_count=page_count,
            total_bytes=page_count * 512,
            last_update=None,
            most_recent=[],
        )

    monkeypatch.setattr(FilesystemCorpusBackend, "stats", _fake_stats)

    result = check_corpus_backend(tmp_path)

    assert result.status == WARN
    assert "Set ATOMIC_AGENTS_CORPUS_BACKEND=sqlite" in result.message
    assert "1001" in result.message


def test_check_corpus_backend_warns_url_without_backend_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ATOMIC_AGENTS_CORPUS_BACKEND_URL set but ATOMIC_AGENTS_CORPUS_BACKEND unset
    returns WARN with the 'URL is being ignored' hint.

    This represents a silent misconfiguration where the operator supplied a URL
    but forgot to also set the backend id.
    """
    _clear_corpus_env(monkeypatch)
    # Use a filesystem:// URL so get_default_corpus_backend can construct the
    # backend successfully (it falls back to filesystem when the backend id is
    # unset, then routes through make_filesystem_corpus_backend_from_url).
    # The WARN fires AFTER successful construction -- the doctor detects that
    # ATOMIC_AGENTS_CORPUS_BACKEND_URL is set but ATOMIC_AGENTS_CORPUS_BACKEND
    # is unset (meaning the URL would be silently ignored for non-filesystem
    # backends like sqlite). Using a filesystem:// URL exercises the real WARN
    # path without making the construction fail first.
    monkeypatch.setenv(
        "ATOMIC_AGENTS_CORPUS_BACKEND_URL",
        f"filesystem://{tmp_path}",
    )
    # Intentionally leave ATOMIC_AGENTS_CORPUS_BACKEND unset.

    result = check_corpus_backend(tmp_path)

    assert result.status == WARN
    assert "URL is being ignored" in result.message


# ──────────────────────────────────────────────────────────────────────────────
# FAIL tests


def test_check_corpus_backend_fails_on_malformed_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ATOMIC_AGENTS_CORPUS_BACKEND=postgres (unregistered backend id) returns FAIL.

    The message must mention the env var so the operator knows which setting
    to fix, and must NOT include raw credential text.
    """
    _clear_corpus_env(monkeypatch)
    monkeypatch.setenv("ATOMIC_AGENTS_CORPUS_BACKEND", "postgres")

    result = check_corpus_backend(tmp_path)

    assert result.status == FAIL
    assert "Could not construct CorpusBackend" in result.message
    assert "ATOMIC_AGENTS_CORPUS_BACKEND" in result.message


def test_check_corpus_backend_fails_on_unwritable_sqlite_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ATOMIC_AGENTS_CORPUS_BACKEND=sqlite with a path that triggers PermissionError
    at construction time returns FAIL.

    We monkeypatch make_sqlite_corpus_backend_from_url to raise PermissionError
    so the test is portable across environments (e.g., macOS running as the
    test user where /dev/null/x.db would be ENOTDIR, not EACCES).
    """
    import atomic_agents.corpus as corpus_pkg

    _clear_corpus_env(monkeypatch)
    monkeypatch.setenv("ATOMIC_AGENTS_CORPUS_BACKEND", "sqlite")
    monkeypatch.setenv(
        "ATOMIC_AGENTS_CORPUS_BACKEND_URL",
        "sqlite:///nonexistent/deep/path/.corpus.db?agent_scope=test-agent",
    )

    def _raise_permission(*args, **kwargs):  # noqa: ANN001
        raise PermissionError("read-only file system")

    monkeypatch.setattr(
        corpus_pkg, "make_sqlite_corpus_backend_from_url", _raise_permission
    )

    result = check_corpus_backend(tmp_path)

    assert result.status == FAIL
    assert "Could not construct CorpusBackend" in result.message


# ──────────────────────────────────────────────────────────────────────────────
# Capability + redaction tests


def test_check_corpus_backend_capability_snapshot_in_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PASS result must include the full capability snapshot in the detail dict.

    Fields: backend_id, supports_full_text_search, supports_semantic_search,
    supports_versioning, embedding_provider, wiki_page_count, raw_page_count.
    """
    _clear_corpus_env(monkeypatch)

    result = check_corpus_backend(tmp_path)

    assert result.status == PASS
    for key in (
        "backend_id",
        "supports_full_text_search",
        "supports_semantic_search",
        "supports_versioning",
        "embedding_provider",
        "wiki_page_count",
        "raw_page_count",
    ):
        assert key in result.detail, f"detail missing key: {key!r}"


def test_check_corpus_backend_url_redaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ATOMIC_AGENTS_CORPUS_BACKEND_URL contains a password component,
    the 'secret' must NOT appear in any detail field.

    We use the filesystem backend with a URL that has a password embedded
    so that the doctor's urlparse-and-replace redaction path is exercised.
    Note: FilesystemCorpusBackend itself ignores the password; the test
    targets the doctor's logging/detail surface.
    """
    import atomic_agents.corpus as corpus_pkg
    from atomic_agents.corpus.filesystem import FilesystemCorpusBackend

    _clear_corpus_env(monkeypatch)
    # Use a filesystem:// URL with credentials embedded.
    monkeypatch.setenv("ATOMIC_AGENTS_CORPUS_BACKEND", "filesystem")
    monkeypatch.setenv(
        "ATOMIC_AGENTS_CORPUS_BACKEND_URL",
        f"filesystem://user:s3cr3tpassword@localhost/{tmp_path}",
    )

    # Make the filesystem URL factory succeed so we get to the detail-building
    # phase. The factory will strip the netloc anyway; we just need the doctor
    # to process the URL-redaction logic and include it in the detail dict.
    # If the factory raises on the netloc, monkeypatch it to return a plain backend.
    def _lenient_factory(url: str) -> FilesystemCorpusBackend:
        return FilesystemCorpusBackend(tmp_path)

    monkeypatch.setattr(
        corpus_pkg, "make_filesystem_corpus_backend_from_url", _lenient_factory
    )

    result = check_corpus_backend(tmp_path)

    # The doctor must have processed the URL; assert no secret leaks.
    detail_str = str(result.detail)
    assert "s3cr3tpassword" not in detail_str
    assert "s3cr3tpassword" not in result.message


def test_check_corpus_backend_in_run_all_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_doctor with an agent_name must include a 'corpus-backend' check result.

    The agent_name check exercises the full dispatch list and ensures
    check_corpus_backend is wired into run_doctor (not accidentally omitted).
    """
    _clear_corpus_env(monkeypatch)

    # run_doctor resolves <agents_root>/<agent_name> as agent_root. Create a
    # minimal structure so the vault/lock/log/profile checks don't crash.
    agent_dir = tmp_path / "my-agent"
    agent_dir.mkdir()
    (agent_dir / "memory").mkdir()
    (agent_dir / "memory" / "INDEX.md").write_text("# INDEX\n", encoding="utf-8")
    (agent_dir / "persona").mkdir()
    (agent_dir / "persona" / "IDENTITY.md").write_text(
        "# IDENTITY\n\nI am a test agent.\n", encoding="utf-8"
    )
    (agent_dir / "model.md").write_text(
        "# model.md\n\n## Default model\n\nclaude-haiku-4-5-20251001\n",
        encoding="utf-8",
    )
    (agent_dir / "tools.md").write_text(
        f"## Read paths\n\n- {agent_dir}\n\n"
        f"## Write paths\n\n- {agent_dir / 'memory'}\n",
        encoding="utf-8",
    )

    results = run_doctor(
        agent_name="my-agent",
        agents_root=tmp_path,
        skip_mcp=True,
    )

    check_names = [r.name for r in results]
    assert "corpus-backend" in check_names
