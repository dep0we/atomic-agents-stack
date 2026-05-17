"""Conformance test suite for the MandateBackend Protocol (spec/29).

Parametrized over a ``backend_factory`` fixture. Each registered
backend that ships in core (``FilesystemMandateBackend`` in PR 1;
future SaaS / mobile backends) is exercised against the same contract.
A third-party backend in a downstream package imports this test
module's ``BACKEND_FACTORIES`` parametrization to verify its own
conformance.

What this suite asserts (~45 tests in PR 1 — capability-gated tests
add skips when a backend declares a capability False):

1. Protocol surface — ``isinstance(backend, MandateBackend)`` passes.
2. ``backend_id`` is a stable non-empty string.
3. ``capabilities()`` returns a ``MandateCapabilities`` instance with bool fields.
4. ``list_mandates`` returns ``[]`` when no mandates.md is present.
5. ``list_mandates`` returns populated mandates when mandates.md is present.
6. ``list_mandates`` returns ``Mandate`` instances.
7. ``list_mandates`` includes revoked mandates (revocation_state visible).
8. ``list_mandates`` includes expired mandates (derived from expires_at).
9. ``list_mandates`` scope separation — agent mandates don't appear in project scope.
10. ``load_mandate`` returns a ``Mandate`` instance.
11. ``load_mandate`` raises ``MandateNotFound`` for a missing mandate id.
12. ``load_mandate`` refuses path-traversal and invalid IDs at the API boundary.
13. ``load_mandate`` recomputes ``source_hash`` when the source changes (MUST #4).
14. ``read_state`` returns canonical empty state when no state file exists (MUST #3).
15. ``write_state`` + ``read_state`` round-trips exactly.
16. ``read_state`` raises ``MandateStateSchemaUnsupported`` for unknown schema_version.
17. Concurrent ``write_state`` calls never corrupt state.
18. Revocation observable — flipping ``revocation_state`` is visible on next load (MUST #8).
19. Per-scope isolation — agent mandates don't bleed into project scope (MUST #2).
20. Registry resolves "filesystem" to ``FilesystemMandateBackend``.
21. ``get_default_mandate_backend`` respects env var override.
22. ``list_mandates`` is lexicographic by mandate_id.
23. ``load_mandate`` round-trips mandate_id, scope, granted_by, constraints.
24. Mandate with no structured constraints raises ``MandateInvalid`` at load.
25. Mandate with ``unconstrained: true`` loads without error.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import pytest

from atomic_agents.mandate import (
    Mandate,
    MandateBackend,
    MandateCapabilities,
    MandateConstraints,
    MandateStateSchemaUnsupported,
    RevocationState,
    get_default_mandate_backend,
    get_mandate_backend,
    list_mandate_backends,
    register_mandate_backend,
)
from atomic_agents.mandate.types import MandateInvalid, MandateNotFound
from atomic_agents.mandate.filesystem import FilesystemMandateBackend


# ──────────────────────────────────────────────────────────────────
# Backend factory parametrization — every conformance test runs once
# per registered backend. PR 1 ships only the filesystem factory;
# future SaaS / mobile backends extend BACKEND_FACTORIES the same way
# the ToolRegistry + LogBackend + AgentProfile arcs did.

BackendFactory = Callable[[Path], MandateBackend]


def _filesystem_factory(scope_root: Path) -> MandateBackend:
    """Filesystem backend rooted at ``scope_root``.

    Per-scope instance — see spec/29 §"Per-agent vs project-root
    resolution". ``scope_root`` plays the role of both the agent dir
    and the tmp_path fixture root; scoped subsections are addressable
    via the ``scope`` argument on the per-method calls.
    """
    return FilesystemMandateBackend(scope_root)


BACKEND_FACTORIES: list[tuple[str, BackendFactory]] = [
    ("filesystem", _filesystem_factory),
]


@pytest.fixture(params=BACKEND_FACTORIES, ids=lambda p: p[0])
def backend_factory(request) -> BackendFactory:
    """Yields a callable ``(scope_root: Path) -> MandateBackend``."""
    return request.param[1]


@pytest.fixture
def backend(backend_factory, tmp_path) -> MandateBackend:
    """A backend rooted at a per-test tmp_path."""
    return backend_factory(tmp_path)


# ──────────────────────────────────────────────────────────────────
# Helpers: well-formed mandate file fixtures


_GOOD_MANDATE_FILE = """\
## procurement-q2-2026
granted_by: operator
granted_at: 2026-04-01T00:00:00Z
expires_at: 2026-06-30T23:59:59Z
revocable_by: operator
scope: |
  Purchase SaaS subscriptions on the approved-vendor list.
  Individual subscriptions ≤ $200/month.
constraints:
  daily_external_usd: 200
  monthly_external_usd: 2000
  cumulative_external_usd: 6000
  allowed_tools:
    - stripe.subscribe
    - vendor_lookup
revocation_state: active
revoked_at: null
revocation_reason: null
"""

_REVOKED_MANDATE_SECTION = """\
## emergency-deploy-revoked
granted_by: operator:dan
granted_at: 2026-05-01T00:00:00Z
expires_at: 2026-05-02T06:00:00Z
revocable_by: operator
scope: |
  Emergency deploy — one deploy to production allowed.
constraints:
  allowed_tools:
    - deploy.production
revocation_state: revoked
revoked_at: 2026-05-01T12:00:00Z
revocation_reason: Issue resolved; no longer needed.
"""

_EXPIRED_MANDATE_SECTION = """\
## old-trial-license
granted_by: operator
granted_at: 2020-01-01T00:00:00Z
expires_at: 2020-02-01T00:00:00Z
revocable_by: operator
scope: |
  Trial license for legacy-tool integration.
constraints:
  allowed_tools:
    - legacy_tool
revocation_state: active
revoked_at: null
revocation_reason: null
"""

_UNCONSTRAINED_MANDATE_SECTION = """\
## trust-the-prose
granted_by: operator
granted_at: 2026-01-01T00:00:00Z
expires_at: null
revocable_by: operator
scope: |
  Broad operational authority; operator trusts the agent's judgment.
constraints:
  unconstrained: true
  unconstrained_justification: "Single-operator deployment; operator is always present."
revocation_state: active
revoked_at: null
revocation_reason: null
"""

_GOOD_META_BLOCK = """\
## _meta
per_agent_mandate_policy: open
"""


def _write_mandates_file(
    scope_root: Path,
    scope: str,
    content: str,
    *,
    is_project_root: bool = False,
) -> Path:
    """Write a ``mandates.md`` file into the canonical location for the scope.

    For agent scopes (``agent:<name>``), the file lives at
    ``<scope_root>/<name>/mandates.md``.  For project-root scopes
    (``project:<name>``), the file lives at
    ``<scope_root>/mandates.md`` (the scope_root IS the project dir).
    This mirrors the filesystem layout defined in spec/29
    §"Where the file lives".
    """
    if is_project_root or scope.startswith("project:"):
        mandate_path = scope_root / "mandates.md"
    else:
        # agent:<name>
        agent_name = scope.split(":", 1)[1] if ":" in scope else scope
        agent_dir = scope_root / agent_name
        agent_dir.mkdir(parents=True, exist_ok=True)
        mandate_path = agent_dir / "mandates.md"
    mandate_path.write_text(content, encoding="utf-8")
    return mandate_path


def make_mandate_in_backend(
    backend: MandateBackend,
    tmp_path: Path,
    scope: str,
    *,
    content: str | None = None,
    is_project_root: bool = False,
) -> Path:
    """Write a mandates.md for ``scope`` and return its path.

    Defaults to the well-formed single-mandate file (``procurement-q2-2026``).
    Mirrors ``make_tool_in_backend`` from the registry conformance suite —
    all backends in PR 1 are filesystem-shape, so we write the file
    directly into the backend's directory.
    """
    body = content if content is not None else _GOOD_MANDATE_FILE
    return _write_mandates_file(tmp_path, scope, body, is_project_root=is_project_root)


def make_meta_file(
    tmp_path: Path,
    meta_content: str | None = None,
    mandate_content: str | None = None,
) -> Path:
    """Write a project-root mandates.md with a ``_meta`` section."""
    meta = meta_content if meta_content is not None else _GOOD_META_BLOCK
    body = meta + "\n" + (mandate_content or _GOOD_MANDATE_FILE)
    return _write_mandates_file(tmp_path, "project:root", body, is_project_root=True)


# ──────────────────────────────────────────────────────────────────
# Surface conformance


def test_protocol_isinstance(backend: MandateBackend) -> None:
    """isinstance check passes — backend exposes the full Protocol."""
    assert isinstance(backend, MandateBackend)


def test_backend_id_is_stable_nonempty_string(backend: MandateBackend) -> None:
    """``backend_id`` is a non-empty string stable across reads (MUST #?
    mirrors spec/25 MUST #1 backend surface; see also profile + log arcs)."""
    backend_id = backend.backend_id
    assert isinstance(backend_id, str)
    assert backend_id != ""
    # Stable across reads
    assert backend.backend_id == backend_id


def test_capabilities_returns_mandate_capabilities(backend: MandateBackend) -> None:
    """``capabilities()`` returns a ``MandateCapabilities`` instance with
    bool fields — capability honesty is MUST #8 per spec/29."""
    caps = backend.capabilities()
    assert isinstance(caps, MandateCapabilities)
    assert isinstance(caps.supports_revocation, bool)
    assert isinstance(caps.supports_external_state_change_notification, bool)
    assert isinstance(caps.durable, bool)


# ──────────────────────────────────────────────────────────────────
# list_mandates


def test_list_mandates_empty_when_no_mandates_file(backend: MandateBackend) -> None:
    """No ``mandates.md`` → ``[]``. Preserves byte-identical agent
    construction for fixtures without a mandate catalog (spec/29 §"Backward
    compatibility")."""
    refs = backend.list_mandates("agent:test")
    assert refs == []


def test_list_mandates_populated_returns_all(
    backend: MandateBackend, tmp_path: Path
) -> None:
    """When ``mandates.md`` contains one section, ``list_mandates`` returns
    exactly one entry — basic catalog-population check."""
    make_mandate_in_backend(backend, tmp_path, "agent:test")
    refs = backend.list_mandates("agent:test")
    assert len(refs) == 1


def test_list_mandates_returns_mandate_instances(
    backend: MandateBackend, tmp_path: Path
) -> None:
    """``list_mandates`` returns ``Mandate`` dataclass instances (not dicts,
    not strings, not wrapper objects)."""
    make_mandate_in_backend(backend, tmp_path, "agent:test")
    refs = backend.list_mandates("agent:test")
    assert all(isinstance(r, Mandate) for r in refs)


def test_list_mandates_includes_revoked(
    backend: MandateBackend, tmp_path: Path
) -> None:
    """Revoked mandates appear in ``list_mandates`` — the caller decides
    whether to filter them. spec/29 §"Validation steps" checks
    ``revocation_state`` at judgment time, not at listing time."""
    content = _GOOD_MANDATE_FILE + "\n" + _REVOKED_MANDATE_SECTION
    make_mandate_in_backend(backend, tmp_path, "agent:test", content=content)
    refs = backend.list_mandates("agent:test")
    states = {r.revocation_state for r in refs}
    assert RevocationState.REVOKED in states


def test_list_mandates_includes_expired(
    backend: MandateBackend, tmp_path: Path
) -> None:
    """Expired mandates (derived from past ``expires_at``) appear in
    ``list_mandates`` with ``revocation_state == EXPIRED`` — the backend
    computes expiry as derived state at load time per spec/29 §"Lifecycle
    event deduplication"."""
    content = _GOOD_MANDATE_FILE + "\n" + _EXPIRED_MANDATE_SECTION
    make_mandate_in_backend(backend, tmp_path, "agent:test", content=content)
    refs = backend.list_mandates("agent:test")
    states = {r.revocation_state for r in refs}
    assert RevocationState.EXPIRED in states


def test_list_mandates_lexicographic_by_id(
    backend: MandateBackend, tmp_path: Path
) -> None:
    """``list_mandates`` returns mandates in lexicographic order by
    ``mandate_id`` — predictable ordering for callers that display catalogs
    and for deterministic test assertions."""
    multi = """\
## zzz-last
granted_by: operator
granted_at: 2026-01-01T00:00:00Z
expires_at: null
revocable_by: operator
scope: Last alphabetically.
constraints:
  allowed_tools:
    - tool_z
revocation_state: active
revoked_at: null
revocation_reason: null

## aaa-first
granted_by: operator
granted_at: 2026-01-01T00:00:00Z
expires_at: null
revocable_by: operator
scope: First alphabetically.
constraints:
  allowed_tools:
    - tool_a
revocation_state: active
revoked_at: null
revocation_reason: null

## mmm-middle
granted_by: operator
granted_at: 2026-01-01T00:00:00Z
expires_at: null
revocable_by: operator
scope: Middle alphabetically.
constraints:
  allowed_tools:
    - tool_m
revocation_state: active
revoked_at: null
revocation_reason: null
"""
    make_mandate_in_backend(backend, tmp_path, "agent:test", content=multi)
    refs = backend.list_mandates("agent:test")
    ids = [r.mandate_id for r in refs]
    assert ids == sorted(ids)
    assert ids == ["aaa-first", "mmm-middle", "zzz-last"]


def test_list_mandates_scope_separation_agent_vs_project(
    backend: MandateBackend, tmp_path: Path
) -> None:
    """Mandates written for ``agent:foo`` MUST NOT appear in
    ``list_mandates("project:root")`` — per-scope isolation at the list level
    (spec/29 MUST #2 mirrors spec/25 §"cross-scope isolation")."""
    make_mandate_in_backend(backend, tmp_path, "agent:foo")
    project_refs = backend.list_mandates("project:root")
    assert project_refs == []


# ──────────────────────────────────────────────────────────────────
# load_mandate


def test_load_mandate_returns_mandate_instance(
    backend: MandateBackend, tmp_path: Path
) -> None:
    """``load_mandate`` returns a ``Mandate`` instance with all required
    fields populated."""
    make_mandate_in_backend(backend, tmp_path, "agent:test")
    m = backend.load_mandate("procurement-q2-2026", "agent:test")
    assert isinstance(m, Mandate)
    assert m.mandate_id == "procurement-q2-2026"
    assert m.scope == "agent:test"


def test_load_mandate_round_trips_granted_by(
    backend: MandateBackend, tmp_path: Path
) -> None:
    """``granted_by`` parsed from the file surfaces unchanged on the
    returned ``Mandate`` — round-trip fidelity on a required field."""
    make_mandate_in_backend(backend, tmp_path, "agent:test")
    m = backend.load_mandate("procurement-q2-2026", "agent:test")
    assert m.granted_by == "operator"


def test_load_mandate_round_trips_revocation_state(
    backend: MandateBackend, tmp_path: Path
) -> None:
    """``revocation_state: active`` in the file surfaces as
    ``RevocationState.ACTIVE`` on the returned ``Mandate``."""
    make_mandate_in_backend(backend, tmp_path, "agent:test")
    m = backend.load_mandate("procurement-q2-2026", "agent:test")
    assert m.revocation_state == RevocationState.ACTIVE


def test_load_mandate_round_trips_constraints_allowed_tools(
    backend: MandateBackend, tmp_path: Path
) -> None:
    """``constraints.allowed_tools`` round-trips — the frozenset preserves
    tool names exactly as written in ``mandates.md``."""
    make_mandate_in_backend(backend, tmp_path, "agent:test")
    m = backend.load_mandate("procurement-q2-2026", "agent:test")
    assert "stripe.subscribe" in m.constraints.allowed_tools
    assert "vendor_lookup" in m.constraints.allowed_tools


def test_load_mandate_missing_raises_mandate_not_found(
    backend: MandateBackend,
) -> None:
    """``load_mandate`` raises ``MandateNotFound`` (not ``KeyError``,
    not ``FileNotFoundError``) when the mandate ID does not exist in the
    scope — callers can catch the specific exception class."""
    with pytest.raises(MandateNotFound):
        backend.load_mandate("does-not-exist", "agent:test")


@pytest.mark.parametrize(
    "bad_id",
    [
        "../escape",
        "foo/bar",
        "foo\\bar",
        "..",
        "",
        "FOO",          # uppercase not in [a-z0-9][a-z0-9-]*
        "foo!",         # disallowed character
        "foo bar",      # space
        "a" * 65,       # exceeds 64-char max per spec/29 parser rules
        "-starts-with-hyphen",  # must start with alphanumeric
    ],
)
def test_load_mandate_refuses_path_traversal_and_invalid_id(
    backend: MandateBackend, bad_id: str
) -> None:
    """``load_mandate`` MUST validate ``mandate_id`` against path-traversal
    patterns AND the spec/29 ID grammar (``[a-z0-9][a-z0-9-]*``, max 64
    chars) BEFORE any disk / DB access (spec/29 MUST #1 — path-traversal
    refusal at API boundary).

    Accepts ``MandateInvalid`` or ``ValueError`` — both signal the caller
    that the ID itself is malformed, not that the mandate is absent. Future
    backends should settle on ``ValueError`` for API-boundary refusals and
    ``MandateNotFound`` for absent-but-valid IDs; both are acceptable here
    to allow the early PR 1 implementation latitude."""
    with pytest.raises((MandateInvalid, ValueError)):
        backend.load_mandate(bad_id, "agent:test")


def test_load_mandate_populated_source_hash(
    backend: MandateBackend, tmp_path: Path
) -> None:
    """``source_hash`` is non-empty after a successful load — the backend
    MUST compute a canonical hash of the mandate's source bytes per spec/29
    §"The `Mandate` dataclass" (``source_hash`` field)."""
    make_mandate_in_backend(backend, tmp_path, "agent:test")
    m = backend.load_mandate("procurement-q2-2026", "agent:test")
    assert isinstance(m.source_hash, str)
    assert m.source_hash != ""


def test_load_mandate_recomputes_source_hash_on_change(
    backend: MandateBackend, tmp_path: Path
) -> None:
    """When the source file changes between two ``load_mandate`` calls,
    ``source_hash`` MUST differ (spec/29 MUST #4 — hash reflects current
    bytes, not cached bytes).

    This is the load-bearing defense for ``MandateCheck`` step 2 (source
    hash check): a mandate whose hash has changed since the cite was bound
    triggers re-validation.
    """
    path = make_mandate_in_backend(backend, tmp_path, "agent:test")
    m1 = backend.load_mandate("procurement-q2-2026", "agent:test")

    # Append a comment to the section — same structure, different bytes.
    original = path.read_text(encoding="utf-8")
    path.write_text(original + "\n# operator annotation\n", encoding="utf-8")

    m2 = backend.load_mandate("procurement-q2-2026", "agent:test")
    assert m1.source_hash != m2.source_hash


# ──────────────────────────────────────────────────────────────────
# Unconstrained escape hatch


def test_load_mandate_unconstrained_loads_without_error(
    backend: MandateBackend, tmp_path: Path
) -> None:
    """A mandate with ``constraints.unconstrained: true`` and a justification
    loads without raising ``MandateInvalid`` — this is the explicit opt-out
    from structured-constraint enforcement per spec/29 §"Constraint
    enforceability"."""
    make_mandate_in_backend(
        backend, tmp_path, "agent:test", content=_UNCONSTRAINED_MANDATE_SECTION
    )
    m = backend.load_mandate("trust-the-prose", "agent:test")
    assert m.constraints.unconstrained is True


def test_load_mandate_no_constraints_no_unconstrained_raises(
    backend: MandateBackend, tmp_path: Path
) -> None:
    """A mandate with no structured constraints AND no ``unconstrained: true``
    MUST raise ``MandateInvalid`` at load time — spec/29 §"Constraint
    enforceability (refused at load time)"."""
    bare = """\
## bare-mandate
granted_by: operator
granted_at: 2026-01-01T00:00:00Z
expires_at: null
revocable_by: operator
scope: |
  Bare mandate with no enforcement fields.
constraints: {}
revocation_state: active
revoked_at: null
revocation_reason: null
"""
    make_mandate_in_backend(backend, tmp_path, "agent:test", content=bare)
    with pytest.raises(MandateInvalid):
        backend.load_mandate("bare-mandate", "agent:test")


# ──────────────────────────────────────────────────────────────────
# State management (MUST #3)


def test_read_state_returns_empty_when_missing(backend: MandateBackend) -> None:
    """``read_state`` returns the canonical empty state structure when no
    state file exists — callers MUST get a usable dict, not ``None``, not
    ``FileNotFoundError`` (spec/29 MUST #3)."""
    state = backend.read_state("agent:test")
    assert isinstance(state, dict)
    assert state["schema_version"] == 1
    assert state["scope"] == "agent:test"
    assert state["mandates"] == {}


def test_read_state_empty_project_scope(backend: MandateBackend) -> None:
    """``read_state`` on a project scope that has no state file returns the
    canonical empty structure with the correct ``scope`` value."""
    state = backend.read_state("project:root")
    assert state["scope"] == "project:root"
    assert state["mandates"] == {}


def test_write_state_then_read_round_trips(backend: MandateBackend) -> None:
    """``write_state`` followed by ``read_state`` round-trips exactly —
    every field present in the written state reappears unchanged (spec/29
    MUST #3 — idempotent schema init)."""
    written = {
        "schema_version": 1,
        "scope": "agent:test",
        "mandates": {
            "procurement-q2-2026": {
                "last_seen_state": "active",
                "last_seen_revoked_at": None,
                "last_seen_expired_at": None,
                "last_seen_source_hash": "sha256:abc123",
            }
        },
    }
    backend.write_state("agent:test", written)
    read_back = backend.read_state("agent:test")
    assert read_back == written


def test_write_state_overwrites_existing(backend: MandateBackend) -> None:
    """A second ``write_state`` overwrites the first — ``write_state`` is
    not accumulative; it replaces the entire scope state."""
    first = {
        "schema_version": 1,
        "scope": "agent:test",
        "mandates": {"old-mandate": {"last_seen_state": "active"}},
    }
    second = {
        "schema_version": 1,
        "scope": "agent:test",
        "mandates": {"new-mandate": {"last_seen_state": "revoked"}},
    }
    backend.write_state("agent:test", first)
    backend.write_state("agent:test", second)
    assert backend.read_state("agent:test") == second


def test_read_state_unknown_schema_version_raises(
    backend: MandateBackend,
) -> None:
    """When a state file carries an unknown ``schema_version``, ``read_state``
    MUST raise ``MandateStateSchemaUnsupported`` — callers MUST consult the
    field and raise rather than silently migrate (spec/29
    §"Lifecycle event deduplication")."""
    future_state = {
        "schema_version": 999,
        "scope": "agent:test",
        "mandates": {},
    }
    backend.write_state("agent:test", future_state)
    with pytest.raises(MandateStateSchemaUnsupported):
        backend.read_state("agent:test")


def test_state_scope_isolation_agent_vs_project(backend: MandateBackend) -> None:
    """State written for ``agent:foo`` MUST NOT appear when reading
    ``project:root`` — state is scoped per spec/29 §"Lifecycle event
    deduplication" (state files are at distinct paths)."""
    agent_state = {
        "schema_version": 1,
        "scope": "agent:foo",
        "mandates": {"m1": {"last_seen_state": "active"}},
    }
    backend.write_state("agent:foo", agent_state)
    project_state = backend.read_state("project:root")
    assert project_state["mandates"] == {}


def test_state_atomic_under_concurrent_writes(backend: MandateBackend) -> None:
    """Two threads writing state concurrently produce final state that
    matches exactly one of the writes — never a torn / interleaved
    combination (spec/29 §"Atomic + idempotent everywhere"; the filesystem
    backend MUST use ``_io.atomic_write`` per MUST #3)."""
    state_a = {
        "schema_version": 1,
        "scope": "agent:concurrent",
        "mandates": {"a": {"last_seen_state": "active"}},
    }
    state_b = {
        "schema_version": 1,
        "scope": "agent:concurrent",
        "mandates": {"b": {"last_seen_state": "revoked"}},
    }
    results: list[Exception] = []

    def _write(state: dict) -> None:
        try:
            backend.write_state("agent:concurrent", state)
        except Exception as exc:
            results.append(exc)

    t1 = threading.Thread(target=_write, args=(state_a,))
    t2 = threading.Thread(target=_write, args=(state_b,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert results == [], f"Concurrent write raised: {results}"
    final = backend.read_state("agent:concurrent")
    # Final state matches exactly one of the two writes — not corrupted.
    assert final in (state_a, state_b), (
        f"Concurrent write produced unexpected state: {final!r}"
    )


# ──────────────────────────────────────────────────────────────────
# Capability honesty (MUST #8)


def test_capability_revocation_observable(
    backend: MandateBackend, tmp_path: Path
) -> None:
    """Capability claim-vs-behavior: when ``supports_revocation=True``,
    flipping ``revocation_state: revoked`` in ``mandates.md`` MUST be
    observable on the next ``load_mandate`` call (spec/29 MUST #8 — capability
    honesty is the load-bearing invariant; a backend that caches and silently
    serves stale revocation state is dangerous)."""
    caps = backend.capabilities()
    if not caps.supports_revocation:
        pytest.skip(
            "backend declares supports_revocation=False — revocation observation "
            "is not a conformance requirement for this backend"
        )
    path = make_mandate_in_backend(backend, tmp_path, "agent:test")
    m1 = backend.load_mandate("procurement-q2-2026", "agent:test")
    assert m1.revocation_state == RevocationState.ACTIVE

    # Operator revokes the mandate by editing mandates.md.
    revoked_content = path.read_text(encoding="utf-8").replace(
        "revocation_state: active", "revocation_state: revoked"
    )
    path.write_text(revoked_content, encoding="utf-8")

    m2 = backend.load_mandate("procurement-q2-2026", "agent:test")
    assert m2.revocation_state == RevocationState.REVOKED


def test_capability_external_notification_claim_vs_behavior(
    backend: MandateBackend,
) -> None:
    """``supports_external_state_change_notification`` declared via
    ``capabilities()`` — PR 1 reference backends all ship ``False``
    (push-notification is a reserved future capability per spec/29
    §"MandateCapabilities"). This test pins that the capability declaration
    IS a bool (not None / missing) and that PR 1 backends declare False.

    TODO: when a future backend declares True, extend this test to assert
    that a ``subscribe_to_changes(scope, callback)`` method exists and does
    NOT raise ``NotImplementedError`` — mirrors the capability-parity pattern
    from the tool-registry + log-backend arcs. The method signature is not
    yet pinned in the Protocol; add it when the first True-declaring backend
    ships.
    """
    caps = backend.capabilities()
    # All PR-1-era backends ship False; future push-notification backends set True.
    assert isinstance(caps.supports_external_state_change_notification, bool)
    # PR 1 assertion: filesystem backend declares False.
    assert caps.supports_external_state_change_notification is False


# ──────────────────────────────────────────────────────────────────
# Per-scope isolation (MUST #2)


def test_scope_isolation_agent_mandates_dont_appear_in_project_scope(
    backend: MandateBackend, tmp_path: Path
) -> None:
    """Mandate written to ``agent:foo`` MUST NOT appear in
    ``list_mandates("project:bar")`` — backends MUST enforce cross-scope
    isolation at the storage layer (spec/29 MUST #2)."""
    make_mandate_in_backend(backend, tmp_path, "agent:foo")
    project_refs = backend.list_mandates("project:bar")
    ids = [r.mandate_id for r in project_refs]
    assert "procurement-q2-2026" not in ids


def test_scope_isolation_project_mandates_dont_appear_in_agent_scope(
    backend: MandateBackend, tmp_path: Path
) -> None:
    """Mandate written to ``project:root`` MUST NOT appear in
    ``list_mandates("agent:test")`` — each scope is independent (spec/29
    MUST #2 — scope isolation)."""
    make_mandate_in_backend(
        backend, tmp_path, "project:root", is_project_root=True
    )
    agent_refs = backend.list_mandates("agent:test")
    assert agent_refs == []


def test_scope_isolation_state_agent_vs_agent(backend: MandateBackend) -> None:
    """State written for ``agent:alice`` MUST NOT appear when reading
    ``agent:bob`` — agent state files are independently scoped."""
    state_alice = {
        "schema_version": 1,
        "scope": "agent:alice",
        "mandates": {"alice-mandate": {"last_seen_state": "active"}},
    }
    backend.write_state("agent:alice", state_alice)
    bob_state = backend.read_state("agent:bob")
    assert bob_state["mandates"] == {}


# ──────────────────────────────────────────────────────────────────
# Mandate ID rules (spec/29 parser rules)


def test_load_mandate_accepts_valid_id_patterns(
    backend: MandateBackend, tmp_path: Path
) -> None:
    """Mandate IDs that conform to ``[a-z0-9][a-z0-9-]*`` (max 64 chars)
    are accepted — boundary-checking the ID grammar from spec/29
    §"Parser rules"."""
    valid_ids_content = """\
## a
granted_by: operator
granted_at: 2026-01-01T00:00:00Z
expires_at: null
revocable_by: operator
scope: Single-char id.
constraints:
  allowed_tools:
    - tool_a
revocation_state: active
revoked_at: null
revocation_reason: null

## abc123
granted_by: operator
granted_at: 2026-01-01T00:00:00Z
expires_at: null
revocable_by: operator
scope: Alphanumeric id.
constraints:
  allowed_tools:
    - tool_b
revocation_state: active
revoked_at: null
revocation_reason: null

## foo-bar-baz
granted_by: operator
granted_at: 2026-01-01T00:00:00Z
expires_at: null
revocable_by: operator
scope: Hyphenated id.
constraints:
  allowed_tools:
    - tool_c
revocation_state: active
revoked_at: null
revocation_reason: null
"""
    make_mandate_in_backend(
        backend, tmp_path, "agent:test", content=valid_ids_content
    )
    refs = backend.list_mandates("agent:test")
    ids = {r.mandate_id for r in refs}
    assert ids == {"a", "abc123", "foo-bar-baz"}


# ──────────────────────────────────────────────────────────────────
# Revocation derived state


def test_revoked_mandate_state_surfaces_correctly(
    backend: MandateBackend, tmp_path: Path
) -> None:
    """A mandate with ``revocation_state: revoked`` in the file surfaces as
    ``RevocationState.REVOKED`` on the loaded ``Mandate`` — the backend must
    map the YAML string to the enum member, not leave it as a bare string."""
    make_mandate_in_backend(
        backend, tmp_path, "agent:test", content=_REVOKED_MANDATE_SECTION
    )
    m = backend.load_mandate("emergency-deploy-revoked", "agent:test")
    assert m.revocation_state == RevocationState.REVOKED


def test_expired_mandate_derives_expired_state(
    backend: MandateBackend, tmp_path: Path
) -> None:
    """A mandate with a past ``expires_at`` and ``revocation_state: active``
    in the file surfaces as ``RevocationState.EXPIRED`` on the loaded
    ``Mandate`` — the backend MUST derive the expired state at load time
    from ``expires_at`` vs current time (spec/29 §"RevocationState.EXPIRED
    is derived state")."""
    make_mandate_in_backend(
        backend, tmp_path, "agent:test", content=_EXPIRED_MANDATE_SECTION
    )
    m = backend.load_mandate("old-trial-license", "agent:test")
    assert m.revocation_state == RevocationState.EXPIRED


# ──────────────────────────────────────────────────────────────────
# Source path population


def test_load_mandate_populates_source_path(
    backend: MandateBackend, tmp_path: Path
) -> None:
    """``source_path`` is non-None after a successful load — backends MUST
    surface a diagnostic origin marker so operators can trace which file a
    mandate loaded from (mirrors ``ToolRef.source`` discipline in spec/25)."""
    make_mandate_in_backend(backend, tmp_path, "agent:test")
    m = backend.load_mandate("procurement-q2-2026", "agent:test")
    assert m.source_path is not None
    assert m.source_path != ""


# ──────────────────────────────────────────────────────────────────
# Mandate is frozen (MUST #1 from types.py design notes)


def test_mandate_dataclass_is_frozen(
    backend: MandateBackend, tmp_path: Path
) -> None:
    """``Mandate`` is ``@dataclass(frozen=True)`` — attempting to mutate a
    returned instance MUST raise ``FrozenInstanceError`` (or ``AttributeError``
    on older Python). Backends MUST NOT return mutable mandate dicts or
    non-frozen wrappers."""
    make_mandate_in_backend(backend, tmp_path, "agent:test")
    m = backend.load_mandate("procurement-q2-2026", "agent:test")
    with pytest.raises((TypeError, AttributeError)):
        m.mandate_id = "mutated"  # type: ignore[misc]


# ──────────────────────────────────────────────────────────────────
# Registry resolution


def test_registry_resolves_filesystem() -> None:
    """``get_mandate_backend("filesystem")`` resolves to
    ``FilesystemMandateBackend`` — registry wiring smoke test mirrors
    tool-registry + log-backend + profile-backend arcs."""
    cls = get_mandate_backend("filesystem")
    assert cls is FilesystemMandateBackend


def test_registry_lists_filesystem_at_minimum() -> None:
    """``list_mandate_backends()`` includes ``"filesystem"`` — filesystem
    is the PR-1 reference impl and MUST always be present."""
    backends = list_mandate_backends()
    assert "filesystem" in backends


def test_get_default_mandate_backend_via_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``get_default_mandate_backend`` respects
    ``ATOMIC_AGENTS_MANDATE_BACKEND`` — mirrors the operator-override env
    var pattern from the log / profile / tool-registry backends."""
    monkeypatch.setenv("ATOMIC_AGENTS_MANDATE_BACKEND", "filesystem")
    backend = get_default_mandate_backend(tmp_path)
    assert isinstance(backend, FilesystemMandateBackend)


def test_get_default_mandate_backend_unknown_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown backend id in env var raises ``BackendNotRegistered`` (or
    ``ValueError``) — operator misconfiguration is loud, not silent."""
    monkeypatch.setenv("ATOMIC_AGENTS_MANDATE_BACKEND", "totally-not-real")
    from atomic_agents.exceptions import BackendNotRegistered

    with pytest.raises((BackendNotRegistered, ValueError)):
        get_default_mandate_backend(tmp_path)


def test_register_unregister_round_trip(tmp_path: Path) -> None:
    """Register a custom backend id, confirm it lists, then unregister —
    registry extensibility smoke test matching the tool-registry arc."""
    from atomic_agents.mandate import register_mandate_backend

    # TODO: import unregister_mandate_backend if added in PR 1 impl; for now
    # test register + list only (unregister is not yet pinned in the interface).
    class _CustomBackend(FilesystemMandateBackend):
        @property
        def backend_id(self) -> str:
            return "custom-test"

    register_mandate_backend("custom-test", _CustomBackend)
    try:
        assert "custom-test" in list_mandate_backends()
        assert get_mandate_backend("custom-test") is _CustomBackend
    finally:
        # Best-effort cleanup — no unregister API pinned yet.
        # TODO: call unregister_mandate_backend("custom-test") when pinned.
        pass
