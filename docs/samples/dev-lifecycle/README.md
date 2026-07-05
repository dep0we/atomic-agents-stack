# dev-lifecycle — sample conductor playbook

This folder is a worked example of a **conductor playbook**: a durable, resumable
orchestration of a multi-stage process with explicit human gates. It encodes the
dev-process-kit software-change lifecycle (idea validation through ship, deploy,
and document) as a single conductor run.

## What you're looking at

```
dev-lifecycle/
└── skills/
    └── dev-lifecycle-playbook/
        └── PLAYBOOK.md      ← the playbook manifest (this is the whole sample)
```

A playbook is a skill whose `SKILL.md`-style manifest is named `PLAYBOOK.md` and
carries a `stages:` block. `discover_playbooks(<agent_root>)` finds it by scanning
`<agent_root>/skills/*/PLAYBOOK.md`, so dropping this folder under an agent root
(or pointing the conductor at this directory as the agent root) makes the playbook
discoverable with no other wiring.

[`skills/dev-lifecycle-playbook/PLAYBOOK.md`](skills/dev-lifecycle-playbook/PLAYBOOK.md)
encodes 11 kit lifecycle stages as **18 conductor stages** — 10 automated stages
that do the work and 8 human gates where you rule before the run continues. Every
gate ruling is recorded in the conductor decision ledger (the run's
`goals/<conductor_run_id>/goal_history.jsonl`) with its author, timestamp, and
rationale, so the scattered-decisions problem is closed: one queryable record of
every decision that shaped the change.

## How it runs

A conductor run advances through the stages in order. Automated stages dispatch an
`agent.call()`/outcome and continue. Gate stages **suspend** the run, returning a
`GateDecision` for a human to answer; the operator resumes the run with a ruling
(`continue`, `halt`, or `skip` where offered), and the run picks up from the
durable ledger cursor. Kill the process and resume later — the ledger is the
resume cursor, so a long, multi-day change survives restarts.

The run carries a `run_cap_usd` ceiling pinned at creation (tree-cap), so the whole
lifecycle has a bounded spend.

## Things to know about this sample

- **Stages 0 (project bootstrap) and 12 (ongoing maintenance)** are documented as
  context prose above and below the YAML block. They are *not* conductor stages —
  they run once-per-project or on their own schedule, not per change.
- **Per-stage `model:` dials are applied** ([#668](https://github.com/dep0we/atomic-agents-stack/issues/668)).
  The 10 automated stages each declare `model: claude-sonnet-4-6-20260101`, and the
  conductor passes it as `model_override=` to `agent.call()` at dispatch (Policy
  enforce-mode `get_effective_model` supersedes it when fleet config is active, per
  spec/32 "fleet-config wins"). Gate stages carry **no** `model:` field — they make
  no actor LLM call (they suspend for a human ruling), so `model:` on a gate stage is
  rejected at parse time as a hard validation error.
- **Automated-stage prompts are generic.** They approximate the intent of each kit
  step in plain language; they do not invoke the kit's slash-commands. This sample
  is content, not a coupling to the kit's tooling.
- **The merge gate's `conflict_keys: ["merge:main"]`** serializes the merge-approval
  gate across concurrent runs. The exact boundary (what the key serializes, and
  when it releases) is documented once, authoritatively, in the merge-gate stage
  comment in `PLAYBOOK.md` — read it there rather than relying on a restatement here.

To adapt this for your own process, copy `PLAYBOOK.md`, rewrite the stage prompts
and gates to match your lifecycle, and set your own `run_cap_usd`.
