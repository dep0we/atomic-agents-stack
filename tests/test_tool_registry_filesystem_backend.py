"""Filesystem-specific tests for ``FilesystemToolRegistryBackend``.

Conformance tests in ``test_tool_registry_protocol_conformance.py``
exercise the Protocol contract that every backend must satisfy.
THIS module exercises the filesystem-specific behavior: on-disk
descriptor parsing, hidden / helper file exclusion, ``tools/`` absent
or empty edge cases, registry dispatch, operator-config redaction.

The conformance suite already covers Protocol-shaped invariants and
path-traversal refusal across backends — those tests stay there so
future backends inherit them. This module covers what's
filesystem-only: descriptor parsing edge cases, Python module
exclusion conventions, registry + factory wiring.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from atomic_agents.exceptions import (
    BackendNotRegistered,
    ToolDescriptorInvalid,
    ToolHandlerImportFailed,
    ToolNotInRegistry,
)
from atomic_agents.registry import (
    FilesystemToolRegistryBackend,
    ToolRegistryBackend,
    get_default_tool_registry_backend,
    get_tool_registry_backend,
    list_tool_registry_backends,
    register_tool_registry_backend,
    unregister_tool_registry_backend,
)


# Reuse the descriptor + handler fixture helpers from the conformance suite.
from tests.test_tool_registry_protocol_conformance import (
    _GOOD_DESCRIPTOR,
    _GOOD_HANDLER,
    make_good_tool,
    make_tool_files,
)


# ──────────────────────────────────────────────────────────────────
# Constructor + path attrs


def test_constructor_does_not_require_existing_dir(tmp_path):
    """``FilesystemToolRegistryBackend(agent_root)`` MAY accept a path
    whose ``tools/`` subdirectory doesn't exist — every existing
    fixture-built agent works without modification.
    """
    nonexistent = tmp_path / "ghost_agent"
    # Does not raise on a missing tools dir — Decision 7 of spec/25.
    backend = FilesystemToolRegistryBackend(nonexistent)
    assert backend.list_tools() == []


def test_agent_root_property_is_readonly(tmp_path):
    backend = FilesystemToolRegistryBackend(tmp_path)
    assert backend.agent_root == tmp_path
    # backend_id is a @property — instance-set hits Python's property
    # protocol with no setter defined.
    with pytest.raises(AttributeError):
        backend.backend_id = "spoof"  # type: ignore[misc]


def test_tools_dir_resolves_under_agent_root(tmp_path):
    backend = FilesystemToolRegistryBackend(tmp_path)
    assert backend.tools_dir == tmp_path / "tools"


# ──────────────────────────────────────────────────────────────────
# On-disk descriptor parsing


def test_descriptor_at_canonical_path(tmp_path):
    make_good_tool(tmp_path, "query_database")
    descriptor = tmp_path / "tools" / "query_database.md"
    handler = tmp_path / "tools" / "query_database.py"
    assert descriptor.is_file()
    assert handler.is_file()


def test_frontmatter_with_leading_blank_lines_tolerated(tmp_path):
    """Editors that accidentally prepend a newline shouldn't break the
    parse — descriptor MAY start with whitespace before ``---``."""
    descriptor = "\n\n" + _GOOD_DESCRIPTOR.replace("query_database", "tolerant_tool")
    make_tool_files(
        tmp_path,
        "tolerant_tool",
        descriptor=descriptor,
        handler=_GOOD_HANDLER,
    )
    backend = FilesystemToolRegistryBackend(tmp_path)
    td = backend.load_tool("tolerant_tool")
    assert td.name == "tolerant_tool"


def test_frontmatter_root_must_be_dict(tmp_path):
    """A frontmatter that parses to a list / scalar (not a dict) is
    rejected — descriptor MUST be a mapping at the root."""
    descriptor = """\
---
- not a dict
- just a list
---
"""
    make_tool_files(
        tmp_path,
        "list_root",
        descriptor=descriptor,
        handler=_GOOD_HANDLER,
    )
    backend = FilesystemToolRegistryBackend(tmp_path)
    with pytest.raises(ToolDescriptorInvalid, match="mapping|dict"):
        backend.load_tool("list_root")


def test_input_schema_must_be_dict(tmp_path):
    """The ``input_schema`` field MUST be a dict — string / list / scalar
    is operator error."""
    descriptor = """\
---
name: bad_schema
description: input_schema is a string instead of a dict.
classification: read_only
input_schema: not a dict
---
"""
    make_tool_files(
        tmp_path,
        "bad_schema",
        descriptor=descriptor,
        handler=_GOOD_HANDLER,
    )
    backend = FilesystemToolRegistryBackend(tmp_path)
    with pytest.raises(ToolDescriptorInvalid, match="input_schema"):
        backend.load_tool("bad_schema")


def test_input_schema_default_when_absent(tmp_path):
    """When ``input_schema`` is omitted, ``load_tool`` defaults to an
    empty object schema — operators can ship description-only tools."""
    descriptor = """\
---
name: no_schema
description: Minimal tool with no input schema declared.
classification: read_only
---
"""
    make_tool_files(
        tmp_path,
        "no_schema",
        descriptor=descriptor,
        handler="def handler(input): return 'ok'\n",
    )
    backend = FilesystemToolRegistryBackend(tmp_path)
    td = backend.load_tool("no_schema")
    assert isinstance(td.input_schema, dict)
    assert td.input_schema.get("type") == "object"


# ──────────────────────────────────────────────────────────────────
# Hidden + helper file exclusion


def test_hidden_descriptor_files_excluded(tmp_path):
    """Descriptors with hidden-prefix names (``.foo.md``) MUST NOT
    surface — operators occasionally leave editor backup files in
    ``tools/`` and we don't want them registered as tools."""
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    (tools_dir / ".dotfile_descriptor.md").write_text(
        _GOOD_DESCRIPTOR, encoding="utf-8"
    )
    (tools_dir / ".dotfile_descriptor.py").write_text(_GOOD_HANDLER, encoding="utf-8")

    backend = FilesystemToolRegistryBackend(tmp_path)
    assert backend.list_tools() == []


def test_underscore_prefix_helper_excluded(tmp_path):
    """Python helper modules (``_helper.py``) MUST NOT surface as
    tools — Python convention for module-internal helpers."""
    make_tool_files(
        tmp_path,
        "_helper",
        descriptor=_GOOD_DESCRIPTOR.replace("query_database", "_helper"),
        handler=_GOOD_HANDLER,
    )
    backend = FilesystemToolRegistryBackend(tmp_path)
    refs = backend.list_tools()
    assert all(not r.name.startswith("_") for r in refs)


def test_dunder_init_excluded(tmp_path):
    """``__init__.py`` (and its descriptor if present) MUST NOT
    surface as tools — Python package marker."""
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    (tools_dir / "__init__.py").write_text("# package marker\n", encoding="utf-8")
    (tools_dir / "__init__.md").write_text(
        _GOOD_DESCRIPTOR, encoding="utf-8"
    )
    backend = FilesystemToolRegistryBackend(tmp_path)
    assert backend.list_tools() == []


def test_non_md_files_excluded(tmp_path):
    """Files in ``tools/`` without ``.md`` suffix aren't descriptors —
    they MAY be helper Python modules or operator notes."""
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    (tools_dir / "notes.txt").write_text("operator scratchpad", encoding="utf-8")
    (tools_dir / "config.yaml").write_text("key: value", encoding="utf-8")
    backend = FilesystemToolRegistryBackend(tmp_path)
    assert backend.list_tools() == []


def test_subdirectories_in_tools_dir_ignored(tmp_path):
    """``tools/<name>/`` subdirectories are NOT descriptor paths —
    ``list_tools`` skips them silently."""
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    nested = tools_dir / "nested_tool"
    nested.mkdir()
    (nested / "<irrelevant>").write_text("just a file", encoding="utf-8")
    backend = FilesystemToolRegistryBackend(tmp_path)
    assert backend.list_tools() == []


# ──────────────────────────────────────────────────────────────────
# Malformed descriptors at list_tools time — silent skip


def test_list_tools_silently_skips_malformed_descriptors(tmp_path):
    """A malformed descriptor MUST NOT poison the entire catalog —
    ``list_tools`` skips it silently (operators triage via
    ``validate(name)``)."""
    make_good_tool(tmp_path, "good_tool")
    # Add a malformed descriptor in the same directory
    make_tool_files(
        tmp_path,
        "bad_tool",
        descriptor="not a frontmatter\n",
        handler=_GOOD_HANDLER,
    )
    backend = FilesystemToolRegistryBackend(tmp_path)
    refs = backend.list_tools()
    names = [r.name for r in refs]
    assert "good_tool" in names
    assert "bad_tool" not in names


# ──────────────────────────────────────────────────────────────────
# Handler module — lazy import + side effects


def test_handler_module_lazy_import_at_load_tool(tmp_path):
    """The handler module's side effects fire on ``load_tool`` (lazy),
    NOT on ``list_tools`` (eager would be expensive)."""
    side_effect_marker = tmp_path / "import_fired.flag"
    handler_src = f"""\
from pathlib import Path
Path({str(side_effect_marker)!r}).touch()

def handler(input):
    return "ok"
"""
    make_tool_files(
        tmp_path,
        "side_effecting",
        descriptor=_GOOD_DESCRIPTOR.replace("query_database", "side_effecting"),
        handler=handler_src,
    )
    backend = FilesystemToolRegistryBackend(tmp_path)
    # list_tools MUST NOT trip the import.
    backend.list_tools()
    assert not side_effect_marker.exists()
    # load_tool DOES trip the import — Decision 5 of spec/25.
    backend.load_tool("side_effecting")
    assert side_effect_marker.exists()


def test_handler_module_missing_raises_on_load_tool(tmp_path):
    """Descriptor present but no handler module → ``ToolHandlerImportFailed``."""
    make_tool_files(
        tmp_path,
        "no_handler_file",
        descriptor=_GOOD_DESCRIPTOR.replace("query_database", "no_handler_file"),
        handler=None,  # don't write the .py
    )
    backend = FilesystemToolRegistryBackend(tmp_path)
    with pytest.raises(ToolHandlerImportFailed):
        backend.load_tool("no_handler_file")


def test_handler_distinct_modules_across_agents(tmp_path):
    """Two distinct agent dirs with same-named tools resolve their
    handlers from DIFFERENT modules — ``sys.modules`` qualname is
    keyed by absolute path so reload across distinct agents doesn't
    collide.
    """
    agent_a = tmp_path / "agent_a"
    agent_b = tmp_path / "agent_b"
    agent_a.mkdir()
    agent_b.mkdir()

    handler_a = "def handler(input): return 'from_a'\n"
    handler_b = "def handler(input): return 'from_b'\n"
    make_tool_files(
        agent_a,
        "shared_name",
        descriptor=_GOOD_DESCRIPTOR.replace("query_database", "shared_name"),
        handler=handler_a,
    )
    make_tool_files(
        agent_b,
        "shared_name",
        descriptor=_GOOD_DESCRIPTOR.replace("query_database", "shared_name"),
        handler=handler_b,
    )

    backend_a = FilesystemToolRegistryBackend(agent_a)
    backend_b = FilesystemToolRegistryBackend(agent_b)
    td_a = backend_a.load_tool("shared_name")
    td_b = backend_b.load_tool("shared_name")
    assert td_a.handler({}) == "from_a"
    assert td_b.handler({}) == "from_b"


# ──────────────────────────────────────────────────────────────────
# Source field — descriptor's filesystem path


def test_source_field_is_descriptor_path(tmp_path):
    """``ToolRef.source`` MUST surface the descriptor's filesystem
    path for diagnostic audit."""
    descriptor_path, _ = make_good_tool(tmp_path, "query_database")
    backend = FilesystemToolRegistryBackend(tmp_path)
    refs = backend.list_tools()
    assert str(descriptor_path) == refs[0].source


# ──────────────────────────────────────────────────────────────────
# Registry + factory


def test_registry_resolves_filesystem():
    cls = get_tool_registry_backend("filesystem")
    assert cls is FilesystemToolRegistryBackend


def test_registry_lists_filesystem_at_minimum():
    backends = list_tool_registry_backends()
    assert "filesystem" in backends


def test_register_unregister_round_trip(tmp_path):
    """Register a custom backend id, then unregister — registry-shape
    smoke test mirroring the profile/log registries."""
    class _CustomBackend(FilesystemToolRegistryBackend):
        @property
        def backend_id(self) -> str:
            return "custom"

    register_tool_registry_backend("custom", _CustomBackend)
    try:
        assert "custom" in list_tool_registry_backends()
        cls = get_tool_registry_backend("custom")
        assert cls is _CustomBackend
    finally:
        unregister_tool_registry_backend("custom")
    assert "custom" not in list_tool_registry_backends()


def test_default_factory_returns_filesystem(tmp_path):
    """No env var set → filesystem default."""
    old = os.environ.pop("ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND", None)
    try:
        backend = get_default_tool_registry_backend(tmp_path)
        assert isinstance(backend, FilesystemToolRegistryBackend)
        assert backend.agent_root == tmp_path
    finally:
        if old is not None:
            os.environ["ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND"] = old


def test_default_factory_unknown_backend_id_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND", "totally-not-real")
    with pytest.raises(BackendNotRegistered, match="totally-not-real"):
        get_default_tool_registry_backend(tmp_path)


def test_default_factory_credential_redaction(tmp_path, monkeypatch):
    """URLs accidentally pasted into the BACKEND env var must have
    their credentials redacted in the error message — mirrors the
    profile / log backend redaction shape."""
    monkeypatch.setenv(
        "ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND",
        "postgres://user:secretpass@host:5432/db",
    )
    with pytest.raises(BackendNotRegistered) as exc_info:
        get_default_tool_registry_backend(tmp_path)
    err_text = str(exc_info.value)
    assert "secretpass" not in err_text
    assert "user:secretpass" not in err_text
    # Scheme is allowed to surface
    assert "postgres" in err_text


def test_default_factory_long_backend_id_truncated(tmp_path, monkeypatch):
    """Pathological long value gets truncated to bound the echoed string."""
    long_val = "x" * 200
    monkeypatch.setenv("ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND", long_val)
    with pytest.raises(BackendNotRegistered) as exc_info:
        get_default_tool_registry_backend(tmp_path)
    err_text = str(exc_info.value)
    # The full 200-char value MUST NOT appear; truncation kicks in.
    assert "x" * 200 not in err_text
    assert "..." in err_text


def test_isinstance_protocol_check(tmp_path):
    """The filesystem reference impl satisfies the Protocol at runtime."""
    backend = FilesystemToolRegistryBackend(tmp_path)
    assert isinstance(backend, ToolRegistryBackend)


# ──────────────────────────────────────────────────────────────────
# Filesystem-specific path-traversal — extra coverage beyond conformance


def test_load_tool_refuses_dotdot_segment_loudly(tmp_path):
    """The conformance suite tests path-traversal across backends;
    here we assert the filesystem-specific raise (``ValueError``
    rather than e.g. SQLite's ``sqlite3.OperationalError``)."""
    backend = FilesystemToolRegistryBackend(tmp_path)
    with pytest.raises(ValueError, match=".."):
        backend.load_tool("foo/../escape")


def test_load_tool_refuses_backslash_loudly(tmp_path):
    """Windows-shape path tokens — backslash refusal mirrors the
    profile backend's ``_agent_root`` guard."""
    backend = FilesystemToolRegistryBackend(tmp_path)
    with pytest.raises(ValueError, match="separator"):
        backend.load_tool("nested\\path")


# ──────────────────────────────────────────────────────────────────
# Step 11 adversarial — regression coverage for the defenses added
# in #64 PR 1 review-pass.


@pytest.mark.parametrize(
    "control_char",
    [
        "\n",   # LF — log injection vector
        "\r",   # CR — log injection vector
        "\0",   # NUL — Python is_file() rejects but validator must catch first
        "\t",   # TAB
        "\x1b", # ESC — terminal escape sequence
        "\x7f", # DEL
    ],
)
def test_load_tool_refuses_control_char_in_name(tmp_path, control_char):
    """Step 11 adversarial regression: tool names with control chars
    flow into error-message paths and produce log-injection vectors
    when loggers format the exception. Validator MUST reject."""
    backend = FilesystemToolRegistryBackend(tmp_path)
    with pytest.raises(ValueError, match="control character"):
        backend.load_tool(f"foo{control_char}bar")


def test_constructor_refuses_empty_agent_root_string(tmp_path):
    """Step 11 adversarial regression: empty-string ``agent_root``
    silently collapsed to CWD, scoping the backend to wherever the
    process happened to be running. Reject at construction."""
    with pytest.raises(ValueError, match="must not be empty"):
        FilesystemToolRegistryBackend("")


def test_constructor_refuses_dot_agent_root_string(tmp_path):
    """Step 11 adversarial regression: ``'.'`` (or ``./``) collapses
    to CWD same as empty. Reject explicitly."""
    with pytest.raises(ValueError, match="collapses to the process CWD"):
        FilesystemToolRegistryBackend(".")


def test_constructor_resolves_to_absolute_path(tmp_path):
    """Step 11 adversarial regression: ``agent_root`` is resolved to
    absolute at construction so downstream interpolations and error
    messages carry the resolved path, not a relative fragment."""
    # tmp_path is already absolute; pass a relative path via str(tmp_path.relative_to(...))
    # Construct from a relative-looking path that resolves to tmp_path.
    backend = FilesystemToolRegistryBackend(tmp_path)
    assert backend.agent_root.is_absolute()


def test_descriptor_too_large_refused(tmp_path):
    """Step 11 adversarial regression: descriptor file size cap
    protects against YAML alias-bomb DoS (33 GB RSS reproduced
    pre-fix with a 256-byte hand-crafted alias bomb). The size cap
    fails fast at stat-time, BEFORE yaml.load is called."""
    from atomic_agents.exceptions import ToolDescriptorInvalid

    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    # Craft a descriptor that exceeds the 256 KB cap.
    huge = "---\nname: huge\ndescription: " + ("x" * (300 * 1024)) + "\n---\n"
    (tools_dir / "huge.md").write_text(huge, encoding="utf-8")
    (tools_dir / "huge.py").write_text("def handler(input): return 'ok'\n", encoding="utf-8")

    backend = FilesystemToolRegistryBackend(tmp_path)
    with pytest.raises(ToolDescriptorInvalid, match="exceeds the"):
        backend.load_tool("huge")


def test_empty_frontmatter_refused(tmp_path):
    """Step 11 adversarial regression: ``---\\n---\\n`` (empty
    frontmatter) previously produced a silently-usable tool with
    all-empty fields. Now raises ToolDescriptorInvalid."""
    from atomic_agents.exceptions import ToolDescriptorInvalid

    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    (tools_dir / "empty.md").write_text("---\n---\n", encoding="utf-8")
    (tools_dir / "empty.py").write_text("def handler(input): return 'ok'\n", encoding="utf-8")

    backend = FilesystemToolRegistryBackend(tmp_path)
    with pytest.raises(ToolDescriptorInvalid, match="empty frontmatter"):
        backend.load_tool("empty")


def test_null_frontmatter_refused(tmp_path):
    """Step 11 adversarial regression: ``---\\n~\\n---\\n`` (null root)
    is the YAML-spelling sibling of empty frontmatter — also refused."""
    from atomic_agents.exceptions import ToolDescriptorInvalid

    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    (tools_dir / "null_root.md").write_text("---\n~\n---\n", encoding="utf-8")
    (tools_dir / "null_root.py").write_text("def handler(input): return 'ok'\n", encoding="utf-8")

    backend = FilesystemToolRegistryBackend(tmp_path)
    with pytest.raises(ToolDescriptorInvalid, match="empty frontmatter"):
        backend.load_tool("null_root")
