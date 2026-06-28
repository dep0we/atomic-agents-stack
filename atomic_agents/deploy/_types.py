"""deploy/_types.py — dataclasses + enums for the deployment planner.

spec/49 §"Execution model — planner → executor". These types are the shared
vocabulary between the planner (builds an ordered list of `Step`s) and the
executor (runs them). They carry no behaviour beyond trivial helpers so the
planner can be tested in isolation from the executor (spec/49 MUST 6 —
`--plan` is side-effect-free).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class StepTag(str, Enum):
    """How a plan step is allowed to run (spec/49 §"Execution model").

    ``AUTO``    — user-space, no consequence beyond the agent's own
                  folder/process. Runs silently.
    ``CONSENT`` — automatable but touches shared/user state (installing the
                  launchd agent, running ``init``). Prompt unless ``--yes``.
    ``MANUAL``  — unautomatable or operator-owned (provider-key setup; network
                  exposure). Print precise instructions; pause/finish.
    """

    AUTO = "auto"
    CONSENT = "consent"
    MANUAL = "manual"


@dataclass(frozen=True)
class Step:
    """One ordered step in a deployment plan.

    ``key``     stable machine identifier (e.g. ``"doctor-gate"``) used by the
                executor to dispatch and by tests to assert on.
    ``tag``     the StepTag deciding how the step may run.
    ``title``   one-line human summary for ``--plan`` output.
    ``detail``  optional longer description shown in the plan.
    """

    key: str
    tag: StepTag
    title: str
    detail: str = ""


@dataclass
class Plan:
    """An ordered list of tagged steps for one ``deploy <agent>`` invocation.

    The plan is pure data: building it makes no billed/LLM call and no
    filesystem mutation (spec/49 MUST 6). The executor consumes it.
    """

    agent: str
    steps: list[Step] = field(default_factory=list)

    def render(self) -> str:
        """Render the tagged plan as human-readable text for ``--plan``.

        ::

            Deployment plan for agent 'researcher' (loopback, macOS launchd):

              1. [auto]    Preflight: Python + PATH + ATOMIC_AGENTS_ROOT
              2. [consent] Ensure agent folder exists (hand off to init)
              ...

        No side effects; pure string assembly.
        """
        lines = [
            f"Deployment plan for agent {self.agent!r} (loopback, macOS launchd):",
            "",
        ]
        for i, step in enumerate(self.steps, start=1):
            tag = f"[{step.tag.value}]"
            lines.append(f"  {i}. {tag:<9} {step.title}")
            if step.detail:
                lines.append(f"        {step.detail}")
        lines.append("")
        lines.append(
            "Run without --plan to execute. Consent steps prompt unless --yes."
        )
        return "\n".join(lines)


class DeployState(str, Enum):
    """The live state of a deployment, derived from launchd at call time.

    spec/49 MUST 12. Never inferred from a cached sidecar file.

    ``ABSENT``  — no plist on disk; the agent is not deployed.
    ``LOADED``  — plist present and bootstrapped, but no running PID (e.g. it
                  has not started yet, or KeepAlive is between restarts).
    ``RUNNING`` — bootstrapped with a live PID.
    ``CRASHED`` — bootstrapped, no PID, and the last exit status was non-zero
                  (KeepAlive is cycling on a failing program).
    """

    ABSENT = "absent"
    LOADED = "loaded"
    RUNNING = "running"
    CRASHED = "crashed"


@dataclass
class LaunchdStatus:
    """A snapshot of one launchd label's state, read at call time.

    ``state``            the derived DeployState.
    ``label``            the launchd label probed.
    ``plist_path``       path the plist would live at (whether or not it exists).
    ``pid``              the running PID, or None.
    ``last_exit_status`` the last exit code launchd recorded, or None.
    ``healthz_ok``       result of an optional /healthz probe, or None if not
                         probed.
    """

    state: DeployState
    label: str
    plist_path: str
    pid: int | None = None
    last_exit_status: int | None = None
    healthz_ok: bool | None = None
