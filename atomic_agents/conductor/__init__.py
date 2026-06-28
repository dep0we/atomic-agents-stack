"""atomic_agents.conductor — durable playbook orchestration (spec/50 PR1+PR2).

A conductor run sequences a PLAYBOOK.md through automated stages and human gates,
persisting each stage result to the goal ledger so that crash-restart resumes from
the last completed stage without re-spending it. Gate stages suspend the run and
return control to the operator; resume() injects the human's decision and continues.

Public API (PR1+PR2):

    run(playbook, subject, agent, ...)    — execute or resume a conductor run
    resume(playbook, subject, agent, conductor_run_id, decision_id, answer, ...) —
                                           answer a suspended gate and continue
    discover_playbooks(agent_root)        — discover PLAYBOOK.md manifests
    validate_playbook_manifest(dir)       — parse and validate one PLAYBOOK.md
    ConductorState                        — read-only projection of run state
    GateDecision                          — the durable gate-decision record (PR2)
    PlaybookManifest                      — parsed PLAYBOOK.md
    StageSpec                             — one stage in a playbook

CLI:

    python -m atomic_agents.conductor run <playbook_name> <subject> <agent_root>
    python -m atomic_agents.conductor resume <agent_root> <conductor_run_id> \\
        --decision-id ID --answer TEXT --rationale TEXT --disposition {continue,skip,halt}

PR3 (#582) adds concurrency and conflict serialization.
PR4 (#583) adds launder-guard, doctor check, and spec/50 LOCK.

C1 principle: the conductor holds no authoritative state. ConductorState is a
fresh projection from Goal / Outcome / Idempotency backends each run() call.
"""

from __future__ import annotations

from .playbook import discover_playbooks, validate_playbook_manifest
from .run import resume, run
from .types import ConductorState, GateDecision, PlaybookManifest, StageSpec

__all__ = [
    "run",
    "resume",
    "discover_playbooks",
    "validate_playbook_manifest",
    "ConductorState",
    "GateDecision",
    "PlaybookManifest",
    "StageSpec",
]
