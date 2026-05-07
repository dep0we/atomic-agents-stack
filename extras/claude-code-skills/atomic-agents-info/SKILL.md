---
name: atomic-agents-info
description: Show config for an Atomic Agent without running it — model, fallback, token caps, cost guardrails, read paths, write paths, hard NOs.
---

# atomic-agents-info

Inspect an Atomic Agent's configuration without invoking the LLM. Useful for debugging "is this agent set up the way I think it is?" questions.

## When to use

- The user says "what model does \<agent\> use?" or "show me \<agent\>'s config" or "what can \<agent\> read?"
- Before a first run, to verify paths and budgets are sane
- When troubleshooting a `WritePathViolation` — confirm what's actually whitelisted
- When auditing across agents — pipe through `for a in $(ls $ATOMIC_AGENTS_ROOT); do atomic-agents info $a; done`

## Invocation

```bash
atomic-agents info <agent>
```

If `ATOMIC_AGENTS_ROOT` is not set: `atomic-agents info <agent> --agents-root <path>`.

## Output shape

```
Agent: <name>
Default model:    <model-id>
Fallback model:   <model-id-or-none>
Max input tokens: <int>
Max output tokens: <int>

Cost guardrails:  enabled / disabled
  Daily cap:      $X.XX → action
  Monthly cap:    $X.XX → action
  Warning thresholds: 50%, 80%

Read paths:       <list>
Write paths:      <list>
External APIs:    <list>
Hard NOs:         <list>
```

For cascaded multi-agent project agents, the resolved (role + instance override) values are shown.

## Common follow-ups

- "Why is it using model X?" → check `<agent>/model.md`. For cascaded agents, instance `model.md` overrides the role's.
- "Why can't it write to Y?" → not in `write_paths`. Add it to the agent's `tools.md`.
- "Edit this config" → there's no CLI for editing; open the markdown files in your editor of choice. The agent will pick up changes on the next run.
