"""Filesystem-specific tests for ``FilesystemMandateBackend``.

Conformance tests in ``test_mandate_protocol_conformance.py`` exercise the
Protocol contract that every backend must satisfy. THIS module exercises
filesystem-specific behavior: on-disk layout discipline, state-file path
conventions, atomic write guarantees, and the URL factory.

The conformance suite already covers Protocol-shaped invariants and invalid
ID refusal across backends — those tests stay there so future backends
inherit them. This module covers what's filesystem-only: directory layout,
``.judge-state/mandates.json`` placement, lazy parent-dir creation, and
URL factory parsing.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from atomic_agents.mandate.filesystem import (
    FilesystemMandateBackend,
    make_filesystem_mandate_backend_from_url,
)
from atomic_agents.mandate import MandateBackend
from atomic_agents.mandate.types import MandateNotFound

# Reuse helpers from the conformance suite
from tests.test_mandate_protocol_conformance import (
    _GOOD_MANDATE_FILE,
    _UNCONSTRAINED_MANDATE_SECTION,
    make_mandate_in_backend,
)


# ──────────────────────────────────────────────────────────────────
# Constructor + path validation


def test_constructor_refuses_empty_scope_root(tmp_path: Path) -> None:
    """Empty-string ``scope_root`` would collapse to CWD — rejected at
    construction to prevent scoping accidents (mirrors the filesystem
    tool-registry backend's empty-root guard)."""
    with pytest.raises(ValueError, match="must not be empty|empty"):
        FilesystemMandateBackend("")


def test_constructor_refuses_dot_scope_root(tmp_path: Path) -> None:
    """``'.'`` collapses to CWD — rejected explicitly. Same shape as the
    tool-registry backend ``test_constructor_refuses_dot_agent_root_string``."""
    with pytest.raises(ValueError, match="CWD|collapses|dot"):
        FilesystemMandateBackend(".")


def test_constructor_refuses_slash_scope_root(tmp_path: Path) -> None:
    """``'/'`` (filesystem root) is too broad a scope for a mandate backend —
    mandate files would overlap with every path on the machine. Rejected."""
    with pytest.raises(ValueError, match="root|filesystem root|/"):
        FilesystemMandateBackend("/")


def test_constructor_does_not_require_existing_dir(tmp_path: Path) -> None:
    """``FilesystemMandateBackend(nonexistent)`` does NOT raise on a missing
    directory — agents without a mandates.md yet are valid (no mandates, not
    an error)."""
    nonexistent = tmp_path / "ghost_agent"
    backend = FilesystemMandateBackend(nonexistent)
    assert backend.list_mandates("agent:ghost") == []


def test_scope_root_property_is_readonly(tmp_path: Path) -> None:
    """``scope_root`` is a read-only property — operators cannot re-scope
    the backend by setting the attribute (mirrors ``agent_root`` in
    ``FilesystemToolRegistryBackend``)."""
    backend = FilesystemMandateBackend(tmp_path)
    assert backend.scope_root == tmp_path
    with pytest.raises(AttributeError):
        backend.scope_root = tmp_path / "other"  # type: ignore[misc]


def test_scope_root_resolves_to_absolute(tmp_path: Path) -> None:
    """``scope_root`` is resolved to an absolute path at construction — downstream
    interpolations and error messages carry the resolved path, not a relative
    fragment."""
    backend = FilesystemMandateBackend(tmp_path)
    assert backend.scope_root.is_absolute()


# ──────────────────────────────────────────────────────────────────
# On-disk layout — mandates.md placement


def test_agent_mandates_md_lives_at_agent_subdirectory(tmp_path: Path) -> None:
    """For scope ``agent:caldwell``, ``mandates.md`` is read from
    ``<scope_root>/caldwell/mandates.md`` — the filesystem backend MUST
    respect the spec/29 §"Where the file lives" per-agent layout."""
    agent_dir = tmp_path / "caldwell"
    agent_dir.mkdir()
    (agent_dir / "mandates.md").write_text(_GOOD_MANDATE_FILE, encoding="utf-8")

    backend = FilesystemMandateBackend(tmp_path)
    refs = backend.list_mandates("agent:caldwell")
    assert len(refs) == 1
    assert refs[0].mandate_id == "procurement-q2-2026"


def test_project_mandates_md_lives_at_scope_root(tmp_path: Path) -> None:
    """For scope ``project:<name>``, ``mandates.md`` is read from
    ``<scope_root>/mandates.md`` — the scope_root IS the project dir
    for project-root mandates (spec/29 §"Where the file lives")."""
    (tmp_path / "mandates.md").write_text(_GOOD_MANDATE_FILE, encoding="utf-8")

    backend = FilesystemMandateBackend(tmp_path)
    refs = backend.list_mandates("project:root")
    assert len(refs) == 1
    assert refs[0].mandate_id == "procurement-q2-2026"


def test_agent_mandates_md_does_not_bleed_into_project_scope(tmp_path: Path) -> None:
    """An agent-scoped mandates.md at ``<scope_root>/<agent>/mandates.md``
    MUST NOT be read when listing ``project:root`` — disk layout discipline
    keeps scopes separate even when both files exist."""
    agent_dir = tmp_path / "some_agent"
    agent_dir.mkdir()
    (agent_dir / "mandates.md").write_text(_GOOD_MANDATE_FILE, encoding="utf-8")

    backend = FilesystemMandateBackend(tmp_path)
    project_refs = backend.list_mandates("project:root")
    assert project_refs == []


def test_missing_mandates_md_is_empty_not_error(tmp_path: Path) -> None:
    """When no ``mandates.md`` exists for the scope, ``list_mandates`` returns
    ``[]`` and ``load_mandate`` raises ``MandateNotFound`` — filesystem layout
    absence is a valid, non-exceptional state."""
    backend = FilesystemMandateBackend(tmp_path)
    assert backend.list_mandates("agent:nobody") == []
    with pytest.raises(MandateNotFound):
        backend.load_mandate("procurement-q2-2026", "agent:nobody")


# ──────────────────────────────────────────────────────────────────
# .judge-state/mandates.json placement


def test_agent_state_file_lives_at_agent_judge_state(tmp_path: Path) -> None:
    """For scope ``agent:caldwell``, the state file MUST be written to
    ``<scope_root>/caldwell/.judge-state/mandates.json`` — the filesystem
    backend MUST respect the spec/29 §"Lifecycle event deduplication"
    per-agent path discipline."""
    backend = FilesystemMandateBackend(tmp_path)
    state = {
        "schema_version": 1,
        "scope": "agent:caldwell",
        "mandates": {},
    }
    backend.write_state("agent:caldwell", state)

    expected_path = tmp_path / "caldwell" / ".judge-state" / "mandates.json"
    assert expected_path.exists(), (
        f"State file not found at expected agent path: {expected_path}"
    )


def test_project_state_file_lives_at_scope_root_judge_state(tmp_path: Path) -> None:
    """For scope ``project:root``, the state file MUST be written to
    ``<scope_root>/.judge-state/mandates.json`` — the filesystem backend
    MUST respect the spec/29 §"Lifecycle event deduplication" project-root
    path discipline."""
    backend = FilesystemMandateBackend(tmp_path)
    state = {
        "schema_version": 1,
        "scope": "project:root",
        "mandates": {},
    }
    backend.write_state("project:root", state)

    expected_path = tmp_path / ".judge-state" / "mandates.json"
    assert expected_path.exists(), (
        f"State file not found at expected project path: {expected_path}"
    )


def test_write_state_creates_parent_dirs_lazily(tmp_path: Path) -> None:
    """``write_state`` creates ``.judge-state/`` if it doesn't exist —
    atomic write discipline includes lazy parent-dir creation so agents
    start up cleanly even on a fresh vault (mirrors ``_io.atomic_write``
    parent-dir behavior throughout the framework)."""
    backend = FilesystemMandateBackend(tmp_path)
    state_dir = tmp_path / "new_agent" / ".judge-state"
    # Confirm dir does not yet exist
    assert not state_dir.exists()

    backend.write_state("agent:new_agent", {
        "schema_version": 1,
        "scope": "agent:new_agent",
        "mandates": {},
    })

    assert state_dir.exists()
    assert (state_dir / "mandates.json").exists()


# ──────────────────────────────────────────────────────────────────
# Source hash — sha256 content fidelity


def test_source_hash_is_sha256_prefixed(tmp_path: Path) -> None:
    """``source_hash`` starts with ``sha256:`` — the filesystem backend
    MUST use the canonical ``sha256:<hex>`` form matching the spec/29
    dataclass description and the JSONL audit event fields."""
    backend = FilesystemMandateBackend(tmp_path)
    make_mandate_in_backend(backend, tmp_path, "agent:test")
    m = backend.load_mandate("procurement-q2-2026", "agent:test")
    assert m.source_hash.startswith("sha256:")


def test_source_hash_is_stable_for_unchanged_file(tmp_path: Path) -> None:
    """Two ``load_mandate`` calls against the same unchanged file produce
    the same ``source_hash`` — the hash is content-derived, not time-derived
    or random."""
    backend = FilesystemMandateBackend(tmp_path)
    make_mandate_in_backend(backend, tmp_path, "agent:test")
    m1 = backend.load_mandate("procurement-q2-2026", "agent:test")
    m2 = backend.load_mandate("procurement-q2-2026", "agent:test")
    assert m1.source_hash == m2.source_hash


# ──────────────────────────────────────────────────────────────────
# source_path — descriptor path for audit


def test_source_path_is_descriptor_file_path(tmp_path: Path) -> None:
    """``Mandate.source_path`` MUST surface the absolute path to
    ``mandates.md`` — diagnostic origin marker for audit consumers
    (mirrors ``ToolRef.source`` filesystem-path discipline)."""
    backend = FilesystemMandateBackend(tmp_path)
    agent_dir = tmp_path / "test"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "mandates.md").write_text(_GOOD_MANDATE_FILE, encoding="utf-8")

    m = backend.load_mandate("procurement-q2-2026", "agent:test")
    expected = str(agent_dir / "mandates.md")
    assert m.source_path == expected


# ──────────────────────────────────────────────────────────────────
# isinstance check


def test_isinstance_protocol_check(tmp_path: Path) -> None:
    """``FilesystemMandateBackend`` satisfies the ``MandateBackend`` Protocol
    at runtime — both the Protocol and the reference impl land in PR 1."""
    backend = FilesystemMandateBackend(tmp_path)
    assert isinstance(backend, MandateBackend)


# ──────────────────────────────────────────────────────────────────
# URL factory


def test_url_factory_parses_filesystem_url(tmp_path: Path) -> None:
    """``make_filesystem_mandate_backend_from_url("filesystem:///path")``
    returns a ``FilesystemMandateBackend`` rooted at ``/path`` — URL factory
    mirrors the tool-registry + profile-backend URL factory patterns."""
    url = f"filesystem://{tmp_path}"
    backend = make_filesystem_mandate_backend_from_url(url)
    assert isinstance(backend, FilesystemMandateBackend)
    assert backend.scope_root == tmp_path


def test_url_factory_refuses_non_filesystem_scheme(tmp_path: Path) -> None:
    """``make_filesystem_mandate_backend_from_url`` refuses non-``filesystem:``
    schemes — operator misconfiguration is loud."""
    with pytest.raises(ValueError, match="scheme|filesystem"):
        make_filesystem_mandate_backend_from_url("sqlite:///path/to/db")


def test_url_factory_refuses_empty_path(tmp_path: Path) -> None:
    """``make_filesystem_mandate_backend_from_url("filesystem://")`` with no
    path raises ``ValueError`` — cannot infer scope_root from an empty URL
    path component."""
    with pytest.raises(ValueError, match="path|empty"):
        make_filesystem_mandate_backend_from_url("filesystem://")
