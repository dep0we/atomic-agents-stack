"""FilesystemCorpusBackend-specific tests.

Tests that exercise filesystem-specific implementation details:
- On-disk layout (where pages and snapshots live)
- INDEX.md skip rule for list_pages
- URL factory pattern + credential redaction
- Sample data parsing (against docs/samples/caldwell/wiki/)
- Side-effect-free construction
- atomic_write usage (no partial writes)

Per spec/34 §"Test coverage" + design doc + prep finding S2 (sample data parsing).
"""

from __future__ import annotations

import hashlib
import re
import shutil
from datetime import date
from pathlib import Path

import pytest

from atomic_agents.corpus.filesystem import (
    FilesystemCorpusBackend,
    make_filesystem_corpus_backend_from_url,
)
from atomic_agents.memory.backend import VersionRef, WritePolicy
from atomic_agents.exceptions import (
    CorpusCorrupted,
    CorpusInvalidName,
    CorpusPageNotFound,
    CorpusVersionNotFound,
    WritePathViolation,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_WIKI = REPO_ROOT / "docs" / "samples" / "caldwell" / "wiki"


# ─────────────────────────────────────────────────────────────────────────────
# Side-effect-free construction (finding H1)


def test_init_does_not_create_directories(tmp_path: Path) -> None:
    """Constructor with a non-existent agent_root path succeeds without
    creating any directories.

    spec/34 implementer contract MUST #2 -- side-effect-free construction.
    """
    non_existent = tmp_path / "ghost-agent-root"
    assert not non_existent.exists()

    backend = FilesystemCorpusBackend(non_existent)

    assert backend is not None
    assert not non_existent.exists(), (
        "FilesystemCorpusBackend.__init__ must not create directories; "
        f"{non_existent} was created"
    )


def test_init_does_not_touch_filesystem(tmp_path: Path) -> None:
    """Constructor with a valid existing path does not stat, mkdir, or walk the path.

    Verifies by recording the directory listing before and after construction;
    it must be byte-identical.
    """
    (tmp_path / "existing_file.txt").write_text("sentinel", encoding="utf-8")
    before = sorted(p.name for p in tmp_path.iterdir())

    backend = FilesystemCorpusBackend(tmp_path)

    after = sorted(p.name for p in tmp_path.iterdir())
    assert before == after, (
        "FilesystemCorpusBackend.__init__ must not touch the filesystem; "
        f"directory listing changed: before={before!r} after={after!r}"
    )
    assert backend is not None


# ─────────────────────────────────────────────────────────────────────────────
# On-disk layout


def test_write_page_creates_wiki_file_at_expected_path(tmp_path: Path) -> None:
    """After write_page(name='topic', corpus='wiki'), the file exists at
    <agent_root>/wiki/topic.md with the expected content.

    spec/34 §"FilesystemCorpusBackend storage layout".
    """
    backend = FilesystemCorpusBackend(tmp_path)
    content = "This is the topic page body."
    policy = WritePolicy(write_paths=[tmp_path])

    backend.write_page(
        name="topic",
        content=content,
        corpus="wiki",
        policy=policy,
    )

    expected_path = tmp_path / "wiki" / "topic.md"
    assert expected_path.exists(), (
        f"Expected page file at {expected_path} after write_page; file not found"
    )
    on_disk = expected_path.read_text(encoding="utf-8")
    assert content in on_disk, f"Page body not found in on-disk content: {on_disk!r}"


def test_version_layout_matches_spec(tmp_path: Path) -> None:
    """After a CAS overwrite, a version snapshot exists at:
    <agent_root>/<corpus>/.versions/<page-stem>/<YYYYMMDDTHHMMSSffffffZ>_<8hex>.md

    spec/34 §"FilesystemCorpusBackend storage layout" + implementer contract MUST #8.
    """
    import hashlib

    backend = FilesystemCorpusBackend(tmp_path)
    policy = WritePolicy(write_paths=[tmp_path])

    # Write the initial page
    first_content = "First version of this page."
    backend.write_page(
        name="mypage",
        content=first_content,
        corpus="wiki",
        policy=policy,
    )

    # Get the current SHA-256 so we can do a CAS overwrite
    page_path = tmp_path / "wiki" / "mypage.md"
    on_disk = page_path.read_text(encoding="utf-8")
    existing_sha = hashlib.sha256(on_disk.encode("utf-8")).hexdigest()

    # CAS overwrite triggers snapshot creation
    backend.write_page(
        name="mypage",
        content="Updated version of this page.",
        corpus="wiki",
        policy=policy,
        expected_content_sha256=existing_sha,
    )

    # Verify the snapshot lives at the spec-mandated location
    versions_dir = tmp_path / "wiki" / ".versions" / "mypage"
    assert versions_dir.is_dir(), (
        f"Expected .versions directory at {versions_dir}; not found"
    )

    snapshot_files = list(versions_dir.iterdir())
    assert len(snapshot_files) == 1, (
        f"Expected exactly one snapshot file in {versions_dir}; got {snapshot_files}"
    )

    snapshot_file = snapshot_files[0]
    # Filename format: <YYYYMMDDTHHMMSSffffffZ>_<8hex>.md
    version_pattern = re.compile(r"^\d{8}T\d{6}\d{6}Z_[0-9a-f]{8}\.md$")
    assert version_pattern.match(snapshot_file.name), (
        f"Snapshot filename {snapshot_file.name!r} does not match expected format "
        r"<YYYYMMDDTHHMMSSffffffZ>_<8hex>.md"
    )


# ─────────────────────────────────────────────────────────────────────────────
# INDEX.md skip rule (LOW from prep pass)


def test_list_pages_skips_index_md_and_dot_prefixed_entries(tmp_path: Path) -> None:
    """list_pages('wiki') skips INDEX.md and dot-prefixed entries like .versions/.

    spec/34 §"FilesystemCorpusBackend storage layout" + prep-pass LOW finding
    (INDEX.md skip rule).
    """
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()

    # Create INDEX.md (must be excluded from list_pages results)
    (wiki_dir / "INDEX.md").write_text("# Index\nThis is the index.", encoding="utf-8")

    # Create a .versions directory (dot-prefixed; must be excluded)
    (wiki_dir / ".versions").mkdir()
    (wiki_dir / ".versions" / "somefile.md").write_text(
        "Version body", encoding="utf-8"
    )

    # Create a real content page (must be included)
    (wiki_dir / "page1.md").write_text("# Page One\nSome content.", encoding="utf-8")

    backend = FilesystemCorpusBackend(tmp_path)
    refs = backend.list_pages("wiki")

    names = [r.name for r in refs]
    assert names == ["page1"], (
        f"list_pages('wiki') should return only ['page1']; got {names!r}. "
        "INDEX.md and dot-prefixed entries must be excluded."
    )


# ─────────────────────────────────────────────────────────────────────────────
# URL factory (finding H2)


def test_url_factory_filesystem_scheme_accepted(tmp_path: Path) -> None:
    """make_filesystem_corpus_backend_from_url('filesystem:///path') returns a
    FilesystemCorpusBackend instance.
    """
    url = f"filesystem://{tmp_path}"
    backend = make_filesystem_corpus_backend_from_url(url)

    assert isinstance(backend, FilesystemCorpusBackend)
    assert backend.backend_id == "filesystem"


def test_url_factory_rejects_non_filesystem_scheme() -> None:
    """Non-filesystem schemes (sqlite://, postgres://) raise ValueError."""
    with pytest.raises(ValueError):
        make_filesystem_corpus_backend_from_url("sqlite:///path/to/db")

    with pytest.raises(ValueError):
        make_filesystem_corpus_backend_from_url("postgres://host/db")


def test_url_factory_credential_redaction() -> None:
    """Error message from URL factory does NOT contain raw credentials.

    Operators may accidentally paste credentialed URLs; the error message
    must be safe to surface in doctor output and CI logs (spec/34 MUST #7).
    """
    bad_url = "postgres://user:pass@host/db"
    with pytest.raises(ValueError) as exc_info:
        make_filesystem_corpus_backend_from_url(bad_url)

    error_message = str(exc_info.value)
    assert "pass" not in error_message, (
        f"Credential 'pass' leaked into the error message: {error_message!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Sample data parsing (finding S2 verification)


def test_read_page_parses_caldwell_sample_named_fields(tmp_path: Path) -> None:
    """read_page parses all named CorpusPage fields from the real Caldwell sample.

    Copies avalanche_vs_snowball.md into an isolated tmp wiki dir, then reads
    it back via the backend and asserts all named frontmatter fields are
    populated correctly.

    Per prep-pass finding S2 -- named fields must map correctly so callers
    querying 'is this page still valid?' don't have to parse extra_frontmatter.
    """
    import datetime

    sample_file = SAMPLE_WIKI / "avalanche_vs_snowball.md"
    if not sample_file.exists():
        pytest.skip("sample data not present")

    # Set up isolated wiki directory
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    shutil.copy(sample_file, wiki_dir / "avalanche_vs_snowball.md")

    backend = FilesystemCorpusBackend(tmp_path)
    page = backend.read_page("avalanche_vs_snowball", "wiki")

    assert page is not None, "read_page returned None for a page that exists"

    # Verify the ref fields
    assert page.ref.name == "avalanche_vs_snowball"
    assert page.ref.corpus == "wiki"

    # Verify all named frontmatter fields map correctly
    assert page.schema_version == 1
    assert page.name == "Avalanche vs Snowball debt payoff methods"
    assert (
        page.description
        == "Two approaches to debt elimination — math-optimal vs psychology-optimal"
    )
    assert page.type == "wiki_page"

    # captured is a date (not datetime) -- PyYAML bare ISO date behavior (prep-pass S2)
    assert isinstance(page.captured, datetime.date), (
        f"captured should be datetime.date (not datetime), got {type(page.captured)}"
    )
    assert page.captured == datetime.date(2026, 4, 22)

    assert isinstance(page.last_seen, datetime.date), (
        f"last_seen should be datetime.date, got {type(page.last_seen)}"
    )
    assert page.last_seen == datetime.date(2026, 5, 4)

    # sources is a list of strings
    assert isinstance(page.sources, list), (
        f"sources should be a list, got {type(page.sources)}"
    )
    assert len(page.sources) == 2
    assert "raw/financial_freedom_book_ch7.md" in page.sources
    assert "raw/cpa_advice_2026-04-15.md" in page.sources

    assert page.provenance == "distilled"
    assert page.confidence == "high"

    # pinned: false in frontmatter -> Python False
    assert page.pinned is False, (
        f"pinned should be False (from frontmatter 'pinned: false'), got {page.pinned!r}"
    )

    # related is a list of strings
    assert isinstance(page.related, list), (
        f"related should be a list, got {type(page.related)}"
    )
    assert len(page.related) == 2

    # expires_at: null in frontmatter -> Python None
    assert page.expires_at is None, (
        f"expires_at should be None (from frontmatter 'expires_at: null'), "
        f"got {page.expires_at!r}"
    )

    # tags is a list
    assert isinstance(page.tags, list), f"tags should be a list, got {type(page.tags)}"
    assert set(page.tags) == {"debt", "methodology", "payoff"}


def test_caldwell_sample_no_spillage_to_extra_frontmatter(tmp_path: Path) -> None:
    """After parsing the Caldwell sample, extra_frontmatter is empty.

    Every key in avalanche_vs_snowball.md maps to a named CorpusPage field.
    No key should spill into extra_frontmatter (prep-pass finding S3: callers
    must not need to parse the dict for structural lifecycle semantics).
    """
    sample_file = SAMPLE_WIKI / "avalanche_vs_snowball.md"
    if not sample_file.exists():
        pytest.skip("sample data not present")

    # Set up isolated wiki directory
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    shutil.copy(sample_file, wiki_dir / "avalanche_vs_snowball.md")

    backend = FilesystemCorpusBackend(tmp_path)
    page = backend.read_page("avalanche_vs_snowball", "wiki")

    assert page is not None, "read_page returned None for a page that exists"
    assert page.extra_frontmatter == {}, (
        f"Every key in the Caldwell sample frontmatter should map to a named "
        f"CorpusPage field; unexpected spillage into extra_frontmatter: "
        f"{page.extra_frontmatter!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Bug 1 — WritePolicy enforcement (CRITICAL C1 + Adversarial AD9)


def test_write_page_respects_write_policy_allow(tmp_path: Path) -> None:
    """write_page succeeds when the page path is under a policy write_path.

    WritePolicy.write_paths=[corpus_subdir] → write_page must allow the write.
    If WritePolicy were dead (Bug 1), this test would still pass — the allow
    case exercises the happy path. The complementary refuse test below is the
    real regression gate.
    """
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir(parents=True, exist_ok=True)

    backend = FilesystemCorpusBackend(tmp_path)
    # Allow writes under the wiki subdir specifically
    policy = WritePolicy(write_paths=[wiki_dir])

    ref = backend.write_page(
        name="allowed-page",
        content="Hello from an allowed write.",
        corpus="wiki",
        policy=policy,
    )

    assert ref.name == "allowed-page"
    assert (wiki_dir / "allowed-page.md").is_file(), (
        "write_page should have created the file when policy allows it"
    )


def test_write_page_refuses_when_outside_write_policy(tmp_path: Path) -> None:
    """write_page raises WritePathViolation when the page is outside all policy write_paths.

    WritePolicy(write_paths=[/tmp/unrelated]) → write to wiki/ must be refused.
    Bug 1: before the fix, this call would succeed because WritePolicy was never
    checked. After the fix, WritePathViolation is raised.
    """
    backend = FilesystemCorpusBackend(tmp_path)
    unrelated = tmp_path / "unrelated-dir"
    unrelated.mkdir()
    policy = WritePolicy(write_paths=[unrelated])

    with pytest.raises(WritePathViolation):
        backend.write_page(
            name="forbidden-page",
            content="This write should be rejected by WritePolicy.",
            corpus="wiki",
            policy=policy,
        )


def test_restore_version_respects_write_policy(tmp_path: Path) -> None:
    """restore_version raises WritePathViolation when the target is outside policy write_paths.

    This exercises the write-policy enforcement path on restore_version specifically
    (which delegates to write_page after reading the snapshot).
    """
    import hashlib

    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir(parents=True, exist_ok=True)

    backend = FilesystemCorpusBackend(tmp_path)
    permissive_policy = WritePolicy(write_paths=[tmp_path])

    # Write initial page
    backend.write_page(
        name="restore-target",
        content="Version one.",
        corpus="wiki",
        policy=permissive_policy,
    )

    # Take a snapshot of it
    version_ref = backend.snapshot("restore-target", "wiki")

    # Overwrite to create a different live version
    page_path = wiki_dir / "restore-target.md"
    existing_sha = hashlib.sha256(page_path.read_bytes()).hexdigest()
    backend.write_page(
        name="restore-target",
        content="Version two.",
        corpus="wiki",
        policy=permissive_policy,
        expected_content_sha256=existing_sha,
    )

    # Now try to restore with a restrictive policy
    unrelated = tmp_path / "unrelated-dir"
    unrelated.mkdir()
    restrictive_policy = WritePolicy(write_paths=[unrelated])

    with pytest.raises(WritePathViolation):
        backend.restore_version(
            name="restore-target",
            corpus="wiki",
            version_ref=version_ref,
            policy=restrictive_policy,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Bug 2 — read_version() path traversal (CRITICAL S-C1 + Adversarial AD12)


def test_read_version_refuses_path_traversal_via_backend_id(tmp_path: Path) -> None:
    """read_version raises CorpusVersionNotFound when backend_id contains path traversal.

    A crafted VersionRef like backend_id='wiki/../../../etc/passwd/x.md' must
    NOT resolve to a file outside agent_root. Before the fix (Bug 2), the path
    was constructed without safe_resolve_under, allowing arbitrary file reads.
    After the fix, CorpusVersionNotFound is raised before any file access.
    """
    backend = FilesystemCorpusBackend(tmp_path)

    # Craft a VersionRef whose backend_id encodes a path traversal attempt.
    # Format: "<corpus>/<stem>/<version_filename>" — embed traversal in any segment.
    malicious_ref = VersionRef(backend_id="wiki/../../../etc/passwd/x.md")

    with pytest.raises(CorpusVersionNotFound):
        backend.read_version(malicious_ref)


def test_list_versions_skips_adversarial_filesystem_entries(tmp_path: Path) -> None:
    """list_versions skips entries whose stem does not match the allowed name charset.

    An adversary with filesystem write access can plant files with names like
    '../escape.md' or 'evil;injection.md' in the .versions directory. These
    must be silently skipped rather than returned as VersionRef entries.
    """
    # Set up a wiki page and its .versions directory
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir(parents=True, exist_ok=True)
    (wiki_dir / "mypage.md").write_text("Page body.", encoding="utf-8")

    versions_dir = wiki_dir / ".versions" / "mypage"
    versions_dir.mkdir(parents=True, exist_ok=True)

    # Plant a valid version file (must appear in results)
    valid_name = "20260101T000000000000Z_abcd1234.md"
    (versions_dir / valid_name).write_text("Valid version.", encoding="utf-8")

    # Plant an adversarial entry (must be skipped)
    # Use a name with characters outside the allowed charset
    bad_name = "evil;injection.md"
    (versions_dir / bad_name).write_text("Bad version.", encoding="utf-8")

    backend = FilesystemCorpusBackend(tmp_path)
    refs = backend.list_versions("mypage", "wiki")

    returned_ids = [r.backend_id for r in refs]

    # The valid entry must appear
    assert any(valid_name in bid for bid in returned_ids), (
        f"Valid version {valid_name!r} should appear in list_versions; got {returned_ids!r}"
    )

    # The adversarial entry must NOT appear
    assert not any("evil" in bid for bid in returned_ids), (
        f"Adversarial entry 'evil;injection.md' must be skipped; "
        f"but it appeared in {returned_ids!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Bug 3 — Raw corpus nested-file name collision (CRITICAL AD4 + AD5)


def test_walk_raw_skips_nested_subdirectories(tmp_path: Path) -> None:
    """list_pages('raw') returns only top-level files; nested subdirs are skipped.

    Without the Bug 3 fix, raw/foo.md and raw/subdir/bar.md both appear in
    list_pages with name='foo' and name='bar'. When two files share the same
    stem (e.g., raw/debt/credit-card.md and raw/income/credit-card.md), the
    returned refs have duplicate names that read_page cannot resolve.

    After the fix (flat-only raw), only raw/foo.md appears; raw/subdir/bar.md
    is silently skipped.
    """
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    # Top-level file — must appear
    (raw_dir / "foo.md").write_text("# Foo\nTop-level raw file.", encoding="utf-8")

    # Nested file — must NOT appear
    subdir = raw_dir / "subdir"
    subdir.mkdir()
    (subdir / "bar.md").write_text("# Bar\nNested raw file.", encoding="utf-8")

    backend = FilesystemCorpusBackend(tmp_path)
    refs = backend.list_pages("raw")

    names = [r.name for r in refs]
    assert "foo" in names, (
        f"Top-level 'foo' must appear in list_pages('raw'); got {names!r}"
    )
    assert "bar" not in names, (
        f"Nested 'bar' (raw/subdir/bar.md) must NOT appear in list_pages('raw'); "
        f"got {names!r}"
    )
    assert len(names) == 1, (
        f"Only one page (top-level 'foo') should be returned; got {names!r}"
    )


def test_query_raw_skips_nested_subdirectories(tmp_path: Path) -> None:
    """query('text', 'raw') matches only top-level raw files; nested files are excluded.

    Parallel to test_walk_raw_skips_nested_subdirectories but exercises the
    query() code path (which uses _collect_raw_files internally).
    """
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    # Top-level file with distinctive content — must be found by query
    (raw_dir / "alpha.md").write_text(
        "# Alpha\nThis document discusses the snowball method.", encoding="utf-8"
    )

    # Nested file with matching content — must NOT be found
    subdir = raw_dir / "nested"
    subdir.mkdir()
    (subdir / "beta.md").write_text(
        "# Beta\nThis nested document also discusses the snowball method.",
        encoding="utf-8",
    )

    backend = FilesystemCorpusBackend(tmp_path)
    results = backend.query("snowball", "raw")

    names = [r.name for r in results]
    assert "alpha" in names, (
        f"Top-level 'alpha' matching 'snowball' must appear in query results; "
        f"got {names!r}"
    )
    assert "beta" not in names, (
        f"Nested 'beta' (raw/nested/beta.md) must NOT appear in query results; "
        f"got {names!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# FIX 1 (CRITICAL C1): CorpusCorrupted coverage


def test_read_page_raises_corpus_corrupted_on_malformed_yaml(tmp_path: Path) -> None:
    """read_page raises CorpusCorrupted when the wiki file has broken YAML frontmatter.

    A file with unclosed bracket syntax in frontmatter must raise CorpusCorrupted,
    not silently return None or propagate a raw yaml.YAMLError.
    """
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    bad_fm_content = "---\nkey: [unclosed bracket\n---\nbody text here\n"
    (wiki_dir / "broken-page.md").write_text(bad_fm_content, encoding="utf-8")

    backend = FilesystemCorpusBackend(tmp_path)

    with pytest.raises(CorpusCorrupted):
        backend.read_page("broken-page", "wiki")


def test_read_page_raises_corpus_corrupted_on_non_utf8_bytes(tmp_path: Path) -> None:
    """read_page raises CorpusCorrupted when the wiki file contains non-UTF-8 bytes.

    A file written with raw non-UTF-8 bytes must raise CorpusCorrupted, not
    silently return None or propagate a raw UnicodeDecodeError.
    """
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    bad_bytes = b"\xff\xfe" + b"invalid utf-8 content"
    (wiki_dir / "non-utf8-page.md").write_bytes(bad_bytes)

    backend = FilesystemCorpusBackend(tmp_path)

    with pytest.raises(CorpusCorrupted):
        backend.read_page("non-utf8-page", "wiki")


# ─────────────────────────────────────────────────────────────────────────────
# FIX 2 (MEDIUM AD10): write_page policy violation must not create orphan snapshot


def test_write_page_policy_violation_does_not_create_orphan_snapshot(
    tmp_path: Path,
) -> None:
    """A WritePolicy violation on CAS overwrite must not leave a snapshot file.

    Regression test for the guard-order bug: previously, _take_snapshot was
    called before _enforce_corpus_write_policy, so a policy violation left
    a phantom snapshot entry in .versions/<stem>/.

    After the fix (traversal guard + policy guard BEFORE snapshot), a
    WritePathViolation leaves the .versions/<stem>/ directory either absent
    or unchanged.
    """
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir(parents=True, exist_ok=True)

    backend = FilesystemCorpusBackend(tmp_path)
    permissive_policy = WritePolicy(write_paths=[tmp_path])

    # Case 1: initial write OK
    backend.write_page(
        name="guarded-page",
        content="Initial content.",
        corpus="wiki",
        policy=permissive_policy,
    )

    # Get the current on-disk SHA for CAS
    page_path = wiki_dir / "guarded-page.md"
    existing_sha = hashlib.sha256(page_path.read_bytes()).hexdigest()

    # Build a restrictive policy that excludes the wiki dir
    unrelated = tmp_path / "other-dir"
    unrelated.mkdir()
    restrictive_policy = WritePolicy(write_paths=[unrelated])

    # CAS overwrite with restrictive policy — must raise WritePathViolation
    with pytest.raises(WritePathViolation):
        backend.write_page(
            name="guarded-page",
            content="New content that should never land.",
            corpus="wiki",
            policy=restrictive_policy,
            expected_content_sha256=existing_sha,
        )

    # Assert NO new snapshot was created
    versions_dir = wiki_dir / ".versions" / "guarded-page"
    if versions_dir.exists():
        snapshot_files = [f for f in versions_dir.iterdir() if f.is_file()]
        assert snapshot_files == [], (
            f"Policy violation must not leave orphan snapshots in {versions_dir}; "
            f"found: {snapshot_files}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# FIX 3 (MEDIUM AD10): Date round-trip through _page_to_frontmatter_dict


def test_restore_version_preserves_date_field_types(tmp_path: Path) -> None:
    """restore_version must preserve date fields as date objects, not strings.

    Regression test for the .isoformat() call in _page_to_frontmatter_dict:
    previously, date objects were serialized to strings before being passed
    to python-frontmatter, so PyYAML wrote a quoted string instead of an
    unquoted ISO date literal. After the fix, date objects pass through
    directly and round-trip as datetime.date.
    """
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir(parents=True, exist_ok=True)

    backend = FilesystemCorpusBackend(tmp_path)
    policy = WritePolicy(write_paths=[tmp_path])

    # Write a page with a date object in `captured`
    captured_date = date(2026, 4, 22)
    backend.write_page(
        name="dated-page",
        content="Body text.",
        corpus="wiki",
        policy=policy,
        frontmatter={"captured": captured_date, "name": "Dated Page"},
    )

    # Snapshot the page
    version_ref = backend.snapshot("dated-page", "wiki")

    # Overwrite with different content so restore is exercised on Case 3 path
    page_path = wiki_dir / "dated-page.md"
    current_sha = hashlib.sha256(page_path.read_bytes()).hexdigest()
    backend.write_page(
        name="dated-page",
        content="Updated body — different content.",
        corpus="wiki",
        policy=policy,
        expected_content_sha256=current_sha,
    )

    # Restore from the snapshot
    backend.restore_version("dated-page", "wiki", version_ref, policy)

    # Read back and assert captured is still a date object, not a string
    page = backend.read_page("dated-page", "wiki")
    assert page is not None, "read_page returned None after restore_version"
    assert isinstance(page.captured, date), (
        f"captured should be datetime.date after restore_version round-trip; "
        f"got {type(page.captured)!r} with value {page.captured!r}"
    )
    assert page.captured == captured_date, (
        f"captured date value changed after round-trip: "
        f"expected {captured_date!r}, got {page.captured!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# FIX 8 (INFO Testing 4): restore_version when live page was deleted


def test_restore_version_recreates_page_when_live_deleted(tmp_path: Path) -> None:
    """restore_version recreates the page when the live file has been deleted.

    After a snapshot, if the live page is externally deleted, restore_version
    must write the page fresh (Case 1 path via write_page) rather than failing.
    """
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir(parents=True, exist_ok=True)

    backend = FilesystemCorpusBackend(tmp_path)
    policy = WritePolicy(write_paths=[tmp_path])

    backend.write_page("deleted-page", "Original content.", "wiki", policy)
    version_ref = backend.snapshot("deleted-page", "wiki")

    # Manually delete the live page file
    page_path = wiki_dir / "deleted-page.md"
    page_path.unlink()
    assert not page_path.exists(), "Test setup: live page file should be deleted"

    # restore_version should recreate it
    backend.restore_version("deleted-page", "wiki", version_ref, policy)

    # Assert the page is back
    page = backend.read_page("deleted-page", "wiki")
    assert page is not None, (
        "restore_version must recreate the live page when it was deleted"
    )
    assert "Original content." in page.body
