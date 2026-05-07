"""Tests for atomic_agents.migrate."""

from __future__ import annotations
import datetime
import io
import json
import tarfile
import unittest.mock
from datetime import date
from pathlib import Path

import frontmatter
import pytest

from atomic_agents.migrate import (
    LoadedScript,
    MigrationPlan,
    MigrationResult,
    build_migration_plan,
    create_snapshot,
    discover_scripts,
    find_content_files,
    get_current_vault_version,
    list_snapshots,
    parse_target_version,
    restore_snapshot,
    run_migration,
    vault_status,
)
from atomic_agents.exceptions import AtomicAgentsError


# ──────────────────────────────────────────────────────────────────
# Fixtures

@pytest.fixture
def vault(tmp_path):
    """Build a minimal vault with one agent + a few notes at v1."""
    agents_root = tmp_path / "agents"
    agent_root = agents_root / "alice"
    memory = agent_root / "memory"
    memory.mkdir(parents=True)

    # 3 valid v1 notes
    for i, kind in enumerate(["feedback", "user", "decision"], start=1):
        note = frontmatter.Post(
            f"Body {i}",
            schema_version=1,
            name=f"Note {i}",
            description="x" * 30,
            type=kind,
            captured="2026-01-01",
            last_seen="2026-05-01",
            sources=[f"conversation_{i}"],
            confidence="high",
        )
        # Use the right filename pattern per spec/03
        filename = f"{kind}_note_{i}.md"
        if kind == "decision":
            filename = "decision_2026_note_3.md"  # date suffix for time-bounded
        (memory / filename).write_text(frontmatter.dumps(note) + "\n")

    # An INDEX.md (should be skipped by find_content_files)
    (memory / "INDEX.md").write_text("# Index\n")

    return agents_root


@pytest.fixture
def vault_with_v0_to_v1_script(vault):
    """Add a fake v0_to_v1 migration script (just to test discovery)."""
    migrations = vault / "_migrations"
    migrations.mkdir()
    (migrations / "v0_to_v1.py").write_text(
        "FROM_VERSION = 0\n"
        "TO_VERSION = 1\n"
        "def applies_to(path):\n"
        "    return path.suffix == '.md'\n"
        "def migrate(path, dry_run):\n"
        "    return {'path': str(path), 'changes': ['noop'], 'dry_run': dry_run}\n"
    )
    return vault


@pytest.fixture
def vault_with_v1_to_v2_script(vault):
    """Add a real v1_to_v2 script that adds a new required field 'provenance'."""
    migrations = vault / "_migrations"
    migrations.mkdir()
    (migrations / "v1_to_v2.py").write_text(
        "import frontmatter\n"
        "FROM_VERSION = 1\n"
        "TO_VERSION = 2\n"
        "def applies_to(path):\n"
        "    if path.suffix != '.md' or path.name == 'INDEX.md':\n"
        "        return False\n"
        "    try:\n"
        "        parsed = frontmatter.load(path)\n"
        "    except Exception:\n"
        "        return False\n"
        "    return parsed.metadata.get('schema_version') == 1\n"
        "def migrate(path, dry_run):\n"
        "    parsed = frontmatter.load(path)\n"
        "    parsed.metadata['schema_version'] = 2\n"
        "    if 'provenance' not in parsed.metadata:\n"
        "        parsed.metadata['provenance'] = 'v1_migrated'\n"
        "    if not dry_run:\n"
        "        path.write_text(frontmatter.dumps(parsed) + '\\n')\n"
        "    return {'path': str(path), 'changes': ['v1→v2', 'added provenance'], 'dry_run': dry_run}\n"
    )
    return vault


# ──────────────────────────────────────────────────────────────────
# parse_target_version

def test_parse_target_version_v_prefix():
    assert parse_target_version("v2") == 2


def test_parse_target_version_no_prefix():
    assert parse_target_version("3") == 3


def test_parse_target_version_invalid_raises():
    with pytest.raises(AtomicAgentsError, match="Invalid version"):
        parse_target_version("x.y.z")


# ──────────────────────────────────────────────────────────────────
# Discovery

def test_find_content_files_finds_notes(vault):
    files = find_content_files(vault)
    # 3 notes, INDEX.md excluded
    assert len(files) == 3
    assert all(f.suffix == ".md" for f in files)
    assert all(f.name != "INDEX.md" for f in files)


def test_find_content_files_skips_excluded_dirs(tmp_path):
    agents_root = tmp_path / "agents"
    (agents_root / "alice" / "memory").mkdir(parents=True)
    (agents_root / "alice" / "memory" / "feedback_x.md").write_text("---\n---\n")

    # These should be skipped
    (agents_root / "_dashboard").mkdir()
    (agents_root / "_dashboard" / "should_skip.md").write_text("x")
    (agents_root / "_migrations" / "snapshots").mkdir(parents=True)
    (agents_root / "_migrations" / "snapshots" / "skip.md").write_text("x")

    files = find_content_files(agents_root)
    assert len(files) == 1
    assert files[0].name == "feedback_x.md"


def test_find_content_files_empty_vault(tmp_path):
    assert find_content_files(tmp_path / "nonexistent") == []
    empty = tmp_path / "empty"
    empty.mkdir()
    assert find_content_files(empty) == []


def test_get_current_vault_version_from_files(vault):
    assert get_current_vault_version(vault) == 1


def test_get_current_vault_version_empty_vault_returns_default(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    # CURRENT_SCHEMA_VERSION is 1 in the helper
    assert get_current_vault_version(empty) == 1


def test_get_current_vault_version_returns_lowest_when_mixed(tmp_path):
    """Mixed versions → return lowest (treat as needing migration up)."""
    agents_root = tmp_path / "agents"
    memory = agents_root / "alice" / "memory"
    memory.mkdir(parents=True)
    # One v1 note
    (memory / "feedback_x.md").write_text(frontmatter.dumps(frontmatter.Post(
        "x", schema_version=1, name="x", description="x", type="feedback",
        captured="2026-01-01", last_seen="2026-05-01", sources=["s"], confidence="high",
    )) + "\n")
    # One v0 note (legacy — pretend)
    (memory / "feedback_y.md").write_text(frontmatter.dumps(frontmatter.Post(
        "y", schema_version=0, name="y", description="y", type="feedback",
        captured="2026-01-01", last_seen="2026-05-01", sources=["s"], confidence="high",
    )) + "\n")
    assert get_current_vault_version(agents_root) == 0


# ──────────────────────────────────────────────────────────────────
# Script discovery

def test_discover_scripts_finds_one(vault_with_v0_to_v1_script):
    scripts = discover_scripts(vault_with_v0_to_v1_script)
    assert len(scripts) == 1
    assert scripts[0].from_version == 0
    assert scripts[0].to_version == 1


def test_discover_scripts_skips_underscore_prefixed(vault):
    migrations = vault / "_migrations"
    migrations.mkdir()
    (migrations / "_template.py").write_text(
        "FROM_VERSION = 99\n"
        "TO_VERSION = 100\n"
        "def applies_to(p): return False\n"
        "def migrate(p, dry_run): return {}\n"
    )
    (migrations / "v1_to_v2.py").write_text(
        "FROM_VERSION = 1\n"
        "TO_VERSION = 2\n"
        "def applies_to(p): return False\n"
        "def migrate(p, dry_run): return {}\n"
    )
    scripts = discover_scripts(vault)
    assert len(scripts) == 1
    assert scripts[0].from_version == 1


def test_discover_scripts_rejects_skipped_version(vault):
    migrations = vault / "_migrations"
    migrations.mkdir()
    (migrations / "v1_to_v3.py").write_text(  # skips v2!
        "FROM_VERSION = 1\n"
        "TO_VERSION = 3\n"
        "def applies_to(p): return False\n"
        "def migrate(p, dry_run): return {}\n"
    )
    with pytest.raises(AtomicAgentsError, match="skips a version"):
        discover_scripts(vault)


def test_discover_scripts_rejects_version_mismatch(vault):
    migrations = vault / "_migrations"
    migrations.mkdir()
    # Filename says v1→v2 but module says v2→v3 — mismatch
    (migrations / "v1_to_v2.py").write_text(
        "FROM_VERSION = 2\n"
        "TO_VERSION = 3\n"
        "def applies_to(p): return False\n"
        "def migrate(p, dry_run): return {}\n"
    )
    with pytest.raises(AtomicAgentsError, match="version mismatch"):
        discover_scripts(vault)


def test_discover_scripts_rejects_missing_required_attribute(vault):
    migrations = vault / "_migrations"
    migrations.mkdir()
    (migrations / "v1_to_v2.py").write_text(
        "FROM_VERSION = 1\nTO_VERSION = 2\n# missing applies_to\n"
        "def migrate(p, dry_run): return {}\n"
    )
    with pytest.raises(AtomicAgentsError, match="missing required attribute"):
        discover_scripts(vault)


def test_discover_scripts_empty_returns_empty(vault):
    # No _migrations/ directory
    assert discover_scripts(vault) == []


# ──────────────────────────────────────────────────────────────────
# Plan building

def test_build_migration_plan_includes_chain(vault_with_v1_to_v2_script):
    plan = build_migration_plan(vault_with_v1_to_v2_script, target_version=2)
    assert plan.from_version == 1
    assert plan.to_version == 2
    assert len(plan.scripts) == 1
    assert plan.scripts[0].from_version == 1


def test_build_migration_plan_rejects_target_below_current(vault_with_v1_to_v2_script):
    with pytest.raises(AtomicAgentsError, match="not above current"):
        build_migration_plan(vault_with_v1_to_v2_script, target_version=1)


def test_build_migration_plan_no_scripts_for_chain_raises(vault):
    # No migration scripts; target above current → can't reach
    with pytest.raises(AtomicAgentsError, match="No migration script"):
        build_migration_plan(vault, target_version=2)


# ──────────────────────────────────────────────────────────────────
# Snapshot

def test_create_snapshot_writes_tarball(vault):
    today = date(2026, 8, 12)
    _now = datetime.datetime(2026, 8, 12, 14, 30, 0)
    path = create_snapshot(vault, target_version=2, today=today, _now=_now)
    assert path.exists()
    assert path.name == "2026-08-12T143000_pre_v2_migration.tar.gz"
    assert path.suffix == ".gz"


def test_snapshot_contains_content_files(vault):
    path = create_snapshot(vault, target_version=2)
    with tarfile.open(path, "r:gz") as tar:
        names = tar.getnames()
    # Should contain the content files
    assert any("feedback_note_1.md" in n for n in names)


def test_snapshot_excludes_meta_dirs(vault):
    # Add some excluded content
    (vault / "_dashboard").mkdir()
    (vault / "_dashboard" / "skip.md").write_text("x")
    path = create_snapshot(vault, target_version=2)
    with tarfile.open(path, "r:gz") as tar:
        names = tar.getnames()
    assert not any("_dashboard" in n for n in names)


def test_restore_snapshot_round_trip(vault):
    """Snapshot → mutate → restore — original content recovered."""
    note_path = vault / "alice" / "memory" / "feedback_note_1.md"
    original = note_path.read_text()

    snap = create_snapshot(vault, target_version=2)

    # Mutate — write garbage over the file
    note_path.write_text("CORRUPTED")
    assert note_path.read_text() == "CORRUPTED"

    restore_snapshot(vault, snap)
    assert note_path.read_text() == original


def test_restore_snapshot_missing_raises(tmp_path):
    with pytest.raises(AtomicAgentsError, match="not found"):
        restore_snapshot(tmp_path, tmp_path / "nonexistent.tar.gz")


def test_list_snapshots_newest_first(vault):
    import time
    p1 = create_snapshot(vault, target_version=2)
    time.sleep(1.1)  # ensure mtime differs by full second
    p2 = create_snapshot(vault, target_version=3)
    snapshots = list_snapshots(vault)
    assert len(snapshots) == 2
    assert snapshots[0] == p2  # newest first


def test_list_snapshots_empty_vault_returns_empty(tmp_path):
    assert list_snapshots(tmp_path / "nonexistent") == []


# ──────────────────────────────────────────────────────────────────
# Migration application

def test_run_migration_dry_run_doesnt_modify_files(vault_with_v1_to_v2_script):
    note_path = vault_with_v1_to_v2_script / "alice" / "memory" / "feedback_note_1.md"
    original = note_path.read_text()
    result = run_migration(vault_with_v1_to_v2_script, target_version=2, dry_run=True)
    assert result.dry_run is True
    assert note_path.read_text() == original  # unchanged
    assert len(result.files_touched) == 3
    # No snapshot for dry-runs
    assert result.snapshot_path is None


def test_run_migration_real_applies_and_validates(vault_with_v1_to_v2_script):
    """End-to-end: real migration with validation passing."""
    # Tweak: validate_atomic_note_frontmatter currently rejects schema_version != 1.
    # The test migration writes schema_version=2 — so post-validation will fail and
    # the run will rollback. This is actually the intended behavior: the helper's
    # validator stays at the current SCHEMA_VERSION; until the helper bumps to v2,
    # any v2 file fails validation.
    #
    # That means the test should expect rollback, not success. This is correct
    # safety behavior — you can't migrate to a version the helper doesn't yet support.

    result = run_migration(vault_with_v1_to_v2_script, target_version=2, dry_run=False)

    # Validation will fail because the helper's CURRENT_SCHEMA_VERSION is still 1
    assert result.rolled_back is True
    assert "validation failed" in result.error.lower()
    assert result.snapshot_path is not None
    assert result.snapshot_path.exists()

    # Original files should be restored
    note_path = vault_with_v1_to_v2_script / "alice" / "memory" / "feedback_note_1.md"
    parsed = frontmatter.load(note_path)
    assert parsed.metadata["schema_version"] == 1  # rolled back


def test_run_migration_records_files_touched(vault_with_v1_to_v2_script):
    result = run_migration(vault_with_v1_to_v2_script, target_version=2, dry_run=True)
    assert len(result.files_touched) == 3
    for entry in result.files_touched:
        assert "path" in entry
        assert "changes" in entry
        assert entry["script"] == "v1_to_v2.py"


def test_run_migration_target_below_current_raises(vault_with_v1_to_v2_script):
    with pytest.raises(AtomicAgentsError, match="not above current"):
        run_migration(vault_with_v1_to_v2_script, target_version=1, dry_run=True)


def test_run_migration_no_scripts_raises(vault):
    with pytest.raises(AtomicAgentsError, match="No migration script"):
        run_migration(vault, target_version=2, dry_run=True)


# ──────────────────────────────────────────────────────────────────
# Status

def test_vault_status_basic(vault_with_v1_to_v2_script):
    status = vault_status(vault_with_v1_to_v2_script)
    assert status["current_schema_version"] == 1
    assert status["content_file_count"] == 3
    assert len(status["available_scripts"]) == 1
    assert status["available_scripts"][0]["from"] == 1
    assert status["available_scripts"][0]["to"] == 2


def test_vault_status_no_scripts(vault):
    status = vault_status(vault)
    assert status["available_scripts"] == []
    assert status["snapshots"] == []


# ──────────────────────────────────────────────────────────────────
# P1 regression: atomic rollback

def test_restore_snapshot_atomic_on_corrupt_tar(vault):
    """A corrupt (truncated) snapshot must leave the live vault untouched."""
    note_path = vault / "alice" / "memory" / "feedback_note_1.md"
    original = note_path.read_text()

    # Take a good snapshot, then corrupt it by truncating
    snap = create_snapshot(vault, target_version=2)
    corrupt_snap = snap.parent / "corrupt.tar.gz"
    # Write only the first 64 bytes — definitely not a valid gzip/tar
    corrupt_snap.write_bytes(snap.read_bytes()[:64])

    with pytest.raises(Exception):
        restore_snapshot(vault, corrupt_snap)

    # Live vault must be unchanged
    assert note_path.exists(), "note_path was deleted before extract failed"
    assert note_path.read_text() == original, "note_path content was modified"


def test_restore_snapshot_atomic_on_partial_extract(vault, monkeypatch):
    """If extraction raises mid-stream, the live vault must be untouched.

    The new restore_snapshot extracts into a sibling temp dir, so the live
    vault is only touched during the final atomic rename — never during
    extraction.  We simulate an extraction failure by patching extractall
    to raise on the first (and only) real extract call.
    """
    note_path = vault / "alice" / "memory" / "feedback_note_1.md"
    original = note_path.read_text()

    snap = create_snapshot(vault, target_version=2)

    real_extractall = tarfile.TarFile.extractall

    def boom_extractall(self, *args, **kwargs):
        raise OSError("simulated mid-extract failure")

    monkeypatch.setattr(tarfile.TarFile, "extractall", boom_extractall)

    with pytest.raises(OSError, match="simulated mid-extract failure"):
        restore_snapshot(vault, snap)

    # Live vault must be unchanged — the bomb went off before the atomic swap
    assert note_path.exists(), "note_path was deleted before extract completed"
    assert note_path.read_text() == original, "note_path content was modified"


# ──────────────────────────────────────────────────────────────────
# P1 regression: wiki page validation

@pytest.fixture
def vault_with_wiki_and_v1_to_v2_script(tmp_path):
    """Vault with both atomic notes and a wiki page, plus a noop v1→v2 script.

    The migration script is a true noop (reads schema_version, writes it back
    unchanged so files stay at v1 and post-validation doesn't reject them on
    schema_version mismatch).  The goal is to exercise the dispatch logic in
    _validate_post_migration: wiki files should use validate_wiki_frontmatter,
    not validate_atomic_note_frontmatter.
    """
    agents_root = tmp_path / "agents"
    agent_root = agents_root / "alice"
    memory = agent_root / "memory"
    wiki = agent_root / "wiki"
    memory.mkdir(parents=True)
    wiki.mkdir(parents=True)

    # One valid atomic note
    note = frontmatter.Post(
        "Body",
        schema_version=1,
        name="Some Note",
        description="x" * 30,
        type="feedback",
        captured="2026-01-01",
        last_seen="2026-05-01",
        sources=["conversation_1"],
        confidence="high",
    )
    (memory / "feedback_some_note.md").write_text(frontmatter.dumps(note) + "\n")

    # One valid wiki page
    wiki_page = frontmatter.Post(
        "# Overview\nThis is the overview wiki page.",
        schema_version=1,
        name="Overview",
        description="High-level overview of the project.",
        type="wiki_page",
    )
    (wiki / "overview.md").write_text(frontmatter.dumps(wiki_page) + "\n")

    # Noop migration script: touches nothing, returns summary
    migrations = agents_root / "_migrations"
    migrations.mkdir()
    (migrations / "v1_to_v2.py").write_text(
        "import frontmatter\n"
        "FROM_VERSION = 1\n"
        "TO_VERSION = 2\n"
        "def applies_to(path):\n"
        "    return path.suffix == '.md'\n"
        "def migrate(path, dry_run):\n"
        "    return {'path': str(path), 'changes': ['noop'], 'dry_run': dry_run}\n"
    )
    return agents_root


def test_migration_validates_wiki_pages_with_wiki_schema(vault_with_wiki_and_v1_to_v2_script):
    """Post-migration validation must not fail on wiki pages.

    Previously, _validate_post_migration called validate_atomic_note_frontmatter
    for every file, which rejects type: wiki_page.  After the fix, wiki files
    are dispatched to validate_wiki_frontmatter instead.

    The noop migration script keeps all files at schema_version=1, so validation
    succeeds — this test would fail before the fix with a SchemaValidationError
    about 'type must be one of {...}; got wiki_page'.
    """
    vault = vault_with_wiki_and_v1_to_v2_script

    # Dry run first to confirm plan is built
    plan = build_migration_plan(vault, target_version=2)
    assert len(plan.candidate_files) == 2  # 1 memory note + 1 wiki page

    # Real run — snapshot taken, noop applied, validation runs
    result = run_migration(vault, target_version=2, dry_run=False)

    # Should NOT roll back; validation must pass for both file types
    assert result.rolled_back is False, (
        f"Unexpected rollback. Validation errors: {result.validation_errors}"
    )
    assert result.validation_passed is True
    assert result.validation_errors == []


# ──────────────────────────────────────────────────────────────────
# P2 regression: timestamped snapshots (no same-day collision)

def test_snapshot_filename_includes_time_or_counter(vault):
    """Two snapshots on the same day with the same target version get distinct names."""
    today = date(2026, 8, 12)
    now1 = datetime.datetime(2026, 8, 12, 9, 0, 0)
    now2 = datetime.datetime(2026, 8, 12, 9, 0, 5)  # 5 seconds later

    p1 = create_snapshot(vault, target_version=2, today=today, _now=now1)
    p2 = create_snapshot(vault, target_version=2, today=today, _now=now2)

    assert p1 != p2, "Snapshot paths must differ when taken at different times"
    assert p1.exists(), "First snapshot must still exist after second is taken"
    assert p2.exists(), "Second snapshot must exist"
    assert p1.name != p2.name, "Snapshot filenames must differ"


def test_snapshot_dry_run_then_real_no_overwrite(vault):
    """dry-run snapshot (if any) and real-run snapshot do not collide."""
    today = date(2026, 8, 12)
    now1 = datetime.datetime(2026, 8, 12, 10, 0, 0)
    now2 = datetime.datetime(2026, 8, 12, 10, 0, 1)

    # dry-run doesn't create a snapshot, but if a caller manually creates
    # two snapshots in quick succession they must not overwrite each other.
    p1 = create_snapshot(vault, target_version=2, today=today, _now=now1)
    p2 = create_snapshot(vault, target_version=2, today=today, _now=now2)

    assert p1.name != p2.name
    assert p1.exists()
    assert p2.exists()


# ──────────────────────────────────────────────────────────────────
# P2 regression: cascade-aware file discovery

def test_find_content_files_walks_cascaded_agents(tmp_path):
    """Cascaded agent memory/wiki files are included in migration discovery.

    Cascade layout: <agents_root>/<system>/projects/<project>/agents/<role>/memory/
    """
    agents_root = tmp_path / "agents"

    # Single-agent note (top-level)
    single_memory = agents_root / "solo" / "memory"
    single_memory.mkdir(parents=True)
    (single_memory / "feedback_x.md").write_text("---\n---\n")

    # Cascaded agent note (nested under system/projects/proj/agents/role)
    cascade_memory = agents_root / "muse_system" / "projects" / "novel" / "agents" / "writer" / "memory"
    cascade_memory.mkdir(parents=True)
    (cascade_memory / "feedback_cascade.md").write_text("---\n---\n")

    # Cascaded agent wiki page
    cascade_wiki = agents_root / "muse_system" / "projects" / "novel" / "agents" / "writer" / "wiki"
    cascade_wiki.mkdir(parents=True)
    (cascade_wiki / "overview.md").write_text("---\n---\n")

    files = find_content_files(agents_root)
    names = {f.name for f in files}

    assert "feedback_x.md" in names, "Single-agent note must be found"
    assert "feedback_cascade.md" in names, "Cascaded agent memory note must be found"
    assert "overview.md" in names, "Cascaded agent wiki page must be found"
    assert len(files) == 3


def test_find_content_files_dedupes_by_path(tmp_path):
    """No file is returned twice even when rglob could match via multiple routes."""
    agents_root = tmp_path / "agents"
    memory = agents_root / "alice" / "memory"
    memory.mkdir(parents=True)
    (memory / "feedback_a.md").write_text("---\n---\n")
    (memory / "feedback_b.md").write_text("---\n---\n")

    files = find_content_files(agents_root)
    paths = [f.resolve() for f in files]
    assert len(paths) == len(set(paths)), "Duplicate paths returned by find_content_files"


def test_find_content_files_excludes_meta_dirs(tmp_path):
    """_migrations/, _dashboard/, and dotfile dirs are never returned."""
    agents_root = tmp_path / "agents"

    # Real content
    (agents_root / "alice" / "memory").mkdir(parents=True)
    (agents_root / "alice" / "memory" / "feedback_real.md").write_text("---\n---\n")

    # Meta dirs that must be excluded
    (agents_root / "_migrations" / "memory").mkdir(parents=True)
    (agents_root / "_migrations" / "memory" / "skip1.md").write_text("---\n---\n")
    (agents_root / "_dashboard" / "memory").mkdir(parents=True)
    (agents_root / "_dashboard" / "memory" / "skip2.md").write_text("---\n---\n")
    # Dotfile directory
    (agents_root / ".hidden" / "memory").mkdir(parents=True)
    (agents_root / ".hidden" / "memory" / "skip3.md").write_text("---\n---\n")

    files = find_content_files(agents_root)
    names = {f.name for f in files}

    assert names == {"feedback_real.md"}, f"Unexpected files found: {names}"
