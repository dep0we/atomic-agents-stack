---
name: atomic-agents-goal
description: Inspect or advance an Atomic Agent's goal state. For goal-driven and hybrid agents that pursue multi-step persistent goals across many sessions.
---

# atomic-agents-goal

Manage the goal state of a goal-driven or hybrid Atomic Agent. Goals live in `<agent>/goal.md` with a sub-goal list; the manager handles status transitions, dispatch order, and history.

## When to use

- The user says "what's \<agent\> working on?" or "where is \<agent\> in the project?"
- After a sub-goal completes, to dispatch the next one
- To abandon a goal that's no longer relevant
- To produce a journal-friendly progress report

This skill is **not** for reactive agents (those without a `goal.md`). If the agent doesn't have one, tell the user and offer to scaffold per `docs/spec/12-goals-and-intent.md`.

## Subcommands

The CLI is `python -m atomic_agents.goal <agent> <subcommand>`.

### `status` — what's the current state?

```bash
python -m atomic_agents.goal <agent> status
```

Shows: goal name, overall completion %, sub-goals broken into pending/in_progress/blocked/complete buckets, the next dispatchable sub-goal.

### `next` — what should the agent work on next?

```bash
python -m atomic_agents.goal <agent> next
```

Returns the highest-priority sub-goal that:
- Is `pending` or `in_progress`
- Has no unresolved `blocked_by` chain

If nothing is dispatchable (everything is complete or all pending sub-goals are blocked), reports that — no exception.

### `advance` — move a sub-goal forward

```bash
python -m atomic_agents.goal <agent> advance <sub-goal-id> --to in_progress
python -m atomic_agents.goal <agent> advance <sub-goal-id> --to complete
python -m atomic_agents.goal <agent> advance <sub-goal-id> --to blocked --reason "<text>"
```

Status transitions enforce sanity (can't go from complete back to pending without explicit `--force`). Each advance appends to `<agent>/goal_history.jsonl`.

### `complete` — entire goal done

```bash
python -m atomic_agents.goal <agent> complete
```

Marks the goal as complete (refuses if any sub-goals are still incomplete unless `--force`), archives `goal.md` to `<agent>/goal_archive/<date>_<slug>.md`, and the agent reverts to reactive mode until a new goal is set.

### `abandon` — stop pursuing this goal (non-destructive)

```bash
python -m atomic_agents.goal <agent> abandon --reason "scope changed; new goal coming"
```

Same as `complete` but recorded as `abandoned`. Goal archive preserved for context.

### `report` — periodic progress for the journal

```bash
python -m atomic_agents.goal <agent> report
```

Produces a markdown progress report with:
- Goal headline + days elapsed
- Sub-goal completion arc
- Pacing analysis (on track / behind / ahead, based on planned vs. elapsed days)
- Recently-completed sub-goals
- Currently-blocked sub-goals + their reasons

Useful as a journal entry — pipe into `<agent>/journal/YYYY-MM/YYYY-MM-DD.md`.

## Modes (reactive vs. goal-driven vs. hybrid)

Set in the agent's persona or `goal.md` frontmatter:
- **Reactive** — no goal; the agent only responds to incoming work items
- **Goal-driven** — every run advances toward the active goal; new work items are evaluated for goal-fit before processing
- **Hybrid** — has a goal but also handles ad-hoc work; the goal is "background"

The goal manager works the same across all three; reactive agents simply have nothing to manage.

## Common follow-ups

- "Run the agent against the next sub-goal" → use `goal next` to fetch the work item, then invoke the run skill with that as the work item
- "Why is this blocked?" → `goal status` shows `blocked_by` reasons inline
- "Show goal history" → `cat <agent>/goal_history.jsonl | jq .`
