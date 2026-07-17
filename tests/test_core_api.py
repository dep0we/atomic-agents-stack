"""``atomic_agents.core_api`` — the core<->fleet contract surface (#742, Phase 2b).

Phase 2a (#736) closed the dangerous *core imports fleet* direction. Phase
2b closes the other half of the seam: the fleet-shaped extension packages
(``advisor/``, ``dashboard/``, ``manage/``) used to reach into core's
*private* (leading-underscore) modules directly -- ``_io``, ``_costs``,
``_platform``, ``_model``. That is now routed through a single thin
re-export module, ``atomic_agents.core_api``, that names itself honestly as
the internal-stable core<->fleet contract (TENSIONS T17 / T7), not a
broadly-advertised public API.

This file covers three things:
  1. The six names are importable from ``core_api`` and are the SAME OBJECT
     as the private originals (a re-export can't silently diverge).
  2. ``get_model_rates()`` -- the T7 payoff -- behaves correctly: known
     model, unknown model, and defensive-copy semantics.
  3. The seam-lock guard: no file under ``advisor/``, ``dashboard/``, or
     ``manage/`` may import ANY leading-underscore core-private module --
     not just the four originally repointed (``_io`` / ``_costs`` /
     ``_platform`` / ``_model``), but any future one too (e.g. ``_llm``,
     ``_capture``) -- statically, via an AST walk over every import shape,
     not a substring/text grep. A substring guard on ``_model`` would
     false-positive on a file like ``manage/set_model.py`` (``set_model``
     contains ``_model``); the AST guard matches whole dotted-path
     SEGMENTS, so it does not.
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

    ...plus two DYNAMIC import shapes with a string-literal first argument
    (parity with Phase 2a's ``test_core_extension_boundary.py`` guard):
      - ``importlib.import_module("atomic_agents._costs")``
      - ``__import__("atomic_agents._io")``

    Relative imports are resolved against `package_parts` (this file's own
    __package__, computed from its location on disk) using the real Python
    relative-import level rule: level L resolves against
    ``package_parts[: len(package_parts) - (L - 1)]``. This is a genuine
    level-aware resolution, not a naive "first dotted component" heuristic.
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
            # Dynamic imports: importlib.import_module(...) / __import__(...)
            # with a string-literal first argument -- parity with Phase 2a's
            # dynamic-import detection.
            func = node.func
            is_import_module = (
                isinstance(func, ast.Attribute) and func.attr == "import_module"
            )
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
