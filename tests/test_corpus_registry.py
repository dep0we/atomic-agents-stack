"""Registry edge-case tests for atomic_agents.corpus (spec/34, issue #65).

These tests exercise the process-local backend registry in
``atomic_agents/corpus/__init__.py`` directly -- they are NOT parametrized
across backends because registry behavior is backend-agnostic (the registry
maps string ids to classes, independent of the class under test).

Kept separate from ``test_corpus_protocol_conformance.py`` for the same
reason that ``test_persona_registry.py`` (if it existed) would be kept
separate from the persona conformance suite: registry tests verify the
lookup/registration plumbing, not the Protocol contract.

Gap 2 from the Step-7 coverage audit:
    - unregister_corpus_backend("filesystem") then re-register
    - get_corpus_backend("unknown_id") raises CorpusBackendNotRegistered
    - list_corpus_backends() returns sorted list including "filesystem"
    - get_default_corpus_backend with ATOMIC_AGENTS_CORPUS_BACKEND=unknown_id
      raises CorpusBackendNotRegistered
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from atomic_agents.corpus import (
    FilesystemCorpusBackend,
    get_corpus_backend,
    get_default_corpus_backend,
    list_corpus_backends,
    register_corpus_backend,
    unregister_corpus_backend,
)
from atomic_agents.exceptions import CorpusBackendNotRegistered


# ──────────────────────────────────────────────────────────────────────────────
# Helpers


class _StubCorpusBackendA:
    """Minimal stub used to test re-registration without touching the live filesystem."""


class _StubCorpusBackendB:
    """Second minimal stub for multi-registration scenarios."""


# ──────────────────────────────────────────────────────────────────────────────
# Tests


def test_unregister_then_reregister_corpus_backend() -> None:
    """unregister_corpus_backend removes an id; register_corpus_backend restores it.

    Sequence:
      1. Register a stub under a test-only id so the real registry is not mutated.
      2. Confirm it is retrievable.
      3. Unregister it.
      4. Confirm get_corpus_backend now raises CorpusBackendNotRegistered.
      5. Re-register the same id with a different class.
      6. Confirm the new class is returned.

    Uses a test-only backend_id ("_test_stub_a") so the "filesystem" default
    registration is never disturbed.  Cleans up after itself regardless of test
    outcome.
    """
    test_id = "_test_stub_a"
    # Ensure clean slate going in (in case a prior failed run left residue)
    unregister_corpus_backend(test_id)

    try:
        # Step 1: register stub A
        register_corpus_backend(test_id, _StubCorpusBackendA)
        assert get_corpus_backend(test_id) is _StubCorpusBackendA

        # Step 2: unregister
        unregister_corpus_backend(test_id)

        # Step 3: confirm absence
        with pytest.raises(CorpusBackendNotRegistered):
            get_corpus_backend(test_id)

        # Step 4: re-register with stub B
        register_corpus_backend(test_id, _StubCorpusBackendB)
        assert get_corpus_backend(test_id) is _StubCorpusBackendB

    finally:
        # Always clean up so other tests are not affected
        unregister_corpus_backend(test_id)


def test_get_corpus_backend_unknown_raises_corpus_backend_not_registered() -> None:
    """get_corpus_backend raises CorpusBackendNotRegistered for an unknown id.

    Uses a backend_id that is highly unlikely to be registered ("__no_such_id__")
    and asserts the exact exception type -- not just any AtomicAgentsError --
    so callers can branch on CorpusBackendNotRegistered specifically.
    """
    unknown_id = "__no_such_corpus_backend_id__"
    # Belt-and-suspenders: ensure it is definitely absent
    unregister_corpus_backend(unknown_id)

    with pytest.raises(CorpusBackendNotRegistered):
        get_corpus_backend(unknown_id)


def test_list_corpus_backends_returns_sorted_list_with_filesystem() -> None:
    """list_corpus_backends() returns a sorted list that includes "filesystem".

    The "filesystem" backend is registered at module import time (bottom of
    corpus/__init__.py).  This test verifies:
    - "filesystem" is present in the returned list.
    - The returned list is in lexicographic (sorted) order.
    - The return type is a plain list of strings.

    If additional test-only backends are registered by other tests running in
    the same process, they appear in sorted order too -- the assertion only
    requires "filesystem" to be present and the list to be sorted; it does not
    assert an exact element count.
    """
    ids = list_corpus_backends()

    assert isinstance(ids, list)
    assert "filesystem" in ids
    # Assert sorted order
    assert ids == sorted(ids), (
        f"list_corpus_backends() returned an unsorted list: {ids}"
    )


def test_get_default_corpus_backend_unknown_id_raises() -> None:
    """get_default_corpus_backend raises CorpusBackendNotRegistered for an unknown env value.

    Patches ATOMIC_AGENTS_CORPUS_BACKEND to an id that is not registered and
    asserts the factory raises CorpusBackendNotRegistered rather than silently
    falling back or returning None.

    Uses a tmp_path-like Path so the call does not create any real directories;
    the exception fires before any filesystem I/O.
    """
    fake_root = Path("/tmp/__corpus_registry_test_agent_root__")

    with patch.dict(os.environ, {"ATOMIC_AGENTS_CORPUS_BACKEND": "not_a_real_backend"}):
        with pytest.raises(CorpusBackendNotRegistered):
            get_default_corpus_backend(fake_root)
