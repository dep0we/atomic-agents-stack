"""Conformance test suite for the ToolRegistryBackend Protocol (spec/25).

Parametrized over a ``backend_factory`` fixture. Each registered
backend that ships in core (``FilesystemToolRegistryBackend`` in PR 1;
``SQLiteToolRegistryBackend`` planned for PR 3) is exercised against
the same contract. A third-party backend in a downstream package
imports this test module's ``BACKEND_FACTORIES`` parametrization to
verify its own conformance.

What this suite asserts (37 tests in PR 1 — capability-gated tests
add skips when a backend declares a capability False):

1. Protocol surface — ``isinstance(backend, ToolRegistryBackend)`` passes.
2. ``backend_id`` is a stable non-empty string.
3. ``capabilities()`` returns a ``ToolRegistryCapabilities`` instance
   with bool fields.
4. ``list_tools`` returns ``[]`` for empty / missing dir (no exception).
5. ``list_tools`` returns one tool when one descriptor present.
6. ``list_tools`` is lexicographic.
7. ``list_tools`` returns ``ToolRef`` instances.
8. ``load_tool`` returns a ``ToolDefinition`` with a callable handler.
9. ``load_tool`` populates description from descriptor.
10. ``load_tool`` populates classification from descriptor.
11. ``load_tool`` raises ``ToolNotInRegistry`` for missing tool.
12. ``load_tool`` raises ``ValueError`` for path-traversal name.
13. ``load_tool`` returned handler invokes correctly with valid input.
14. ``validate`` is STATIC — does not execute the handler.
15. ``validate`` of missing tool returns ok=False with not-in-registry error.
16. ``validate`` reports missing description as warning, not error.
17. ``validate`` reports missing classification as warning, not error.
18. ``validate`` reports invalid classification as error.
19. ``validate`` reports handler-not-callable as error.
20. ``validate`` reports descriptor parse failure as error.
21. ``validate`` reports handler-import-failure as error.
22. ``validate`` returns ok=True with no errors for well-formed tool.
23. ``install`` raises NotImplementedError when supports_install=False.
24. ``uninstall`` raises NotImplementedError when supports_uninstall=False.
25. ``list_skills_catalog`` raises NotImplementedError when
    supports_skills_catalog=False.
26. ``load_skill_catalog_body`` raises NotImplementedError when
    supports_skills_catalog=False.
27. ``ToolRef.version`` round-trips on backends without versioning
    (field preserved on returned ref).
28. ``ToolRef.classification`` round-trip preserves frontmatter value.
29. ``ToolRef.source`` populated by backend (non-empty for filesystem).
30. ``ToolRef.to_dict / from_dict`` round-trip preserves all fields.
31. Descriptor without ``name`` field — load_tool succeeds (file stem
    is canonical source).
32. Descriptor with mismatched ``name`` field raises
    ``ToolDescriptorInvalid``.
33. Descriptor with invalid YAML raises ``ToolDescriptorInvalid``.
34. Descriptor missing frontmatter raises ``ToolDescriptorInvalid``.
35. Handler module without ``handler`` symbol raises
    ``ToolHandlerImportFailed`` on load_tool.
36. ``ToolNameCollision`` semantics survive — registering a backend-
    loaded tool into the in-memory ``ToolRegistry`` that already has
    a same-named tool raises ``ToolNameCollision``.
37. ``capabilities`` claim-vs-behavior parity — when
    ``supports_install=True`` the method MUST NOT raise
    NotImplementedError (and the inverse).
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest

from atomic_agents.exceptions import (
    ToolDescriptorInvalid,
    ToolHandlerImportFailed,
    ToolNameCollision,
    ToolNotInRegistry,
)
from atomic_agents.registry import (
    FilesystemToolRegistryBackend,
    SQLiteToolRegistryBackend,
    ToolRef,
    ToolRegistryBackend,
    ToolRegistryCapabilities,
    ValidationResult,
)
from atomic_agents.tools import ToolDefinition, ToolRegistry


# ──────────────────────────────────────────────────────────────────
# Backend factory parametrization — every conformance test runs once
# per registered backend. PR 3 of #64 added the SQLite factory; the
# parametrization is the conformance-suite contract that future
# backends extend (downstream packages append their factory to
# ``BACKEND_FACTORIES`` in their own test module).

BackendFactory = Callable[[Path], ToolRegistryBackend]


def _filesystem_factory(agent_root: Path) -> ToolRegistryBackend:
    """Filesystem backend rooted at ``agent_root`` (the agent's own dir).

    Per-agent backend instance — see spec/25 Decision 9.
    """
    return FilesystemToolRegistryBackend(agent_root)


def _sqlite_factory(agent_root: Path) -> ToolRegistryBackend:
    """SQLite backend rooted at ``<agent_root>/.tools.db``.

    Single-scope conformance — uses ``agent_scope='default'`` for the
    parametrized suite. Cross-scope isolation tests live in the
    SQLite-specific suite (``test_tool_registry_sqlite_backend.py``)
    and construct backends with explicit per-test scopes. Matches the
    #63 PR 3 precedent (``_sqlite_factory`` for the profile arc).
    """
    return SQLiteToolRegistryBackend(
        agent_root / ".tools.db",
        agent_scope="default",
        handlers_root=agent_root / ".handlers",
    )


BACKEND_FACTORIES: list[tuple[str, BackendFactory]] = [
    ("filesystem", _filesystem_factory),
    ("sqlite", _sqlite_factory),
]


@pytest.fixture(params=BACKEND_FACTORIES, ids=lambda p: p[0])
def backend_factory(request) -> BackendFactory:
    """Yields a callable ``(agent_root: Path) -> ToolRegistryBackend``."""
    return request.param[1]


@pytest.fixture
def backend(backend_factory, tmp_path) -> ToolRegistryBackend:
    """A backend rooted at a per-test tmp_path."""
    return backend_factory(tmp_path)


# ──────────────────────────────────────────────────────────────────
# Helpers for fixture construction


_GOOD_DESCRIPTOR = """\
---
name: query_database
description: Run a read-only SQL query against the analytics warehouse.
classification: read_only
input_schema:
  type: object
  properties:
    query:
      type: string
      description: The SQL query to run.
  required: [query]
---

# Operator notes

The handler lives next door at `query_database.py`.
"""

_GOOD_HANDLER = """\
def handler(input):
    return f"ran query: {input['query']!r}"
"""


def make_tool_files(
    agent_root: Path,
    name: str,
    *,
    descriptor: str | None = None,
    handler: str | None = None,
) -> tuple[Path, Path]:
    """Write a descriptor + handler pair into ``<agent_root>/tools/``.

    Both default to the well-formed pair above. Pass ``descriptor=None``
    or ``handler=None`` to skip writing that file. Returns
    ``(descriptor_path, handler_path)`` for assertions.
    """
    tools_dir = agent_root / "tools"
    tools_dir.mkdir(exist_ok=True)
    descriptor_path = tools_dir / f"{name}.md"
    handler_path = tools_dir / f"{name}.py"
    if descriptor is not None:
        descriptor_path.write_text(descriptor, encoding="utf-8")
    if handler is not None:
        handler_path.write_text(handler, encoding="utf-8")
    return descriptor_path, handler_path


def make_good_tool(agent_root: Path, name: str = "query_database") -> tuple[Path, Path]:
    """Convenience: write the default well-formed descriptor + handler."""
    return make_tool_files(
        agent_root,
        name,
        descriptor=_GOOD_DESCRIPTOR.replace("query_database", name),
        handler=_GOOD_HANDLER,
    )


def make_tool_in_backend(
    backend: ToolRegistryBackend,
    tmp_path: Path,
    name: str = "query_database",
    *,
    descriptor: str | None = None,
    handler: str | None = None,
) -> None:
    """Install a tool into the backend via its native shape.

    Plan-subagent Risk J fix: the original `make_tool_in_backend(backend, tmp_path,
    name)` writes to `<tmp_path>/tools/<name>.{md,py}` — works for
    filesystem because the filesystem backend reads those files;
    FAILS on SQLite because the SQLite backend reads its rows, not
    files in `<tmp_path>/tools/`. This helper dispatches per-backend-
    type via the `supports_install` capability:

    - **Filesystem (supports_install=False)**: writes descriptor +
      handler to ``<tmp_path>/tools/``. The backend's ``list_tools()``
      walks that dir.
    - **SQLite (supports_install=True)**: writes descriptor + handler
      to a staging dir under ``<tmp_path>/.staging/``, then calls
      ``backend.install(source=<staging_dir>)``. The handler file ends
      up at the backend's ``handlers_root``; the SQL row references
      that path.

    For backends with ``supports_install=True`` but a different
    ``install(source=...)`` shape (e.g., future PyPI / git backends),
    the helper can be extended via a backend_id check — until then,
    the assumption is that ``supports_install=True`` → directory-
    source install (the SQLite shape).

    Mirrors ``make_agent_in_backend`` from the profile arc — Plan-
    subagent Decision 4 of #63 PR 3.
    """
    descriptor = descriptor or _GOOD_DESCRIPTOR.replace("query_database", name)
    handler = handler or _GOOD_HANDLER

    if backend.capabilities().supports_install:
        # SQLite (or future install-capable backends) — install via
        # directory-source.
        staging = tmp_path / ".staging" / name
        staging.mkdir(parents=True, exist_ok=True)
        (staging / f"{name}.md").write_text(descriptor, encoding="utf-8")
        (staging / f"{name}.py").write_text(handler, encoding="utf-8")
        backend.install(source=str(staging))
    else:
        # Filesystem — write directly into the backend's tools_dir.
        # The factory rooted the backend at tmp_path, so its tools_dir
        # is <tmp_path>/tools/.
        make_tool_files(
            tmp_path, name, descriptor=descriptor, handler=handler
        )


# ──────────────────────────────────────────────────────────────────
# Surface conformance


def test_protocol_isinstance(backend):
    """isinstance check passes — backend exposes the full Protocol."""
    assert isinstance(backend, ToolRegistryBackend)


def test_backend_id_is_stable_nonempty_string(backend):
    backend_id = backend.backend_id
    assert isinstance(backend_id, str)
    assert backend_id != ""
    # Stable across reads
    assert backend.backend_id == backend_id


def test_capabilities_returns_tool_registry_capabilities(backend):
    caps = backend.capabilities()
    assert isinstance(caps, ToolRegistryCapabilities)
    # Every field is a bool
    assert isinstance(caps.supports_install, bool)
    assert isinstance(caps.supports_uninstall, bool)
    assert isinstance(caps.supports_versioning, bool)
    assert isinstance(caps.supports_sandbox_validate, bool)
    assert isinstance(caps.supports_skills_catalog, bool)
    assert isinstance(caps.durable, bool)


# ──────────────────────────────────────────────────────────────────
# list_tools


def test_list_tools_empty_when_no_tools_dir(backend):
    """No tools/ dir → []. Preserves byte-identical agent construction
    for fixtures without explicit tool catalogs."""
    refs = backend.list_tools()
    assert refs == []


def test_list_tools_populated(backend, tmp_path):
    make_tool_in_backend(backend, tmp_path, "query_database")
    refs = backend.list_tools()
    assert len(refs) == 1
    assert refs[0].name == "query_database"


def test_list_tools_lexicographic_order(backend, tmp_path):
    make_tool_in_backend(backend, tmp_path, "zeta_tool")
    make_tool_in_backend(backend, tmp_path, "alpha_tool")
    make_tool_in_backend(backend, tmp_path, "mid_tool")
    refs = backend.list_tools()
    names = [r.name for r in refs]
    assert names == sorted(names)
    assert names == ["alpha_tool", "mid_tool", "zeta_tool"]


def test_list_tools_returns_tool_ref_instances(backend, tmp_path):
    make_tool_in_backend(backend, tmp_path, "query_database")
    refs = backend.list_tools()
    assert all(isinstance(r, ToolRef) for r in refs)


# ──────────────────────────────────────────────────────────────────
# load_tool


def test_load_tool_returns_tool_definition_with_callable_handler(backend, tmp_path):
    make_tool_in_backend(backend, tmp_path, "query_database")
    td = backend.load_tool("query_database")
    assert isinstance(td, ToolDefinition)
    assert callable(td.handler)


def test_load_tool_populates_description(backend, tmp_path):
    make_tool_in_backend(backend, tmp_path, "query_database")
    td = backend.load_tool("query_database")
    assert "Run a read-only SQL query" in td.description


def test_load_tool_populates_classification(backend, tmp_path):
    make_tool_in_backend(backend, tmp_path, "query_database")
    td = backend.load_tool("query_database")
    assert td.classification == "read_only"


def test_load_tool_missing_raises_tool_not_in_registry(backend):
    with pytest.raises(ToolNotInRegistry, match="not found"):
        backend.load_tool("does_not_exist")


def test_load_tool_handler_invokes_correctly(backend, tmp_path):
    """The materialized handler is fully usable — round-trips through
    a real call."""
    make_tool_in_backend(backend, tmp_path, "query_database")
    td = backend.load_tool("query_database")
    result = td.handler({"query": "SELECT 1"})
    assert "ran query" in result
    assert "SELECT 1" in result


# ──────────────────────────────────────────────────────────────────
# Tier B round-trip pins — spec/25 MUST #4 (#207)
#
# Spec/25 §"Implementer contract for registry-backed tool backends"
# MUST #4 Tier B says structured-storage backends (SQLite-shape) MUST
# round-trip every ``ToolDefinition`` field that affects dispatch —
# ``name``, ``description``, ``classification``, ``input_schema``,
# ``handler`` — losslessly. The earlier conformance tests pin name
# (via ``list_tools`` + ``load_tool``), classification (via
# ``test_tool_ref_classification_round_trip``), description-substring,
# and handler-callable+invokes. The three tests below close the gap:
# strict round-trip on input_schema, exact-string round-trip on
# description, and handler invocation+arity (catches a Tier B backend
# that silently stores a stub or rewrites the signature).
#
# Both reference backends pass without code changes — these are
# regression pins for future PyPI / HTTP / SaaS-database adapters.


_RICH_SCHEMA_DESCRIPTOR = """\
---
name: echo_rich
description: Echo a message back — supports "quotes", unicode ✓, and counts.
classification: read_only
input_schema:
  type: object
  properties:
    msg:
      type: string
      description: The message to echo back verbatim.
      minLength: 1
    count:
      type: integer
      minimum: 1
      maximum: 10
      default: 1
    options:
      type: object
      properties:
        upper:
          type: boolean
      additionalProperties: false
  required: [msg]
  additionalProperties: false
---
"""

_RICH_SCHEMA_EXPECTED = {
    "type": "object",
    "properties": {
        "msg": {
            "type": "string",
            "description": "The message to echo back verbatim.",
            "minLength": 1,
        },
        "count": {
            "type": "integer",
            "minimum": 1,
            "maximum": 10,
            "default": 1,
        },
        "options": {
            "type": "object",
            "properties": {"upper": {"type": "boolean"}},
            "additionalProperties": False,
        },
    },
    "required": ["msg"],
    "additionalProperties": False,
}

_RICH_SCHEMA_DESCRIPTION = (
    'Echo a message back — supports "quotes", unicode ✓, and counts.'
)

_RICH_SCHEMA_HANDLER = """\
def handler(input):
    msg = input["msg"]
    count = input.get("count", 1)
    upper = (input.get("options") or {}).get("upper", False)
    out = (msg.upper() if upper else msg)
    return " ".join([out] * count)
"""


def test_load_tool_round_trips_input_schema(backend, tmp_path):
    """Spec/25 MUST #4 Tier B: ``input_schema`` MUST round-trip
    losslessly across install → load_tool. Pins against silent drift:
    structured-storage backends MUST preserve nested objects,
    integer constraints (``minimum``/``maximum``/``default``),
    booleans (``additionalProperties: false``), and required arrays
    — a backend that re-serializes via JSON without preserving the
    structured shape would pass the existing description-substring
    test while violating the Tier B round-trip MUST.
    """
    make_tool_in_backend(
        backend,
        tmp_path,
        "echo_rich",
        descriptor=_RICH_SCHEMA_DESCRIPTOR,
        handler=_RICH_SCHEMA_HANDLER,
    )
    td = backend.load_tool("echo_rich")
    assert td.input_schema == _RICH_SCHEMA_EXPECTED, (
        f"input_schema round-trip failed: got {td.input_schema!r}, "
        f"expected {_RICH_SCHEMA_EXPECTED!r}"
    )


def test_load_tool_round_trips_description(backend, tmp_path):
    """Spec/25 MUST #4 Tier B: ``description`` MUST round-trip exactly
    across install → load_tool. Pins against silent normalization
    (whitespace trim, quote-style coercion, unicode escape) that
    would change what the LLM sees in the system prompt.
    """
    make_tool_in_backend(
        backend,
        tmp_path,
        "echo_rich",
        descriptor=_RICH_SCHEMA_DESCRIPTOR,
        handler=_RICH_SCHEMA_HANDLER,
    )
    td = backend.load_tool("echo_rich")
    assert td.description == _RICH_SCHEMA_DESCRIPTION, (
        f"description round-trip failed: got {td.description!r}, "
        f"expected {_RICH_SCHEMA_DESCRIPTION!r}"
    )


def test_load_tool_round_trips_handler_callable(backend, tmp_path):
    """Spec/25 MUST #4 Tier B: the materialized ``handler`` MUST be a
    callable that dispatches correctly — install → load_tool MUST NOT
    return a stub, a wrapped placeholder, or a handler with a
    rewritten signature. Catches a Tier B backend that drops the
    actual handler body and substitutes a no-op.

    Invokes with structured input that exercises the handler's
    real branches (``msg`` + ``count`` + nested ``options.upper``)
    and asserts the return value matches the source semantics —
    a stub returning ``None`` or echoing input would fail both.
    """
    make_tool_in_backend(
        backend,
        tmp_path,
        "echo_rich",
        descriptor=_RICH_SCHEMA_DESCRIPTOR,
        handler=_RICH_SCHEMA_HANDLER,
    )
    td = backend.load_tool("echo_rich")
    assert callable(td.handler), "handler MUST be callable after load_tool"
    out = td.handler({"msg": "hi", "count": 3, "options": {"upper": True}})
    assert out == "HI HI HI", (
        f"handler invocation round-trip failed: got {out!r}, expected 'HI HI HI'"
    )


# ──────────────────────────────────────────────────────────────────
# Path-traversal refusal — spec/25 MUST #1


@pytest.mark.parametrize(
    "bad_name",
    [
        "../escape",
        "foo/bar",
        "foo\\bar",
        ".hidden",
        "..",
        "",
        "foo/../escape",  # embedded `..` after a real segment
    ],
)
def test_load_tool_refuses_path_traversal(backend, bad_name):
    """``load_tool`` MUST validate ``name`` against path-traversal
    BEFORE any disk / DB access (spec/25 MUST #1).

    The contract is **fail-fast with ValueError**, NOT
    ToolNotInRegistry. A backend that defers to "let the disk lookup
    miss" semantically validates AFTER the unsafe path concat — which
    is exactly the F-3 shape #63 PR 3 fixed in the AgentProfile arc.
    Locking the conformance test to ValueError forces every future
    backend (SQLite, PyPI, git) to validate at the API boundary.
    """
    with pytest.raises(ValueError):
        backend.load_tool(bad_name)


# ──────────────────────────────────────────────────────────────────
# validate — static check, no handler execution


def test_validate_does_not_execute_handler(backend, tmp_path):
    """Spec/25 Decision 6 — ``validate`` MUST NOT execute the handler.

    The descriptor + handler write a side-effect file at module-import
    time; ``validate()`` triggers import (handler module must load to
    check signature) which writes the side-effect file. But the
    HANDLER FUNCTION itself MUST NOT be called — its execution would
    write a SECOND file. We assert exactly one file appears.
    """
    side_effect_marker = tmp_path / "import_side_effect.flag"
    handler_call_marker = tmp_path / "handler_called.flag"
    handler_src = f"""\
from pathlib import Path
Path({str(side_effect_marker)!r}).touch()

def handler(input):
    Path({str(handler_call_marker)!r}).touch()
    return "called"
"""
    make_tool_in_backend(
        backend,
        tmp_path,
        "side_effecting",
        descriptor=_GOOD_DESCRIPTOR.replace("query_database", "side_effecting"),
        handler=handler_src,
    )
    backend.validate("side_effecting")
    # Import side-effect fires (handler module loaded) — that's
    # documented behavior in spec/25 Decision 6 + R8 of the plan.
    assert side_effect_marker.exists()
    # But the handler function MUST NOT have been called.
    assert not handler_call_marker.exists()


def test_validate_missing_tool_returns_not_ok(backend):
    """``validate`` of a missing tool MUST NOT raise — returns
    ok=False with descriptive error (matches LLM ``validate`` shape)."""
    result = backend.validate("does_not_exist")
    assert isinstance(result, ValidationResult)
    assert result.ok is False
    assert any("not in registry" in e.lower() or "no descriptor" in e.lower() for e in result.errors)


def test_validate_missing_description_is_warning(backend, tmp_path):
    """Missing description is hygiene, not a hard error — tool is
    still dispatchable."""
    descriptor = """\
---
name: no_description
classification: read_only
---
"""
    make_tool_in_backend(
        backend,
        tmp_path,
        "no_description",
        descriptor=descriptor,
        handler=_GOOD_HANDLER,
    )
    result = backend.validate("no_description")
    assert result.ok is True
    assert any("description" in w.lower() for w in result.warnings)
    assert result.errors == []


def test_validate_missing_classification_is_warning(backend, tmp_path):
    """Missing classification falls back to ``external_side_effect``
    at dispatch — usable but flagged."""
    descriptor = """\
---
name: no_class
description: A tool with no classification declared.
---
"""
    make_tool_in_backend(
        backend,
        tmp_path,
        "no_class",
        descriptor=descriptor,
        handler=_GOOD_HANDLER,
    )
    result = backend.validate("no_class")
    assert result.ok is True
    assert any("classification" in w.lower() for w in result.warnings)


def test_validate_invalid_classification_is_error(backend, tmp_path):
    """Classification not in the ActionClass enum is an error — the
    judge layer would reject this tool at dispatch."""
    descriptor = """\
---
name: bad_class
description: A tool with an invalid classification.
classification: super_dangerous
---
"""
    make_tool_in_backend(
        backend,
        tmp_path,
        "bad_class",
        descriptor=descriptor,
        handler=_GOOD_HANDLER,
    )
    result = backend.validate("bad_class")
    assert result.ok is False
    assert any("classification" in e.lower() for e in result.errors)


def test_validate_handler_module_missing_handler_symbol(backend, tmp_path):
    """Handler module imports cleanly but exposes no ``handler`` symbol —
    tool is unusable.

    **Lazy-validation regime only.** Backends with ``supports_install=True``
    catch this at install time (the install path imports the handler to
    verify it has a ``handler`` symbol). For those backends, the
    equivalent assertion lives in the install-rejection tests in the
    backend-specific suite.
    """
    if backend.capabilities().supports_install:
        pytest.skip(
            "install-capable backends reject missing handler symbol at "
            "install time; see backend-specific suite for install-rejection test"
        )
    descriptor = _GOOD_DESCRIPTOR.replace("query_database", "no_handler")
    handler = "# Empty module — no `handler` symbol exposed.\n"
    make_tool_files(
        tmp_path,
        "no_handler",
        descriptor=descriptor,
        handler=handler,
    )
    result = backend.validate("no_handler")
    assert result.ok is False
    assert any("handler" in e.lower() for e in result.errors)


def test_validate_handler_symbol_not_callable(backend, tmp_path):
    """Handler module exposes a ``handler`` symbol that isn't callable
    (e.g., ``handler = 42``) — distinct branch from "no handler symbol".

    Lazy-validation regime only — install-capable backends now reject
    non-callable handlers at install time via the strengthened
    ``_import_handler`` callable check (Step 11 specialist finding).
    """
    if backend.capabilities().supports_install:
        pytest.skip(
            "install-capable backends reject non-callable handler at "
            "install time; see backend-specific install-rejection test"
        )
    descriptor = _GOOD_DESCRIPTOR.replace("query_database", "non_callable")
    handler = "# handler exists but is not callable\nhandler = 42\n"
    make_tool_files(
        tmp_path,
        "non_callable",
        descriptor=descriptor,
        handler=handler,
    )
    result = backend.validate("non_callable")
    assert result.ok is False
    assert any(
        "not callable" in e.lower() or "handler" in e.lower()
        for e in result.errors
    )


def test_validate_descriptor_parse_failure_is_error(backend, tmp_path):
    """Frontmatter unparseable → error (tool unusable).

    Lazy-validation regime only — install-capable backends reject
    malformed descriptors at install time.
    """
    if backend.capabilities().supports_install:
        pytest.skip(
            "install-capable backends reject malformed descriptors at "
            "install time; see backend-specific suite"
        )
    bad_descriptor = "no frontmatter here at all\nplain markdown\n"
    make_tool_files(
        tmp_path,
        "bad_descriptor",
        descriptor=bad_descriptor,
        handler=_GOOD_HANDLER,
    )
    result = backend.validate("bad_descriptor")
    assert result.ok is False
    assert any(
        "frontmatter" in e.lower() or "descriptor" in e.lower()
        for e in result.errors
    )


def test_validate_handler_import_failure_is_error(backend, tmp_path):
    """Handler module raises at import time → error.

    Lazy-validation regime only — install-capable backends reject
    broken handler modules at install time (import is part of the
    install validation).
    """
    if backend.capabilities().supports_install:
        pytest.skip(
            "install-capable backends reject broken handler imports at "
            "install time; see backend-specific suite"
        )
    descriptor = _GOOD_DESCRIPTOR.replace("query_database", "broken_handler")
    handler = "raise RuntimeError('broken handler module')\n"
    make_tool_files(
        tmp_path,
        "broken_handler",
        descriptor=descriptor,
        handler=handler,
    )
    result = backend.validate("broken_handler")
    assert result.ok is False
    assert any(
        "import" in e.lower() or "broken handler" in e.lower()
        for e in result.errors
    )


def test_validate_well_formed_tool_returns_ok(backend, tmp_path):
    """Happy path — well-formed descriptor + handler → ok=True, no
    errors, no warnings."""
    make_tool_in_backend(backend, tmp_path, "query_database")
    result = backend.validate("query_database")
    assert result.ok is True
    assert result.errors == []
    assert result.warnings == []


# ──────────────────────────────────────────────────────────────────
# Capability gates — install / uninstall / skill catalog


def test_install_raises_when_unsupported(backend):
    """``supports_install=False`` → ``install`` raises NotImplementedError."""
    caps = backend.capabilities()
    if caps.supports_install:
        pytest.skip("backend supports install — covered in PR 3 SQLite tests")
    with pytest.raises(NotImplementedError):
        backend.install("local:///some/path")


def test_uninstall_raises_when_unsupported(backend):
    """``supports_uninstall=False`` → ``uninstall`` raises NotImplementedError."""
    caps = backend.capabilities()
    if caps.supports_uninstall:
        pytest.skip("backend supports uninstall — covered in PR 3 SQLite tests")
    with pytest.raises(NotImplementedError):
        backend.uninstall("any_name")


def test_list_skills_catalog_raises_when_unsupported(backend):
    """``supports_skills_catalog=False`` → ``list_skills_catalog`` raises."""
    caps = backend.capabilities()
    if caps.supports_skills_catalog:
        pytest.skip("backend publishes a skill catalog — future capability")
    with pytest.raises(NotImplementedError):
        backend.list_skills_catalog()


def test_load_skill_catalog_body_raises_when_unsupported(backend):
    """``supports_skills_catalog=False`` → ``load_skill_catalog_body`` raises."""
    caps = backend.capabilities()
    if caps.supports_skills_catalog:
        pytest.skip("backend publishes a skill catalog — future capability")
    with pytest.raises(NotImplementedError):
        backend.load_skill_catalog_body("any_skill")


# ──────────────────────────────────────────────────────────────────
# ToolRef field round-trip


def test_tool_ref_version_round_trips(backend, tmp_path):
    """Even on backends declaring ``supports_versioning=False``,
    ``ToolRef.version`` round-trips (filesystem always returns None;
    future PyPI backends populate from package metadata)."""
    make_tool_in_backend(backend, tmp_path, "query_database")
    refs = backend.list_tools()
    ref = refs[0]
    # Field is present on the dataclass
    assert hasattr(ref, "version")
    # Filesystem returns None (no version semantics in on-disk layout)
    # — for backends declaring supports_versioning=True, the field is
    # populated from the backend's native version source.
    if not backend.capabilities().supports_versioning:
        assert ref.version is None


def test_tool_ref_classification_round_trip(backend, tmp_path):
    """Classification from descriptor frontmatter surfaces on ToolRef."""
    make_tool_in_backend(backend, tmp_path, "query_database")
    refs = backend.list_tools()
    assert refs[0].classification == "read_only"


def test_tool_ref_source_populated(backend, tmp_path):
    """Backends MUST set ``ToolRef.source`` to a meaningful origin
    marker for diagnostic audit. Empty string is allowed only when
    the backend genuinely can't surface an origin (rare — purely
    structural)."""
    make_tool_in_backend(backend, tmp_path, "query_database")
    refs = backend.list_tools()
    # Filesystem sets the descriptor path; SQLite would set
    # `sqlite://<table>/<scope>/<name>` etc. Either way: non-empty.
    assert refs[0].source != ""


def test_tool_ref_to_dict_from_dict_round_trip():
    """``ToolRef.to_dict / from_dict`` preserves every field."""
    original = ToolRef(
        name="example",
        description="Example tool description.",
        classification="reversible_write",
        version="1.2.3",
        source="local:///path/to/example.md",
    )
    round_tripped = ToolRef.from_dict(original.to_dict())
    assert round_tripped == original


# ──────────────────────────────────────────────────────────────────
# Descriptor edge cases


def test_descriptor_without_name_field_succeeds(backend, tmp_path):
    """When the descriptor omits the ``name`` field, the file stem is
    the canonical source — ``load_tool("stem_only")`` succeeds."""
    descriptor = """\
---
description: A tool without an explicit name field.
classification: read_only
---
"""
    make_tool_in_backend(
        backend,
        tmp_path,
        "stem_only",
        descriptor=descriptor,
        handler=_GOOD_HANDLER,
    )
    td = backend.load_tool("stem_only")
    assert td.name == "stem_only"


def test_descriptor_with_mismatched_name_raises(backend, tmp_path):
    """When the descriptor declares a ``name`` that doesn't match the
    file stem, ``load_tool`` MUST raise — defensive against operator typos.

    Lazy-validation regime only — install-capable backends catch the
    mismatch at install time (the install path validates descriptor
    name matches file stem).
    """
    if backend.capabilities().supports_install:
        pytest.skip(
            "install-capable backends reject descriptor name mismatch at "
            "install time; see backend-specific suite"
        )
    descriptor = """\
---
name: typo_name
description: The descriptor's name doesn't match the file stem.
classification: read_only
---
"""
    make_tool_files(
        tmp_path,
        "correct_name",
        descriptor=descriptor,
        handler=_GOOD_HANDLER,
    )
    with pytest.raises(ToolDescriptorInvalid, match="name"):
        backend.load_tool("correct_name")


def test_descriptor_invalid_yaml_raises(backend, tmp_path):
    """Malformed YAML in the frontmatter → ``ToolDescriptorInvalid``.

    Lazy-validation regime only — install-capable backends reject
    malformed YAML at install time.
    """
    if backend.capabilities().supports_install:
        pytest.skip(
            "install-capable backends reject malformed YAML at install time; "
            "see backend-specific suite"
        )
    descriptor = """\
---
name: bad_yaml
description: "open quote without close
classification: read_only
---
"""
    make_tool_files(
        tmp_path,
        "bad_yaml",
        descriptor=descriptor,
        handler=_GOOD_HANDLER,
    )
    with pytest.raises(ToolDescriptorInvalid):
        backend.load_tool("bad_yaml")


def test_descriptor_missing_frontmatter_raises(backend, tmp_path):
    """A markdown file with no ``---`` frontmatter delimiter →
    ``ToolDescriptorInvalid`` on load.

    Lazy-validation regime only — install-capable backends reject
    descriptors lacking frontmatter at install time.
    """
    if backend.capabilities().supports_install:
        pytest.skip(
            "install-capable backends reject missing-frontmatter descriptors "
            "at install time; see backend-specific suite"
        )
    descriptor = "# Plain Markdown\n\nNo frontmatter here.\n"
    make_tool_files(
        tmp_path,
        "no_frontmatter",
        descriptor=descriptor,
        handler=_GOOD_HANDLER,
    )
    with pytest.raises(ToolDescriptorInvalid):
        backend.load_tool("no_frontmatter")


def test_handler_module_without_handler_symbol_raises(backend, tmp_path):
    """Handler module imports cleanly but exposes no ``handler`` →
    ``ToolHandlerImportFailed`` on load.

    Lazy-validation regime only — install-capable backends reject
    handler modules missing the ``handler`` symbol at install time.
    """
    if backend.capabilities().supports_install:
        pytest.skip(
            "install-capable backends reject missing handler symbol at "
            "install time; see backend-specific suite"
        )
    descriptor = _GOOD_DESCRIPTOR.replace("query_database", "no_handler_symbol")
    handler = "x = 1\n# no `handler` callable here\n"
    make_tool_files(
        tmp_path,
        "no_handler_symbol",
        descriptor=descriptor,
        handler=handler,
    )
    with pytest.raises(ToolHandlerImportFailed, match="handler"):
        backend.load_tool("no_handler_symbol")


# ──────────────────────────────────────────────────────────────────
# Integration with existing tools.py — ToolNameCollision survives the indirection


def test_tool_name_collision_survives_through_backend_load(backend, tmp_path):
    """When the operator has pre-registered a same-named tool in the
    in-memory ``ToolRegistry``, ``register()`` (with default
    ``allow_overwrite=False``) MUST raise ``ToolNameCollision`` even
    when the second tool came through the backend's ``load_tool``.

    Spec/25 Decision 8 — the in-memory ``ToolRegistry`` is the
    dispatch surface; ``ToolNameCollision`` semantics stay unchanged
    after PR 2 wiring.
    """
    make_tool_in_backend(backend, tmp_path, "query_database")

    # Operator's programmatic registration first (operator intent wins
    # on the collision shape).
    in_memory = ToolRegistry()
    in_memory.register(
        ToolDefinition(
            name="query_database",
            description="Operator-provided tool.",
            input_schema={"type": "object", "properties": {}, "required": []},
            handler=lambda input: "from operator",
        )
    )

    # Backend-loaded tool with the same name → collision on register
    backend_td = backend.load_tool("query_database")
    with pytest.raises(ToolNameCollision):
        in_memory.register(backend_td)


# ──────────────────────────────────────────────────────────────────
# Capability claim-vs-behavior parity


def test_capability_parity_install(backend, tmp_path):
    """``supports_install=True`` ↔ ``install`` actually installs.

    Step 11 / plan-subagent Risk C: the original test only checked
    that ``NotImplementedError`` wasn't raised. A backend whose
    install() silently no-op'd would pass — every other exception
    was swallowed via ``except Exception: pass``. This strengthened
    shape uses a real well-formed source and asserts the installed
    tool surfaces in ``list_tools()`` afterwards.
    """
    caps = backend.capabilities()
    if caps.supports_install:
        # Real well-formed install — verify the tool actually persists.
        staging = tmp_path / ".parity_staging" / "parity_tool"
        staging.mkdir(parents=True, exist_ok=True)
        (staging / "parity_tool.md").write_text(
            _GOOD_DESCRIPTOR.replace("query_database", "parity_tool"),
            encoding="utf-8",
        )
        (staging / "parity_tool.py").write_text(_GOOD_HANDLER, encoding="utf-8")
        ref = backend.install(source=str(staging))
        assert ref.name == "parity_tool"
        # Confirm the tool actually landed in the catalog.
        assert "parity_tool" in [r.name for r in backend.list_tools()]
        # Confirm load_tool() materializes a usable handler.
        td = backend.load_tool("parity_tool")
        assert callable(td.handler)
    else:
        with pytest.raises(NotImplementedError):
            backend.install("local:///any/path")


def test_capability_parity_uninstall(backend):
    """``supports_uninstall=True`` ↔ ``uninstall`` does not raise
    NotImplementedError. PR 3's SQLite backend flips this True; PR 1's
    filesystem ships False. Without this test, a backend lying about
    supports_uninstall=True would slip silent."""
    caps = backend.capabilities()
    if caps.supports_uninstall:
        try:
            backend.uninstall("nonexistent_tool")
        except NotImplementedError:
            pytest.fail(
                "backend claims supports_uninstall=True but uninstall() "
                "raised NotImplementedError"
            )
        except Exception:
            pass
    else:
        with pytest.raises(NotImplementedError):
            backend.uninstall("any_name")


def test_capability_parity_skills_catalog_list(backend):
    """``supports_skills_catalog=True`` ↔ ``list_skills_catalog`` does
    not raise NotImplementedError. Both PR-1-era backends ship False
    (skill catalog is a reserved future capability per spec/25
    Decision 2)."""
    caps = backend.capabilities()
    if caps.supports_skills_catalog:
        try:
            backend.list_skills_catalog()
        except NotImplementedError:
            pytest.fail(
                "backend claims supports_skills_catalog=True but "
                "list_skills_catalog() raised NotImplementedError"
            )
    else:
        with pytest.raises(NotImplementedError):
            backend.list_skills_catalog()


def test_capability_parity_skills_catalog_body(backend):
    """``supports_skills_catalog=True`` ↔ ``load_skill_catalog_body``
    does not raise NotImplementedError on a happy-path call."""
    caps = backend.capabilities()
    if caps.supports_skills_catalog:
        try:
            backend.load_skill_catalog_body("any_skill")
        except NotImplementedError:
            pytest.fail(
                "backend claims supports_skills_catalog=True but "
                "load_skill_catalog_body() raised NotImplementedError"
            )
        except Exception:
            # Other exceptions (skill not found, etc.) are fine.
            pass
    else:
        with pytest.raises(NotImplementedError):
            backend.load_skill_catalog_body("any_skill")


@pytest.mark.parametrize(
    "bad_name",
    [
        "../escape",
        "foo/bar",
        "foo\\bar",
        ".hidden",
        "..",
        "",
        "foo/../escape",
    ],
)
def test_uninstall_refuses_path_traversal(backend, bad_name):
    """``uninstall`` MUST validate ``name`` against path-traversal at
    the API boundary (spec/25 MUST #1). Skipped on backends that
    don't support uninstall (they raise NotImplementedError before
    the validation runs — PR 3's SQLite is the first implementer)."""
    caps = backend.capabilities()
    if not caps.supports_uninstall:
        pytest.skip("backend does not support uninstall")
    with pytest.raises(ValueError):
        backend.uninstall(bad_name)


def test_uninstall_unknown_name_is_idempotent(backend):
    """``uninstall`` MUST be a no-op for unknown names (spec/25 §
    'uninstall semantics'). Mirrors the in-memory ToolRegistry.unregister
    precedent. Skipped on backends without uninstall support."""
    caps = backend.capabilities()
    if not caps.supports_uninstall:
        pytest.skip("backend does not support uninstall")
    # Pre-state — empty catalog
    before = backend.list_tools()
    # Should NOT raise.
    backend.uninstall("definitely_not_in_the_catalog")
    # Catalog unchanged.
    after = backend.list_tools()
    assert [r.name for r in before] == [r.name for r in after]
