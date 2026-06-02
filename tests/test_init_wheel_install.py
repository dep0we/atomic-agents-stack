"""Wheel install verification (OQ11 from prep synthesis).

The wizard's advisor template tree must ship in the wheel via hatchling's
auto-inclusion (existing ``packages = ["atomic_agents"]`` directive in
pyproject.toml). This test catches drops where the templates accidentally
fall out of the build.

Marked with ``RUN_WHEEL_INSTALL_TESTS`` so CI can opt in by setting the env
var to ``"1"``. Skipped by default for fast local runs.
"""

from __future__ import annotations

import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_WHEEL_INSTALL_TESTS") != "1",
    reason="Wheel install tests are slow; set RUN_WHEEL_INSTALL_TESTS=1 to enable",
)


def _project_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    raise RuntimeError(
        "Could not locate project root (no pyproject.toml found in any parent dir)"
    )


# Every file that must appear inside the wheel ZIP.
# These are the advisor template tree + the three init-module Python files.
_REQUIRED_WHEEL_PATHS = [
    "atomic_agents/init/templates/advisor/persona/IDENTITY.md",
    "atomic_agents/init/templates/advisor/persona/SOUL.md",
    "atomic_agents/init/templates/advisor/persona/USER.md",
    "atomic_agents/init/templates/advisor/tools.md",
    "atomic_agents/init/templates/advisor/model.md",
    "atomic_agents/init/templates/advisor/memory/INDEX.md",
    "atomic_agents/init/templates/advisor/wiki/INDEX.md",
    "atomic_agents/init/__init__.py",
    "atomic_agents/init/wizard.py",
    "atomic_agents/init/constants.py",
]


def _build_wheel(dist_dir: Path, project: Path) -> Path:
    """Run ``uv build --wheel`` and return the path to the built wheel."""
    result = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(dist_dir)],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, (
        f"uv build failed (exit {result.returncode}).\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )
    wheels = list(dist_dir.glob("*.whl"))
    assert len(wheels) == 1, f"Expected exactly one wheel in dist dir, found: {wheels}"
    return wheels[0]


def test_wheel_includes_advisor_template_tree(tmp_path: Path) -> None:
    """Build a wheel and verify every advisor template file ships inside it.

    Catches the failure mode where hatchling's ``packages`` directive misses
    package data or the templates directory is accidentally excluded via a
    gitignore / hatch exclude rule.
    """
    project = _project_root()
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()

    wheel = _build_wheel(dist_dir, project)

    with zipfile.ZipFile(wheel) as zf:
        names = set(zf.namelist())

    missing = [p for p in _REQUIRED_WHEEL_PATHS if p not in names]
    assert not missing, (
        f"Wheel is missing {len(missing)} required file(s):\n"
        + "\n".join(f"  {p}" for p in missing)
        + f"\n\nWheel ZIP contains {len(names)} entries. "
        "First 40 matching 'atomic_agents/':\n"
        + "\n".join(sorted(n for n in names if n.startswith("atomic_agents/"))[:40])
    )


def test_wheel_install_end_to_end(tmp_path: Path) -> None:
    """Install the wheel into a fresh venv and run the init list-templates command.

    Proves that the lazy-import + importlib.resources access pattern works
    under a real wheel install (not just an editable install). If the templates
    are missing from the wheel, the CLI command will fail or produce output
    that does not contain 'advisor'.
    """
    project = _project_root()
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    venv_dir = tmp_path / "venv"

    # Build the wheel.
    wheel = _build_wheel(dist_dir, project)

    # Create a fresh virtual environment.
    subprocess.run(
        ["uv", "venv", str(venv_dir)],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )

    # Resolve the venv Python binary.
    if sys.platform == "win32":
        venv_python = venv_dir / "Scripts" / "python.exe"
    else:
        venv_python = venv_dir / "bin" / "python"

    # Install only the wheel (no dev extras) so we exercise the real
    # installed-package path, not the editable source tree.
    subprocess.run(
        ["uv", "pip", "install", "--python", str(venv_python), str(wheel)],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )

    # Run ``atomic-agents init --list-templates`` via the installed entry point.
    result = subprocess.run(
        [str(venv_python), "-m", "atomic_agents.cli", "init", "--list-templates"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"'atomic-agents init --list-templates' exited {result.returncode}.\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )
    assert "advisor" in result.stdout.lower(), (
        f"'--list-templates' output did not contain 'advisor'.\nstdout: {result.stdout}"
    )
