"""FilesystemToolRegistryBackend — per-agent directory-tree reference impl.

This is the default backend for single-host deployments. It walks
``<agent_root>/tools/`` for tool descriptors (``<name>.md`` with YAML
frontmatter) paired with handler modules (``<name>.py`` exposing a
callable named ``handler``). The reference impl is **opt-in via
filesystem layout** — when ``tools/`` is empty or absent, ``list_tools()``
returns ``[]`` and no behavior changes for any of the framework's 96
existing ``AtomicAgent(...)`` construction sites.

Descriptor format (``<agent>/tools/<name>.md``):

.. code-block:: text

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

    # Operator notes (ignored by the framework)

    The handler lives in `query_database.py`. See the wiki for our
    query-pattern conventions.

Handler convention (``<agent>/tools/<name>.py``):

.. code-block:: python

    def handler(input: dict) -> str:
        # Operator-defined behavior. Receives the LLM's input dict.
        return f"Result for {input['query']!r}"

Three surface promises hold across PR 1 → PR 2:

1. **No surface change when ``tools/`` is empty or absent.** Every
   existing ``AtomicAgent(...)`` test site has no ``tools/`` directory
   in its fixture — ``list_tools()`` returns ``[]``, the PR 2 wiring
   loop iterates zero times, and the in-memory ``ToolRegistry`` is
   identical to today's.

2. **Lazy handler import (Decision 5 of spec/25).** ``list_tools()``
   parses descriptor frontmatter only — fast on a 50-tool agent. The
   handler module's Python import only fires inside ``load_tool(name)``
   when a specific tool is materialized. Operators with side-effecting
   handler modules (e.g., ``requests.post(...)`` at import time) trip
   the side effect on first dispatch, not on agent construction.

3. **Path-traversal refused at the API boundary** (spec/25 MUST #1).
   Operator-controlled ``name`` is validated against ``/``, ``..``,
   leading ``.``, and backslash BEFORE any disk access. Mirrors the
   ``FilesystemAgentProfileBackend._agent_root`` shape; reuses the
   same security invariants the AgentProfile arc locked in spec/24.

Scope: bound at construction. ``FilesystemToolRegistryBackend(agent_root)``
operates on ONE agent's ``<agent_root>/tools/`` directory. Per-agent
backend instances are the right shape because filesystem tools are
per-agent (vs. ``FilesystemAgentProfileBackend`` which is rooted at
``agents_root`` because agents are siblings). A future PyPI / git
backend would be process-shared because the catalog itself is shared.

Thread-safety: each method opens / reads / closes its own file handles
inside its own call. ``importlib.util.spec_from_file_location`` is
thread-safe at the Python-import-machinery level; the descriptor parse
holds no shared state.
"""

from __future__ import annotations

import hashlib
import importlib.util
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import yaml

# Prefer the C-accelerated YAML loader (libyaml-backed) when available —
# ~11x faster than the pure-Python parser on the frontmatter parse path,
# which fires once per descriptor in ``list_tools()`` and again on
# ``load_tool()``. Falls back to ``SafeLoader`` when libyaml isn't
# linked (some Alpine / minimal-build Python installs). Both are safe
# loaders — no ``!!python/object`` deserialization — so this is a pure
# perf win with zero behavior change.
try:
    from yaml import CSafeLoader as _YamlLoader  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover — fall-through is environment-dependent
    from yaml import SafeLoader as _YamlLoader  # type: ignore[assignment]

from ..exceptions import (
    ToolDescriptorInvalid,
    ToolHandlerImportFailed,
    ToolNotInRegistry,
)
from ..tools import ToolDefinition
from .types import ToolRef, ToolRegistryCapabilities, ValidationResult

_logger = logging.getLogger(__name__)

# Canonical descriptor + handler suffixes. Filesystem layout pairs
# ``<name>.md`` (operator-facing markdown with YAML frontmatter) with
# ``<name>.py`` (handler module exposing a callable named ``handler``).
_DESCRIPTOR_SUFFIX = ".md"
_HANDLER_SUFFIX = ".py"

# Hard upper bound on descriptor file size before yaml.load is even
# attempted. PyYAML's CSafeLoader doesn't cap alias expansion — a
# 256-byte descriptor with a 9-level "billion-laughs" alias bomb
# materializes to gigabytes of in-memory nodes when downstream
# consumers walk the tree. Step 11 adversarial reproduced 33 GB RSS
# with a single hand-crafted descriptor. 256 KB is generous for a
# legitimate descriptor (a typed JSON schema in frontmatter is
# typically < 4 KB) and small enough to fail-fast on a poisoned file
# before parsing.
_MAX_DESCRIPTOR_BYTES = 256 * 1024

# Names that are walked-past but never surface as tools. Underscore-prefix
# is the Python convention for "internal helper"; ``__init__.py`` is the
# Python package marker (operators may use ``tools/`` as a Python
# package to share helpers across handlers).
_HIDDEN_PREFIX = "."
_HELPER_PREFIX = "_"

# Path-traversal + control-character validation for operator-controlled
# ``name`` — refuse problematic shapes BEFORE any disk access (spec/25
# MUST #1). Mirrors ``FilesystemAgentProfileBackend._agent_root``.
#
# Step 11 adversarial finding: a deny-list (``/``, ``\\``, ``.``, ``..``)
# left control characters (``\n``, ``\r``, ``\0``, ``\t``, ``\x1b``,
# etc.) passing through. Embedded newlines then surface in error-
# message paths (``tool 'foo' not found at /agent/tools/foo\nbar.md``)
# producing log-injection vectors when the framework's loggers format
# the exception. Adding control-char refusal to the deny-list closes
# this without restricting the legitimate-name space (alphanumeric +
# dashes / underscores / dots).
def _validate_tool_name(name: str) -> None:
    """Reject ``name`` that could escape ``<agent_root>/tools/`` or
    smuggle control characters into derived paths.

    Spec/25 MUST #1 — backends with native filesystem semantics MUST
    validate ``name`` at the API boundary. The check refuses:

    - Empty strings
    - Path separators (``/``, ``\\``)
    - Leading dots (hidden-file traversal + Python helper convention)
    - Parent-dir tokens (``..``)
    - Control characters (NUL through ``\\x1f`` and DEL ``\\x7f``) —
      Step 11 adversarial caught a log-injection path where embedded
      newlines/CR in a name interpolated into ``ToolNotInRegistry``
      error-message paths split log lines.

    Raises ``ValueError`` for a consistent caller-facing shape
    regardless of the specific failure mode. (``ValueError`` rather
    than a tagged exception class matches the
    ``FilesystemAgentProfileBackend._agent_root`` precedent —
    consistent across sibling Protocol backends.)
    """
    if not name:
        raise ValueError("tool name must not be empty")
    if "/" in name or "\\" in name:
        raise ValueError(
            f"tool name {name!r} contains a path separator — "
            f"filesystem backend rejects names with '/' or '\\'"
        )
    if name.startswith("."):
        raise ValueError(
            f"tool name {name!r} starts with '.' — refused to "
            f"prevent hidden-file traversal"
        )
    if ".." in name:
        raise ValueError(
            f"tool name {name!r} contains '..' — path traversal refused"
        )
    # Reject any control character (C0 range 0x00-0x1f and DEL 0x7f).
    # Operator-controlled names should be plain identifiers; anything
    # else is a typo or an attack.
    for ch in name:
        if ord(ch) < 0x20 or ord(ch) == 0x7f:
            raise ValueError(
                f"tool name {name!r} contains a control character "
                f"(0x{ord(ch):02x}) — refused to prevent log injection "
                f"and path-token splitting"
            )


# Tool names that begin with one of the helper / hidden prefixes are
# walked past at ``list_tools`` time. The check is separate from
# ``_validate_tool_name`` because the latter raises (rejecting a
# malicious lookup) while this one silently skips (rejecting a benign
# helper file that happens to live in ``tools/``).
def _is_helper_or_hidden(stem: str) -> bool:
    """Return True for stems we exclude from ``list_tools`` enumeration."""
    return (
        stem.startswith(_HIDDEN_PREFIX)
        or stem.startswith(_HELPER_PREFIX)
        or stem == "__init__"
    )


@dataclass(frozen=True)
class _ParsedDescriptor:
    """Internal helper — frontmatter + body extracted from ``<name>.md``.

    ``body`` is reserved for PR 3+ operator-notes surfacing (e.g., the
    descriptor's prose appearing in CLI inspection output). PR 1's
    framework path consumes ``frontmatter`` only; keeping ``body``
    populated here means PR 3 doesn't need a parser re-spin.
    """

    frontmatter: dict[str, Any]
    body: str
    descriptor_path: Path


class FilesystemToolRegistryBackend:
    """Directory-tree ``ToolRegistryBackend`` — per-agent ``tools/`` walk.

    Conforms to the ``ToolRegistryBackend`` Protocol. Constructed once
    per agent; the ``agent_root`` is the agent's own directory under
    which ``tools/<name>.md`` + ``tools/<name>.py`` live. PR 2 wires
    the framework's default tool registry backend at module import via
    ``get_default_tool_registry_backend(agent_root)``.

    Args:
        agent_root: directory whose ``tools/`` subdirectory contains
            this agent's descriptor + handler files. The directory
            MAY not exist at construction time — operators with no
            ``tools/`` directory get a fully-functional empty backend
            (``list_tools() -> []``). This is intentional: every
            existing fixture-built agent works without modification,
            and there's no operator action required to "opt out" of
            filesystem tools.
    """

    @property
    def backend_id(self) -> str:
        return "filesystem"

    def __init__(self, agent_root: Path | str) -> None:
        # ``agent_root`` may not exist on disk (test fixtures construct
        # backends before populating the dir; valid use case). The
        # Protocol contract only requires ``list_tools`` / ``load_tool``
        # work — both tolerate the missing dir by returning empty /
        # raising ``ToolNotInRegistry``.
        #
        # BUT: empty string and ``"."`` MUST be rejected — they collapse
        # to CWD silently and the backend would walk whatever happened
        # to live in the process's working directory's ``tools/``. Step
        # 11 adversarial caught this as an operator-misconfiguration
        # silent-failure shape. ``Path.resolve()`` produces an absolute
        # path so downstream interpolations and error messages carry
        # the resolved location, not a relative fragment.
        if isinstance(agent_root, str) and not agent_root.strip():
            raise ValueError(
                "FilesystemToolRegistryBackend agent_root must not be "
                "empty — passing '' would silently scope the backend "
                "to the process's current working directory"
            )
        resolved = Path(agent_root).resolve(strict=False)
        # The resolved path is always absolute; reject any bizarre case
        # (e.g., Path('').resolve() yields the current dir which we
        # treat as operator error rather than silent CWD-acceptance).
        if str(resolved) == str(Path.cwd()) and str(agent_root) in {"", ".", "./"}:
            raise ValueError(
                f"FilesystemToolRegistryBackend agent_root={agent_root!r} "
                f"collapses to the process CWD — pass an absolute path "
                f"or an explicit relative-to-known-root value"
            )
        self._agent_root = resolved

    @property
    def agent_root(self) -> Path:
        """The agent directory this backend is bound to. Read-only after construction."""
        return self._agent_root

    @property
    def tools_dir(self) -> Path:
        """The ``<agent_root>/tools/`` directory. MAY not exist."""
        return self._agent_root / "tools"

    # ────────────────────────────────────────────────────────────
    # Discovery

    def list_tools(self) -> list[ToolRef]:
        """Walk ``<agent_root>/tools/*.md`` and return lex-ordered ToolRefs.

        Empty list when ``tools/`` is absent — this preserves byte-
        identical agent-construction behavior for every existing
        fixture (Decision 8 of spec/25 — surface change is opt-in via
        filesystem layout). Descriptors that fail to parse are SKIPPED
        with a debug log line; operators who want the parse error
        surfaced call ``validate(name)`` for the specific tool.

        Step 11 adversarial regression fix: ``OSError`` from
        ``iterdir()`` (chmod 000 on the dir, filesystem error,
        unreadable mount) is treated as 'this tools/ dir is not
        usable' and returns ``[]`` — same shape as 'tools/ absent'.
        Without this defense, a single misconfigured permission on
        one agent's ``tools/`` would block EVERY ``AtomicAgent``
        construction process-wide (the wiring loop at
        ``agent.py:381`` calls this method unconditionally on every
        agent build). The behavior is logged at WARN so operators
        triaging "why is my tool not showing up?" find the cause.
        """
        tools_dir = self.tools_dir
        if not tools_dir.is_dir():
            return []

        try:
            entries = sorted(tools_dir.iterdir())
        except OSError as exc:
            _logger.warning(
                "could not enumerate tool registry at %s: %s: %s — "
                "treating as empty",
                tools_dir,
                type(exc).__name__,
                exc,
            )
            return []

        refs: list[ToolRef] = []
        for entry in entries:
            if not entry.is_file():
                continue
            if entry.suffix != _DESCRIPTOR_SUFFIX:
                continue
            stem = entry.stem
            if _is_helper_or_hidden(stem):
                continue
            try:
                parsed = _parse_descriptor(entry)
            except ToolDescriptorInvalid as exc:
                # ``list_tools`` is the discovery surface — a
                # malformed descriptor shouldn't poison the entire
                # catalog. Operators triaging a failure call
                # ``validate(stem)`` which surfaces the parse error
                # via ``ValidationResult.errors`` rather than raising.
                _logger.debug(
                    "skipping malformed descriptor %s: %s", entry, exc
                )
                continue
            refs.append(_ref_from_parsed(stem, parsed))
        # ``sorted(tools_dir.iterdir())`` already orders by entry name,
        # but explicit sort by ToolRef.name guards against subtle
        # filesystem-iter ordering surprises on case-insensitive
        # filesystems.
        refs.sort(key=lambda r: r.name)
        return refs

    def load_tool(self, name: str) -> ToolDefinition:
        """Parse ``<tools>/<name>.md`` + import ``<tools>/<name>.py``.

        Returns a ``ToolDefinition`` carrying the handler callable +
        input_schema + description + classification. The handler is
        imported lazily — ``list_tools`` doesn't pay the import cost.
        """
        _validate_tool_name(name)

        descriptor_path = self.tools_dir / f"{name}{_DESCRIPTOR_SUFFIX}"
        if not descriptor_path.is_file():
            raise ToolNotInRegistry(
                f"tool {name!r} not found in filesystem registry at "
                f"{descriptor_path} — available: "
                f"{[ref.name for ref in self.list_tools()]}"
            )

        parsed = _parse_descriptor(descriptor_path)
        handler = _import_handler(self.tools_dir / f"{name}{_HANDLER_SUFFIX}", name)

        # Resolve fields from the descriptor's frontmatter, defaulting
        # to safe values when absent. The frontmatter's ``name`` MUST
        # match the file stem when present (defensive — catches
        # operator typos where the descriptor moved files but the
        # frontmatter wasn't updated). Empty / absent descriptor name
        # falls back to the file stem so single-source-of-truth stays
        # the filename.
        descriptor_name = parsed.frontmatter.get("name") or name
        if descriptor_name != name:
            raise ToolDescriptorInvalid(
                f"tool {name!r} descriptor at {descriptor_path} declares "
                f"name={descriptor_name!r} — must match file stem"
            )

        description = str(parsed.frontmatter.get("description", "") or "")
        classification = parsed.frontmatter.get("classification")
        if classification is not None:
            classification = str(classification)

        input_schema = parsed.frontmatter.get("input_schema") or {
            "type": "object",
            "properties": {},
            "required": [],
        }
        if not isinstance(input_schema, dict):
            raise ToolDescriptorInvalid(
                f"tool {name!r} descriptor at {descriptor_path} has "
                f"input_schema of type {type(input_schema).__name__} — "
                f"expected dict"
            )

        return ToolDefinition(
            name=name,
            description=description,
            input_schema=input_schema,
            handler=handler,
            classification=classification,
        )

    def validate(self, name: str) -> ValidationResult:
        """Static check on the named tool — does NOT execute the handler.

        Spec/25 Decision 6: ``validate()`` is the audit-time call;
        parse + import + signature-check only. Operator-supplied
        handlers with side-effecting top-level code will still trip
        their side effect at import time (this is intentional —
        ``validate`` is the surface that exposes such handlers to the
        operator's eyes; the alternative is silent acceptance until
        first dispatch).
        """
        errors: list[str] = []
        warnings: list[str] = []

        try:
            _validate_tool_name(name)
        except ValueError as exc:
            return ValidationResult(ok=False, errors=[str(exc)], warnings=[])

        descriptor_path = self.tools_dir / f"{name}{_DESCRIPTOR_SUFFIX}"
        if not descriptor_path.is_file():
            return ValidationResult(
                ok=False,
                errors=[
                    f"tool {name!r} not in registry (no descriptor at {descriptor_path})"
                ],
                warnings=[],
            )

        try:
            parsed = _parse_descriptor(descriptor_path)
        except ToolDescriptorInvalid as exc:
            return ValidationResult(ok=False, errors=[str(exc)], warnings=[])

        descriptor_name = parsed.frontmatter.get("name")
        if descriptor_name is not None and descriptor_name != name:
            errors.append(
                f"descriptor declares name={descriptor_name!r}; expected {name!r}"
            )

        if not parsed.frontmatter.get("description"):
            warnings.append(
                f"tool {name!r} has no description — LLM-routing quality degrades"
            )

        classification = parsed.frontmatter.get("classification")
        if classification is None:
            warnings.append(
                f"tool {name!r} has no classification — judge layer will "
                f"default to external_side_effect at dispatch"
            )
        elif classification not in {
            "read_only",
            "reversible_write",
            "external_side_effect",
            "high_risk",
        }:
            errors.append(
                f"tool {name!r} classification {classification!r} is not a "
                f"valid ActionClass — must be one of read_only, "
                f"reversible_write, external_side_effect, high_risk"
            )

        handler_path = self.tools_dir / f"{name}{_HANDLER_SUFFIX}"
        if not handler_path.is_file():
            errors.append(
                f"tool {name!r} handler module missing at {handler_path}"
            )
        else:
            try:
                handler = _import_handler(handler_path, name)
            except ToolHandlerImportFailed as exc:
                errors.append(str(exc))
            else:
                if not callable(handler):
                    errors.append(
                        f"tool {name!r} handler at {handler_path} is not "
                        f"callable (got {type(handler).__name__})"
                    )

        return ValidationResult(ok=not errors, errors=errors, warnings=warnings)

    # ────────────────────────────────────────────────────────────
    # Capability-gated mutation — filesystem declares False

    def install(self, source: str, version: str | None = None) -> ToolRef:
        """Filesystem backend does NOT support install — raises.

        Spec/25 Decision 7: operators install by editing
        ``<agent>/tools/`` directly. Inventing an ``install()`` here
        would either be a network call from a "filesystem" backend
        (surprising) or a ``cp`` wrapper (superfluous). Future PyPI /
        git / HTTP backends own the install seam.
        """
        raise NotImplementedError(
            "FilesystemToolRegistryBackend does not support install — "
            "operators install by writing <agent>/tools/<name>.md + "
            "<agent>/tools/<name>.py directly. Future PyPI / git / "
            "HTTP backends own the install seam."
        )

    def uninstall(self, name: str) -> None:
        """Filesystem backend does NOT support uninstall — raises.

        Same rationale as ``install`` — operators delete the descriptor
        + handler files directly.
        """
        raise NotImplementedError(
            "FilesystemToolRegistryBackend does not support uninstall — "
            "operators delete <agent>/tools/<name>.md + "
            "<agent>/tools/<name>.py directly."
        )

    # ────────────────────────────────────────────────────────────
    # Reserved skill catalog — filesystem declares False

    def list_skills_catalog(self) -> list[ToolRef]:
        """Filesystem backend does NOT publish a skill catalog — raises.

        Spec/25 Decision 2: per-agent **mounted** skills live on the
        ``AgentProfileBackend.list_skills(agent_id)`` surface (locked
        in spec/24). The ToolRegistryBackend's skill-catalog surface
        is reserved for future PyPI / git / company-internal-HTTP
        backends that publish installable skills (analogous to
        installable tools).
        """
        raise NotImplementedError(
            "FilesystemToolRegistryBackend does not publish a skill "
            "catalog — per-agent mounted skills live on AgentProfileBackend "
            "(spec/24). The catalog surface is reserved for future "
            "PyPI / git / HTTP backends (spec/25 Decision 2)."
        )

    def load_skill_catalog_body(self, name: str) -> str:
        """Filesystem backend does NOT publish a skill catalog — raises."""
        raise NotImplementedError(
            "FilesystemToolRegistryBackend does not publish a skill "
            "catalog body — see list_skills_catalog rationale (spec/25 "
            "Decision 2)."
        )

    # ────────────────────────────────────────────────────────────
    # Capabilities

    def capabilities(self) -> ToolRegistryCapabilities:
        return ToolRegistryCapabilities(
            # Operator's text editor + ``cp`` are the install primitive
            # for the filesystem backend (Decision 7 of spec/25).
            supports_install=False,
            supports_uninstall=False,
            # ``ToolRef.version`` round-trips but filesystem has no
            # version semantics (Decision 4 of spec/25). Future PyPI /
            # git backends flip this True.
            supports_versioning=False,
            # ``validate()`` is static-only on this backend; sandboxed
            # validation is reserved for a future capability (Decision
            # 6 of spec/25).
            supports_sandbox_validate=False,
            # Skill catalog reserved (Decision 2 of spec/25). Future
            # PyPI / git / HTTP backends own this seam; per-agent
            # mounted skills stay on AgentProfileBackend.
            supports_skills_catalog=False,
            # The <agent>/tools/ directory itself is durable — files
            # written by an operator survive restart.
            durable=True,
        )


# ────────────────────────────────────────────────────────────────────
# Module-level helpers


# YAML frontmatter delimiter — same shape captures use (spec/03) and
# skills use (spec/18). Three hyphens at start-of-line, body following.
_FRONTMATTER_DELIMITER = re.compile(r"^---\s*$", re.MULTILINE)


def _parse_descriptor(descriptor_path: Path) -> _ParsedDescriptor:
    """Parse ``<tools>/<name>.md`` frontmatter + body.

    Raises ``ToolDescriptorInvalid`` for:
    - Missing/malformed frontmatter delimiters
    - YAML parse errors (frontmatter or input_schema)
    - Frontmatter root that isn't a dict

    The body is returned alongside the parsed frontmatter for future
    use (PR 3+ may surface descriptor bodies as operator notes); PR 1's
    framework path consumes only the frontmatter.
    """
    try:
        file_size = descriptor_path.stat().st_size
    except OSError as exc:
        raise ToolDescriptorInvalid(
            f"could not stat tool descriptor at {descriptor_path}: {exc}"
        ) from exc
    if file_size > _MAX_DESCRIPTOR_BYTES:
        raise ToolDescriptorInvalid(
            f"tool descriptor at {descriptor_path} is {file_size} bytes — "
            f"exceeds the {_MAX_DESCRIPTOR_BYTES}-byte limit. Operators "
            f"with legitimately-large schemas should split into separate "
            f"tools."
        )
    try:
        raw = descriptor_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ToolDescriptorInvalid(
            f"could not read tool descriptor at {descriptor_path}: {exc}"
        ) from exc

    # Frontmatter MUST open with ``---`` on the first line — relaxed
    # to allow leading whitespace-only lines so editors that
    # accidentally prepend a newline don't break the parse.
    stripped = raw.lstrip("\n\r")
    if not stripped.startswith("---"):
        raise ToolDescriptorInvalid(
            f"tool descriptor at {descriptor_path} is missing YAML "
            f"frontmatter — descriptors MUST start with '---'"
        )

    # Find the closing ``---`` delimiter for the frontmatter block.
    matches = list(_FRONTMATTER_DELIMITER.finditer(stripped))
    if len(matches) < 2:
        raise ToolDescriptorInvalid(
            f"tool descriptor at {descriptor_path} is missing closing "
            f"frontmatter delimiter '---'"
        )

    frontmatter_text = stripped[matches[0].end():matches[1].start()]
    body = stripped[matches[1].end():].lstrip("\n\r")

    try:
        frontmatter = yaml.load(frontmatter_text, Loader=_YamlLoader)
    except yaml.YAMLError as exc:
        raise ToolDescriptorInvalid(
            f"tool descriptor at {descriptor_path} has invalid YAML "
            f"frontmatter: {exc}"
        ) from exc

    # Step 11 adversarial caught an empty / null frontmatter silently
    # producing a usable tool with all-empty fields. Reject it
    # explicitly — descriptor MUST declare at minimum a description
    # OR a name field (the file stem is the canonical name source,
    # so even a minimal descriptor needs SOMETHING the operator
    # authored).
    if frontmatter is None:
        raise ToolDescriptorInvalid(
            f"tool descriptor at {descriptor_path} has empty frontmatter "
            f"— must declare at minimum a description field"
        )
    if not isinstance(frontmatter, dict):
        raise ToolDescriptorInvalid(
            f"tool descriptor at {descriptor_path} has frontmatter of "
            f"type {type(frontmatter).__name__} — expected mapping"
        )

    return _ParsedDescriptor(
        frontmatter=frontmatter,
        body=body,
        descriptor_path=descriptor_path,
    )


def _ref_from_parsed(name: str, parsed: _ParsedDescriptor) -> ToolRef:
    """Build a ``ToolRef`` from a successfully-parsed descriptor.

    ``version`` is always ``None`` on filesystem (spec/25 Decision 4 —
    no version semantics in the on-disk layout). ``source`` carries
    the descriptor's filesystem path for diagnostic audit.
    """
    description = str(parsed.frontmatter.get("description", "") or "")
    classification = parsed.frontmatter.get("classification")
    if classification is not None:
        classification = str(classification)
    return ToolRef(
        name=name,
        description=description,
        classification=classification,
        version=None,
        source=str(parsed.descriptor_path),
    )


def _import_handler(handler_path: Path, name: str) -> Callable[[dict], Any]:
    """Import ``<tools>/<name>.py`` and return its ``handler`` callable.

    Raises ``ToolHandlerImportFailed`` for any import failure or for a
    module that imports cleanly but lacks the ``handler`` symbol.
    Spec/25 Decision 5: lazy import — caller-driven, not
    construction-driven. Operators with side-effecting top-level code
    will still trip the side effect here on first dispatch.
    """
    if not handler_path.is_file():
        raise ToolHandlerImportFailed(
            f"handler module for tool {name!r} not found at {handler_path}"
        )

    # Module qualname is for traceback / __name__ legibility only —
    # ``spec.loader.exec_module()`` does NOT populate ``sys.modules`` for
    # specs built via ``spec_from_file_location`` (each call returns a
    # fresh module). Use a SHA-256 of the absolute path so the qualname
    # is deterministic across process restarts AND collision-free under
    # any operator's directory layout. ``hash()`` of a string is
    # PYTHONHASHSEED-randomized per process and would yield a different
    # qualname every restart — visually surprising in tracebacks.
    _path_digest = hashlib.sha256(
        str(handler_path.absolute()).encode("utf-8")
    ).hexdigest()[:16]
    module_qualname = (
        f"atomic_agents._registry_handlers.{_path_digest}_{name}"
    )

    spec = importlib.util.spec_from_file_location(module_qualname, handler_path)
    if spec is None or spec.loader is None:
        raise ToolHandlerImportFailed(
            f"could not build import spec for handler at {handler_path}"
        )

    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise ToolHandlerImportFailed(
            f"handler module {handler_path} failed to import for tool "
            f"{name!r}: {type(exc).__name__}: {exc}"
        ) from exc

    handler = getattr(module, "handler", None)
    if handler is None:
        raise ToolHandlerImportFailed(
            f"handler module {handler_path} for tool {name!r} does not "
            f"expose a `handler` callable"
        )

    return handler
