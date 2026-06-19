"""atomic_agents.deploy — the deployment conductor (spec/48).

`atomic-agents deploy <agent>` takes an operator from "I have an agent folder"
to "the agent is running, supervised, and verified on this machine," then
GUIDES (never performs) the network-exposure step.

This package owns no new runtime. It sequences the surfaces that already exist
(`init`, `doctor`, `serve`), installs a per-user launchd agent that runs
`atomic-agents serve`, verifies it on loopback, and prints tailored exposure
guidance. See spec/48 for the Implementer Contract (12 MUSTs).

Public surface (the orchestrator entry points wired into cli.py):

    deploy(...)         — plan + execute a loopback deployment, verify, then
                          print exposure guidance. With ``plan_only`` prints the
                          tagged plan and exits with ZERO side effects (MUST 6).
    deploy_status(...)  — report the live deployment state from launchd (MUST 12).
    deploy_down(...)    — tear a deployment down: bootout + remove plist (MUST 12).

The lower-level modules (kept internal):

    _types     — Plan / Step / StepTag / DeployState / LaunchdStatus dataclasses
    _launchd   — plist renderer + launchctl install/teardown/status (macOS)
    _ports     — port precedence resolution + pre-bootstrap socket-bind probe
    _verify    — non-mutating, unbilled, predicate-based loopback verification
    _exposure  — exposure guidance (guide, NEVER perform)
    _conductor — the planner + executor that ties the modules together
"""

from __future__ import annotations

from ._conductor import (
    DeployError,
    deploy,
    deploy_down,
    deploy_status,
    plan_deploy,
)
from ._types import (
    DeployState,
    LaunchdStatus,
    Plan,
    Step,
    StepTag,
)

__all__ = [
    "DeployError",
    "deploy",
    "deploy_down",
    "deploy_status",
    "plan_deploy",
    "DeployState",
    "LaunchdStatus",
    "Plan",
    "Step",
    "StepTag",
]
