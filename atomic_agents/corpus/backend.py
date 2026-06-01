"""CorpusBackend Protocol -- the contract every corpus backend satisfies.

This is the eleventh open Protocol in the protocol-pattern series alongside
MemoryBackend (#57), LLMBackend (#87), JudgeBackend (#112), LockBackend (#60),
LogBackend (#61), AgentProfileBackend (#63), ToolRegistryBackend (#64),
MandateBackend (#124), PolicyBackend (#89), and PersonaBackend (#62).

``CorpusBackend`` abstracts ``<agent>/wiki/`` (Atomic Wiki -- distilled
knowledge in the Karpathy style) and ``<agent>/raw/`` (source documents --
PDFs, transcripts, operator-ingested content) behind a Protocol so the
framework core stays small and alternate storage substrates (SQLite-FTS5,
Postgres, pgvector) drop in without forking.

All four PRs of issue #65 have shipped. spec/34 is locked. The 9-MUST Implementer
Contract is final. See ``docs/spec/34-corpus-backend.md`` for the normative contract.

``VersionRef`` and ``WritePolicy`` are re-used verbatim from
``atomic_agents/memory/backend.py`` for cross-Protocol uniformity (Premise P7).

See ``docs/spec/34-corpus-backend.md`` for the full normative contract.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from .types import (
    CorpusCapabilities,
    CorpusPage,
    CorpusRef,
    CorpusStats,
)
from ..memory.backend import VersionRef, WritePolicy


# ──────────────────────────────────────────────────────────────────────────────
# Protocol definition


@runtime_checkable
class CorpusBackend(Protocol):
    """Contract every corpus backend implementation must satisfy.

    Implementations MUST NOT subclass this Protocol -- it is structural.
    Implementations satisfy it by exposing the methods below with the
    documented behavior. The ``@runtime_checkable`` decorator enables
    ``isinstance(obj, CorpusBackend)`` to perform a method-presence check
    (not a signature check -- signatures are static-typing's job).

    Every method that takes ``name`` validates it against the charset rule
    ``[a-zA-Z0-9_.+@-]+`` at the API boundary BEFORE any storage access.
    Path-traversal tokens (``..``, ``/``, ``\\``), control characters
    (``\\x00``-``\\x1f``, ``\\x7f``), leading dots, and empty strings are
    refused -- raise ``CorpusInvalidName``.

    Every method that takes ``corpus`` validates it is one of
    ``{"wiki", "raw"}`` -- raise ``ValueError`` otherwise.

    Capability-gated behavior is declared via ``capabilities``. The
    conformance suite gates tests on the flags. Backends that misreport
    capabilities produce silent failures rather than loud refusals
    (spec/34 Implementer Contract MUST -- finalized at PR 4 LOCK, per
    PersonaBackend precedent at spec/33).
    """

    # ─── Capability advertisement ─────────────────────────────────────────

    @property
    def capabilities(self) -> CorpusCapabilities:
        """Advertise what this backend instance supports.

        Returns a frozen ``CorpusCapabilities`` dataclass. The values are a
        contract, not a hint -- conformance tests assert claim-vs-behavior
        parity. Backends that declare ``supports_versioning=False`` MUST raise
        ``NotImplementedError`` on the full snapshot trio. Backends that
        declare ``supports_full_text_search=True`` MUST use indexed FTS when
        ``supports_semantic_search`` is False.

        This property MUST return the same value across calls for the lifetime
        of the backend instance.
        """
        ...

    # ─── Read operations ──────────────────────────────────────────────────

    def list_pages(
        self,
        corpus: Literal["wiki", "raw"],
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[CorpusRef]:
        """Return lightweight page references for the given corpus.

        Returns up to ``limit`` ``CorpusRef`` entries sorted by
        ``last_modified`` descending. ``offset`` supports paging through large
        corpora without loading all page bodies.

        ``FilesystemCorpusBackend`` MUST skip ``INDEX.md`` and any dot-prefixed
        entries (``.versions/``, ``.gitkeep``, etc.) when walking the
        directory. This mirrors the exclusion pattern ``persona/filesystem.py``
        uses for dot-prefixed entries so operators learn the convention once.

        Returns an empty list when the corpus directory does not exist or is
        empty. Never raises on a missing corpus directory.
        """
        ...

    def read_page(
        self,
        name: str,
        corpus: Literal["wiki", "raw"],
    ) -> CorpusPage | None:
        """Return the full ``CorpusPage`` for the named page, or ``None``.

        Returns ``None`` when the page does not exist. This is the routine
        presence-check return convention (distinct from ``read_version``, which
        raises ``CorpusVersionNotFound`` on failure because a missing version
        body indicates an unexpected infrastructure failure, not a routine
        presence check). See spec/34 D12 and the ``read_version`` docstring.

        ``name`` is the bare filename stem (e.g., ``"avalanche-vs-snowball"``).
        ``corpus`` must be ``"wiki"`` or ``"raw"``.
        """
        ...

    def render_index_summary(
        self,
        corpus: Literal["wiki", "raw"],
    ) -> str:
        """Return the corpus's INDEX-equivalent content as a string.

        For ``corpus="wiki"``, ``FilesystemCorpusBackend`` returns the content
        of ``wiki/INDEX.md`` verbatim when it exists. ``SQLiteCorpusBackend``
        synthesizes equivalent prose from page metadata (title, description,
        last_modified). The caller cannot distinguish the two; both satisfy the
        contract.

        Returns an **empty string** when:

        - The corpus directory does not exist, OR
        - The directory exists but has no ``INDEX.md`` analog.

        ``render_index_summary("raw")`` returns an empty string on
        ``FilesystemCorpusBackend``; raw corpora typically have no INDEX
        equivalent. The empty-string contract lets callers branch on truthiness
        the same way they branch on the legacy ``wiki_index.read_text()``
        direct-read pattern, so the PR 3 call-site migration at
        ``agent.py:2937-2939`` and ``bundle.py:_render_memory_breakpoint`` is
        a single ``if self.corpus_backend is None:`` branch.

        This is the primary migration target for ``agent.py:2937-2939`` (PR 3).
        """
        ...

    # ─── Write operations ─────────────────────────────────────────────────

    def write_page(
        self,
        name: str,
        content: str,
        corpus: Literal["wiki", "raw"],
        policy: WritePolicy,
        *,
        frontmatter: dict | None = None,
        expected_content_sha256: str | None = None,
    ) -> CorpusRef:
        """Write a corpus page, enforcing the 4-case behavior table (finding CQ1).

        Mirrors ``MemoryBackend.write_note``'s idempotency and CAS (compare-
        and-swap) discipline so operators reason about writes the same way
        across both Protocols.

        **Case 1 -- fresh write**
            Trigger: page does not exist at ``(name, corpus)``.
            Action: Write page via ``_io.atomic_write``; update
            INDEX-equivalent.

        **Case 2 -- content-identical idempotent no-op**
            Trigger: page exists; body + frontmatter SHA-256 unchanged.
            Action: No-op. Safe under crash recovery and re-delivery.
            Returns the existing ``CorpusRef`` without touching storage.

        **Case 3 -- explicit overwrite via CAS**
            Trigger: page exists; content differs; ``expected_content_sha256``
            matches the current on-disk (or SQL) SHA-256 of the page body +
            frontmatter.
            Action: Snapshot the existing version if
            ``capabilities.supports_versioning=True``; write new content via
            ``_io.atomic_write``; update INDEX-equivalent.

        **Case 4 -- collision (safe default refusal)**
            Trigger: page exists; content differs; ``expected_content_sha256``
            is ``None`` OR does not match the current hash.
            Action: Raise ``CorpusPageExists`` when no expected hash was
            supplied. Raise ``CorpusPreconditionFailed`` when a hash was
            supplied but it did not match the current on-disk value.

        CAS via ``expected_content_sha256`` is the **only** safe overwrite
        path. Silent overwrite without explicit operator intent is refused by
        default. This mirrors ``MemoryPreconditionFailed`` (spec/20:318) in
        naming and semantics.

        All writes MUST go through ``_io.atomic_write`` (prep-pass SEVERE S5).
        Never ``target.write_text()`` directly; partial writes are visible to
        concurrent readers on POSIX between ``open()`` and ``flush()``.
        """
        ...

    # ─── Versioning (capability-gated) ───────────────────────────────────

    def list_versions(
        self,
        name: str,
        corpus: Literal["wiki", "raw"],
    ) -> list[VersionRef]:
        """Return version references for the named page, newest first.

        Returns a list of opaque ``VersionRef`` tokens. Callers must never
        parse or construct ``VersionRef.backend_id`` directly; tokens are
        passed back to ``read_version`` and ``restore_version`` only.

        Backends with ``capabilities.supports_versioning=False`` MUST raise
        ``NotImplementedError``. The conformance suite gates this test on the
        capability flag.
        """
        ...

    def read_version(
        self,
        version_ref: VersionRef,
    ) -> CorpusPage:
        """Return the full ``CorpusPage`` from a version snapshot.

        Unlike ``read_page``, which returns ``None`` for a missing page,
        ``read_version`` **raises** ``CorpusVersionNotFound`` when the version
        body is not accessible. This distinction is intentional (spec/34 D12):

        - A missing page is a routine presence check -- callers should expect
          it and branch on the ``None`` return.
        - A missing version body indicates an unexpected infrastructure failure,
          specifically the case where a SQL row exists in the versions table
          but the on-disk body file has been deleted or is unreadable under the
          hybrid storage shape (SQL metadata + bodies on disk). This is a real
          production scenario distinct from "version does not exist" and is
          named explicitly so the Implementer Contract covers it.

        ``CorpusVersionNotFound`` is raised for BOTH sub-cases:
        (a) the version reference does not exist at all, OR
        (b) the version body file is missing or unreadable under the hybrid
        storage shape.

        This matches the ``MemoryBackend`` precedent: ``read_note`` returns
        ``None``; ``read_version`` raises.
        """
        ...

    def restore_version(
        self,
        name: str,
        corpus: Literal["wiki", "raw"],
        version_ref: VersionRef,
        policy: WritePolicy,
    ) -> CorpusRef:
        """Restore the page content at ``version_ref`` as the live version.

        Internally calls the write path via the CAS (Case 3) logic so the
        existing live version is snapshotted before the restore lands (making
        the restore itself reversible). The ``policy`` parameter is enforced
        on the write target.

        Returns a ``CorpusRef`` pointing at the restored live page.

        Raises ``CorpusPageNotFound`` when the page named ``name`` does not
        exist. Raises ``CorpusVersionNotFound`` when ``version_ref`` is not
        accessible (see ``read_version`` for the two sub-cases).

        Backends with ``capabilities.supports_versioning=False`` MUST raise
        ``NotImplementedError``.
        """
        ...

    def snapshot(
        self,
        name: str,
        corpus: Literal["wiki", "raw"],
        *,
        label: str | None = None,
    ) -> VersionRef:
        """Create an explicit version snapshot of the current page content.

        Captures the current body + frontmatter and stores it as an immutable
        version snapshot. The optional ``label`` is a human-readable tag stored
        alongside the snapshot metadata (e.g., ``"pre-major-edit"``).

        Returns the opaque ``VersionRef`` token for the new snapshot. The token
        is monotonic / sortable so ``list_versions`` can return chronological
        order without a secondary sort key.

        Raises ``CorpusPageNotFound`` when the page named ``name`` does not
        exist (cannot snapshot a page that has never been written or has since
        been deleted).

        Backends with ``capabilities.supports_versioning=False`` MUST raise
        ``NotImplementedError``.
        """
        ...

    # ─── Search ───────────────────────────────────────────────────────────

    def query(
        self,
        text: str,
        corpus: Literal["wiki", "raw"],
        *,
        top_k: int = 10,
    ) -> list[CorpusRef]:
        """Search the corpus and return the top-k matching page references.

        Behavior follows the fixed capability-precedence rule (spec/34 D10,
        finding A2). The rule is mandatory so the Protocol contract is
        unambiguous for Postgres backends that may have both pgvector AND
        tsquery available simultaneously:

        **Tier 1 -- semantic search (``supports_semantic_search=True``)**
            MUST use embedding-vector cosine match (pgvector or equivalent).
            FTS infrastructure is NOT exercised on this code path even if
            ``supports_full_text_search`` is also ``True``.
            **Semantic WINS when both flags are True** -- this is the explicit
            precedence rule, not a hint.

        **Tier 2 -- full-text search (``supports_semantic_search=False``,
        ``supports_full_text_search=True``)**
            MUST use indexed full-text search (SQLite FTS5, Postgres tsquery,
            or equivalent). Ships in PR 2 via ``SQLiteCorpusBackend``.

        **Tier 3 -- substring fallback (both flags False)**
            MUST fall back to case-insensitive substring match on page body +
            title + frontmatter-tag match, ordered by match count descending.
            This is the ``FilesystemCorpusBackend`` default (Premise P3).

        There is no caller-choice ``mode`` kwarg in v1.0; the precedence rule
        is the contract. v1.1+ may add a ``mode: Literal["auto", "semantic",
        "fts"]`` kwarg if operators surface a real need for "exact FTS match
        even when semantic is available" (e.g., literal-string lookup). A
        default of ``"auto"`` would preserve v1.0 precedence behavior with no
        breaking change.

        Returns up to ``top_k`` ``CorpusRef`` entries. Returns an empty list
        when no pages match.
        """
        ...

    # ─── Stats ────────────────────────────────────────────────────────────

    def stats(self, corpus: Literal["wiki", "raw"]) -> CorpusStats:
        """Return per-corpus health and statistics.

        Used by ``doctor.check_corpus_backend`` to surface the page-count
        cliff WARN: when ``capabilities.supports_full_text_search=False`` AND
        ``page_count`` exceeds ~1000 pages, the doctor emits a WARN with an
        actionable hint to pin ``SQLiteCorpusBackend`` (finding P1 from
        /plan-eng-review 2026-05-29).

        ``CorpusStats.most_recent`` is populated with the top-N
        ``CorpusRef`` entries by ``last_modified`` (descending). Backends that
        do not have a physical ``INDEX.md`` (e.g., ``SQLiteCorpusBackend``)
        use ``most_recent`` to synthesize the INDEX-equivalent content for
        ``render_index_summary``.
        """
        ...

    # ─── Lifecycle ────────────────────────────────────────────────────────

    def close(self) -> None:
        """Release any resources held by this backend instance.

        Called by the framework when the agent run ends or the backend is
        replaced. Implementations MUST make this method idempotent -- calling
        ``close()`` twice must not raise. Database backends close connection
        pools; filesystem backends are typically no-ops.
        """
        ...


# ──────────────────────────────────────────────────────────────────────────────
# Registry note

# Backend registry lives in atomic_agents/corpus/__init__.py:
#   register_corpus_backend(backend_id, cls)
#   unregister_corpus_backend(backend_id)
#   get_corpus_backend(backend_id) -> type[CorpusBackend]
#   list_corpus_backends() -> list[str]
#   get_default_corpus_backend(agent_root) -> CorpusBackend
# See finding A4 from /plan-eng-review 2026-05-29 for the placement decision.
# Dominant 6-of-10 precedent: Lock / Log / Profile / ToolRegistry / Judge / LLM
# all place register_X_backend in __init__.py, not backend.py.
