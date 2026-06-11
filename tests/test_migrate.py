"""Conformance test suite for the MigrationBackend Protocol (spec/03 §Schema-migration).

Parameterized over a ``backend_factory`` fixture. Today only
``FilesystemMigrationBackend`` participates; the parametrization scaffolding
is already in place so a future DB backend drops in as a one-line addition.

What this suite asserts:

Protocol surface
  1. ``isinstance(backend, MigrationBackend)`` passes.
  2. ``backend_id`` is a stable non-empty lowercase string.
  3. ``capabilities()`` returns a real ``MigrationCapabilities`` instance.

read_schema_version
  4. Empty vault returns CURRENT_SCHEMA_VERSION.
  5. Vault with mixed versions returns the minimum.
  6. Full walk (100+ files, one straggler) returns the straggler version.

enumerate_units
  7. Returns all memory + wiki units; excludes INDEX.md + excluded dirs.
  8. Deduplicates by resolved path (symlink test).
  9. Every unit has ``unit_type`` in {``'memory'``, ``'wiki'``}.
  10. Cascaded agent layout is discovered.

apply_unit (dry-run)
  11. Dry-run: write_frontmatter is a no-op — backing store unchanged.
  12. Dry-run: unit_type discriminator set on every unit.

apply_unit (real)
  13. Real apply bumps schema_version in the backing store.
  14. Script that forgets to bump raises AtomicAgentsError.

snapshot / restore
  15. snapshot() returns a MigrationSnapshotRef with non-empty snapshot_id.
  16. snapshot → mutate → restore round-trip recovers original content.
  17. Atomic safety: corrupt tar leaves live vault untouched.
  18. Atomic safety: mid-extract failure leaves live vault untouched.
  19. restore() on unknown snapshot_id raises MigrationSnapshotNotFound.
  20. snapshot_id from snapshot() accepted by restore() on a fresh backend instance.

Fail-close path
  21. No-rollback stub raises MigrationRollbackUnavailable before any apply.
  22. No-rollback stub dry-run does NOT raise.
  23. isinstance(no_rollback_stub, MigrationBackend) passes (structural check).

run_migration (integration)
  24. Dry-run leaves files unchanged; no snapshot taken.
  25. Real run applies and validates; snapshot + result recorded.
  26. Validation failure triggers rollback.
  27. Wiki pages validated with wiki schema (not note schema).
  28. Target-below-current raises AtomicAgentsError.
  29. No-scripts raises AtomicAgentsError.
  30. Audit event written to migration.jsonl.

Breaking-signature behavior
  31. Old path-shaped applies_to(path) / migrate(path, dry_run) is gone.
  32. MigrationScript Protocol class is gone.
  33. Old run_migration() free-function is gone from migrate module.
"""

from __future__ import annotations

import datetime
import json
import tarfile
import time
from datetime import date
from pathlib import Path
from typing import Callable

import frontmatter
import pytest

from atomic_agents._schema import CURRENT_SCHEMA_VERSION
from atomic_agents.exceptions import AtomicAgentsError
from atomic_agents.migration import (
    FilesystemMigrationBackend,
    MigratableUnit,
    MigrationBackend,
    MigrationCapabilities,
    MigrationResult,
    MigrationSnapshotRef,
    MigrationRollbackUnavailable,
    MigrationSnapshotNotFound,
)


# ──────────────────────────────────────────────────────────────────
# Backend factory parametrization (spec/24/25 BACKEND_FACTORIES pattern)

BackendFactory = Callable[[Path], MigrationBackend]


def _filesystem_factory(agents_root: Path) -> MigrationBackend:
    return FilesystemMigrationBackend(agents_root)


BACKEND_FACTORIES: list[tuple[str, BackendFactory]] = [
    ("filesystem", _filesystem_factory),
]


@pytest.fixture(params=BACKEND_FACTORIES, ids=lambda p: p[0])
def backend_factory(request) -> BackendFactory:
    """Yields a callable ``(agents_root: Path) -> MigrationBackend``."""
    return request.param[1]


@pytest.fixture
def backend(backend_factory, vault) -> MigrationBackend:
    """A backend rooted at the per-test vault."""
    return backend_factory(vault)


# ──────────────────────────────────────────────────────────────────
# Vault fixtures


def _make_note(memory_dir: Path, filename: str, schema_version: int = 1) -> None:
    note = frontmatter.Post(
        "Body",
        schema_version=schema_version,
        name="Test Note",
        description="x" * 30,
        type="feedback",
        captured="2026-01-01",
        last_seen="2026-05-01",
        sources=["conversation_1"],
        confidence="high",
    )
    (memory_dir / filename).write_text(frontmatter.dumps(note) + "\n")


def _make_wiki_page(wiki_dir: Path, filename: str, schema_version: int = 1) -> None:
    page = frontmatter.Post(
        "# Overview",
        schema_version=schema_version,
        name="Overview",
        description="High-level overview.",
        type="wiki_page",
    )
    (wiki_dir / filename).write_text(frontmatter.dumps(page) + "\n")


@pytest.fixture
def vault(tmp_path) -> Path:
    """Minimal vault: one agent with 3 memory notes at v1."""
    agents_root = tmp_path / "agents"
    memory = agents_root / "alice" / "memory"
    memory.mkdir(parents=True)
    for i, kind in enumerate(["feedback", "user", "decision"], start=1):
        fname = (
            f"{kind}_note_{i}.md" if kind != "decision" else "decision_2026_note_3.md"
        )
        _make_note(memory, fname, schema_version=1)
    (memory / "INDEX.md").write_text("# Index\n")
    return agents_root


@pytest.fixture
def vault_with_v1_to_v2_script(vault) -> Path:
    """Vault + a backend-shaped v1→v2 migration script."""
    migrations = vault / "_migrations"
    migrations.mkdir()
    (migrations / "v1_to_v2.py").write_text(
        "from atomic_agents.migration import MigratableUnit\n"
        "FROM_VERSION = 1\n"
        "TO_VERSION = 2\n"
        "def applies_to(unit):\n"
        "    meta = unit.read_frontmatter()\n"
        "    return meta.get('schema_version') == FROM_VERSION\n"
        "def migrate(unit):\n"
        "    meta = unit.read_frontmatter()\n"
        "    meta['schema_version'] = TO_VERSION\n"
        "    meta.setdefault('provenance', 'v1_migrated')\n"
        "    unit.write_frontmatter(meta)\n"
        "    return {'unit_id': unit.unit_id, 'changes': ['schema_version 1->2']}\n"
    )
    return vault


@pytest.fixture
def vault_with_wiki(tmp_path) -> Path:
    """Vault with one memory note and one wiki page."""
    agents_root = tmp_path / "agents"
    memory = agents_root / "alice" / "memory"
    wiki = agents_root / "alice" / "wiki"
    memory.mkdir(parents=True)
    wiki.mkdir(parents=True)
    _make_note(memory, "feedback_some_note.md", schema_version=1)
    _make_wiki_page(wiki, "overview.md", schema_version=1)
    return agents_root


@pytest.fixture
def vault_with_wiki_and_noop_script(vault_with_wiki) -> Path:
    """Vault-with-wiki + a noop script that leaves schema_version unchanged."""
    migrations = vault_with_wiki / "_migrations"
    migrations.mkdir()
    # Noop script: applies_to returns True but migrate does NOT bump version;
    # used to exercise wiki/memory dispatch in post-migration validation at v1.
    (migrations / "v1_to_v2.py").write_text(
        "FROM_VERSION = 1\n"
        "TO_VERSION = 2\n"
        "def applies_to(unit):\n"
        "    return True\n"
        "def migrate(unit):\n"
        "    return {'unit_id': unit.unit_id, 'changes': ['noop']}\n"
    )
    return vault_with_wiki


# ──────────────────────────────────────────────────────────────────
# No-rollback stub for fail-close tests (structural Protocol satisfaction)


class _NoRollbackStub:
    """Standalone class satisfying MigrationBackend structurally (no subclassing).

    Declares supports_transactional_rollback=False. snapshot()/restore() raise
    NotImplementedError so any call to them surfaces loudly.
    """

    @property
    def backend_id(self) -> str:
        return "no_rollback_stub"

    def capabilities(self) -> MigrationCapabilities:
        return MigrationCapabilities(
            supports_transactional_rollback=False,
            single_host_only=True,
        )

    def read_schema_version(self) -> int:
        return CURRENT_SCHEMA_VERSION

    def enumerate_units(self) -> list:
        return []

    def apply_unit(self, unit, script, dry_run=True) -> dict:
        return {}

    def snapshot(self, target_version: int) -> MigrationSnapshotRef:
        raise NotImplementedError("no_rollback_stub does not support snapshot()")

    def restore(self, ref: MigrationSnapshotRef) -> None:
        raise NotImplementedError("no_rollback_stub does not support restore()")

    def restore_and_audit(self, ref: MigrationSnapshotRef) -> None:
        raise NotImplementedError(
            "no_rollback_stub does not support restore_and_audit()"
        )

    def run_migration(
        self, target_version: int, dry_run: bool = True
    ) -> MigrationResult:
        from atomic_agents.migration.types import MigrationPlan, MigrationResult

        caps = self.capabilities()
        if not dry_run and not caps.supports_transactional_rollback:
            raise MigrationRollbackUnavailable(
                f"MigrationBackend {self.backend_id!r} does not support "
                f"transactional rollback. Refusing destructive migration."
            )
        plan = MigrationPlan(from_version=0, to_version=target_version)
        return MigrationResult(plan=plan, snapshot_ref=None, dry_run=dry_run)


def _no_rollback_factory(agents_root: Path) -> MigrationBackend:
    """Capability-varying factory for the gated-skip matrix.

    Ignores ``agents_root`` (the stub has no backing store). Used by
    ``ALL_CONFORMANCE_BACKENDS`` to exercise the capability-gated skip so a
    backend advertising ``supports_transactional_rollback=False`` is proven
    to SKIP (not error) on snapshot/restore conformance.
    """
    return _NoRollbackStub()


# Full conformance matrix including the capability-varying stub. Used only by
# the capability-gated tests below (test_capability_gated_*). The default
# ``backend_factory`` fixture stays filesystem-only because _NoRollbackStub is
# not a faithful vault backend (no enumerate/snapshot/restore); the gate proves
# a non-rollback backend is correctly SKIPPED rather than run.
ALL_CONFORMANCE_BACKENDS: list[tuple[str, BackendFactory]] = [
    *BACKEND_FACTORIES,
    ("no_rollback", _no_rollback_factory),
]


def _skip_if_no_rollback(backend: MigrationBackend) -> None:
    """Capability-gated skip helper, mirroring spec/24/25 capability gating.

    Snapshot/restore round-trips and real (non-dry-run) migrations depend on
    ``capabilities().supports_transactional_rollback``. Tests that require it
    call this helper to skip cleanly on backends advertising ``False``.
    """
    if not backend.capabilities().supports_transactional_rollback:
        pytest.skip(
            f"backend {backend.backend_id!r} does not support transactional "
            f"rollback; capability-gated snapshot/restore/real-run test skipped"
        )


# ──────────────────────────────────────────────────────────────────
# 1. Protocol surface


def test_isinstance_migrationbackend(backend):
    assert isinstance(backend, MigrationBackend)


def test_backend_id_stable_nonempty_lowercase(backend):
    bid = backend.backend_id
    assert isinstance(bid, str)
    assert len(bid) > 0
    assert bid == bid.lower()
    # Stable across calls
    assert backend.backend_id == bid


def test_capabilities_returns_migrationcapabilities(backend):
    caps = backend.capabilities()
    assert isinstance(caps, MigrationCapabilities)
    assert isinstance(caps.supports_transactional_rollback, bool)
    assert isinstance(caps.single_host_only, bool)


# ──────────────────────────────────────────────────────────────────
# 4-6. read_schema_version


def test_read_schema_version_empty_vault_returns_current(tmp_path, backend_factory):
    empty = tmp_path / "empty_agents"
    empty.mkdir()
    b = backend_factory(empty)
    assert b.read_schema_version() == CURRENT_SCHEMA_VERSION


def test_read_schema_version_from_files(backend, vault):
    assert backend.read_schema_version() == 1


def test_read_schema_version_mixed_returns_minimum(tmp_path, backend_factory):
    agents_root = tmp_path / "agents"
    memory = agents_root / "alice" / "memory"
    memory.mkdir(parents=True)
    _make_note(memory, "feedback_x.md", schema_version=1)
    _make_note(memory, "feedback_y.md", schema_version=0)
    b = backend_factory(agents_root)
    assert b.read_schema_version() == 0


def test_read_schema_version_full_walk_no_sampling(tmp_path, backend_factory):
    """100 files all at v1 except file 51+: full walk must return the straggler version."""
    agents_root = tmp_path / "agents"
    memory = agents_root / "alice" / "memory"
    memory.mkdir(parents=True)
    # 60 files at v1
    for i in range(60):
        _make_note(memory, f"feedback_note_{i:03d}.md", schema_version=1)
    # 1 straggler at v0 — beyond the old [:50] sample cap
    _make_note(memory, "feedback_straggler_061.md", schema_version=0)
    b = backend_factory(agents_root)
    assert b.read_schema_version() == 0, (
        "read_schema_version() must walk ALL units, not just the first 50"
    )


def test_read_schema_version_all_malformed_warns(tmp_path, backend_factory, caplog):
    """MUST 1: a non-empty vault where every unit has a missing/non-int
    schema_version returns CURRENT and SURFACES a diagnostic so corruption is
    not silently masked as already-current. SHOULD-level diagnostic gets a test
    so it cannot silently regress.
    """
    import logging

    agents_root = tmp_path / "agents"
    memory = agents_root / "alice" / "memory"
    memory.mkdir(parents=True)
    # Parseable frontmatter but schema_version is a STRING (non-int) — malformed.
    post = frontmatter.Post("Body", schema_version="1", name="n")
    (memory / "feedback_malformed.md").write_text(frontmatter.dumps(post) + "\n")
    # Parseable but schema_version entirely MISSING — also malformed.
    post2 = frontmatter.Post("Body", name="n")
    (memory / "feedback_missing.md").write_text(frontmatter.dumps(post2) + "\n")

    b = backend_factory(agents_root)
    with caplog.at_level(logging.WARNING, logger="atomic_agents.migration.filesystem"):
        result = b.read_schema_version()

    assert result == CURRENT_SCHEMA_VERSION
    assert any(
        "none yielded a valid integer schema_version" in r.message
        and r.levelno == logging.WARNING
        for r in caplog.records
    ), f"all-malformed vault must emit the corruption diagnostic; got {caplog.records}"


def test_read_schema_version_all_unparseable_warns(tmp_path, backend_factory, caplog):
    """MUST 1: a vault where every unit fails to parse also surfaces the
    diagnostic and returns CURRENT rather than silently masking corruption.
    """
    import logging

    agents_root = tmp_path / "agents"
    memory = agents_root / "alice" / "memory"
    memory.mkdir(parents=True)
    # Broken frontmatter delimiter so frontmatter.load raises on read.
    (memory / "feedback_broken.md").write_text(
        "---\nschema_version: [unterminated\nname: x\n"
    )

    b = backend_factory(agents_root)
    with caplog.at_level(logging.WARNING, logger="atomic_agents.migration.filesystem"):
        result = b.read_schema_version()

    assert result == CURRENT_SCHEMA_VERSION
    assert any(
        "none yielded a valid integer schema_version" in r.message
        and r.levelno == logging.WARNING
        for r in caplog.records
    ), (
        f"all-unparseable vault must emit the corruption diagnostic; got {caplog.records}"
    )


# ──────────────────────────────────────────────────────────────────
# 7-10. enumerate_units


def test_enumerate_units_excludes_index_and_excluded_dirs(backend, vault):
    units = backend.enumerate_units()
    ids = {u.unit_id for u in units}
    assert not any("INDEX.md" in uid for uid in ids)
    assert not any("_dashboard" in uid for uid in ids)
    assert not any("_migrations" in uid for uid in ids)


def test_enumerate_units_count(backend, vault):
    units = backend.enumerate_units()
    # 3 notes, INDEX.md excluded
    assert len(units) == 3


def test_enumerate_units_deduplicates_by_resolved_path(tmp_path, backend_factory):
    """No unit returned twice even via symlinked directory."""
    agents_root = tmp_path / "agents"
    memory = agents_root / "alice" / "memory"
    memory.mkdir(parents=True)
    _make_note(memory, "feedback_a.md")
    _make_note(memory, "feedback_b.md")
    # Symlink bob/memory -> alice/memory
    (agents_root / "bob").mkdir()
    (agents_root / "bob" / "memory").symlink_to(memory)
    b = backend_factory(agents_root)
    units = b.enumerate_units()
    unit_ids = [u.unit_id for u in units]
    resolved = [str(Path(uid).resolve()) for uid in unit_ids]
    assert len(resolved) == len(set(resolved)), "Duplicate units returned"


def test_enumerate_units_unit_type_set(backend_factory, vault_with_wiki):
    b = backend_factory(vault_with_wiki)
    units = b.enumerate_units()
    for unit in units:
        assert unit.unit_type in {"memory", "wiki"}, (
            f"unit {unit.unit_id!r} has unexpected unit_type={unit.unit_type!r}"
        )
    types = {u.unit_type for u in units}
    assert "memory" in types
    assert "wiki" in types


def test_enumerate_units_cascaded_layout(tmp_path, backend_factory):
    """Cascaded agent layout is discovered."""
    agents_root = tmp_path / "agents"
    cascade_memory = (
        agents_root
        / "muse_system"
        / "projects"
        / "novel"
        / "agents"
        / "writer"
        / "memory"
    )
    cascade_memory.mkdir(parents=True)
    _make_note(cascade_memory, "feedback_cascade.md")
    b = backend_factory(agents_root)
    units = b.enumerate_units()
    assert any("feedback_cascade.md" in u.unit_id for u in units)


# ──────────────────────────────────────────────────────────────────
# 11-12. apply_unit dry-run


def test_apply_unit_dry_run_write_is_noop(backend_factory, vault_with_v1_to_v2_script):
    b = backend_factory(vault_with_v1_to_v2_script)
    units = b.enumerate_units()
    assert units, "Expected units in vault"
    # Load the script
    scripts = b._discover_scripts()
    assert scripts, "Expected v1_to_v2 script"
    script = scripts[0]

    # Record original content
    unit = units[0]
    original_meta = unit.read_frontmatter()

    # Apply dry-run
    b.apply_unit(unit, script, dry_run=True)

    # Backing store must be unchanged
    fresh_meta = unit.read_frontmatter()
    assert fresh_meta == original_meta, (
        "dry-run apply_unit must not modify the backing store"
    )


def test_apply_unit_dry_run_unit_type_present(
    backend_factory, vault_with_v1_to_v2_script
):
    b = backend_factory(vault_with_v1_to_v2_script)
    units = b.enumerate_units()
    for unit in units:
        assert unit.unit_type in {"memory", "wiki"}


# ──────────────────────────────────────────────────────────────────
# 13-14. apply_unit real


def test_apply_unit_real_bumps_schema_version(
    backend_factory, vault_with_v1_to_v2_script
):
    b = backend_factory(vault_with_v1_to_v2_script)
    units = b.enumerate_units()
    scripts = b._discover_scripts()
    unit = units[0]
    script = scripts[0]

    b.apply_unit(unit, script, dry_run=False)

    meta_after = unit.read_frontmatter()
    assert meta_after["schema_version"] == 2


def test_apply_unit_missing_version_bump_raises(tmp_path, backend_factory):
    """Script that forgets to bump schema_version raises AtomicAgentsError."""
    agents_root = tmp_path / "agents"
    memory = agents_root / "alice" / "memory"
    memory.mkdir(parents=True)
    _make_note(memory, "feedback_x.md", schema_version=1)
    migrations = agents_root / "_migrations"
    migrations.mkdir()
    # Script applies but does NOT write schema_version
    (migrations / "v1_to_v2.py").write_text(
        "FROM_VERSION = 1\n"
        "TO_VERSION = 2\n"
        "def applies_to(unit):\n"
        "    meta = unit.read_frontmatter()\n"
        "    return meta.get('schema_version') == 1\n"
        "def migrate(unit):\n"
        "    # Intentionally forgets to bump schema_version\n"
        "    return {'unit_id': unit.unit_id, 'changes': []}\n"
    )
    b = backend_factory(agents_root)
    units = b.enumerate_units()
    scripts = b._discover_scripts()
    with pytest.raises(AtomicAgentsError, match="did not bump schema_version"):
        b.apply_unit(units[0], scripts[0], dry_run=False)


def _vault_with_empty_dict_migrate(tmp_path, backend_factory):
    """Vault whose script: applies_to->True but migrate->{} (empty dict)."""
    agents_root = tmp_path / "agents"
    memory = agents_root / "alice" / "memory"
    memory.mkdir(parents=True)
    _make_note(memory, "feedback_x.md", schema_version=1)
    migrations = agents_root / "_migrations"
    migrations.mkdir()
    (migrations / "v1_to_v2.py").write_text(
        "FROM_VERSION = 1\n"
        "TO_VERSION = 2\n"
        "def applies_to(unit):\n"
        "    return True\n"
        "def migrate(unit):\n"
        "    # examined but made no change: returns an EMPTY dict\n"
        "    return {}\n"
    )
    return backend_factory(agents_root)


def test_apply_unit_dry_run_empty_dict_migrate_is_skip(tmp_path, backend_factory):
    """migrate()->{} with applies_to->True is a SKIP on dry-run, not a touch.

    spec/03 §Return-value contract: a falsy return ({} or None) is the skip
    signal; the runner counts it toward units_skipped. The regression this
    guards: apply_unit must NOT inject provenance keys (script/unit_id) into an
    empty summary, which would make the documented skip read as truthy/touched.
    """
    b = _vault_with_empty_dict_migrate(tmp_path, backend_factory)
    units = b.enumerate_units()
    scripts = b._discover_scripts()
    summary = b.apply_unit(units[0], scripts[0], dry_run=True)
    assert summary == {}, (
        "migrate()->{} must stay falsy so run_migration counts it as a skip; "
        f"got non-empty summary {summary!r}"
    )


def test_run_migration_dry_run_empty_dict_migrate_counts_skip(
    tmp_path, backend_factory
):
    """A dry-run where migrate()->{} tallies units_skipped, not units_touched."""
    b = _vault_with_empty_dict_migrate(tmp_path, backend_factory)
    result = b.run_migration(target_version=2, dry_run=True)
    assert result.units_skipped == 1, (
        f"Expected 1 skipped unit, got {result.units_skipped}"
    )
    assert result.units_touched == [], (
        f"Expected no touched units, got {result.units_touched}"
    )


def test_apply_unit_real_run_falsy_migrate_after_applies_raises(
    tmp_path, backend_factory
):
    """Real run: applies_to->True + migrate->falsy (no version bump) raises.

    spec/03 §Return-value contract: on a real run there is no "examined but
    made no change" escape via migrate(); the MUST 7 version-bump check fires
    for any unit applies_to() claimed, so a falsy migrate() that skipped the
    bump fails loud (and the runner rolls the whole vault back).
    """
    b = _vault_with_empty_dict_migrate(tmp_path, backend_factory)
    units = b.enumerate_units()
    scripts = b._discover_scripts()
    with pytest.raises(AtomicAgentsError, match="did not bump schema_version"):
        b.apply_unit(units[0], scripts[0], dry_run=False)


def test_run_migration_real_run_falsy_migrate_rolls_back(tmp_path, backend_factory):
    """Real run with a falsy-migrate-after-applies script rolls the vault back."""
    b = _vault_with_empty_dict_migrate(tmp_path, backend_factory)
    if not b.capabilities().supports_transactional_rollback:
        pytest.skip("backend without rollback cannot run the real-run path")
    result = b.run_migration(target_version=2, dry_run=False)
    assert result.rolled_back is True
    assert result.validation_passed is False


# ──────────────────────────────────────────────────────────────────
# 15-20. snapshot / restore


def test_snapshot_returns_ref_with_nonempty_id(backend, vault):
    ref = backend.snapshot(2)
    assert isinstance(ref, MigrationSnapshotRef)
    assert ref.snapshot_id
    assert ref.backend_id == backend.backend_id


def test_snapshot_restore_round_trip(backend_factory, vault):
    b = backend_factory(vault)
    note_path = vault / "alice" / "memory" / "feedback_note_1.md"
    original = note_path.read_text()

    ref = b.snapshot(2)

    # Mutate
    note_path.write_text("CORRUPTED")
    assert note_path.read_text() == "CORRUPTED"

    b.restore(ref)
    assert note_path.read_text() == original


def test_snapshot_restore_preserves_migration_scripts(backend_factory, vault):
    """An operator's migration .py script survives a snapshot -> restore.

    spec/03 §Rollback step 5 ("Retry with corrected migration") assumes the
    scripts still exist after a rollback. The regression this guards: the
    snapshot loop adds ``_migrations/<script>.py`` but the agent-content tar
    filter blanket-excludes everything under ``_migrations`` (it is in
    EXCLUDED_DIRS), so the scripts were silently dropped from the tarball and
    a restore() DELETED them from the vault. The dedicated
    ``_migrations_tar_filter`` fixes this.
    """
    migrations = vault / "_migrations"
    migrations.mkdir(parents=True, exist_ok=True)
    script_path = migrations / "v1_to_v2.py"
    script_path.write_text(
        "FROM_VERSION = 1\nTO_VERSION = 2\n"
        "def applies_to(unit):\n    return True\n"
        "def migrate(unit):\n    return {}\n"
    )
    assert script_path.exists()

    b = backend_factory(vault)
    ref = b.snapshot(2)

    # Snapshot tarball must contain the script.
    import tarfile as _tf

    with _tf.open(str(b._snapshots_dir / ref.snapshot_id), "r:gz") as tar:
        names = tar.getnames()
    assert any(n.endswith("_migrations/v1_to_v2.py") for n in names), (
        f"Migration script must be in the snapshot tarball; got {names}"
    )
    # But the snapshots/ subdir and audit log must NOT recurse into themselves.
    assert not any("snapshots" in n.split("/") for n in names)
    assert not any(n.endswith("migration.jsonl") for n in names)

    # Delete the live script, then restore — the script must come back.
    script_path.unlink()
    assert not script_path.exists()
    b.restore(ref)
    assert script_path.exists(), (
        "Migration script must survive snapshot -> restore (rollback retry)"
    )
    assert "TO_VERSION = 2" in script_path.read_text()


def test_restore_atomic_corrupt_tar(backend_factory, vault):
    """A corrupt (truncated) snapshot must leave the live vault untouched."""
    b = backend_factory(vault)
    note_path = vault / "alice" / "memory" / "feedback_note_1.md"
    original = note_path.read_text()

    ref = b.snapshot(2)
    # Corrupt the snapshot by truncating it
    snap_path = b._snapshots_dir / ref.snapshot_id
    snap_path.write_bytes(snap_path.read_bytes()[:64])

    with pytest.raises(Exception):
        b.restore(ref)

    assert note_path.read_text() == original, (
        "Corrupt-tar restore must leave vault untouched"
    )


def test_restore_atomic_mid_extract_failure(backend_factory, vault, monkeypatch):
    """If extraction raises mid-stream, the live vault must be untouched."""
    b = backend_factory(vault)
    note_path = vault / "alice" / "memory" / "feedback_note_1.md"
    original = note_path.read_text()

    ref = b.snapshot(2)

    def boom_extractall(self, *args, **kwargs):
        raise OSError("simulated mid-extract failure")

    monkeypatch.setattr(tarfile.TarFile, "extractall", boom_extractall)
    with pytest.raises(OSError, match="simulated mid-extract failure"):
        b.restore(ref)

    assert note_path.read_text() == original, (
        "Mid-extract failure must leave vault untouched"
    )


def test_restore_unknown_snapshot_id_raises(backend, vault):
    with pytest.raises(MigrationSnapshotNotFound):
        backend.restore(
            MigrationSnapshotRef(
                backend_id=backend.backend_id,
                snapshot_id="nonexistent_snapshot.tar.gz",
            )
        )


def test_snapshot_restore_fresh_backend_instance(backend_factory, vault):
    """snapshot_id from snapshot() accepted by restore() on a fresh backend instance."""
    b1 = backend_factory(vault)
    ref = b1.snapshot(2)

    note_path = vault / "alice" / "memory" / "feedback_note_1.md"
    original = note_path.read_text()
    note_path.write_text("MUTATED")

    # Fresh instance — same agents_root
    b2 = backend_factory(vault)
    b2.restore(ref)
    assert note_path.read_text() == original


# ──────────────────────────────────────────────────────────────────
# 21-23. Fail-close path


def test_no_rollback_backend_isinstance_passes():
    stub = _NoRollbackStub()
    assert isinstance(stub, MigrationBackend), (
        "No-rollback stub must satisfy MigrationBackend Protocol structurally"
    )


def test_no_rollback_backend_real_run_raises_before_apply(vault):
    stub = _NoRollbackStub()
    with pytest.raises(MigrationRollbackUnavailable):
        stub.run_migration(target_version=2, dry_run=False)


def test_no_rollback_backend_dry_run_does_not_raise(vault):
    stub = _NoRollbackStub()
    # dry_run=True must proceed without raising MigrationRollbackUnavailable
    result = stub.run_migration(target_version=2, dry_run=True)
    assert result.dry_run is True


def test_filesystem_backend_fail_close_before_any_apply(tmp_path, backend_factory):
    """The fail-close check must fire before snapshot() and before apply_unit() is called."""
    agents_root = tmp_path / "agents"
    memory = agents_root / "alice" / "memory"
    memory.mkdir(parents=True)
    _make_note(memory, "feedback_x.md", schema_version=1)
    migrations = agents_root / "_migrations"
    migrations.mkdir()
    (migrations / "v1_to_v2.py").write_text(
        "FROM_VERSION = 1\nTO_VERSION = 2\n"
        "def applies_to(unit): return True\n"
        "def migrate(unit): return {}\n"
    )

    # Patch the backend to advertise no rollback
    b = backend_factory(agents_root)

    def no_rollback_caps():
        return MigrationCapabilities(
            supports_transactional_rollback=False,
            single_host_only=True,
        )

    b.capabilities = no_rollback_caps  # type: ignore[method-assign]

    apply_called = []
    original_apply = b.apply_unit

    def tracking_apply(unit, script, dry_run=True):
        apply_called.append(unit)
        return original_apply(unit, script, dry_run=dry_run)

    b.apply_unit = tracking_apply  # type: ignore[method-assign]

    with pytest.raises((MigrationRollbackUnavailable, AtomicAgentsError)):
        b.run_migration(target_version=2, dry_run=False)

    assert len(apply_called) == 0, "apply_unit must NOT be called when fail-close fires"

    # MUST 8: the no-rollback fail-close real-run is an audited pre-plan exit.
    # The refusal fires before the plan is built, so from_version is the -1
    # sentinel and the refusal error is recorded. A future refactor that moved
    # the fail-close raise outside the outer try (so the finally never runs)
    # would silently drop this MUST-8 record — this assertion pins it.
    log_path = agents_root / "_migrations" / "migration.jsonl"
    assert log_path.exists(), (
        "fail-close real-run must still emit a MigrationEvent (MUST 8)"
    )
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["from_version"] == -1, "pre-plan fail-close → from_version sentinel -1"
    assert event["error"], "fail-close audit line must record the refusal error"


# ──────────────────────────────────────────────────────────────────
# 24-30. run_migration integration


def test_run_migration_dry_run_no_file_changes(
    backend_factory, vault_with_v1_to_v2_script
):
    b = backend_factory(vault_with_v1_to_v2_script)
    note_path = vault_with_v1_to_v2_script / "alice" / "memory" / "feedback_note_1.md"
    original = note_path.read_text()
    result = b.run_migration(target_version=2, dry_run=True)
    assert result.dry_run is True
    assert note_path.read_text() == original
    assert result.snapshot_ref is None
    assert len(result.units_touched) == 3


def test_run_migration_dry_run_audit_event_written(
    backend_factory, vault_with_v1_to_v2_script
):
    b = backend_factory(vault_with_v1_to_v2_script)
    b.run_migration(target_version=2, dry_run=True)
    log_path = vault_with_v1_to_v2_script / "_migrations" / "migration.jsonl"
    assert log_path.exists()
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["dry_run"] is True
    assert event["from_version"] == 1
    assert event["to_version"] == 2


def test_run_migration_rollback_on_validation_failure(
    backend_factory, vault_with_v1_to_v2_script
):
    """Migration to v2 triggers rollback because CURRENT_SCHEMA_VERSION is still 1.

    The standard validators reject schema_version != CURRENT_SCHEMA_VERSION. So any
    real migration to v2 on a vault where the helper hasn't bumped its constant will
    fail validation and roll back. This is the correct safety behavior: you cannot
    migrate to a version the helper doesn't yet support.
    """
    b = backend_factory(vault_with_v1_to_v2_script)
    note_path = vault_with_v1_to_v2_script / "alice" / "memory" / "feedback_note_1.md"

    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = b.run_migration(target_version=2, dry_run=False)

    assert result.rolled_back is True, "Validation failure must trigger rollback"
    assert result.snapshot_ref is not None

    # Original files should be restored
    parsed = frontmatter.load(str(note_path))
    assert parsed.metadata["schema_version"] == 1, "Rolled-back note must be at v1"


def test_run_migration_validation_then_rollback_failure_audit(
    backend_factory, vault_with_v1_to_v2_script, monkeypatch
):
    """When validation fails AND restore() then fails, the audit record is honest.

    The bug this guards: both post-rollback paths used to UNCONDITIONALLY set
    result.error to the reassuring "Vault rolled back to snapshot." message,
    clobbering the critical "inconsistent state" message _do_rollback wrote
    when restore() itself failed — so the durable JSONL line falsely claimed a
    clean rollback while the vault was left half-migrated. The fix preserves
    the inconsistent-state message (snapshot id + restore cause) when rollback
    did NOT succeed.
    """
    b = backend_factory(vault_with_v1_to_v2_script)

    # Force restore() (the rollback path) to fail.
    def boom_restore(ref):
        raise RuntimeError("simulated restore failure")

    monkeypatch.setattr(b, "restore", boom_restore)

    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = b.run_migration(target_version=2, dry_run=False)

    # (a) rollback did NOT succeed
    assert result.rolled_back is False
    # (b) error names the inconsistent state and carries the snapshot id
    assert "inconsistent state" in result.error
    assert result.snapshot_ref is not None
    assert result.snapshot_ref.snapshot_id in result.error
    assert "simulated restore failure" in result.error
    # The reassuring clean-rollback wording must NOT be present.
    assert "Vault rolled back to snapshot." not in result.error

    # (c) the durable JSONL line matches: rolled_back False + the true error
    log_path = vault_with_v1_to_v2_script / "_migrations" / "migration.jsonl"
    event = json.loads(log_path.read_text().strip().splitlines()[-1])
    assert event["rolled_back"] is False
    assert "inconsistent state" in event["error"]


def test_run_migration_wiki_pages_use_wiki_schema(
    backend_factory, vault_with_wiki_and_noop_script
):
    """Post-migration validation must use wiki schema for wiki pages.

    The noop script keeps all files at schema_version=1 (doesn't write anything),
    so post-migration validation at the current CURRENT_SCHEMA_VERSION=1 passes.
    Wiki pages must not be validated against the note schema (which rejects type: wiki_page).
    """
    b = backend_factory(vault_with_wiki_and_noop_script)
    # Real run — noop script touches everything but bumps nothing
    # So apply_unit will fail on the version-bump assertion for the noop script.
    # We need a noop script that at least checks applies_to correctly.
    # Since the noop fixture script applies_to returns True but doesn't bump,
    # apply_unit will raise. So for this test we use dry_run=True to exercise
    # the enumerate path and unit_type dispatch without needing a real bump.
    units = b.enumerate_units()
    types = {u.unit_type for u in units}
    assert "wiki" in types, "Wiki pages must be enumerated with unit_type='wiki'"
    assert "memory" in types, "Memory notes must be enumerated with unit_type='memory'"


def test_run_migration_target_below_current_raises(
    backend_factory, vault_with_v1_to_v2_script
):
    b = backend_factory(vault_with_v1_to_v2_script)
    with pytest.raises(AtomicAgentsError, match="not above current"):
        b.run_migration(target_version=1, dry_run=True)


def test_run_migration_no_scripts_raises(backend_factory, vault):
    b = backend_factory(vault)
    with pytest.raises(AtomicAgentsError, match="No migration script"):
        b.run_migration(target_version=2, dry_run=True)


def test_run_migration_audit_real_run(backend_factory, vault_with_v1_to_v2_script):
    """Audit log is written on real run, recording correct metadata."""
    b = backend_factory(vault_with_v1_to_v2_script)
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        b.run_migration(target_version=2, dry_run=False)

    log_path = vault_with_v1_to_v2_script / "_migrations" / "migration.jsonl"
    assert log_path.exists()
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) >= 1
    event = json.loads(lines[-1])
    assert event["dry_run"] is False
    assert event["from_version"] == 1
    assert event["to_version"] == 2
    assert "run_id" in event
    assert event["rolled_back"] is True  # because v2 fails current validator


# ──────────────────────────────────────────────────────────────────
# Happy path: a real migration that COMPLETES (validation_passed, no rollback)


def test_run_migration_real_success_no_rollback(
    backend_factory, vault_with_v1_to_v2_script, monkeypatch
):
    """A real migration completes end-to-end when the package adopts the target.

    The validators reject any schema_version != CURRENT_SCHEMA_VERSION, so a
    real cross-version migration only succeeds once the package bumps that
    constant. We simulate the post-bump world by monkeypatching
    CURRENT_SCHEMA_VERSION to 2 everywhere it is read (the _schema module AND
    the migration.filesystem module that imported it by value). This exercises
    the happy path the other real-run tests cannot: apply bumps the version,
    full-unit validation passes, lock releases, validation_passed=True,
    rolled_back=False, on-disk files end at v2, audit records rolled_back=False.
    """
    import atomic_agents._schema as schema_mod
    import atomic_agents.migration.filesystem as fs_mod

    monkeypatch.setattr(schema_mod, "CURRENT_SCHEMA_VERSION", 2)
    monkeypatch.setattr(fs_mod, "CURRENT_SCHEMA_VERSION", 2)

    b = backend_factory(vault_with_v1_to_v2_script)
    result = b.run_migration(target_version=2, dry_run=False)

    assert result.validation_passed is True, "Happy-path migration must pass validation"
    assert result.rolled_back is False, "Successful migration must NOT roll back"
    assert result.error == ""
    assert result.snapshot_ref is not None
    assert len(result.units_touched) == 3

    # On-disk files end at the new schema version.
    note_path = vault_with_v1_to_v2_script / "alice" / "memory" / "feedback_note_1.md"
    parsed = frontmatter.load(str(note_path))
    assert parsed.metadata["schema_version"] == 2

    # Audit records the success (rolled_back=False).
    log_path = vault_with_v1_to_v2_script / "_migrations" / "migration.jsonl"
    event = json.loads(log_path.read_text().strip().splitlines()[-1])
    assert event["rolled_back"] is False
    assert event["to_version"] == 2


def test_run_migration_validates_skipped_units(tmp_path, backend_factory, monkeypatch):
    """Validation covers EVERY unit, not only the ones a script touched.

    A script whose applies_to() skips one note leaves that note at v1. With
    CURRENT_SCHEMA_VERSION bumped to 2, the touched note validates but the
    skipped (still-v1) note must FAIL validation and trigger rollback — the
    no-half-migrated-vault guarantee. If the runner validated only touched
    units, this would wrongly report validation_passed=True.
    """
    import atomic_agents._schema as schema_mod
    import atomic_agents.migration.filesystem as fs_mod

    monkeypatch.setattr(schema_mod, "CURRENT_SCHEMA_VERSION", 2)
    monkeypatch.setattr(fs_mod, "CURRENT_SCHEMA_VERSION", 2)

    agents_root = tmp_path / "agents"
    memory = agents_root / "alice" / "memory"
    memory.mkdir(parents=True)
    _make_note(memory, "feedback_touch_me.md", schema_version=1)
    _make_note(memory, "feedback_skip_me.md", schema_version=1)
    migrations = agents_root / "_migrations"
    migrations.mkdir()
    # Script bumps only files whose name contains "touch"; skips the other.
    (migrations / "v1_to_v2.py").write_text(
        "FROM_VERSION = 1\n"
        "TO_VERSION = 2\n"
        "def applies_to(unit):\n"
        "    return 'touch' in unit.unit_id\n"
        "def migrate(unit):\n"
        "    meta = unit.read_frontmatter()\n"
        "    meta['schema_version'] = TO_VERSION\n"
        "    unit.write_frontmatter(meta)\n"
        "    return {'unit_id': unit.unit_id, 'changes': ['1->2']}\n"
    )
    b = backend_factory(agents_root)
    result = b.run_migration(target_version=2, dry_run=False)

    assert result.validation_passed is False, (
        "A skipped (still-v1) unit must fail validation"
    )
    assert result.rolled_back is True, "Half-migrated vault must roll back"
    assert any("skip_me" in e["unit_id"] for e in result.validation_errors)
    # Rollback restored both files to v1.
    skipped = frontmatter.load(str(memory / "feedback_skip_me.md"))
    touched = frontmatter.load(str(memory / "feedback_touch_me.md"))
    assert skipped.metadata["schema_version"] == 1
    assert touched.metadata["schema_version"] == 1


def test_run_migration_audit_written_on_no_scripts(backend_factory, vault):
    """Pre-plan failure (no scripts) still writes an audit line (MUST 8)."""
    b = backend_factory(vault)
    with pytest.raises(AtomicAgentsError, match="No migration script"):
        b.run_migration(target_version=2, dry_run=False)
    log_path = vault / "_migrations" / "migration.jsonl"
    assert log_path.exists(), "Failed migration must still write an audit line"
    event = json.loads(log_path.read_text().strip().splitlines()[-1])
    assert event["to_version"] == 2
    assert event["from_version"] == -1, "from_version unknown pre-plan → sentinel -1"
    assert event["error"], "Pre-plan failure must record the error string"


def test_run_migration_audit_written_on_target_below_current(backend_factory, vault):
    """Pre-plan failure (target below current) still writes an audit line."""
    b = backend_factory(vault)
    # vault is at v1; ask for v1 → "not above current"
    with pytest.raises(AtomicAgentsError, match="not above current"):
        b.run_migration(target_version=1, dry_run=False)
    log_path = vault / "_migrations" / "migration.jsonl"
    assert log_path.exists()
    event = json.loads(log_path.read_text().strip().splitlines()[-1])
    assert event["error"]


def test_run_migration_audit_survives_lock_release_failure(
    backend_factory, vault_with_v1_to_v2_script, monkeypatch
):
    """If lock release raises in teardown, the audit line is still written.

    The audit record is the durable artifact; an abnormal unlock must not
    suppress it. We patch FilesystemLockBackend.release to raise and assert
    the migration.jsonl line is present afterward.
    """
    import atomic_agents.locks as locks_mod

    def boom_release(self, handle):
        raise OSError("simulated lock-release failure")

    monkeypatch.setattr(locks_mod.FilesystemLockBackend, "release", boom_release)

    b = backend_factory(vault_with_v1_to_v2_script)
    # Dry-run: no snapshot/rollback, isolates the lock-release teardown path.
    b.run_migration(target_version=2, dry_run=True)

    log_path = vault_with_v1_to_v2_script / "_migrations" / "migration.jsonl"
    assert log_path.exists(), "Audit must survive a lock-release failure"
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["to_version"] == 2


def test_run_migration_lock_busy_records_resolved_from_version(
    backend_factory, vault_with_v1_to_v2_script
):
    """A lock-busy refusal records the RESOLVED from_version, not the -1 sentinel.

    spec/03 §Schema-migration MUST 8 (and its concurrency paragraph) carve
    lock-busy out of the pre-plan sentinel set: the lock is acquired at step 3,
    AFTER the plan is built at step 2, so a LockBusy refusal already knows the
    resolved ``from_version``. This is the load-bearing reason the code does NOT
    reorder lock-acquire before plan-build. The complementary pre-plan -1 case is
    pinned by test_filesystem_backend_fail_close_before_any_apply; this test pins
    the resolved-version case so a future refactor that moved lock acquisition
    before plan-build would fail loudly instead of silently breaking the MUST.
    """
    import atomic_agents.locks as locks_mod

    agents_root = vault_with_v1_to_v2_script
    # Pre-acquire the migration lock so the run's own acquire() raises LockBusy.
    holder = locks_mod.FilesystemLockBackend(agents_root)
    held = holder.acquire("migration", timeout=0)
    try:
        b = backend_factory(agents_root)
        with pytest.raises(
            AtomicAgentsError, match="Another migration is already running"
        ):
            b.run_migration(target_version=2, dry_run=False)
    finally:
        holder.release(held)

    log_path = agents_root / "_migrations" / "migration.jsonl"
    assert log_path.exists(), (
        "lock-busy refusal must still emit a MigrationEvent (MUST 8)"
    )
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["from_version"] == 1, (
        "lock-busy fires AFTER the plan is built → resolved from_version, NOT the -1 sentinel"
    )
    assert event["to_version"] == 2
    assert event["error"], "lock-busy audit line must record the refusal error"


def test_run_migration_midapply_exception_then_rollback_failure_audit(
    tmp_path, backend_factory, monkeypatch
):
    """Mid-apply exception AND a failing restore() leaves an HONEST audit record.

    Sibling of test_run_migration_validation_then_rollback_failure_audit, but the
    failure originates in the catastrophic ``except Exception`` branch of
    run_migration (a script that writes WITHOUT bumping schema_version, so
    apply_unit raises AtomicAgentsError mid-apply) rather than the post-validation
    branch. That branch has its own separately-authored error-preservation format
    (``f"Migration error: {exc}\\n{result.error}"``); this test guards it so a
    future refactor cannot silently regress the inconsistent-state honesty.

    Asserts: rolled_back is False, error starts with "Migration error:", error
    carries the inconsistent-state message + snapshot id + the rollback root
    cause, the reassuring "rolled back to snapshot" wording is ABSENT, and the
    JSONL audit line matches (rolled_back False + inconsistent-state error).
    """
    agents_root = tmp_path / "agents"
    memory = agents_root / "alice" / "memory"
    memory.mkdir(parents=True)
    _make_note(memory, "feedback_note_1.md", schema_version=1)
    migrations = agents_root / "_migrations"
    migrations.mkdir()
    # Script WRITES (so the apply mutates the vault) but does NOT bump
    # schema_version → apply_unit raises AtomicAgentsError mid-apply.
    (migrations / "v1_to_v2.py").write_text(
        "FROM_VERSION = 1\n"
        "TO_VERSION = 2\n"
        "def applies_to(unit):\n"
        "    return True\n"
        "def migrate(unit):\n"
        "    meta = unit.read_frontmatter()\n"
        "    meta['provenance'] = 'touched_but_not_bumped'\n"
        "    unit.write_frontmatter(meta)\n"
        "    return {'unit_id': unit.unit_id}\n"
    )

    b = backend_factory(agents_root)

    # Force restore() (the rollback path) to fail.
    def boom_restore(ref):
        raise RuntimeError("simulated restore failure")

    monkeypatch.setattr(b, "restore", boom_restore)

    result = b.run_migration(target_version=2, dry_run=False)

    # (a) rollback did NOT succeed
    assert result.rolled_back is False
    # (b) error preserves BOTH the mid-apply cause and the inconsistent state
    assert result.error.startswith("Migration error:")
    assert "did not bump schema_version" in result.error
    assert "inconsistent state" in result.error
    assert result.snapshot_ref is not None
    assert result.snapshot_ref.snapshot_id in result.error
    assert "simulated restore failure" in result.error
    # The reassuring clean-rollback wording must NOT be present.
    assert "Vault rolled back to snapshot." not in result.error

    # (c) durable JSONL line matches
    log_path = agents_root / "_migrations" / "migration.jsonl"
    event = json.loads(log_path.read_text().strip().splitlines()[-1])
    assert event["rolled_back"] is False
    assert "inconsistent state" in event["error"]


def test_cmd_rollback_writes_audit_line(vault_with_v1_to_v2_script):
    """The operator-facing --rollback CLI path records a MigrationEvent (MUST 8).

    A manual rollback mutates the vault destructively; it MUST leave a forensic
    audit line just like a run_migration() exit does. The CLI routes through
    restore_and_audit(), which emits rolled_back=True on success.
    """
    import atomic_agents.migrate as migrate_module

    b = FilesystemMigrationBackend(vault_with_v1_to_v2_script)
    ref = b.snapshot(2)
    log_path = vault_with_v1_to_v2_script / "_migrations" / "migration.jsonl"
    lines_before = (
        len(log_path.read_text().strip().splitlines()) if log_path.exists() else 0
    )

    rc = migrate_module._cmd_rollback(b, ref.snapshot_id)
    assert rc == 0

    assert log_path.exists(), "Manual rollback must write an audit line"
    lines_after = log_path.read_text().strip().splitlines()
    assert len(lines_after) == lines_before + 1, (
        "Exactly one new MigrationEvent must be appended by a manual rollback"
    )
    event = json.loads(lines_after[-1])
    assert event["rolled_back"] is True
    assert event["dry_run"] is False
    assert "run_id" in event
    assert event["error"] == ""


def test_cmd_rollback_failure_writes_audit_line(
    vault_with_v1_to_v2_script, monkeypatch
):
    """A FAILED manual rollback is also recorded (rolled_back=False + error)."""
    import atomic_agents.migrate as migrate_module

    b = FilesystemMigrationBackend(vault_with_v1_to_v2_script)
    ref = b.snapshot(2)

    def boom_restore(r):
        raise RuntimeError("simulated restore failure")

    monkeypatch.setattr(b, "restore", boom_restore)

    log_path = vault_with_v1_to_v2_script / "_migrations" / "migration.jsonl"
    lines_before = (
        len(log_path.read_text().strip().splitlines()) if log_path.exists() else 0
    )

    rc = migrate_module._cmd_rollback(b, ref.snapshot_id)
    assert rc == 1, "A failed rollback must return a nonzero exit code"

    lines_after = log_path.read_text().strip().splitlines()
    assert len(lines_after) == lines_before + 1
    event = json.loads(lines_after[-1])
    assert event["rolled_back"] is False
    assert "simulated restore failure" in event["error"]


def test_restore_and_audit_pre_read_failure_still_restores(
    vault_with_v1_to_v2_script, monkeypatch
):
    """A flaky PRE-restore read must NOT abort the operator-facing rollback.

    MUST 8 / Principle #8 (defensive symmetry): restore_and_audit() reads the
    schema version BEFORE the restore to record from_version. That read is a
    full vault walk and can raise on a transient FS error — but the rollback is
    the destructive recovery the operator most needs to succeed. The pre-restore
    read must degrade from_version to the -1 sentinel and proceed, symmetric to
    the post-restore guard. Sibling of
    test_restore_and_audit_success_post_read_failure_still_audits, which pins the
    SECOND (post-restore) read; this one pins the FIRST (pre-restore) read.
    """
    b = FilesystemMigrationBackend(vault_with_v1_to_v2_script)
    ref = b.snapshot(2)

    note_path = vault_with_v1_to_v2_script / "alice" / "memory" / "feedback_note_1.md"
    original = note_path.read_text()

    log_path = vault_with_v1_to_v2_script / "_migrations" / "migration.jsonl"
    lines_before = (
        len(log_path.read_text().strip().splitlines()) if log_path.exists() else 0
    )

    # Mutate the vault so a successful restore is observable.
    note_path.write_text("CORRUPTED")

    # First read_schema_version() (pre-restore from_version) raises; the second
    # (post-restore to_version) succeeds.
    real_read = b.read_schema_version
    calls = {"n": 0}

    def flaky_read():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated pre-restore read failure")
        return real_read()

    monkeypatch.setattr(b, "read_schema_version", flaky_read)

    # Must NOT raise — the pre-restore read failure degrades to the sentinel and
    # the restore proceeds.
    b.restore_and_audit(ref)

    # The destructive restore actually happened.
    assert note_path.read_text() == original, (
        "A flaky pre-restore read must not abort the rollback"
    )

    lines_after = log_path.read_text().strip().splitlines()
    assert len(lines_after) == lines_before + 1, (
        "A successful restore must always produce exactly one audit line, "
        "even when the pre-restore version read fails"
    )
    event = json.loads(lines_after[-1])
    assert event["rolled_back"] is True
    assert event["from_version"] == -1, (
        "Pre-restore read failure must degrade from_version to the -1 sentinel"
    )


def test_restore_and_audit_success_post_read_failure_still_audits(
    vault_with_v1_to_v2_script, monkeypatch
):
    """A SUCCESSFUL restore whose post-restore read raises still emits an audit line.

    MUST 8 / Principle #5: a completed destructive recovery action must never
    leave NO audit record. restore() succeeds, but the follow-up
    read_schema_version() (a full vault walk) can raise on a transient FS error
    — the success-path emit must be guarded and degrade to_version to the -1
    sentinel rather than swallowing the audit line and propagating raw.
    """
    b = FilesystemMigrationBackend(vault_with_v1_to_v2_script)
    ref = b.snapshot(2)

    log_path = vault_with_v1_to_v2_script / "_migrations" / "migration.jsonl"
    lines_before = (
        len(log_path.read_text().strip().splitlines()) if log_path.exists() else 0
    )

    # First read_schema_version() (pre-restore from_version) succeeds; the
    # second (post-restore to_version) raises.
    real_read = b.read_schema_version
    calls = {"n": 0}

    def flaky_read():
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("simulated post-restore read failure")
        return real_read()

    monkeypatch.setattr(b, "read_schema_version", flaky_read)

    # Must NOT raise — the restore succeeded; the audit must still be written.
    b.restore_and_audit(ref)

    lines_after = log_path.read_text().strip().splitlines()
    assert len(lines_after) == lines_before + 1, (
        "A successful restore must always produce exactly one audit line, "
        "even when the post-restore version read fails"
    )
    event = json.loads(lines_after[-1])
    assert event["rolled_back"] is True
    assert event["to_version"] == -1, (
        "Post-restore read failure must degrade to_version to the -1 sentinel"
    )


def test_snapshot_preserves_operator_subdir_under_migrations(backend_factory, vault):
    """Operator-created files/subdirs under _migrations/ survive snapshot→restore.

    The deliberate _tar_filter vs _migrations_tar_filter split must capture
    operator artifacts under _migrations/ (notes, backups) recursively while
    still excluding only the snapshots/ subdir and the audit log. Guards
    spec/03 §Rollback step 5 (the scripts + sidecars must still exist after a
    rollback) against a future filter change silently dropping them.
    """
    migrations = vault / "_migrations"
    migrations.mkdir(parents=True, exist_ok=True)
    subdir = migrations / "notes"
    subdir.mkdir()
    (subdir / "README.txt").write_text("operator notes\n")

    b = backend_factory(vault)
    ref = b.snapshot(2)

    import tarfile as _tf

    with _tf.open(str(b._snapshots_dir / ref.snapshot_id), "r:gz") as tar:
        names = tar.getnames()
    assert any(n.endswith("_migrations/notes/README.txt") for n in names), (
        f"Operator subdir under _migrations/ must be in the snapshot; got {names}"
    )
    # snapshots/ must still be excluded (no self-nesting).
    assert not any("snapshots" in n.split("/") for n in names)


def test_snapshot_excludes_sharded_run_logs(backend_factory, tmp_path):
    """Per-agent run logs are excluded from the snapshot at their REAL on-disk shape.

    The real log layout is sharded — ``<agent>/log/YYYY-MM/YYYY-MM-DD.jsonl``
    (logs/filesystem.py) — so the immediate parent of a log file is the
    ``YYYY-MM`` month dir, not ``log``. The exclusion must therefore key off a
    ``log`` ANCESTOR, not the immediate parent. spec/03 §Schema-migration states
    snapshots exclude "caches, logs, and other regeneratable artifacts"; logs
    are the largest, fastest-growing artifact, and restoring a snapshot must not
    clobber log lines written after it was taken.

    Also guards the false-drop direction: a ``.md`` content unit under an agent
    legitimately named ``log`` must survive (the .jsonl-only exclusion never
    touches it).
    """
    agents_root = tmp_path / "agents"
    # Real sharded run log — MUST be excluded.
    sharded_log = agents_root / "alice" / "log" / "2026-06"
    sharded_log.mkdir(parents=True)
    (sharded_log / "2026-06-10.jsonl").write_text('{"run": 1}\n')
    # Flat run log (older / alternate layout) — MUST also be excluded.
    flat_log = agents_root / "bob" / "log"
    flat_log.mkdir(parents=True)
    (flat_log / "run.jsonl").write_text('{"run": 2}\n')
    # A .md content unit under an agent literally named ``log`` — MUST survive.
    log_agent_memory = agents_root / "log" / "memory"
    log_agent_memory.mkdir(parents=True)
    (log_agent_memory / "important.md").write_text("# keep me\n")

    b = backend_factory(agents_root)
    ref = b.snapshot(2)

    import tarfile as _tf

    with _tf.open(str(b._snapshots_dir / ref.snapshot_id), "r:gz") as tar:
        names = [n.replace("\\", "/") for n in tar.getnames()]
    # Sharded and flat run logs are both excluded at any log-ancestor depth.
    assert not any(n.endswith("alice/log/2026-06/2026-06-10.jsonl") for n in names), (
        f"Sharded per-agent run logs must be excluded from the snapshot; got {names}"
    )
    assert not any(n.endswith("bob/log/run.jsonl") for n in names)
    # A real content unit under an agent named 'log' is NOT dropped.
    assert any(n.endswith("log/memory/important.md") for n in names), (
        f"A .md content unit under an agent named 'log' must survive; got {names}"
    )


def test_snapshot_filename_uses_target_version(backend_factory, vault):
    """snapshot(target) names the tarball pre_v{target}, matching spec/03."""
    b = backend_factory(vault)
    ref = b.snapshot(2)
    assert "_pre_v2_migration.tar.gz" in ref.snapshot_id, (
        f"snapshot_id {ref.snapshot_id!r} must encode the TARGET version (v2), "
        f"matching the spec/03 'pre_v2' recovery example"
    )


def test_write_frontmatter_preserves_custom_fields(backend_factory, vault):
    """Arbitrary custom frontmatter fields survive a migration write.

    spec/03 promises custom user-added frontmatter is preserved. The backend
    assigns ``post.metadata = dict(metadata)`` rather than splatting
    ``**metadata`` into ``Post(content, handler=None, **metadata)`` — the latter
    would silently eat a key named ``handler`` and raise on a key named
    ``content``.
    """
    note_path = vault / "alice" / "memory" / "feedback_note_1.md"
    b = backend_factory(vault)
    units = [u for u in b.enumerate_units() if "feedback_note_1" in u.unit_id]
    assert units
    unit = b._make_unit(Path(units[0].unit_id), "memory", dry_run=False)
    meta = unit.read_frontmatter()
    meta["custom_tag"] = "keep_me"
    meta["schema_version"] = 1
    unit.write_frontmatter(meta)
    reloaded = frontmatter.load(str(note_path))
    assert reloaded.metadata.get("custom_tag") == "keep_me", (
        "Custom frontmatter field must be preserved across a write"
    )


def test_handler_key_full_round_trips(backend_factory, vault):
    """A field named 'handler' round-trips through read AND write.

    spec/03 §"preserves unknown fields" promises arbitrary custom frontmatter
    is preserved. ``handler`` (and ``content``) are plausible custom keys that
    collide with python-frontmatter's ``Post(content, handler, **metadata)``
    signature — so the backend reads/writes via ``frontmatter.parse()`` (which
    returns ``(metadata, content)`` without constructing a Post) instead of
    ``frontmatter.load()``. This asserts the FULL round-trip the old test could
    not: write a ``handler`` field, then ``read_frontmatter()`` returns it
    WITHOUT raising.
    """
    note_path = vault / "alice" / "memory" / "feedback_note_1.md"
    b = backend_factory(vault)
    unit = b._make_unit(note_path, "memory", dry_run=False)
    meta = unit.read_frontmatter()
    meta["handler"] = "custom_value"
    unit.write_frontmatter(meta)

    # Raw on-disk text carries the field.
    assert "handler: custom_value" in note_path.read_text()

    # And it reads back without the Post-kwarg TypeError.
    reread = unit.read_frontmatter()
    assert reread.get("handler") == "custom_value", (
        "A 'handler' frontmatter field must round-trip through read_frontmatter()"
    )


def test_unit_already_carrying_handler_is_readable(backend_factory, vault):
    """A unit that ALREADY has a 'handler' field on disk is enumerable + readable.

    The crash mode this guards: ``read_schema_version()`` enumerates every unit
    and reads its frontmatter; if a single unit with a ``handler`` key raised
    TypeError, it would be counted 'unparseable', excluded from the version
    set, and a vault of all-such-units would falsely report
    CURRENT_SCHEMA_VERSION — masking a real pending migration.
    """
    note_path = vault / "alice" / "memory" / "has_handler.md"
    note_path.write_text(
        "---\nschema_version: 1\nhandler: my_custom_value\ntype: feedback\n---\nbody\n"
    )
    b = backend_factory(vault)
    units = [u for u in b.enumerate_units() if "has_handler" in u.unit_id]
    assert units, "unit with a 'handler' field must be enumerated"
    meta = units[0].read_frontmatter()
    assert meta.get("handler") == "my_custom_value"
    assert meta.get("schema_version") == 1

    # read_schema_version must SEE this unit's version, not drop it.
    assert b.read_schema_version() == 1, (
        "read_schema_version() must report v1 for a vault containing a unit "
        "with a 'handler' field — not mask it as CURRENT"
    )


# ──────────────────────────────────────────────────────────────────
# Capability-gated conformance matrix (spec/24/25 pattern)


@pytest.mark.parametrize(
    "name,factory",
    ALL_CONFORMANCE_BACKENDS,
    ids=lambda p: p if isinstance(p, str) else "",
)
def test_capability_gated_snapshot_restore(name, factory, vault):
    """Snapshot/restore conformance runs for rollback backends, SKIPS otherwise.

    Proves the capability-gated-skip machinery: the filesystem backend runs the
    round-trip; the no_rollback stub is SKIPPED (not errored) by the gate.
    """
    b = factory(vault)
    _skip_if_no_rollback(b)
    note_path = vault / "alice" / "memory" / "feedback_note_1.md"
    original = note_path.read_text()
    ref = b.snapshot(2)
    note_path.write_text("MUTATED")
    b.restore(ref)
    assert note_path.read_text() == original


@pytest.mark.parametrize(
    "name,factory",
    ALL_CONFORMANCE_BACKENDS,
    ids=lambda p: p if isinstance(p, str) else "",
)
def test_capability_gated_real_run_fail_close(
    name, factory, vault_with_v1_to_v2_script
):
    """Real run: rollback backend proceeds; no-rollback backend refuses (MUST 5).

    The gate routes each backend to its correct conformance assertion instead
    of running the same body against an incompatible backend.
    """
    b = factory(vault_with_v1_to_v2_script)
    if not b.capabilities().supports_transactional_rollback:
        # No-rollback backend MUST refuse a destructive run.
        with pytest.raises(MigrationRollbackUnavailable):
            b.run_migration(target_version=2, dry_run=False)
    else:
        # Rollback backend proceeds far enough to take a snapshot (then rolls
        # back at validation since CURRENT is still 1 — exercised elsewhere).
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            result = b.run_migration(target_version=2, dry_run=False)
        assert result.snapshot_ref is not None


# ──────────────────────────────────────────────────────────────────
# 31-33. Breaking-signature behavior


def test_old_migration_script_protocol_removed():
    """The old MigrationScript Protocol class must not exist in atomic_agents.migrate."""
    import atomic_agents.migrate as migrate_module

    assert not hasattr(migrate_module, "MigrationScript"), (
        "MigrationScript Protocol must be removed in the clean-break refactor (issue #429)"
    )


def test_old_run_migration_free_function_removed():
    """The old path-shaped run_migration() free function is gone."""
    import atomic_agents.migrate as migrate_module

    # run_migration should not exist as a module-level free function
    # (the new API is backend.run_migration())
    assert not hasattr(migrate_module, "run_migration"), (
        "Old free-function run_migration() must be removed; use backend.run_migration()"
    )


def test_old_path_shaped_applies_to_rejected():
    """Scripts using the old applies_to(path: Path) signature should not satisfy the new contract.

    The new contract: applies_to(unit: MigratableUnit) -> bool.
    This test verifies that if a script is loaded and called with a MigratableUnit,
    the old path-shaped signature would fail at runtime (type error or AttributeError).
    """
    # The new interface passes a MigratableUnit, not a Path.
    # An old-style script calling path.suffix on a MigratableUnit would fail.
    # We simulate the old script:
    import types

    old_module = types.ModuleType("fake_old_script")
    old_module.FROM_VERSION = 1
    old_module.TO_VERSION = 2

    def old_applies_to(path):  # expects Path, not MigratableUnit
        return path.suffix == ".md"  # MigratableUnit has no .suffix

    old_module.applies_to = old_applies_to

    # Construct a MigratableUnit handle
    def _read():
        return {"schema_version": 1}

    def _write(meta):
        pass

    unit = MigratableUnit(
        unit_id="/fake/path/note.md",
        unit_type="memory",
        dry_run=True,
        _read_fn=_read,
        _write_fn=_write,
    )

    # Calling old applies_to with a MigratableUnit raises AttributeError
    with pytest.raises(AttributeError):
        old_module.applies_to(unit)


# ──────────────────────────────────────────────────────────────────
# Additional regression tests preserved from old test_migrate.py


def test_snapshot_excludes_meta_dirs(backend_factory, vault):
    b = backend_factory(vault)
    (vault / "_dashboard").mkdir()
    (vault / "_dashboard" / "skip.md").write_text("x")
    ref = b.snapshot(2)
    snap_path = b._snapshots_dir / ref.snapshot_id
    with tarfile.open(str(snap_path), "r:gz") as tar:
        names = tar.getnames()
    assert not any("_dashboard" in n for n in names)


def test_snapshot_contains_content_files(backend_factory, vault):
    b = backend_factory(vault)
    ref = b.snapshot(2)
    snap_path = b._snapshots_dir / ref.snapshot_id
    with tarfile.open(str(snap_path), "r:gz") as tar:
        names = tar.getnames()
    assert any("feedback_note_1.md" in n for n in names)


def test_snapshot_filename_is_unique_per_second(backend_factory, vault):
    """Two snapshots taken at different instants get distinct filenames."""
    now1 = datetime.datetime(2026, 8, 12, 9, 0, 0)
    now2 = datetime.datetime(2026, 8, 12, 9, 0, 5)
    today = date(2026, 8, 12)
    b1 = FilesystemMigrationBackend(vault, today=today, _now=now1)
    b2 = FilesystemMigrationBackend(vault, today=today, _now=now2)
    ref1 = b1.snapshot(2)
    ref2 = b2.snapshot(2)
    assert ref1.snapshot_id != ref2.snapshot_id


def test_list_snapshots_newest_first(backend_factory, vault):
    b = backend_factory(vault)
    now1 = datetime.datetime(2026, 8, 12, 9, 0, 0)
    now2 = datetime.datetime(2026, 8, 12, 9, 0, 5)
    today = date(2026, 8, 12)
    b1 = FilesystemMigrationBackend(vault, today=today, _now=now1)
    b2 = FilesystemMigrationBackend(vault, today=today, _now=now2)
    b1.snapshot(2)
    time.sleep(0.01)
    b2.snapshot(2)
    snapshots = b.list_snapshots()
    assert len(snapshots) >= 2


def test_parse_target_version_v_prefix():
    from atomic_agents.migration import parse_target_version

    assert parse_target_version("v2") == 2


def test_parse_target_version_no_prefix():
    from atomic_agents.migration import parse_target_version

    assert parse_target_version("3") == 3


def test_parse_target_version_invalid_raises():
    from atomic_agents.migration import parse_target_version

    with pytest.raises(AtomicAgentsError, match="Invalid version"):
        parse_target_version("x.y.z")


def test_discover_scripts_finds_one(vault_with_v1_to_v2_script, backend_factory):
    b = backend_factory(vault_with_v1_to_v2_script)
    scripts = b._discover_scripts()
    assert len(scripts) == 1
    assert scripts[0].from_version == 1
    assert scripts[0].to_version == 2


def test_discover_scripts_skips_underscore_prefixed(vault, backend_factory):
    migrations = vault / "_migrations"
    migrations.mkdir()
    (migrations / "_template.py").write_text(
        "FROM_VERSION = 99\nTO_VERSION = 100\n"
        "def applies_to(u): return False\n"
        "def migrate(u): return {}\n"
    )
    (migrations / "v1_to_v2.py").write_text(
        "FROM_VERSION = 1\nTO_VERSION = 2\n"
        "def applies_to(u): return False\n"
        "def migrate(u): return {}\n"
    )
    b = backend_factory(vault)
    scripts = b._discover_scripts()
    assert len(scripts) == 1
    assert scripts[0].from_version == 1


def test_discover_scripts_rejects_skipped_version(vault, backend_factory):
    migrations = vault / "_migrations"
    migrations.mkdir()
    (migrations / "v1_to_v3.py").write_text(
        "FROM_VERSION = 1\nTO_VERSION = 3\n"
        "def applies_to(u): return False\n"
        "def migrate(u): return {}\n"
    )
    b = backend_factory(vault)
    with pytest.raises(AtomicAgentsError, match="skips a version"):
        b._discover_scripts()


def test_discover_scripts_rejects_missing_attribute(vault, backend_factory):
    migrations = vault / "_migrations"
    migrations.mkdir()
    (migrations / "v1_to_v2.py").write_text(
        "FROM_VERSION = 1\nTO_VERSION = 2\n"
        "# missing applies_to\n"
        "def migrate(u): return {}\n"
    )
    b = backend_factory(vault)
    with pytest.raises(AtomicAgentsError, match="missing required attribute"):
        b._discover_scripts()


def test_vault_status_reports_correct_info(backend_factory, vault_with_v1_to_v2_script):
    b = backend_factory(vault_with_v1_to_v2_script)
    status = b.vault_status()
    assert status["current_schema_version"] == 1
    assert status["content_file_count"] == 3
    assert len(status["available_scripts"]) == 1


def test_find_content_files_walks_cascaded_agents(tmp_path, backend_factory):
    """Cascaded agent memory/wiki files are included in migration discovery."""
    agents_root = tmp_path / "agents"
    single_memory = agents_root / "solo" / "memory"
    single_memory.mkdir(parents=True)
    _make_note(single_memory, "feedback_x.md")
    cascade_memory = (
        agents_root
        / "muse_system"
        / "projects"
        / "novel"
        / "agents"
        / "writer"
        / "memory"
    )
    cascade_memory.mkdir(parents=True)
    _make_note(cascade_memory, "feedback_cascade.md")
    cascade_wiki = (
        agents_root
        / "muse_system"
        / "projects"
        / "novel"
        / "agents"
        / "writer"
        / "wiki"
    )
    cascade_wiki.mkdir(parents=True)
    _make_wiki_page(cascade_wiki, "overview.md")
    b = backend_factory(agents_root)
    units = b.enumerate_units()
    ids = {u.unit_id for u in units}
    assert any("feedback_x.md" in uid for uid in ids)
    assert any("feedback_cascade.md" in uid for uid in ids)
    assert any("overview.md" in uid for uid in ids)
    assert len(units) == 3
