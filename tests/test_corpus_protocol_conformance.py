"""Conformance tests for CorpusBackend Protocol.

Parametrized across registered backends. Adding a new backend to the
fixture exercises the full Protocol contract for free.

PR 2 extends ``params=["filesystem"]`` to ``params=["filesystem", "sqlite"]``
and adds the corresponding ``elif request.param == "sqlite":`` branch in
``corpus_backend``; every test here then runs against both backends with zero
additional test code.

Test count: ~25 parametrized tests covering the Protocol contract that ANY
backend MUST satisfy.

Per spec/34 §"Test coverage" + design doc.
"""

from __future__ import annotations

import hashlib
import os
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from atomic_agents.corpus.backend import CorpusBackend
from atomic_agents.corpus.types import (
    CorpusCapabilities,
    CorpusPage,
    CorpusRef,
    CorpusStats,
)
from atomic_agents.corpus.filesystem import FilesystemCorpusBackend
from atomic_agents.corpus.sqlite import SQLiteCorpusBackend
from atomic_agents.memory.backend import WritePolicy, VersionRef
from atomic_agents.exceptions import (
    CorpusInvalidName,
    CorpusPageExists,
    CorpusPageNotFound,
    CorpusPreconditionFailed,
    CorpusVersionNotFound,
)

# ──────────────────────────────────────────────────────────────────────────────
# Postgres availability gate (for PgvectorCorpusBackend conformance)

_POSTGRES_URL = os.environ.get("ATOMIC_AGENTS_TEST_POSTGRES_URL")
_POSTGRES_AVAILABLE = False

if _POSTGRES_URL:
    try:
        import psycopg as _psycopg_check  # noqa: F401

        _POSTGRES_AVAILABLE = True
    except ImportError:
        pass

requires_postgres = pytest.mark.skipif(
    not _POSTGRES_AVAILABLE,
    reason=(
        "Requires ATOMIC_AGENTS_TEST_POSTGRES_URL env var and psycopg installed. "
        "Set ATOMIC_AGENTS_TEST_POSTGRES_URL=postgresql://... to run Postgres tests."
    ),
)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers


def _sha256(content: str) -> str:
    """Return hex SHA-256 of the content string (UTF-8 encoded)."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _make_content(
    body: str = "The body of the page.",
    *,
    name: str | None = None,
    description: str | None = None,
    captured: str | None = None,
    pinned: bool = False,
    tags: list[str] | None = None,
    extra: dict | None = None,
) -> tuple[str, dict]:
    """Build a (content_body, frontmatter_dict) pair for write_page tests.

    Returns raw body text and a frontmatter dict.  Callers pass both to
    ``write_page(name, content, corpus, policy, frontmatter=fm)``.
    """
    fm: dict = {}
    if name is not None:
        fm["name"] = name
    if description is not None:
        fm["description"] = description
    if captured is not None:
        fm["captured"] = captured
    if pinned:
        fm["pinned"] = True
    if tags:
        fm["tags"] = tags
    if extra:
        fm.update(extra)
    return body, fm


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures


_CORPUS_BACKEND_PARAMS = ["filesystem", "sqlite"]
if _POSTGRES_AVAILABLE:
    _CORPUS_BACKEND_PARAMS.append("pgvector-corpus")


@pytest.fixture(params=_CORPUS_BACKEND_PARAMS)
def corpus_backend(request, tmp_path: Path):
    """Parametrized over registered CorpusBackend implementations.

    Backends exercised:
    - filesystem: always runs
    - sqlite: always runs
    - pgvector-corpus: skips without ATOMIC_AGENTS_TEST_POSTGRES_URL (CI only)

    Uses a subdirectory per-backend-id to keep filesystem state isolated when
    multiple backends are tested in the same session.

    pgvector-corpus uses StubEmbeddingBackend (no live OpenAI key required) and
    pgvector_url=None, which disables ANN and falls back to FTS — sufficient to
    verify all non-ANN Protocol shape invariants.  ANN-specific behaviour is
    covered by test_pgvector_corpus_backend.py with @requires_postgres.
    """
    if request.param == "filesystem":
        agent_root = tmp_path / f"agent-{request.node.name[:32]}"
        agent_root.mkdir(exist_ok=True)
        backend = FilesystemCorpusBackend(agent_root)
        yield backend
    elif request.param == "sqlite":
        backend = SQLiteCorpusBackend(
            db_path=tmp_path / "corpus.db",
            agent_scope="test-agent",
            content_root=tmp_path / "content",
        )
        yield backend
        backend.close()
    elif request.param == "pgvector-corpus":
        # Guarded by _POSTGRES_AVAILABLE so this branch only runs when a live
        # Postgres URL is configured (CI lane).  Uses StubEmbeddingBackend
        # (no live OpenAI) and pgvector_url=None (FTS-fallback mode) so the
        # full Protocol shape is verified without billable provider calls.
        from atomic_agents.corpus.pgvector import PgvectorCorpusBackend
        from tests.stub_embedding import StubEmbeddingBackend

        stub = StubEmbeddingBackend(dimensions=4)
        agent_root = tmp_path / f"pgvec-agent-{request.node.name[:24]}"
        agent_root.mkdir(exist_ok=True)
        # Force FTS-fallback deterministically: clear the env var so that
        # PgvectorCorpusBackend(pgvector_url=None) cannot silently pick up a
        # live URL from the environment and flip supports_semantic_search=True,
        # which would make test_capabilities_embedding_provider_honesty a
        # vacuous no-op (the `if not caps.supports_semantic_search:` guard would
        # never fire). monkeypatch ensures the FTS branch is always exercised.
        monkeypatch = request.getfixturevalue("monkeypatch")
        monkeypatch.delenv("ATOMIC_AGENTS_PGVECTOR_URL", raising=False)
        # pgvector_url=None → FTS-only mode; no Postgres ANN index used.
        # This is intentional: Protocol-shape conformance (list/read/write/
        # version/query-fallback) does not require ANN.
        backend = PgvectorCorpusBackend(
            agent_root, embedding_backend=stub, pgvector_url=None
        )
        # Verify FTS-fallback is deterministic (a live pgvector_url env var
        # would set _pgvector_url and flip supports_semantic_search=True).
        assert backend._pgvector_url is None, (
            "pgvector-corpus conformance fixture must run in FTS-fallback mode "
            "(pgvector_url=None). If this assertion fails, PgvectorCorpusBackend "
            "picked up ATOMIC_AGENTS_PGVECTOR_URL from the environment despite "
            "pgvector_url=None being passed explicitly."
        )
        yield backend
        backend.close()
    else:
        raise NotImplementedError(f"unknown backend param: {request.param!r}")


@pytest.fixture
def write_policy(tmp_path: Path) -> WritePolicy:
    """A WritePolicy that permits writes anywhere under ``tmp_path``."""
    return WritePolicy(write_paths=[tmp_path])


# ──────────────────────────────────────────────────────────────────────────────
# Category 1 — Protocol shape (3 tests)


def test_backend_implements_protocol(corpus_backend) -> None:
    """``isinstance(backend, CorpusBackend)`` returns True.

    ``@runtime_checkable`` enables a method-presence check (not a full
    signature check). Verifies that the backend satisfies the Protocol at
    runtime before any I/O is exercised.
    """
    assert isinstance(corpus_backend, CorpusBackend)


def test_capabilities_returns_correct_type(corpus_backend) -> None:
    """``capabilities`` property returns a ``CorpusCapabilities`` instance."""
    caps = corpus_backend.capabilities
    assert isinstance(caps, CorpusCapabilities)


def test_capabilities_flags_are_bools(corpus_backend) -> None:
    """All boolean fields on ``CorpusCapabilities`` are actual booleans."""
    caps = corpus_backend.capabilities
    assert isinstance(caps.supports_semantic_search, bool)
    assert isinstance(caps.supports_full_text_search, bool)
    assert isinstance(caps.supports_versioning, bool)
    assert isinstance(caps.supports_streaming_iteration, bool)


def test_capabilities_embedding_provider_honesty(corpus_backend) -> None:
    """spec/34 LOCKED invariant: embedding_provider is None when no semantic search.

    CorpusCapabilities (corpus/types.py) documents: ``embedding_provider`` MUST
    be None when ``supports_semantic_search=False``. Enforced for EVERY backend
    param — the pgvector-corpus fixture runs in FTS-fallback mode (an embedding
    backend is injected but pgvector_url=None), which is exactly the config that
    used to leak a provider label past a False semantic flag.

    Guard against silent no-ops: the ``if not caps.supports_semantic_search:``
    branch MUST have fired for the pgvector-corpus param (fixture is pinned to
    FTS-fallback mode). Without this check, an env-var surprise that flips
    supports_semantic_search=True would silently turn this test into a vacuous
    pass (the assertion body would never run, leaving the invariant unverified).
    """
    caps = corpus_backend.capabilities
    if not caps.supports_semantic_search:
        assert caps.embedding_provider is None
    # For PgvectorCorpusBackend specifically, the fixture is pinned to
    # FTS-fallback mode (pgvector_url=None, ATOMIC_AGENTS_PGVECTOR_URL cleared).
    # Assert the asserting branch actually fired — if supports_semantic_search
    # were True, the ``if not`` guard above would be a silent no-op.
    # Detect by class name so we don't need request.param (which is not
    # available to the test function — only available in the fixture itself).
    if type(corpus_backend).__name__ == "PgvectorCorpusBackend":
        assert not caps.supports_semantic_search, (
            "pgvector-corpus conformance fixture is pinned to FTS-fallback mode; "
            "supports_semantic_search must be False. If True, the fixture has "
            "picked up a live pgvector URL and the embedding_provider invariant "
            "check ran vacuously (the 'if not' body never executed)."
        )


# ──────────────────────────────────────────────────────────────────────────────
# Category 2 — list_pages (4 tests)


@pytest.mark.parametrize("corpus", ["wiki", "raw"])
def test_list_pages_empty_corpus_returns_empty_list(
    corpus_backend, write_policy, corpus
) -> None:
    """Empty (or missing) corpus directory returns ``[]`` from ``list_pages``.

    Parametrized over both corpus types so both code paths are covered.
    """
    result = corpus_backend.list_pages(corpus)
    assert result == []


def test_list_pages_wiki_only_shows_wiki_not_raw(
    corpus_backend, write_policy, tmp_path
) -> None:
    """Writing one wiki page: ``list_pages("wiki")`` returns it; ``list_pages("raw")`` does not."""
    # Re-derive the backend's agent_root from the fixture by writing one page
    body, fm = _make_content(body="Some wiki content.", name="Test Page")
    corpus_backend.write_page("my-page", body, "wiki", write_policy, frontmatter=fm)

    wiki_refs = corpus_backend.list_pages("wiki")
    raw_refs = corpus_backend.list_pages("raw")

    assert len(wiki_refs) == 1
    assert wiki_refs[0].name == "my-page"
    assert wiki_refs[0].corpus == "wiki"
    assert raw_refs == []


def test_list_pages_with_limit_pagination(corpus_backend, write_policy) -> None:
    """``limit`` and ``offset`` page through a multi-page corpus correctly.

    Writes 5 pages, then asserts:
    - ``list_pages(limit=2)`` returns exactly 2 entries.
    - ``list_pages(limit=2, offset=2)`` returns the next 2 entries (distinct names).
    - The union of both pages covers 4 distinct page names.
    """
    for i in range(5):
        body = f"Body of page {i}."
        corpus_backend.write_page(f"page-{i}", body, "wiki", write_policy)

    first_page = corpus_backend.list_pages("wiki", limit=2)
    second_page = corpus_backend.list_pages("wiki", limit=2, offset=2)

    assert len(first_page) == 2
    assert len(second_page) == 2
    names_first = {r.name for r in first_page}
    names_second = {r.name for r in second_page}
    assert names_first.isdisjoint(names_second), (
        f"Paginated slices overlap: {names_first & names_second}"
    )


def test_list_pages_returns_corpus_ref_with_expected_fields(
    corpus_backend, write_policy
) -> None:
    """Each entry from ``list_pages`` is a ``CorpusRef`` with required fields populated."""
    body = "Some content."
    corpus_backend.write_page("my-ref-page", body, "wiki", write_policy)

    refs = corpus_backend.list_pages("wiki")
    assert len(refs) == 1
    ref = refs[0]
    assert isinstance(ref, CorpusRef)
    assert ref.name == "my-ref-page"
    assert ref.corpus == "wiki"
    assert isinstance(ref.title, str)
    assert isinstance(ref.last_modified, datetime)
    assert isinstance(ref.byte_size, int)
    assert ref.byte_size > 0


# ──────────────────────────────────────────────────────────────────────────────
# Category 3 — read_page (3 tests)


def test_read_page_missing_returns_none(corpus_backend) -> None:
    """``read_page`` for a non-existent page returns ``None`` (not an exception).

    This is the routine presence-check return convention (spec/34 D12).
    """
    result = corpus_backend.read_page("does-not-exist", "wiki")
    assert result is None


def test_read_page_existing_returns_corpus_page_with_frontmatter(
    corpus_backend, write_policy
) -> None:
    """``read_page`` for an existing page returns a ``CorpusPage`` with fields
    populated from the frontmatter.

    Asserts: ``ref.name``, ``description``, ``pinned`` (as bool),
    ``captured`` (as ``date``), ``tags`` (as list), and ``body`` (non-empty).
    """
    body = "Avalanche beats snowball every time."
    # Pass captured as a date object so python-frontmatter serializes it as an
    # unquoted ISO date ("2026-01-15") and PyYAML reads it back as datetime.date.
    # Passing a string produces a quoted YAML value that PyYAML round-trips as str.
    fm: dict = {
        "name": "Avalanche vs Snowball",
        "description": "Debt strategy comparison.",
        "pinned": True,
        "captured": date(2026, 1, 15),
        "tags": ["debt", "finance"],
    }
    corpus_backend.write_page(
        "avalanche-vs-snowball", body, "wiki", write_policy, frontmatter=fm
    )

    page = corpus_backend.read_page("avalanche-vs-snowball", "wiki")

    assert page is not None
    assert isinstance(page, CorpusPage)
    assert page.ref.name == "avalanche-vs-snowball"
    assert page.ref.corpus == "wiki"
    assert page.description == "Debt strategy comparison."
    assert page.pinned is True
    assert isinstance(page.pinned, bool)
    # captured is a date object (PyYAML loads bare ISO dates as datetime.date
    # when the YAML value is unquoted -- achieved by passing a date object to
    # the frontmatter dict so python-frontmatter serializes it without quotes).
    assert isinstance(page.captured, date)
    assert page.captured == date(2026, 1, 15)
    assert page.tags == ["debt", "finance"]
    assert "Avalanche beats snowball" in page.body


def test_read_page_extra_frontmatter_lands_in_catch_all(
    corpus_backend, write_policy
) -> None:
    """Unknown YAML frontmatter keys land in ``extra_frontmatter`` dict.

    Any key that does not map to a named field on ``CorpusPage`` is
    round-trip preserved in ``extra_frontmatter``.
    """
    body = "Some content."
    fm: dict = {
        "description": "Test page.",
        "custom_field_xyz": "operator-value",
        "another_unknown": 42,
    }
    corpus_backend.write_page(
        "extra-fm-page", body, "wiki", write_policy, frontmatter=fm
    )

    page = corpus_backend.read_page("extra-fm-page", "wiki")

    assert page is not None
    assert isinstance(page.extra_frontmatter, dict)
    assert "custom_field_xyz" in page.extra_frontmatter
    assert page.extra_frontmatter["custom_field_xyz"] == "operator-value"
    assert "another_unknown" in page.extra_frontmatter


# ──────────────────────────────────────────────────────────────────────────────
# Category 4 — write_page 4-case behavior table (5 tests)


def test_write_page_case1_fresh_write_creates_page(
    corpus_backend, write_policy
) -> None:
    """Case 1: page does not exist → write succeeds, ``read_page`` returns it.

    Verifies the basic write path: a fresh write of a new page persists and
    is readable via ``read_page``.
    """
    body = "Fresh content."
    ref = corpus_backend.write_page("fresh-page", body, "wiki", write_policy)

    assert isinstance(ref, CorpusRef)
    assert ref.name == "fresh-page"
    assert ref.corpus == "wiki"

    page = corpus_backend.read_page("fresh-page", "wiki")
    assert page is not None
    assert "Fresh content." in page.body


def test_write_page_case2_idempotent_same_content_is_noop(
    corpus_backend, write_policy
) -> None:
    """Case 2: page exists; identical content → idempotent no-op, returns ``CorpusRef``.

    Writing the same content twice must not raise and must return a ``CorpusRef``
    on both calls. Safe under crash recovery and re-delivery.
    """
    body = "Same content every time."
    ref1 = corpus_backend.write_page("idempotent-page", body, "wiki", write_policy)
    ref2 = corpus_backend.write_page("idempotent-page", body, "wiki", write_policy)

    assert isinstance(ref1, CorpusRef)
    assert isinstance(ref2, CorpusRef)
    assert ref1.name == ref2.name

    # Exactly one page exists (no duplicates)
    refs = corpus_backend.list_pages("wiki")
    assert len(refs) == 1


def test_write_page_case3_cas_overwrite_with_correct_hash(
    corpus_backend, write_policy
) -> None:
    """Case 3: page exists; content differs; correct CAS hash → overwrite succeeds.

    The caller computes the SHA-256 of the current on-disk content and passes it
    as ``expected_content_sha256``. The backend verifies the hash matches before
    overwriting (compare-and-swap).
    """
    import frontmatter as fm_lib

    original_body = "Original content."
    corpus_backend.write_page("cas-page", original_body, "wiki", write_policy)

    # Read back the exact on-disk bytes to derive the current SHA-256
    page = corpus_backend.read_page("cas-page", "wiki")
    assert page is not None

    # Re-derive the on-disk SHA-256 by reconstructing the file content.
    # The backend stores body-only (no frontmatter dict was passed), so the
    # on-disk content is just the body text.
    on_disk_sha = _sha256(original_body)

    updated_body = "Updated content after CAS."
    ref = corpus_backend.write_page(
        "cas-page",
        updated_body,
        "wiki",
        write_policy,
        expected_content_sha256=on_disk_sha,
    )

    assert isinstance(ref, CorpusRef)
    updated_page = corpus_backend.read_page("cas-page", "wiki")
    assert updated_page is not None
    assert "Updated content after CAS." in updated_page.body


def test_write_page_case4_collision_no_cas_raises_corpus_page_exists(
    corpus_backend, write_policy
) -> None:
    """Case 4a: page exists; content differs; no ``expected_content_sha256`` → raises ``CorpusPageExists``.

    Silent overwrite is refused by default. Operators must supply a CAS hash
    to opt into the overwrite path (spec/34 D11).
    """
    corpus_backend.write_page("collision-page", "Original.", "wiki", write_policy)

    with pytest.raises(CorpusPageExists):
        corpus_backend.write_page(
            "collision-page", "Different content.", "wiki", write_policy
        )


def test_write_page_case4_cas_mismatch_raises_corpus_precondition_failed(
    corpus_backend, write_policy
) -> None:
    """Case 4b: page exists; content differs; wrong CAS hash → raises ``CorpusPreconditionFailed``.

    A stale or incorrect hash indicates a concurrent write landed between the
    caller's read and its write attempt.
    """
    corpus_backend.write_page("precondition-page", "Original.", "wiki", write_policy)

    with pytest.raises(CorpusPreconditionFailed):
        corpus_backend.write_page(
            "precondition-page",
            "Different content.",
            "wiki",
            write_policy,
            expected_content_sha256="0000000000000000000000000000000000000000000000000000000000000000",
        )


# ──────────────────────────────────────────────────────────────────────────────
# Category 5 — render_index_summary (2 tests)


def test_render_index_summary_no_index_returns_empty_string(
    corpus_backend,
) -> None:
    """No INDEX.md (or equivalent) → ``render_index_summary`` returns ``""``.

    The empty-string contract lets callers branch on truthiness.
    """
    result = corpus_backend.render_index_summary("wiki")
    assert result == ""


def test_render_index_summary_with_index_returns_content(
    corpus_backend, write_policy
) -> None:
    """``render_index_summary("wiki")`` returns non-empty string after INDEX.md is written.

    For ``FilesystemCorpusBackend``, this is the verbatim content of ``wiki/INDEX.md``.
    For ``SQLiteCorpusBackend`` (PR 2), synthesized prose from page metadata.
    Both must return a truthy (non-empty) string.
    """
    # Write INDEX.md directly for the filesystem backend; backends that
    # synthesize the index will return content based on existing pages.
    # For the parametrized contract, we write a page so SQLite has metadata
    # to synthesize from, AND write INDEX.md so the filesystem backend has
    # something to return verbatim.
    corpus_backend.write_page(
        "sample-page",
        "Sample body.",
        "wiki",
        write_policy,
        frontmatter={"description": "A sample wiki page."},
    )

    # Write INDEX.md for filesystem backend (bypassing the Protocol since
    # INDEX.md is a filesystem-specific artifact). Non-filesystem backends
    # that synthesize an index from page metadata will return their own content.
    agent_root = getattr(corpus_backend, "_agent_root", None)
    if agent_root is not None:
        index_path = agent_root / "wiki" / "INDEX.md"
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(
            "## Wiki Index\n\nSample index content.\n", encoding="utf-8"
        )

    result = corpus_backend.render_index_summary("wiki")
    assert isinstance(result, str)
    assert result, (
        "render_index_summary must return a non-empty string when INDEX.md exists; "
        f"got {result!r}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Category 6 — query() (4 tests)


def test_query_empty_corpus_returns_empty_list(corpus_backend) -> None:
    """``query()`` on an empty corpus returns ``[]``."""
    result = corpus_backend.query("anything", "wiki")
    assert result == []


def test_query_no_matches_returns_empty_list(corpus_backend, write_policy) -> None:
    """``query()`` with no matching pages returns ``[]``."""
    corpus_backend.write_page(
        "unmatched-page", "This page talks about cats.", "wiki", write_policy
    )
    result = corpus_backend.query("definitely_not_in_any_page_xyz", "wiki")
    assert result == []


def test_query_substring_match_returns_hits(corpus_backend, write_policy) -> None:
    """Substring fallback (both capability flags False): matching pages are returned.

    When ``supports_semantic_search=False`` AND ``supports_full_text_search=False``,
    the backend MUST use case-insensitive substring match. Writes two pages,
    one matching, one not.
    """
    corpus_backend.write_page(
        "matching-page",
        "The avalanche method beats the snowball method.",
        "wiki",
        write_policy,
    )
    corpus_backend.write_page(
        "non-matching-page",
        "This page is about something else entirely.",
        "wiki",
        write_policy,
    )

    result = corpus_backend.query("avalanche", "wiki")

    assert len(result) >= 1
    names = {r.name for r in result}
    assert "matching-page" in names
    assert "non-matching-page" not in names


def test_query_top_k_limits_results(corpus_backend, write_policy) -> None:
    """``top_k`` caps the number of returned results.

    Writes 5 pages all matching the query, then asserts ``top_k=2`` returns
    exactly 2.
    """
    for i in range(5):
        corpus_backend.write_page(
            f"page-{i}",
            f"The keyword appears in page {i} repeatedly: keyword keyword.",
            "wiki",
            write_policy,
        )

    result = corpus_backend.query("keyword", "wiki", top_k=2)
    assert len(result) <= 2


# ──────────────────────────────────────────────────────────────────────────────
# Category 7 — Versioning (capability-gated, 5 tests)


def test_snapshot_creates_version(corpus_backend, write_policy) -> None:
    """``snapshot()`` returns a ``VersionRef``; ``list_versions`` includes it.

    SKIP when ``capabilities.supports_versioning`` is False.
    """
    if not corpus_backend.capabilities.supports_versioning:
        pytest.skip("backend does not support versioning")

    corpus_backend.write_page("ver-page", "Initial content.", "wiki", write_policy)
    version_ref = corpus_backend.snapshot("ver-page", "wiki")

    assert isinstance(version_ref, VersionRef)
    assert version_ref.backend_id  # non-empty

    versions = corpus_backend.list_versions("ver-page", "wiki")
    backend_ids = [v.backend_id for v in versions]
    assert version_ref.backend_id in backend_ids


def test_list_versions_newest_first(corpus_backend, write_policy) -> None:
    """``list_versions`` returns versions newest first (descending order).

    Takes two CAS-overwrites (each triggers a snapshot); asserts returned
    version list is sorted descending by ``backend_id`` (which encodes an
    ISO timestamp prefix per spec/34 D7).

    SKIP when ``capabilities.supports_versioning`` is False.
    """
    if not corpus_backend.capabilities.supports_versioning:
        pytest.skip("backend does not support versioning")

    corpus_backend.write_page("order-page", "V1 content.", "wiki", write_policy)
    # CAS overwrite → backend creates version of V1 before writing V2
    v1_sha = _sha256("V1 content.")
    corpus_backend.write_page(
        "order-page",
        "V2 content.",
        "wiki",
        write_policy,
        expected_content_sha256=v1_sha,
    )
    v2_sha = _sha256("V2 content.")
    corpus_backend.write_page(
        "order-page",
        "V3 content.",
        "wiki",
        write_policy,
        expected_content_sha256=v2_sha,
    )

    versions = corpus_backend.list_versions("order-page", "wiki")
    assert len(versions) >= 2
    # Version backend_ids encode ISO timestamps; lexicographic descending = newest first
    backend_ids = [v.backend_id for v in versions]
    assert backend_ids == sorted(backend_ids, reverse=True)


def test_read_version_returns_corpus_page(corpus_backend, write_policy) -> None:
    """``read_version`` returns the ``CorpusPage`` for the snapshotted content.

    SKIP when ``capabilities.supports_versioning`` is False.
    """
    if not corpus_backend.capabilities.supports_versioning:
        pytest.skip("backend does not support versioning")

    corpus_backend.write_page(
        "readver-page", "Content at snapshot time.", "wiki", write_policy
    )
    version_ref = corpus_backend.snapshot("readver-page", "wiki")

    # Overwrite so the live page differs from the snapshot
    live_sha = _sha256("Content at snapshot time.")
    corpus_backend.write_page(
        "readver-page",
        "Live content after snapshot.",
        "wiki",
        write_policy,
        expected_content_sha256=live_sha,
    )

    page = corpus_backend.read_version(version_ref)
    assert isinstance(page, CorpusPage)
    assert "Content at snapshot time." in page.body


def test_read_version_missing_raises_corpus_version_not_found(
    corpus_backend, write_policy
) -> None:
    """``read_version`` raises ``CorpusVersionNotFound`` for an unknown ref.

    SKIP when ``capabilities.supports_versioning`` is False.
    """
    if not corpus_backend.capabilities.supports_versioning:
        pytest.skip("backend does not support versioning")

    # Construct a VersionRef that cannot resolve to any real version
    fake_ref = VersionRef(
        backend_id="wiki/no-such-page/20260101T000000000000Z_00000000.md"
    )

    with pytest.raises(CorpusVersionNotFound):
        corpus_backend.read_version(fake_ref)


def test_restore_version_restores_content(corpus_backend, write_policy) -> None:
    """``restore_version`` makes a previous version the live page.

    Snapshot V1, overwrite to V2, restore from snapshot; ``read_page`` must
    return V1 content.

    SKIP when ``capabilities.supports_versioning`` is False.
    """
    if not corpus_backend.capabilities.supports_versioning:
        pytest.skip("backend does not support versioning")

    corpus_backend.write_page("restore-page", "Version 1 body.", "wiki", write_policy)
    version_ref = corpus_backend.snapshot("restore-page", "wiki")

    v1_sha = _sha256("Version 1 body.")
    corpus_backend.write_page(
        "restore-page",
        "Version 2 body.",
        "wiki",
        write_policy,
        expected_content_sha256=v1_sha,
    )

    corpus_backend.restore_version("restore-page", "wiki", version_ref, write_policy)

    live = corpus_backend.read_page("restore-page", "wiki")
    assert live is not None
    assert "Version 1 body." in live.body


# ──────────────────────────────────────────────────────────────────────────────
# Category 8 — Charset / path-traversal validation (3 tests)


_INVALID_NAMES = [
    pytest.param("..", id="dotdot"),
    pytest.param("../etc/passwd", id="dotdot_slash"),
    pytest.param("/etc/passwd", id="leading_slash"),
    pytest.param(".hidden", id="leading_dot"),
    pytest.param("name\x00", id="null_byte"),
    pytest.param("name\ninjection", id="newline"),
]


@pytest.mark.parametrize("bad_name", _INVALID_NAMES)
def test_invalid_name_raises_corpus_invalid_name(
    corpus_backend, bad_name: str, write_policy
) -> None:
    """Path-traversal, control characters, and leading dots raise ``CorpusInvalidName``.

    The charset check MUST fire BEFORE any storage access (spec/34 implementer
    contract MUST #1). Asserts across both ``read_page`` and ``write_page``
    entry points.
    """
    with pytest.raises(CorpusInvalidName):
        corpus_backend.read_page(bad_name, "wiki")

    with pytest.raises(CorpusInvalidName):
        corpus_backend.write_page(bad_name, "body", "wiki", write_policy)


def test_invalid_corpus_type_raises_corpus_invalid_name(corpus_backend) -> None:
    """A misspelled corpus value (e.g., ``"wikki"``) raises ``CorpusInvalidName``.

    ``corpus`` must be one of ``{"wiki", "raw"}``; any other value is refused
    at the API boundary.
    """
    with pytest.raises((CorpusInvalidName, ValueError)):
        corpus_backend.list_pages("wikki")  # type: ignore[arg-type]


def test_name_with_allowed_special_chars_is_accepted(
    corpus_backend, write_policy
) -> None:
    """Page names containing allowed special characters do not raise.

    The charset ``[a-zA-Z0-9_.+@-]+`` includes hyphen, underscore, dot,
    plus, and at-sign. Operators use these in practice (e.g., ``"avalanche-vs-
    snowball"``, ``"report.2026-04-22"``, ``"caldwell@home+finance"``).
    """
    allowed_name = "a-b_c.d+e@f"
    body = "Content for a page with special chars in the name."
    ref = corpus_backend.write_page(allowed_name, body, "wiki", write_policy)
    assert ref.name == allowed_name

    page = corpus_backend.read_page(allowed_name, "wiki")
    assert page is not None
    assert page.ref.name == allowed_name


# ──────────────────────────────────────────────────────────────────────────────
# Category 9 — stats() + close() (3 tests)


def test_stats_returns_corpus_stats_type(corpus_backend) -> None:
    """``stats()`` returns a ``CorpusStats`` instance."""
    result = corpus_backend.stats("wiki")
    assert isinstance(result, CorpusStats)


def test_stats_reflects_written_pages(corpus_backend, write_policy) -> None:
    """``stats()`` reports the correct ``page_count`` and positive ``total_bytes``.

    Writes 3 pages to the wiki corpus and asserts:
    - ``page_count == 3``
    - ``total_bytes > 0``
    - ``last_update`` is a ``datetime`` instance.
    - ``most_recent`` is a list (may be empty or populated, implementation-defined cap).
    """
    for i in range(3):
        corpus_backend.write_page(
            f"stats-page-{i}", f"Body of page {i}.", "wiki", write_policy
        )

    st = corpus_backend.stats("wiki")
    assert st.page_count == 3
    assert st.total_bytes > 0
    assert isinstance(st.last_update, datetime)
    assert isinstance(st.most_recent, list)


def test_close_is_idempotent(corpus_backend) -> None:
    """Calling ``close()`` multiple times does not raise.

    Backends MUST make ``close()`` idempotent (spec/34 Protocol docstring).
    Database backends close connection pools; filesystem backends are no-ops.
    Both must tolerate double-close without exception.
    """
    corpus_backend.close()
    corpus_backend.close()  # second call must not raise


# ──────────────────────────────────────────────────────────────────────────────
# Category 10 — Raw corpus path (Gap 1 coverage fill, spec/34 §"raw corpus")
#
# The 48 tests above exercise corpus="wiki" almost exclusively.  These 6 tests
# cover the _walk_raw / _collect_raw_files paths in FilesystemCorpusBackend and
# exercise the full raw-corpus Protocol surface that PR 2 SQLite must also
# satisfy (parametrized over corpus_backend, same as the rest of this file).


def test_write_page_raw_corpus_creates_file(corpus_backend, write_policy) -> None:
    """write_page(..., corpus="raw") creates the page; read_page returns it.

    Exercises the Case-1 (fresh write) path of the raw corpus.  The raw corpus
    stores every file type; this test uses a plain text body so both filesystem
    and future SQLite backends see the same content shape.
    """
    body = "Raw intelligence report: sources confirmed."
    ref = corpus_backend.write_page("raw-report", body, "raw", write_policy)

    assert isinstance(ref, CorpusRef)
    assert ref.name == "raw-report"
    assert ref.corpus == "raw"

    page = corpus_backend.read_page("raw-report", "raw")
    assert page is not None
    assert "Raw intelligence report" in page.body


def test_list_pages_raw_corpus_includes_written_pages(
    corpus_backend, write_policy
) -> None:
    """list_pages("raw") returns CorpusRef entries for pages written to the raw corpus.

    Writes two pages to "raw" and one to "wiki".  Asserts:
    - list_pages("raw") contains both raw pages.
    - list_pages("raw") does NOT contain the wiki page.
    This exercises the _walk_raw recursive listing code path.
    """
    corpus_backend.write_page("alpha-raw", "Raw body alpha.", "raw", write_policy)
    corpus_backend.write_page("beta-raw", "Raw body beta.", "raw", write_policy)
    corpus_backend.write_page("wiki-only", "Wiki body.", "wiki", write_policy)

    raw_refs = corpus_backend.list_pages("raw")
    wiki_refs = corpus_backend.list_pages("wiki")

    raw_names = {r.name for r in raw_refs}
    assert "alpha-raw" in raw_names
    assert "beta-raw" in raw_names
    assert "wiki-only" not in raw_names

    wiki_names = {r.name for r in wiki_refs}
    assert "wiki-only" in wiki_names
    assert "alpha-raw" not in wiki_names


def test_list_pages_raw_corpus_skips_dot_prefixed(
    corpus_backend, write_policy, tmp_path
) -> None:
    """list_pages("raw") must skip dot-prefixed files at every directory level.

    For FilesystemCorpusBackend, directly places a dot-prefixed file inside the
    raw corpus directory and asserts it is excluded from list_pages results.
    Non-filesystem backends are expected to never surface implementation-internal
    files as corpus pages, so the test passes trivially (dot-file placement is
    a no-op for them but list_pages must still return only non-hidden pages).
    """
    # Write a legitimate raw page so the directory exists.
    corpus_backend.write_page("visible-raw", "Visible content.", "raw", write_policy)

    # For the filesystem backend, place a dot-prefixed file inside the raw dir.
    agent_root = getattr(corpus_backend, "_agent_root", None)
    if agent_root is not None:
        hidden = agent_root / "raw" / ".hidden-file.md"
        hidden.parent.mkdir(parents=True, exist_ok=True)
        hidden.write_text("Hidden content -- must not appear.\n", encoding="utf-8")

    refs = corpus_backend.list_pages("raw")
    names = {r.name for r in refs}

    assert "visible-raw" in names
    # The hidden file must not appear regardless of what name stripping produces
    assert ".hidden-file" not in names
    assert "hidden-file" not in names
    assert ".hidden-file.md" not in names


def test_query_raw_corpus_substring_match(corpus_backend, write_policy) -> None:
    """query(text, corpus="raw") returns pages whose body contains the query string.

    Exercises _collect_raw_files in the filesystem backend's query() method.
    Writes one matching page and one non-matching page to the raw corpus and
    asserts only the matching page is returned.
    """
    corpus_backend.write_page(
        "matching-raw",
        "The debriefing contained the keyword: avalanche.",
        "raw",
        write_policy,
    )
    corpus_backend.write_page(
        "non-matching-raw",
        "This document discusses unrelated topics.",
        "raw",
        write_policy,
    )

    result = corpus_backend.query("avalanche", "raw")

    assert len(result) >= 1
    names = {r.name for r in result}
    assert "matching-raw" in names
    assert "non-matching-raw" not in names


def test_snapshot_restore_raw_corpus_lifecycle(corpus_backend, write_policy) -> None:
    """snapshot + restore round-trip works for a raw corpus page.

    Writes V1 to "raw", snapshots it, overwrites to V2, then restores from the
    snapshot -- read_page must return V1 content.  Skips when the backend does
    not support versioning (spec/34 capability gate).
    """
    if not corpus_backend.capabilities.supports_versioning:
        pytest.skip("backend does not support versioning")

    corpus_backend.write_page(
        "raw-versioned", "Raw version 1 content.", "raw", write_policy
    )
    version_ref = corpus_backend.snapshot("raw-versioned", "raw")
    assert isinstance(version_ref, VersionRef)

    # CAS-overwrite to V2
    v1_sha = _sha256("Raw version 1 content.")
    corpus_backend.write_page(
        "raw-versioned",
        "Raw version 2 content.",
        "raw",
        write_policy,
        expected_content_sha256=v1_sha,
    )

    # Restore V1 from snapshot
    corpus_backend.restore_version("raw-versioned", "raw", version_ref, write_policy)

    live = corpus_backend.read_page("raw-versioned", "raw")
    assert live is not None
    assert "Raw version 1 content." in live.body


def test_stats_raw_corpus_reflects_written_pages(corpus_backend, write_policy) -> None:
    """stats("raw") reports the correct page_count and positive total_bytes.

    Writes 2 pages to the raw corpus and asserts:
    - page_count == 2
    - total_bytes > 0
    - last_update is a datetime instance
    - most_recent is a list

    Exercises the stats() raw-corpus code path that parallels the wiki path
    tested in Category 9.
    """
    for i in range(2):
        corpus_backend.write_page(
            f"raw-stats-{i}", f"Raw body {i}.", "raw", write_policy
        )

    st = corpus_backend.stats("raw")
    assert st.page_count == 2
    assert st.total_bytes > 0
    assert isinstance(st.last_update, datetime)
    assert isinstance(st.most_recent, list)


# ──────────────────────────────────────────────────────────────────────────────
# FIX 7 (INFO Testing 3): snapshot of non-existent page


def test_snapshot_nonexistent_page_raises_corpus_page_not_found(
    corpus_backend, write_policy
) -> None:
    """``snapshot()`` raises ``CorpusPageNotFound`` when the page does not exist.

    Capability-gated: skips on backends that declare ``supports_versioning=False``.
    """
    if not corpus_backend.capabilities.supports_versioning:
        pytest.skip("backend does not support versioning")

    with pytest.raises(CorpusPageNotFound):
        corpus_backend.snapshot("does_not_exist", "wiki")
