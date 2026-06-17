"""Source-hygiene guards — catch un-restored negative-control strips.

The project's review methodology relies on EMPIRICAL negative-control strips:
to prove a fix is load-bearing, a reviewer temporarily disables it (e.g. by
prefixing a guard with ``False and``), runs the test to confirm it goes RED,
then restores the guard. The failure mode this test defends against is an
UN-RESTORED strip shipping to production — exactly the #520 PR2 P0 where
``if False and _begin_decision.state == _DEDUP_COMPLETED:`` turned the
Phase-2 dedup short-circuit into dead code (double-spend on the
lookup→commit→begin race).

A constant ``False``/``True`` operand inside a boolean guard (``if False:``,
``False and ...``, ``... and False``, ``if True and ...``) is ALWAYS either
dead code or an un-restored strip; neither belongs in a merged diff. We use
``ast`` (not a regex over raw text) so docstring/comment prose that merely
*describes* the pattern, and legitimate keyword defaults like ``flag=False``,
do not false-trip the guard.
"""

from __future__ import annotations

import ast
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parent.parent / "atomic_agents"


def _python_sources() -> list[Path]:
    return sorted(_PKG_ROOT.rglob("*.py"))


def _is_bool_const(node: ast.AST, value: bool) -> bool:
    return isinstance(node, ast.Constant) and node.value is value


class _StripFinder(ast.NodeVisitor):
    """Flag dead/strip boolean guards:

    * ``if False:`` / ``while False:`` — a literal-False test
    * ``if True and ...:`` — a no-op True conjunct on a guard test
    * any ``BoolOp`` (``and``/``or``) with a literal ``True``/``False`` operand
      — the ``False and X`` / ``X and False`` short-circuit-strip shape
    """

    def __init__(self) -> None:
        self.hits: list[tuple[int, str]] = []

    def _check_test(self, test: ast.AST) -> None:
        if _is_bool_const(test, False):
            self.hits.append((getattr(test, "lineno", 0), "literal `False` test"))
        if isinstance(test, ast.BoolOp) and any(
            _is_bool_const(v, True) for v in test.values
        ):
            self.hits.append(
                (getattr(test, "lineno", 0), "`True and ...` no-op conjunct")
            )

    def visit_If(self, node: ast.If) -> None:
        self._check_test(node.test)
        self.generic_visit(node)

    def visit_While(self, node: ast.While) -> None:
        self._check_test(node.test)
        self.generic_visit(node)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        # Any literal bool operand in an and/or is a strip residue or dead code.
        for v in node.values:
            if _is_bool_const(v, False) or _is_bool_const(v, True):
                self.hits.append(
                    (getattr(node, "lineno", 0), "literal bool operand in BoolOp")
                )
                break
        self.generic_visit(node)


def test_no_unrestored_negative_control_strips_in_runtime_source():
    """No ``if False`` / ``False and`` / ``and False`` / ``if True and`` boolean
    guards in the runtime package. These are dead guards or un-restored
    negative-control strips — the #520 PR2 P0 shipped exactly this
    (``if False and _begin_decision.state == _DEDUP_COMPLETED:``)."""
    offenders: list[str] = []
    for path in _python_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        finder = _StripFinder()
        finder.visit(tree)
        for lineno, why in finder.hits:
            offenders.append(f"{path.relative_to(_PKG_ROOT.parent)}:{lineno}: {why}")
    assert not offenders, (
        "Un-restored negative-control strip / dead boolean guard found in "
        "runtime source (see #520 PR2 P0 — ``if False and ...`` shipped as "
        "dead code):\n" + "\n".join(offenders)
    )


def test_hygiene_guard_self_check():
    """Sanity: the AST finder actually flags the strip shapes it targets (so
    the guard above is not vacuously green) and does NOT flag benign bool use."""

    def _hits(src: str) -> list[tuple[int, str]]:
        finder = _StripFinder()
        finder.visit(ast.parse(src))
        return finder.hits

    strips = [
        "if False and x == 1:\n    pass",
        "if False:\n    pass",
        "y = a and False",
        "if True and cond:\n    pass",
    ]
    for src in strips:
        assert _hits(src), f"finder failed to flag a real strip shape:\n{src}"

    benign = [
        "def f(flag=False):\n    return flag",
        "value: bool = False",
        "return False",
        "x = a and b",
        'doc = "if False and this is prose it is fine"',
    ]
    for src in benign:
        assert not _hits(src), f"finder false-flagged benign code:\n{src}"
