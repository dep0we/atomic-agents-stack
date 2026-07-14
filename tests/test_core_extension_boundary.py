"""Import-direction guard: core must never reach into fleet/extension modules (#736).

Phase 2a of the core/extension split separates fleet-shaped tooling
(``atomic_agents/advisor/``, ``atomic_agents/dashboard/``,
``atomic_agents/manage/``) from the core framework so it can eventually live
in its own installable package. The one place core is allowed to reach into
that tooling today is a LAZY import inside ``cli.py``'s ``manage`` dispatch
handler (``_cmd_manage``) -- and even that import only fires when an operator
actually runs ``atomic-agents manage ...``, never at module import time.

This test statically scans every other core module's source for imports of
``atomic_agents.advisor`` / ``atomic_agents.dashboard`` / ``atomic_agents.manage``
(absolute or relative, module-level or inside a function body -- a lazy
import inside a dispatch function still counts as that module importing the
extension). It explicitly covers ``cli.py`` and ``doctor.py`` per #736, and
folds in every other core file for good measure.

``deploy`` is a core command (the single-agent "run my agent as a service"
story) -- importing it is expected and must NOT be flagged.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import atomic_agents

REPO_ROOT = Path(__file__).parent.parent
PACKAGE_ROOT = Path(atomic_agents.__file__).parent

# The three fleet-shaped packages core must never import. NOT "deploy" --
# deploy stays core (see module docstring) and importing it is fine.
FORBIDDEN_EXTENSION_PACKAGES = frozenset({"advisor", "dashboard", "manage"})

# Directories under atomic_agents/ that are themselves the extension
# packages -- their own internal imports of each other / self-imports are
# not a core->extension violation and are excluded from the scan.
_EXCLUDED_DIR_NAMES = FORBIDDEN_EXTENSION_PACKAGES | {"__pycache__"}


def _core_python_files() -> list[Path]:
    """Every .py file under atomic_agents/ that is NOT inside an extension dir."""
    files: list[Path] = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        rel_parts = path.relative_to(PACKAGE_ROOT).parts
        # Exclude anything living under an extension package directory
        # (e.g. atomic_agents/manage/foo.py) -- only the REST of core is in scope.
        if any(part in _EXCLUDED_DIR_NAMES for part in rel_parts[:-1]):
            continue
        files.append(path)
    return files


def _module_root_after_atomic_agents(module: str) -> str | None:
    """First dotted component after 'atomic_agents.' in an absolute import,
    or None if `module` doesn't start with 'atomic_agents.' (or is bare
    'atomic_agents', which is not a submodule reference)."""
    prefix = "atomic_agents."
    if not module.startswith(prefix):
        return None
    return module[len(prefix) :].split(".", 1)[0]


def _forbidden_root_of_module_string(module_str: str) -> str | None:
    """Given a dynamic-import module string literal, return the forbidden
    extension root it targets, or None.

    Matches the same two shapes the static scanner treats as
    ``atomic_agents``-internal:
      - absolute: ``"atomic_agents.manage"`` / ``"atomic_agents.manage.foo"``
      - relative: ``".manage"`` / ``"..dashboard.costs"`` (as passed to
        ``importlib.import_module(".manage", __package__)`` from a module
        inside the package)

    A bare ``"manage"`` with no dots and no ``atomic_agents.`` prefix resolves
    to a top-level third-party module, NOT our extension package, so it is
    deliberately NOT flagged -- consistent with the static-import handling.
    """
    prefix = "atomic_agents."
    if module_str.startswith(prefix):
        root = module_str[len(prefix) :].split(".", 1)[0]
        return root if root in FORBIDDEN_EXTENSION_PACKAGES else None
    if module_str.startswith("."):
        # Relative dynamic import: strip leading dots, take first component.
        stripped = module_str.lstrip(".")
        root = stripped.split(".", 1)[0] if stripped else None
        return root if root in FORBIDDEN_EXTENSION_PACKAGES else None
    return None


def _string_literal(node: ast.expr) -> str | None:
    """Return the value of a string-literal AST node, or None if not a
    plain string constant (a dynamically-computed module name is out of
    scope for a static scanner and left for the runtime sys.modules guard)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def find_forbidden_imports(source: str, filename: str) -> list[str]:
    """Return a human-readable violation string per forbidden import found.

    Handles all four STATIC import shapes:
      - ``import atomic_agents.manage``               (absolute)
      - ``from atomic_agents.manage import X``         (absolute from)
      - ``from atomic_agents import manage``           (absolute from, bare pkg)
      - ``from .manage import X`` / ``from . import manage``  (relative)

    ...plus two DYNAMIC import shapes with a string-literal first argument:
      - ``importlib.import_module("atomic_agents.manage")`` (also the bare
        ``import_module(...)`` name, and relative ``import_module(".manage")``)
      - ``__import__("atomic_agents.dashboard")``

    Relative imports are resolved purely on dotted-name shape (the first
    module component, or the imported name when ``from . import X``) since
    every file scanned here already lives inside the ``atomic_agents``
    package -- a relative import can only ever resolve to a sibling/cousin
    module within it. Dynamically-computed module names (non-string-literal
    args) are out of scope for a static scanner and are covered instead by
    the runtime ``sys.modules`` guard in test_quickstart_core_only.py.
    """
    tree = ast.parse(source, filename=filename)
    violations: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = _module_root_after_atomic_agents(alias.name)
                if root in FORBIDDEN_EXTENSION_PACKAGES:
                    violations.append(
                        f"{filename}:{node.lineno}: `import {alias.name}`"
                    )

        elif isinstance(node, ast.Call):
            # Dynamic imports: importlib.import_module(...) / import_module(...)
            # / __import__(...) with a string-literal first argument.
            func = node.func
            is_import_module = (
                isinstance(func, ast.Attribute) and func.attr == "import_module"
            ) or (isinstance(func, ast.Name) and func.id == "import_module")
            is_dunder_import = isinstance(func, ast.Name) and func.id == "__import__"
            if (is_import_module or is_dunder_import) and node.args:
                literal = _string_literal(node.args[0])
                if literal is not None:
                    root = _forbidden_root_of_module_string(literal)
                    if root is not None:
                        call_name = (
                            "__import__" if is_dunder_import else "import_module"
                        )
                        violations.append(
                            f"{filename}:{node.lineno}: `{call_name}({literal!r})`"
                        )

        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                # Relative import: `from .manage import x`, `from ..manage import x`,
                # or `from . import manage`.
                module = node.module or ""
                first_component = module.split(".", 1)[0] if module else None
                if first_component in FORBIDDEN_EXTENSION_PACKAGES:
                    dots = "." * node.level
                    names = ", ".join(alias.name for alias in node.names)
                    violations.append(
                        f"{filename}:{node.lineno}: "
                        f"`from {dots}{module} import {names}`"
                    )
                elif not module:
                    dots = "." * node.level
                    for alias in node.names:
                        if alias.name in FORBIDDEN_EXTENSION_PACKAGES:
                            violations.append(
                                f"{filename}:{node.lineno}: "
                                f"`from {dots} import {alias.name}`"
                            )
            else:
                module = node.module or ""
                root = _module_root_after_atomic_agents(module)
                if root in FORBIDDEN_EXTENSION_PACKAGES:
                    names = ", ".join(alias.name for alias in node.names)
                    violations.append(
                        f"{filename}:{node.lineno}: `from {module} import {names}`"
                    )
                elif module == "atomic_agents":
                    for alias in node.names:
                        if alias.name in FORBIDDEN_EXTENSION_PACKAGES:
                            violations.append(
                                f"{filename}:{node.lineno}: "
                                f"`from atomic_agents import {alias.name}`"
                            )

    return violations


# ---------------------------------------------------------------------------
# Scope helpers -- used by the cli.py assertion to prove the ONE allowed
# `.manage` import is LAZY (nested inside _cmd_manage), not module-level.
# ---------------------------------------------------------------------------


def _manage_importfrom_nodes(source: str, filename: str) -> list[ast.ImportFrom]:
    """Every `from .manage import ...` (relative, first component 'manage')
    ImportFrom node in the source."""
    tree = ast.parse(source, filename=filename)
    nodes: list[ast.ImportFrom] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            first = module.split(".", 1)[0] if module else None
            if node.level and node.level > 0 and first == "manage":
                nodes.append(node)
    return nodes


def _module_level_import_linenos(source: str) -> set[int]:
    """Line numbers of every import statement at MODULE top level (direct
    children of the module body, i.e. not nested in any function/class)."""
    tree = ast.parse(source)
    linenos: set[int] = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            linenos.add(node.lineno)
    return linenos


def _enclosing_function_name(source: str, target_lineno: int) -> str | None:
    """Name of the innermost function whose body encloses target_lineno, or
    None if the line is not inside any function (i.e. module-level)."""
    tree = ast.parse(source)
    best: tuple[int, str] | None = None  # (span, name) -- smallest span wins
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = node.lineno
            end = getattr(node, "end_lineno", start)
            if start <= target_lineno <= end:
                span = end - start
                if best is None or span < best[0]:
                    best = (span, node.name)
    return best[1] if best is not None else None


# ---------------------------------------------------------------------------
# The guard itself
# ---------------------------------------------------------------------------

# The one documented, load-bearing exception: cli.py's `manage` dispatch
# handler (`_cmd_manage`) lazy-imports `.manage` -- but only inside that
# function body, and only so `atomic-agents manage ...` can reach
# `run_manage` without the agent_registry/logs/principal import cost landing
# on every OTHER CLI invocation (principle #6 / spec/55 note). manage is
# fleet-shaped and stays in-repo for now (per #736 scope); this is the single
# known instance where "core imports a fleet package" is expected today.
# cli.py is excluded from this broad sweep so the exception can be scoped
# precisely -- test_named_core_files_import_nothing_from_fleet below asserts
# it is EXACTLY this one lazy `.manage` import and nothing else (no
# module-top import, no advisor/dashboard import), so the exception can't
# silently widen.
_EXPECTED_EXCEPTIONS = frozenset({"atomic_agents/cli.py"})


def test_no_core_module_imports_fleet_extension_packages():
    """No file under atomic_agents/ (outside advisor/dashboard/manage
    themselves, and outside the one documented cli.py exception above) may
    import advisor, dashboard, or manage -- at module top OR lazily inside a
    function body."""
    all_violations: list[str] = []
    for path in _core_python_files():
        rel = str(path.relative_to(REPO_ROOT))
        if rel in _EXPECTED_EXCEPTIONS:
            continue
        source = path.read_text(encoding="utf-8")
        all_violations.extend(find_forbidden_imports(source, rel))

    assert not all_violations, (
        "Core module(s) import a fleet/extension package "
        "(advisor/dashboard/manage). Core must reach the fleet layer, if at "
        "all, only via a lazy import inside cli.py's manage dispatch "
        "handler.\n" + "\n".join(all_violations)
    )


@pytest.mark.parametrize("relpath", ["atomic_agents/cli.py", "atomic_agents/doctor.py"])
def test_named_core_files_import_nothing_from_fleet(relpath: str):
    """Explicit per-file coverage for cli.py and doctor.py, per #736.

    cli.py is the one file that used to hardcode `from .manage import
    run_manage` -- after the #736 registration-mechanism refactor it must
    have ZERO references to manage/advisor/dashboard, not even inside a lazy
    dispatch function (the manage dispatch now goes through the same
    `_cmd_manage` handler, which still lazy-imports `.manage` -- see the
    companion assertion below for that one intentional, load-bearing
    exception, kept isolated so the assertion above stays a hard zero for
    doctor.py without special-casing).
    """
    path = REPO_ROOT / relpath
    source = path.read_text(encoding="utf-8")
    violations = find_forbidden_imports(source, relpath)

    if relpath == "atomic_agents/doctor.py":
        assert not violations, (
            "doctor.py must never import advisor/dashboard/manage:\n"
            + "\n".join(violations)
        )
        return

    # cli.py: the ONLY allowed hit is the lazy `from .manage import run_manage`
    # inside `_cmd_manage`'s dispatch body (never at module top, never for
    # advisor/dashboard). Anything else is a regression.
    unexpected = [v for v in violations if "run_manage" not in v]
    assert not unexpected, (
        "cli.py has an unexpected fleet-extension import beyond the single "
        "lazy `.manage` import inside _cmd_manage:\n" + "\n".join(unexpected)
    )
    assert len(violations) <= 1, (
        "cli.py should import `.manage` at most once (inside _cmd_manage's "
        f"lazy dispatch import); found {len(violations)}:\n" + "\n".join(violations)
    )
    if violations:
        assert "manage" in violations[0] and "run_manage" in violations[0]

    # ...and it must be LAZY (nested inside _cmd_manage's body), not a
    # module-level import. A module-level `from .manage import run_manage`
    # would satisfy the "at most one, contains run_manage" checks above but
    # defeat the whole progressive-disclosure point -- so assert on scope,
    # not just count. This is what makes a regression to a module-top import
    # FAIL the test.
    manage_imports = _manage_importfrom_nodes(source, relpath)
    assert manage_imports, (
        "expected exactly one `from .manage import ...` in cli.py; found none "
        "(if the import was removed entirely, update this test)"
    )
    module_level = _module_level_import_linenos(source)
    for imp in manage_imports:
        assert imp.lineno not in module_level, (
            f"cli.py has a MODULE-LEVEL `from .manage import ...` at line "
            f"{imp.lineno}; the manage import must be lazy (nested inside "
            f"_cmd_manage's function body), never at module top."
        )
    enclosing = _enclosing_function_name(source, manage_imports[0].lineno)
    assert enclosing == "_cmd_manage", (
        f"cli.py's `.manage` import is inside {enclosing!r}, expected it "
        f"nested inside `_cmd_manage`'s dispatch body."
    )


# ---------------------------------------------------------------------------
# Negative control: prove the scanner actually catches what it claims to.
# A guard that never fires on synthetic bad input could be silently vacuous.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "import atomic_agents.manage\n",
        "from atomic_agents.manage import run_manage\n",
        "from atomic_agents import manage\n",
        "from .manage import run_manage\n",
        "from . import manage\n",
        "from ..dashboard import costs\n",
        "import atomic_agents.advisor.score\n",
        # Dynamic imports (finding #3): importlib.import_module + __import__.
        'import importlib\nimportlib.import_module("atomic_agents.manage")\n',
        'from importlib import import_module\nimport_module("atomic_agents.dashboard")\n',
        'importlib.import_module("atomic_agents.advisor.score")\n',
        'importlib.import_module(".manage", __package__)\n',
        'importlib.import_module("..dashboard.costs", __package__)\n',
        '__import__("atomic_agents.manage")\n',
    ],
)
def test_scanner_flags_each_forbidden_import_shape(source: str):
    violations = find_forbidden_imports(source, "synthetic.py")
    assert violations, f"scanner failed to flag forbidden import in: {source!r}"


@pytest.mark.parametrize(
    "source",
    [
        "import atomic_agents.deploy\n",  # deploy is core -- must NOT be flagged
        "from atomic_agents.deploy import deploy\n",
        "from . import deploy\n",
        "import atomic_agents\n",  # bare package import, not a submodule
        "from atomic_agents.agent import AtomicAgent\n",
        "import os, sys\n",
        "from .memory import get_default_memory_backend\n",
        # A local variable/function literally named 'manage' must not false-positive
        # when it is not the target of an atomic_agents-rooted import.
        "from some_other_package import manage\n",
        # Dynamic imports of NON-forbidden targets must not be flagged.
        'importlib.import_module("atomic_agents.deploy")\n',  # deploy is core
        'importlib.import_module("atomic_agents.memory")\n',
        'importlib.import_module("some_third_party.manage")\n',  # not our package
        '__import__("os")\n',
        # A dynamically-computed (non-literal) module name is out of scope for
        # the static scanner (covered by the runtime sys.modules guard).
        'importlib.import_module("atomic_agents." + pkg)\n',
        "importlib.import_module(mod_name)\n",
        # An unrelated method literally named import_module-ish must not fire
        # (attr must be exactly 'import_module').
        'obj.import_modules("atomic_agents.manage")\n',
    ],
)
def test_scanner_does_not_flag_allowed_imports(source: str):
    violations = find_forbidden_imports(source, "synthetic.py")
    assert not violations, f"scanner false-flagged an allowed import: {violations!r}"
