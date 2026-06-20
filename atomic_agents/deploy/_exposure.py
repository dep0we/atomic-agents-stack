"""deploy/_exposure.py — exposure guidance (GUIDE, NEVER PERFORM).

spec/49 §"Exposure guidance" + MUST 11. After a verified loopback deployment,
deploy prints the operator's next step to reach the agent from another device.
It DETECTS the environment to tailor the guidance, but it NEVER performs the
exposure — it does not run ``tailscale serve``, edit a perimeter config, open a
firewall, or terminate TLS. The agent's reachability is the operator's
perimeter responsibility (spec/37).

  detect tailscale (`tailscale status --json`, READ-ONLY) ─┐
                                                            ├─► guidance TEXT
  present?  → exact `tailscale serve --bg ...` + caveats ───┘
  absent?   → pointer to docs/deployment/serve.md perimeter options

The ONLY subprocess this module runs is the read-only ``tailscale status
--json`` detection probe. It is routed through an injectable ``runner`` so tests
assert detection logic + that NO exposure command is ever issued.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Callable

Runner = Callable[..., "subprocess.CompletedProcess[str]"]

_SERVE_DOCS = "docs/deployment/serve.md"


def _default_runner(argv: list[str]) -> "subprocess.CompletedProcess[str]":
    """Production runner: run a subprocess, capture text, never raise."""
    return subprocess.run(  # noqa: PLW1510 -- returncode inspected by caller
        argv,
        capture_output=True,
        text=True,
        timeout=10,
    )


def detect_tailscale(*, runner: Runner = _default_runner) -> bool:
    """Return True if Tailscale is present and reporting status (READ-ONLY).

    spec/49 MUST 11: detection is ``tailscale status --json`` succeeding. This
    is a read-only probe — it never configures or runs an exposure command. If
    the ``tailscale`` binary is absent, returns False without running anything.
    """
    if shutil.which("tailscale") is None:
        return False
    try:
        cp = runner(["tailscale", "status", "--json"])
    except (OSError, subprocess.SubprocessError):
        return False
    return cp.returncode == 0


def exposure_guidance(
    port: int,
    *,
    tailscale_present: bool,
) -> str:
    """Build the exposure guidance TEXT (spec/49 MUST 11 — guide, not perform).

    ``tailscale_present`` is the result of ``detect_tailscale``; this function
    is pure (no subprocess), so the guidance text is unit-testable on its own.

    Tailscale present → the EXACT command + the one-time cert prerequisite + the
    first-request warm-up note + a pointer to the authoritative recipe.
    Tailscale absent  → a short pointer to the perimeter options and a plain
    statement that the agent is currently loopback-only.
    """
    if tailscale_present:
        return (
            "Next step — expose the agent to your other devices (you run this; "
            "deploy does NOT):\n"
            f"\n    tailscale serve --bg http://127.0.0.1:{port}\n\n"
            "One-time prerequisite: enable HTTPS certificates in the tailnet "
            "admin console.\n"
            "Note: the first HTTPS request may be slow while the certificate "
            "provisions.\n"
            f"See {_SERVE_DOCS} for the authoritative recipe."
        )
    return (
        "The agent is currently loopback-only (reachable from this machine).\n"
        "To reach it from another device, set up a perimeter yourself "
        "(deploy does NOT):\n"
        "  - Tailscale Serve\n"
        "  - Cloudflare Access\n"
        "  - a reverse proxy\n"
        "  - Identity-Aware Proxy (IAP)\n"
        f"See {_SERVE_DOCS} for the perimeter options."
    )
