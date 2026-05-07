# Claude Code skills

[Claude Code](https://claude.com/claude-code) skills are markdown instruction files that Claude follows when invoked. Each skill here wraps one Atomic Agents CLI surface.

## Install

```bash
# Copy the skills you want into your Claude Code skills directory
cp -R extras/claude-code-skills/atomic-agents-* ~/.claude/skills/
```

After copying, restart Claude Code (or open a new session) and the skills are available — type `/atomic-agents-eval` (etc.) or just describe what you want and Claude will route to the right skill.

## What's here

| Skill | Wraps | When to use it |
|---|---|---|
| [`atomic-agents-run`](atomic-agents-run/SKILL.md) | `atomic-agents run <agent>` | Invoke an agent against a work item interactively |
| [`atomic-agents-info`](atomic-agents-info/SKILL.md) | `atomic-agents info <agent>` | Inspect an agent's config without running it |
| [`atomic-agents-eval`](atomic-agents-eval/SKILL.md) | `python -m atomic_agents.eval` | Run rubric-based evals against golden tests |
| [`atomic-agents-tune`](atomic-agents-tune/SKILL.md) | `python -m atomic_agents.tuning` | Generate or apply a tuning proposal from recent eval runs |
| [`atomic-agents-goal`](atomic-agents-goal/SKILL.md) | `python -m atomic_agents.goal` | Inspect/advance a goal-driven agent's goal state |
| [`atomic-agents-dashboard`](atomic-agents-dashboard/SKILL.md) | `python -m atomic_agents.dashboard` | Render or serve the cost dashboard |
| [`atomic-agents-migrate`](atomic-agents-migrate/SKILL.md) | `python -m atomic_agents.migrate` | Run vault schema migrations or restore a snapshot |

## How skills compose

Each skill is independent, but they're designed to chain naturally:

```
atomic-agents-run → produces a journal entry + log + (maybe) captures
       ↓
atomic-agents-eval → scores the agent's recent behavior against rubrics
       ↓
atomic-agents-tune → analyzes patterns, proposes edits to persona/memory
       ↓
atomic-agents-goal → if goal-driven, records progress + dispatches next sub-goal
```

The dashboard skill is read-only — you can run it any time to see costs and run history.

## Customizing

These are templates. If your team uses different default agents or wants a project-specific naming scheme, copy the SKILL.md, rename the skill (e.g. `acme-eval`), and edit the bash invocations to suit your repo conventions. The skills are just bash wrappers around the CLI; nothing magical.
