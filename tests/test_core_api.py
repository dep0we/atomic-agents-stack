"""``atomic_agents.core_api`` — the core<->fleet contract surface (#742, Phase 2b).

Phase 2a (#736) closed the dangerous *core imports fleet* direction. Phase
2b closes the other half of the seam: the fleet-shaped extension packages
(``advisor/``, ``dashboard/``, ``manage/``) used to reach into core's
*private* (leading-underscore) modules directly -- ``_io``, ``_costs``,
``_platform``, ``_model``. That is now routed through a single thin
re-export module, ``atomic_agents.core_api``, that names itself honestly as
the internal-stable core<->fleet contract (TENSIONS T17 / T7), not a
broadly-advertised public API.

This file covers four things:
  1. The six names are importable from ``core_api`` and are the SAME OBJECT
     as the private originals (a re-export can't silently diverge).
  2. ``get_model_rates()`` -- the T7 payoff -- behaves correctly: known
     model, unknown model, and defensive-copy semantics.
  3. The seam-lock guard: no SOURCE file under ``advisor/``, ``dashboard/``,
     or ``manage/`` may import ANY leading-underscore core-private module --
     not just the four originally repointed (``_io`` / ``_costs`` /
     ``_platform`` / ``_model``), but any future one too (e.g. ``_llm``,
     ``_capture``) -- statically, via an AST walk over every import shape,
     not a substring/text grep. A substring guard on ``_model`` would
     false-positive on a file like ``manage/set_model.py`` (``set_model``
     contains ``_model``); the AST guard matches whole dotted-path
     SEGMENTS, so it does not.
  4. The test-layer half of the same seam (#743, TENSIONS T17
     test-seam-guard ruling): extension-owned TEST files identified by
     filename convention (``tests/advisor/`` as a subdirectory, plus
     the flat, prefix-named ``tests/test_advisor_*.py``,
     ``tests/test_dashboard_*.py``, and ``tests/test_manage_*.py`` files at
     the ``tests/`` root) are scanned with the same AST walker. This is a
     filename-scoped guard, not a scan of every test file that happens to
     import extension code -- a test file outside these conventions (e.g.
     ``tests/test_workflow_aggregate.py``, which imports dashboard code
     under an unmatched name) is not covered; tracked as a follow-up
     in #747.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import atomic_agents
import atomic_agents._costs as _costs
import atomic_agents._io as _io
import atomic_agents._model as _model
import atomic_agents._platform as _platform
import atomic_agents.core_api as core_api

REPO_ROOT = Path(__file__).parent.parent
PACKAGE_ROOT = Path(atomic_agents.__file__).parent

EXPECTED_ALL = [
    "atomic_write",
    "safe_resolve_under",
    "get_agents_root",
    "parse_model_md",
    "calc_cost",
    "get_model_rates",
]

_DIVERGED_MSG = (
    "re-export diverged from the private original -- if core reimplemented "
    "this helper, update the core_api seam contract deliberately"
)


# ---------------------------------------------------------------------------
# 1. Importability + __all__
# ---------------------------------------------------------------------------


def test_all_six_names_importable():
    from atomic_agents.core_api import (
        atomic_write,
        safe_resolve_under,
        get_agents_root,
        parse_model_md,
        calc_cost,
        get_model_rates,
    )

    for name in (
        atomic_write,
        safe_resolve_under,
        get_agents_root,
        parse_model_md,
        calc_cost,
        get_model_rates,
    ):
        assert callable(name)


def test_dunder_all_matches_exactly_six_names():
    assert core_api.__all__ == EXPECTED_ALL


# ---------------------------------------------------------------------------
# 2. Re-export identity -- same object as the private original
# ---------------------------------------------------------------------------


def test_atomic_write_is_the_same_object_as_io_private():
    assert core_api.atomic_write is _io.atomic_write, _DIVERGED_MSG


def test_safe_resolve_under_is_the_same_object_as_io_private():
    assert core_api.safe_resolve_under is _io.safe_resolve_under, _DIVERGED_MSG


def test_get_agents_root_is_the_same_object_as_platform_private():
    assert core_api.get_agents_root is _platform.get_agents_root, _DIVERGED_MSG


def test_parse_model_md_is_the_same_object_as_model_private():
    assert core_api.parse_model_md is _model.parse_model_md, _DIVERGED_MSG


def test_calc_cost_is_the_same_object_as_costs_private():
    assert core_api.calc_cost is _costs.calc_cost, _DIVERGED_MSG


def test_get_model_rates_is_the_same_object_as_costs_private():
    assert core_api.get_model_rates is _costs.get_model_rates, _DIVERGED_MSG


# ---------------------------------------------------------------------------
# 3. get_model_rates() behavior
# ---------------------------------------------------------------------------


def test_get_model_rates_known_model_returns_correct_rates():
    rates = core_api.get_model_rates("claude-opus-4-8")
    assert rates == _costs.PRICING["claude-opus-4-8"]
    # Structure, not values -- a routine PRICING update must not break this
    # seam test (T7: a price edit is never an API break). The equality
    # assertion above already fully proves the accessor returns the correct
    # entry.
    assert "input" in rates and "output" in rates


def test_get_model_rates_unknown_model_returns_none():
    assert core_api.get_model_rates("not-a-real-model-id") is None


def test_get_model_rates_returns_a_copy_not_the_live_entry():
    rates = core_api.get_model_rates("claude-opus-4-8")
    original_output = _costs.PRICING["claude-opus-4-8"]["output"]

    rates["output"] = -999.0

    assert _costs.PRICING["claude-opus-4-8"]["output"] == original_output
    assert rates is not _costs.PRICING["claude-opus-4-8"]


# ---------------------------------------------------------------------------
# 4. Seam-lock guard: advisor/dashboard/manage import no core-private module
# ---------------------------------------------------------------------------

# Documentation-only: the four core-private modules the extension packages
# originally borrowed from and were repointed off of at #742. The actual
# guard predicate (in `_flag_if_forbidden` below) is broader -- it flags ANY
# leading-underscore top-level module under `atomic_agents`, not just this
# set, so a future core-private module (e.g. `_llm`, `_capture`) is caught
# even before anyone adds it here. Matched by dotted-path SEGMENT, never by
# substring, so a future `manage/set_model.py` (which contains the substring
# "_model") is not a false positive.
FORBIDDEN_CORE_PRIVATE_MODULES = frozenset({"_io", "_costs", "_platform", "_model"})

EXTENSION_PACKAGE_DIRS = ("advisor", "dashboard", "manage")


def _extension_python_files() -> list[Path]:
    """Every .py file recursively under advisor/, dashboard/, manage/.

    Recursive so nested packages (e.g. dashboard/panels/) are covered, not
    just the top-level files of each extension package.
    """
    files: list[Path] = []
    for pkg_name in EXTENSION_PACKAGE_DIRS:
        pkg_dir = PACKAGE_ROOT / pkg_name
        if not pkg_dir.is_dir():
            continue
        for path in pkg_dir.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            files.append(path)
    return files


def _file_package_parts(path: Path) -> tuple[str, ...]:
    """Dotted __package__ tuple for path (the directory chain, no filename).

    Correct for BOTH a regular module and an __init__.py: in either case
    Python's __package__ for that file is the dotted name of the directory
    it lives in (repo-relative, starting at 'atomic_agents').
    """
    rel = path.relative_to(PACKAGE_ROOT.parent)
    return rel.parts[:-1]


def find_core_private_imports(
    source: str, filename: str, package_parts: tuple[str, ...]
) -> list[str]:
    """Return a human-readable violation string per forbidden import found.

    Handles all four STATIC import shapes an extension file could use to
    reach a core-private module:
      - absolute: ``import atomic_agents._io`` / ``import atomic_agents._costs as c``
      - absolute from: ``from atomic_agents._io import atomic_write``
      - absolute from bare pkg: ``from atomic_agents import _io``
      - relative: ``from .._io import atomic_write`` / ``from .. import _io``

    ...plus two DYNAMIC import shapes with an ABSOLUTE string-literal first
    argument:
      - ``importlib.import_module("atomic_agents._costs")`` (also the bare
        ``import_module("atomic_agents._costs")`` name, i.e.
        ``from importlib import import_module`` then ``import_module(...)``)
      - ``__import__("atomic_agents._io")``

    Relative imports are resolved against `package_parts` (this file's own
    __package__, computed from its location on disk) using the real Python
    relative-import level rule: level L resolves against
    ``package_parts[: len(package_parts) - (L - 1)]``. This is a genuine
    level-aware resolution, not a naive "first dotted component" heuristic.

    Coverage note -- two gaps remain versus Phase 2a's
    ``test_core_extension_boundary.py`` guard, neither fixed here, both
    tracked in #747:
      1. A RELATIVE dynamic-import string, e.g.
         ``importlib.import_module(".._costs", __package__)``, is NOT
         resolved against `package_parts` the way Phase 2a's guard resolves
         it -- only an ABSOLUTE ``"atomic_agents...."`` string literal is
         recognized here, so a relative dynamic-import string silently
         escapes this guard.
      2. A string-literal target passed to something like
         ``unittest.mock.patch("atomic_agents._costs.PRICING", ...)`` or
         ``monkeypatch.setattr("atomic_agents._costs.PRICING", ...)`` is an
         ordinary call argument, not an import node, so it is not caught
         here either. None of the extension source or test files do this
         today.
    """
    tree = ast.parse(source, filename=filename)
    violations: list[str] = []

    def _flag_if_forbidden(
        target_parts: tuple[str, ...], lineno: int, text: str
    ) -> None:
        if (
            len(target_parts) >= 2
            and target_parts[0] == "atomic_agents"
            and target_parts[1].startswith("_")
        ):
            violations.append(f"{filename}:{lineno}: {text}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                # Absolute only -- `import` never takes a relative form.
                # Covers both `import atomic_agents._io` and the aliased
                # `import atomic_agents._costs as c` (alias.name is the
                # dotted path regardless of `as`).
                target_parts = tuple(alias.name.split("."))
                _flag_if_forbidden(target_parts, node.lineno, f"`import {alias.name}`")

        elif isinstance(node, ast.Call):
            # Dynamic imports: importlib.import_module(...) / bare
            # import_module(...) / __import__(...) with an ABSOLUTE
            # string-literal first argument. Unlike Phase 2a's guard, this
            # does NOT resolve a RELATIVE dynamic-import string (e.g.
            # import_module(".._costs", __package__)) -- see the coverage
            # note in this function's docstring and #747.
            func = node.func
            is_import_module = (
                isinstance(func, ast.Attribute) and func.attr == "import_module"
            ) or (isinstance(func, ast.Name) and func.id == "import_module")
            is_dunder_import = isinstance(func, ast.Name) and func.id == "__import__"
            if (is_import_module or is_dunder_import) and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    target_parts = tuple(arg.value.split("."))
                    call_name = "__import__" if is_dunder_import else "import_module"
                    _flag_if_forbidden(
                        target_parts,
                        node.lineno,
                        f"`{call_name}({arg.value!r})`",
                    )

        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                # Relative import -- resolve against this file's own package.
                if node.level - 1 > len(package_parts):
                    # Relative import climbs above the repo root; cannot
                    # possibly resolve into atomic_agents.* -- not our concern.
                    continue
                base_parts = package_parts[: len(package_parts) - (node.level - 1)]
                if node.module:
                    target_parts = base_parts + tuple(node.module.split("."))
                    names = ", ".join(alias.name for alias in node.names)
                    dots = "." * node.level
                    _flag_if_forbidden(
                        target_parts,
                        node.lineno,
                        f"`from {dots}{node.module} import {names}`",
                    )
                else:
                    # `from .. import _io` -- the imported NAME is the module.
                    dots = "." * node.level
                    for alias in node.names:
                        target_parts = base_parts + (alias.name,)
                        _flag_if_forbidden(
                            target_parts,
                            node.lineno,
                            f"`from {dots} import {alias.name}`",
                        )
            else:
                module = node.module or ""
                if module == "atomic_agents":
                    # `from atomic_agents import _io`
                    for alias in node.names:
                        target_parts = ("atomic_agents", alias.name)
                        _flag_if_forbidden(
                            target_parts,
                            node.lineno,
                            f"`from atomic_agents import {alias.name}`",
                        )
                else:
                    # `from atomic_agents._io import atomic_write`
                    target_parts = tuple(module.split("."))
                    names = ", ".join(alias.name for alias in node.names)
                    _flag_if_forbidden(
                        target_parts,
                        node.lineno,
                        f"`from {module} import {names}`",
                    )

    return violations


def test_extension_packages_import_no_core_private_module():
    """advisor/, dashboard/, manage/ import no leading-underscore core-private module."""
    all_violations: list[str] = []
    for path in _extension_python_files():
        rel = str(path.relative_to(REPO_ROOT))
        source = path.read_text(encoding="utf-8")
        package_parts = _file_package_parts(path)
        all_violations.extend(find_core_private_imports(source, rel, package_parts))

    assert not all_violations, (
        "Extension package(s) import a core-private (leading-underscore) "
        "module directly. Route through atomic_agents.core_api instead -- "
        "that is the whole point of the core<->fleet contract surface "
        "(Phase 2b, #742).\n" + "\n".join(all_violations)
    )


# ---------------------------------------------------------------------------
# 5. Seam-lock guard, test layer: advisor/dashboard/manage TEST files import
#    no core-private module either (#743, TENSIONS T17 test-seam-guard).
# ---------------------------------------------------------------------------

# tests/advisor/ is a real subdirectory (has __init__.py + conftest.py) and
# is walked recursively, the same shape as _extension_python_files() on the
# source side. dashboard/ and manage/ tests are NOT under a tests/dashboard/
# or tests/manage/ directory today -- verified via `ls tests/`: they are
# flat, prefix-named files directly at the tests/ root (e.g.
# tests/test_dashboard_console.py, tests/test_manage_govern.py). A naive
# port of the directory-walk pattern to tests/dashboard/ and tests/manage/
# would silently discover zero files. EXTENSION_TEST_PACKAGE_DIRS still
# lists all three names (mirroring EXTENSION_PACKAGE_DIRS) so that if a
# tests/dashboard/ or tests/manage/ directory is ever added, the walk below
# picks it up automatically with no code change -- the same
# "if not pkg_dir.is_dir(): continue" existence-check pattern
# _extension_python_files() already uses.
EXTENSION_TEST_PACKAGE_DIRS = ("advisor", "dashboard", "manage")

# Flat-file glob patterns for the extension test suites that don't (yet)
# live under a tests/<pkg>/ subdirectory. Matched at the tests/ root only
# (non-recursive) -- this is a deliberate explicit allowlist, not a broad
# "any test file" scan: roughly 40 core test files (test_core_api.py itself,
# test_costs.py, test_llm_protocol_conformance.py, test_conductor.py, ...)
# legitimately import core-private modules directly by design and must
# never be swept into this scan.
# Extension-importing test files that don't match tests/advisor/,
# test_dashboard_*.py, or test_manage_*.py (e.g. tests/test_workflow_
# aggregate.py) are deliberately out of scope for now -- tracked as a
# follow-up in #747, not fixed here.
EXTENSION_TEST_FILE_GLOB_PATTERNS = (
    "test_advisor_*.py",
    "test_dashboard_*.py",
    "test_manage_*.py",
)


def _extension_test_files(tests_root: Path = REPO_ROOT / "tests") -> list[Path]:
    """Every extension-owned TEST file: the test-side half of the seam guard.

    ``tests_root`` defaults to the real repo ``tests/`` directory but is
    overridable so tests can point the same discovery logic at a synthetic
    on-disk layout (proving the glob/walk actually reaches real files, not
    just that the shared AST walker can flag a synthetic source string).

    Deliberately does NOT walk ``tests/`` as a whole and does NOT widen the
    underlying ``_flag_if_forbidden`` predicate -- see the module docstring
    point 4 and the EXTENSION_TEST_FILE_GLOB_PATTERNS comment above for why.
    """
    files: list[Path] = []
    seen: set[Path] = set()

    for pkg_name in EXTENSION_TEST_PACKAGE_DIRS:
        pkg_dir = tests_root / pkg_name
        if not pkg_dir.is_dir():
            continue
        for path in pkg_dir.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            if path not in seen:
                seen.add(path)
                files.append(path)

    for pattern in EXTENSION_TEST_FILE_GLOB_PATTERNS:
        for path in tests_root.glob(pattern):
            if path not in seen:
                seen.add(path)
                files.append(path)

    return files


def test_extension_test_files_import_no_core_private_module():
    """advisor/, dashboard/, manage/ TEST files import no core-private module.

    The test-layer twin of ``test_extension_packages_import_no_core_private_
    module`` above, scoped to the same filename conventions documented at
    ``EXTENSION_TEST_FILE_GLOB_PATTERNS`` (the ``tests/advisor/`` subdir plus
    ``test_{advisor,dashboard,manage}_*.py`` at the ``tests/`` root) -- NOT
    every test file that happens to import extension code (see #747 for the
    known gap). Within that scope, the read/membership sites that used to do
    ``from atomic_agents._costs import PRICING``
    (tests/advisor/test_advisor_recommend.py,
    tests/advisor/test_advisor_score.py) were repointed onto
    ``core_api.get_model_rates()`` in the same PR, so this must already be
    green.
    """
    all_violations: list[str] = []
    for path in _extension_test_files():
        rel = str(path.relative_to(REPO_ROOT))
        source = path.read_text(encoding="utf-8")
        package_parts = _file_package_parts(path)
        all_violations.extend(find_core_private_imports(source, rel, package_parts))

    assert not all_violations, (
        "Extension-owned TEST file(s) import a core-private (leading-"
        "underscore) module directly. Route through atomic_agents.core_api "
        "instead, or monkeypatch the extension-owned binding that resolves "
        "it, instead of mutating the core-private table (#743 "
        "test-seam-guard).\n" + "\n".join(all_violations)
    )


def test_extension_test_file_discovery_includes_known_real_files():
    """End-to-end discovery check against the REAL repo disk layout -- not
    just the synthetic-string unit tests below, which only prove the shared
    AST walker can flag a violation once handed a source string. This
    proves the glob/walk step actually reaches the files it claims to."""
    discovered = {str(p.relative_to(REPO_ROOT)) for p in _extension_test_files()}
    assert "tests/advisor/test_advisor_score.py" in discovered
    assert "tests/advisor/test_advisor_recommend.py" in discovered
    assert "tests/test_dashboard_console.py" in discovered
    assert "tests/test_manage_govern.py" in discovered


def test_extension_test_file_discovery_excludes_core_test_files():
    """Must never sweep in tests/test_core_api.py (self) or other core test
    files that legitimately import core-private modules by design -- an
    over-broad discovery would false-positive the whole suite the moment
    this guard runs (#743 P0/P1 findings)."""
    discovered = {str(p.relative_to(REPO_ROOT)) for p in _extension_test_files()}
    assert "tests/test_core_api.py" not in discovered
    assert "tests/test_costs.py" not in discovered
    assert "tests/test_llm_protocol_conformance.py" not in discovered
    assert "tests/test_conductor.py" not in discovered


def test_extension_test_file_discovery_ignores_unrelated_flat_file(tmp_path):
    """A flat file at the tests/ root that does NOT match either glob
    pattern (e.g. a tests/test_costs.py-shaped name) must never be
    discovered -- proves the glob predicate is a genuine explicit allowlist,
    not an "any .py file at the root" scan."""
    unrelated = tmp_path / "test_costs.py"
    unrelated.write_text("from atomic_agents._costs import PRICING\n", encoding="utf-8")

    discovered = _extension_test_files(tests_root=tmp_path)

    assert unrelated not in discovered


@pytest.mark.parametrize(
    "relative_path",
    [
        "advisor/test_injected_violation.py",
        "test_dashboard_injected_violation.py",
        "test_manage_injected_violation.py",
    ],
)
def test_strip_red_discovery_and_scan_fires_on_test_file_violation(
    tmp_path, relative_path
):
    """strip-RED, end-to-end: proves the discovery+scan PIPELINE (not just
    the underlying AST walker) fires when a core-private import is re-added
    to a real file on disk shaped like each of the three extension test
    surfaces this guard covers (tests/advisor/, test_dashboard_*.py,
    test_manage_*.py). Mirroring only the already-tested AST predicate
    (test_scanner_flags_each_forbidden_import_shape) would say nothing
    about whether the NEW discovery step reaches these files at all."""
    bad_file = tmp_path / relative_path
    bad_file.parent.mkdir(parents=True, exist_ok=True)
    bad_file.write_text("from atomic_agents._costs import PRICING\n", encoding="utf-8")

    discovered = _extension_test_files(tests_root=tmp_path)
    assert bad_file in discovered, (
        f"discovery failed to find the injected violation file at {relative_path!r}"
    )

    violations: list[str] = []
    for path in discovered:
        rel = str(path.relative_to(tmp_path))
        source = path.read_text(encoding="utf-8")
        # Package parts don't need to resolve against the real repo tree for
        # this control -- test files structurally cannot reach an
        # atomic_agents.* module via a RELATIVE import (relative imports
        # can't cross top-level package boundaries), so only the absolute-
        # import branches of find_core_private_imports are exercised here.
        violations.extend(find_core_private_imports(source, rel, ("tests",)))

    assert violations, (
        f"guard failed to fire on an injected core-private import at {relative_path!r}"
    )


def test_extension_test_scan_does_not_flag_extension_internal_private(tmp_path):
    """A dashboard/manage-INTERNAL private submodule import (e.g.
    ``atomic_agents.dashboard._status``, ``atomic_agents.manage._routine``)
    must stay unflagged -- the shared ``_flag_if_forbidden`` predicate
    checks only ``target_parts[1]`` (the segment immediately after
    ``atomic_agents``), so ``dashboard``/``manage`` (not underscore-
    prefixed) is correctly exempt. This proves that holds through the NEW
    discovery+scan pipeline too, not just the underlying predicate."""
    ok_file = tmp_path / "test_dashboard_ok.py"
    ok_file.write_text(
        "from atomic_agents.dashboard._status import status_for_agent\n",
        encoding="utf-8",
    )

    discovered = _extension_test_files(tests_root=tmp_path)
    assert ok_file in discovered

    violations = find_core_private_imports(
        ok_file.read_text(encoding="utf-8"), str(ok_file), ("tests",)
    )
    assert not violations, (
        f"false-flagged an extension-internal (not core-private) import: {violations!r}"
    )


# ---------------------------------------------------------------------------
# Negative controls -- prove the scanner actually catches what it claims to,
# and that it does NOT false-positive on a lookalike name.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "from .._io import atomic_write\n",
        "from .._costs import PRICING\n",
        "from .._platform import get_agents_root\n",
        "from .._model import parse_model_md\n",
        "from .. import _io\n",
        "from atomic_agents._io import atomic_write\n",
        "from atomic_agents import _costs\n",
        "import atomic_agents._costs\n",
        "import atomic_agents._costs as c\n",
        "import atomic_agents._model\n",
        # Widened-guard controls (fix 1): a NON-repointed core-private
        # module must also be caught, not just the original four.
        "from .._llm import call_llm\n",
        "from atomic_agents._capture import capture\n",
        # Dynamic imports (fix 2): importlib.import_module + __import__.
        'importlib.import_module("atomic_agents._costs")\n',
        '__import__("atomic_agents._io")\n',
        # Bare-name form (fix 3, closes a #747 gap): `from importlib import
        # import_module` then `import_module(...)` -- Phase 2a's guard
        # already caught this; ours did not until this fix.
        'import_module("atomic_agents._io")\n',
    ],
)
def test_scanner_flags_each_forbidden_import_shape(source: str):
    # package_parts as if this were e.g. atomic_agents/dashboard/serve.py
    violations = find_core_private_imports(
        source, "synthetic.py", ("atomic_agents", "dashboard")
    )
    assert violations, f"scanner failed to flag forbidden import in: {source!r}"


@pytest.mark.parametrize(
    "source",
    [
        "from ..core_api import atomic_write\n",  # the correct post-2b form
        "from ..core_api import get_model_rates, calc_cost\n",
        "from .costs import discover_agents\n",
        "from ._shared import page_shell\n",
        "import os, sys\n",
        "from atomic_agents.deploy import deploy\n",  # deploy is core but not forbidden
        "from atomic_agents import core_api\n",
        # The M1 false-positive this guard exists to avoid: a substring guard
        # on '_model' would wrongly flag a manage/set_model.py-shaped import.
        "from ..core_api import parse_model_md  # used by set_model.py\n",
        # Dynamic import of the ALLOWED core_api seam (fix 2 parity control).
        'importlib.import_module("atomic_agents.core_api")\n',
    ],
)
def test_scanner_does_not_flag_allowed_imports(source: str):
    violations = find_core_private_imports(
        source, "synthetic.py", ("atomic_agents", "manage")
    )
    assert not violations, f"scanner false-flagged an allowed import: {violations!r}"


def test_scanner_does_not_false_positive_on_set_model_module_name():
    """The M1 finding this guard exists to prevent: a substring match on
    '_model' would false-positive on `manage/set_model.py` (the name
    literally contains the substring '_model'). The AST guard matches
    dotted-path SEGMENTS, so importing a module simply NAMED set_model
    (not `_model`) must never be flagged, regardless of its own imports."""
    source = (
        "from ..core_api import parse_model_md\n"
        "\n"
        "def set_model(agent, model_id):\n"
        "    return parse_model_md(agent)\n"
    )
    violations = find_core_private_imports(
        source, "atomic_agents/manage/set_model.py", ("atomic_agents", "manage")
    )
    assert not violations, (
        f"false positive on a set_model.py-shaped file (M1 regression): {violations!r}"
    )
