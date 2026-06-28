"""atomic_agents.conductor — durable playbook orchestration (spec/50 PR1).

A conductor run sequences a PLAYBOOK.md through automated stages, persisting
each stage result to the goal ledger so that crash-restart resumes from the
last completed stage without re-spending it.

Public API (PR1 — automated stages only):

    run(playbook, subject, agent, ...)    — execute or resume a conductor run
    discover_playbooks(agent_root)        — discover PLAYBOOK.md manifests
    validate_playbook_manifest(dir)       — parse and validate one PLAYBOOK.md
    ConductorState                        — read-only projection of run state
    PlaybookManifest                      — parsed PLAYBOOK.md
    StageSpec                             — one stage in a playbook

CLI:

    python -m atomic_agents.conductor run <playbook_name> <subject> <agent_root>

PR2 (#581) adds gate stages (is_gate=True), await_decision(), and resume().
PR3 (#582) adds concurrency and conflict serialization.
PR4 (#583) adds launder-guard, doctor check, and spec/50 LOCK.

C1 principle: the conductor holds no authoritative state. ConductorState is a
fresh projection from Goal / Outcome / Idempotency backends each run() call.
"""

from __future__ import annotations

from .playbook import discover_playbooks, validate_playbook_manifest
from .run import run
from .types import ConductorState, PlaybookManifest, StageSpec

__all__ = [
    "run",
    "discover_playbooks",
    "validate_playbook_manifest",
    "ConductorState",
    "PlaybookManifest",
    "StageSpec",
]
