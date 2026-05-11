# Governance for Atomic Agents — solo operators and small teams

This guide covers what you need to know before deploying an Atomic Agent autonomously. It's sized for real conditions: one person managing a few agents on their laptop, or a 2-10 person team sharing a vault. For enterprises running 100+ agents with formal change management, treat this as a starting point and layer appropriate process on top.

If you haven't shipped your first agent yet, start with [getting-started.md](getting-started.md) first. This guide assumes you've gotten something running and now you're asking: *"Is it safe to let this run on its own?"*

---

## When this guide applies

**Solo operator:** you built the agent, you own the vault, you're the only person running things. You probably have 1-5 agents. The risk model is personal: mistakes cost you tokens, time, or unwanted writes to your filesystem. No organizational exposure.

**Small team:** 2-10 people sharing a vault in git. Someone made an agent and wants a colleague to use it. The risk model expands slightly: someone else's agent can misbehave on your machine, and the team needs enough shared convention to not step on each other. A simple agent registry and naming convention are all the governance structure you need.

For either case, the goal is the same: before an agent runs on a schedule without anyone watching, you should be confident it won't do something expensive, destructive, or confusing. This guide gives you the checklist and the ongoing habits to stay there.

---

## The risk model — what can an Atomic Agent actually do wrong?

### Bad capture writes

**The risk.** The capture system is how agents write to memory — a model-emitted JSON payload that the helper writes to `memory/` as an atomic note and updates the INDEX. The write path is how the agent learns. A badly formed capture, a maliciously crafted work item, or a permissive write path could result in notes written to wrong locations, INDEX corruption, or in the worst case, writes outside the agent's own folder.

**Existing mitigations.** Path traversal in the `merge_into` field was closed in PR #29. Orphan recovery (note written but INDEX not updated) was handled in PR #35. Memory versioning (PR #46) snapshots every overwrite before it happens, so you can roll back a bad write with `restore_version`. The capture schema validation rejects any payload missing required fields or using unknown types before anything hits disk.

**What still requires your vigilance.** The framework validates structure, not factual accuracy. An LLM-emitted capture can pass schema validation and still write a misleading note — for example, a `feedback` note that says "the user prefers X" when that's not true, introduced via a crafted work item. Review captures in new agents' early runs. The write paths in `tools.md` are the real containment boundary: keep them narrow.

---

### Cost runaway

**The risk.** An agent on a cron schedule or in a delegation loop makes repeated LLM calls. If cost guardrails are disabled or caps are set too high, a misconfigured schedule or an infinite-ish tool loop can run up API costs before you notice.

**Existing mitigations.** `model.md` carries a `cost_guardrails` block with per-day and per-month caps, warning thresholds at 50% and 80%, and configurable cap actions (`skip`, `fallback`, `alert`). These are enforced by the shared helper before every LLM call. The custom tools loop (PR #45) has an iteration cap (default 5, max 20) so a tool-calling agent can't loop forever. Parallel helper batch reservation (PR #35) pre-checks whether the whole fan-out can fit in the coordinator's headroom before spawning any threads.

**What still requires your vigilance.** Cost guardrails default to `enabled: false` for new agents. This is intentional — you want to observe real usage before guessing at caps — but it means a new agent on cron has no cap at all. Flip to `enabled: true` and set reasonable values before scheduling any agent for unattended runs. Check the cost dashboard weekly during the first month. The dashboard is at `<agents_root>/_dashboard/index.html` and can be regenerated with `python -m atomic_agents.dashboard render`.

---

### Tool handler misbehavior

**The risk.** Custom tools (PR #45) let operators attach Python callbacks that the LLM can invoke during inference. The framework validates the input against the JSON Schema and catches handler exceptions — but the handler itself is your code. If your handler has a SQL injection vulnerability, runs unsanitized subprocess commands, or makes external writes outside the agent's write paths, that's on you.

**Existing mitigations.** `ToolCallResult.error` captures handler exceptions without letting them abort the run. The iteration cap prevents a bad tool from being called indefinitely. Every tool call gets its own JSONL log line with `tool_name`, `latency_ms`, and `error` — so failure is visible.

**What still requires your vigilance.** The framework cannot inspect what your handler does. Before attaching a custom tool: verify it error-handles its own failures explicitly, does not call `eval()` or `subprocess.run` on unsanitized model input, and does not write to paths outside the agent's declared write paths. Treat the handler as operator-owned code with the same care you'd give any code that processes externally-influenced input.

---

### Delegate cascade

**The risk.** Delegation (PR #43, spec/15) lets a coordinator agent call specialist agents at runtime. A misconfigured roster, a coordinator without a cost cap, or a specialist that unexpectedly delegates further could create a call tree that's hard to audit and expensive to run.

**Existing mitigations.** Delegation is one level only — a specialist cannot sub-delegate (enforced by `SelfDelegationError` and the absence of a nested roster mechanism in v1). The coordinator's cost cap governs the entire tree: `delegate_parallel` pre-reserves worst-case cost before spawning any threads and raises `CostGuardrailBlocked` if the reservation exceeds headroom. Every delegate call logs both the coordinator's JSONL line and the target's own log line, linked by `delegate_run_id`. Self-delegation is explicitly refused.

**What still requires your vigilance.** The roster in `roster.md` is a declaration, not a trust grant. Only list agents you actually trust and understand in a coordinator's roster. An agent you don't control shouldn't be in a roster unless you've reviewed its persona, tools, and write paths. Keep rosters small — Anthropic's own guidance suggests ≤20, and for solo/small-team use, 3-5 is plenty.

---

### Memory drift

**The risk.** Over months, an agent's memory accumulates notes that are stale, contradictory, or superseded. A stale `feedback` note that says "prefer format X" when the real preference has changed to Y causes the agent to behave inconsistently. An agent with contradictory memories on the same topic doesn't know which is true.

**Existing mitigations.** The spec's lint pass (spec/05) surfaces duplicates, contradictions, stale notes (last_seen > 90 days), expired notes, orphans, and schema drift. The dream pipeline (PR #44) does periodic memory consolidation — distilling and archiving across the memory layer. The tuning analyzer (spec/11) surfaces patterns from eval history. Memory versioning (PR #46) preserves full history before every overwrite.

**What still requires your vigilance.** Lint is a signal, not an automatic action. Run it periodically (`python -m atomic_agents.eval <agent>` surfaces quality issues; the lint report lands at `<agent>/log/lint_YYYY-MM-DD.md`) and resolve what it surfaces. The dream pipeline needs to be triggered manually or on a schedule — it doesn't run itself. And promotion from memory to persona requires your review, never auto-promotion.

---

### Skill content drift

**The risk.** Skills are reusable expertise modules stored as markdown files in `<agent>/skills/<skill-name>/SKILL.md`. Like any documentation, they can go stale: the instructions describe an API that changed, reference a file that moved, or use terminology that no longer matches the agent's domain.

**Existing mitigations.** The `atomic-agents skills <agent>` CLI command validates every skill's manifest (name format, required fields, description length, body line count) and reports warnings. The body length warning (> 500 lines) is a proxy for "this skill is getting too big to maintain." Skills are plain files — they're in git and diffable.

**What still requires your vigilance.** The linter checks structure, not content freshness. Review each deployed agent's skills when you do quarterly persona reviews. If the workflow a skill describes has changed, the skill's instructions need to change too. An outdated skill is worse than no skill — the agent follows the wrong guidance with confidence.

---

### Persona injection via captured input

**The risk.** The capture pathway is triggered by LLM output — the model decides to emit a capture marker based on the work item it received. A work item that's been crafted to push the model toward writing a specific note ("remember that the user prefers to always include X") could result in a capture that misrepresents the operator's actual preferences.

**Existing mitigations.** The capture schema enforces structural validity — required fields, locked type taxonomy, field length limits. Tool-call captures (Path 1) go through SDK-level schema validation before the helper sees them. The write path enforcement means a capture can't write outside `memory/`.

**What still requires your vigilance.** The framework validates structure, not intent. For agents that process external input (emails, user-submitted text, scraped content), review captures in early runs to verify the model isn't writing notes that over-generalize from one unusual input. The principle: memory should reflect your actual preferences, not the preferences of whoever sent a work item.

---

## Pre-deploy checklist

Run through this before enabling any scheduled or autonomous run. A "no" is a blocker — fix it first.

- [ ] `persona/IDENTITY.md` describes the agent's scope and explicitly states what's out of scope. Ambiguous scope is the most common source of unexpected behavior.
- [ ] `tools.md` write paths are specific and minimal. No write path of `/`, `~/`, or `~/<user>/`. Write access scoped to the agent's own subfolder.
- [ ] `tools.md` has a `## Hard NOs` section listing actions this agent must never take, regardless of what it's asked.
- [ ] `model.md` has `cost_guardrails.enabled: true` with sensible daily and monthly caps. If you're not sure what "sensible" looks like, run the agent manually a few times first, check the dashboard, and use 3× the observed daily average as the cap.
- [ ] If the agent uses custom tools: every handler has explicit error handling on the failure path. No unescaped subprocess calls, no `eval()`.
- [ ] If the agent has a `roster.md`: every agent in the roster is one you understand. You've reviewed its `tools.md` and `persona/IDENTITY.md`.
- [ ] If the agent has skills: `atomic-agents skills <agent>` runs with zero hard errors. Warnings addressed or deliberately accepted.
- [ ] The agent has at least 3 golden eval tests covering a happy path, an edge case, and an adversarial input. If you haven't written evals yet, this is the moment — evals before autonomous deployment, not after.
- [ ] You've run the agent manually at least 3-5 times and reviewed the outputs. First-run surprises are easier to handle before the schedule starts.
- [ ] The first week of scheduled runs will be monitored. Commit to checking the log manually each day for the first 7 days. After that, weekly is enough.
- [ ] Sensitive material (PII, secrets, credentials) is not present in `memory/`. If any note contains sensitive content, use `redact_version()` to scrub it while preserving the audit trail.
- [ ] If the agent runs on another machine: the vault is synced via git (or Obsidian Sync), not manual file copy. The `.versions/` directory and `_cache/` are gitignored appropriately.
- [ ] You know how to stop the agent if something goes wrong. For launchd: `launchctl unload <plist>`. For cron: `crontab -e` and delete the entry. For Claude Code skills: the skill wrapper doesn't auto-schedule — invocations are always user-initiated.

---

## Naming conventions

Consistent names prevent the "what does this agent actually do?" question six months from now.

### Agent names

Lowercase, hyphens as word separators, descriptive. Pick a name that says something about the role.

Good: `financial-advisor`, `research-brief`, `calendar-digest`, `code-reviewer`
Bad: `agent1`, `helper`, `my-agent`, `test`

Avoid reusing names across teams without a namespace prefix (`acme-research-brief` vs `generic-research-brief`). The name is a directory name — it appears in paths, logs, and dashboard tables.

**Reserved words — don't use as agent names or prefixes:** `claude`, `anthropic`, `atomic_agents`, `atomic-agents`.

### Memory note filenames

The locked taxonomy is `{type}_{short_topic}.md`. Type is one of: `user`, `feedback`, `project`, `decision`, `reference`. Short topic: lowercase, underscores, descriptive.

Good: `feedback_response_format.md`, `decision_debt_payoff_order.md`, `user_communication_style.md`
Bad: `note1.md`, `memory_2026.md`, `temp.md`

Don't include dates in the filename — dates go in frontmatter (`captured`, `last_seen`).

### Skill names

Gerund form (present-participle verb phrase), lowercase, hyphens.

Good: `financial-modeling`, `contract-review`, `data-extraction`
Bad: `finance`, `contracts`, `Data`, `financial_modeling`

The gerund form signals "something the agent does with this skill," which helps the model route correctly. Noun forms are acceptable when gerunds are awkward (`tax-law-us`, `python-pandas`).

**Reserved words** for skill names: same list as agent names — `anthropic`, `claude`, `atomic_agents`.

### Tool names

Namespaced with an underscore separator: `{source}_{action}`.

Good: `db_query`, `gcal_fetch`, `slack_post`, `fs_read`
Bad: `query`, `get`, `tool1`

Namespacing prevents collision when multiple tools are registered and makes log lines readable (`"tool_name": "db_query"` tells you more than `"tool_name": "query"`).

---

## The agent registry — knowing what you've deployed

Without a registry, you'll forget what you shipped. The cost dashboard shows you costs, but it doesn't tell you *why* an agent exists, when you last reviewed it, or what triggers it. A simple `AGENTS.md` in your vault root fills that gap.

### Solo operator

Create `<agents_root>/AGENTS.md`. One entry per deployed agent. Update it when you deploy or retire an agent.

### Small team

Same shape, but keep it in the shared git repo. Add a "primary contact" field per agent — whoever's responsible for reviewing it. Anyone pushing changes to an agent runs through the pre-deploy checklist before merging.

### Template

Below is a starter template. Copy this into `<agents_root>/AGENTS.md` and fill it in.

```markdown
# Agent Registry

Last updated: YYYY-MM-DD

## Active agents

### <agent-name>

**Purpose:** One sentence: what this agent does and why it exists.
**Owner:** Who to ask questions about this agent.
**Last reviewed:** YYYY-MM-DD — what changed or was confirmed.
**Triggers:**
- Cron: `0 7 * * *` (daily at 07:00 CT) via launchd plist `com.atomic-agents.run.<agent-name>.plist`
- Skill: `/atomic-agents-run` in Claude Code
**Skills deployed:** List of `<agent>/skills/` subdirectories, or "none"
**Custom tools:** List of tool names in the ToolRegistry, or "none"
**Roster:** List of agent names in `roster.md`, or "none"
**Notes:** Anything an operator picking this up cold needs to know.

---

<!-- Add one section per agent. -->

## Retired agents

| Agent name | Retired | Reason | Archive path |
|---|---|---|---|
| research-v1 | 2026-03-15 | Superseded by research-v2 | <agents_root>/_archive/research-v1-20260315/ |
```

### Example entry

```markdown
### financial-advisor

**Purpose:** Personal financial advisor — tracks household finances, debt payoff
progress, and spending patterns. Runs a daily digest at 07:00 CT.
**Owner:** &lt;your-name&gt;
**Last reviewed:** 2026-05-07 — confirmed debt payoff order is still correct.
**Triggers:**
- Cron: `0 7 * * *` daily via `com.atomic-agents.run.financial-advisor.plist`
- Skill: ad-hoc via `/atomic-agents-run` in Claude Code
**Skills deployed:** financial-modeling
**Custom tools:** db_query (read-only SQLite, household budget DB)
**Roster:** none
**Notes:** Write paths are scoped to `~/agents/financial-advisor/`. Never touches
external accounts. Hard NO on any outbound financial action.
```

---

## Versioning your vault

The vault is plain markdown. Git it.

```bash
cd <agents_root>
git init
# Add a .gitignore first (see below)
git add .
git commit -m "initial vault"
```

### What to gitignore

```gitignore
# Cache directories — derived from markdown, per-machine
**/_cache/

# Lock files — runtime-only
**/.lock

# Shell wrapper logs (if you keep them near the vault)
*.log
```

The `.versions/` directories (memory versioning, PR #46) **should** be committed — they are the audit trail and are part of the vault's source of truth. Don't gitignore them.

### Branch and tag discipline

- Experiment on a branch. Never experiment on the live vault directly. A branch named `experiment/caldwell-new-soul` is far easier to abandon than a half-modified live persona.
- Tag when behavior stabilizes: `git tag agents-v1.0`. This gives you a rollback point before major changes (new agent, significant persona edit, tuning proposal applied).
- Per-note rollback is covered by memory versioning: `restore_version()` rolls a single note back to any snapshot. Use this for individual note mistakes; use git for larger rollbacks.

### Multi-machine setups

If the vault runs on two machines (e.g., dev laptop and always-on server), git is the right sync mechanism — not rsync, not Obsidian Sync alone, not manual file copy. Obsidian Sync can layer on top of git for real-time accessibility, but git is the source of truth and the merge layer.

Never copy agent folders between machines by hand when both machines have run the agent. You'll lose log entries or capture writes from one side.

---

## When to deprecate an agent

Agents accumulate. Without explicit retirement, you end up with agents that no one uses, run quietly on a cron schedule, and occasionally surface in the cost dashboard as unexplained spend.

### Triggers that should prompt a deprecation decision

- Eval scores trending downward over 3+ consecutive weekly runs with no clear explanation.
- The agent hasn't been invoked in 30 days (check the cost dashboard's per-agent table).
- The workflow it was built for has ended or changed substantially.
- You haven't reviewed its log in 60+ days — if you don't care enough to check it, the agent probably shouldn't be running.
- A newer agent supersedes it and you've migrated whatever work it was doing.

These aren't automatic kill switches. They're signals to make a conscious choice: retire it, revive it, or acknowledge it's still useful and reset your review clock.

### Retirement process

1. Remove the scheduled trigger. `launchctl unload <plist>` on macOS; delete the crontab entry on Linux.
2. Archive the agent's folder: `mv <agents_root>/<agent-name> <agents_root>/_archive/<agent-name>-<YYYYMMDD>/`
3. Remove the agent's Claude Code skill files if any were installed at `~/.claude/skills/`.
4. Update `AGENTS.md` — move the agent to the "Retired agents" table with date and reason.
5. Commit the archive and the updated `AGENTS.md`.

Don't delete the folder immediately. Archive first; delete after 90 days if nothing comes back to bite you. The journal, log, and memory are often more useful than you expect when debugging a successor agent.

---

## Periodic operator practices

A cadence that works for solo operators and small teams. Nothing here requires much time — most of these are a few-minute scan.

### Daily (when an agent is in its first week of autonomous runs)

- Check the agent's log for yesterday's runs: `cat <agent>/log/YYYY-MM/YYYY-MM-DD.jsonl | python -m json.tool | less`
- Look for `"status": "error"` or `"status": "skipped"` entries. Errors need triage. Skips mean a cost cap was hit — verify it was expected.
- If the agent writes captures, spot-check one or two: do they reflect what actually happened in the work item?

### Weekly

- Skim the cost dashboard's global view. Any agent spiking unexpectedly?
- Scan each deployed agent's last 3-5 log entries. Any pattern of errors, cost escalation, or unusual model switches?
- Check for lint report files in each agent's log directory. If one exists, review it. Resolution doesn't have to be immediate, but it shouldn't sit for more than two weeks.

### Monthly

- Run evals on each agent: `python -m atomic_agents.eval <agent>`. Verify scores are stable or improving. A consistent decline is an early warning that the model, the persona, or the world has changed.
- Check the cost dashboard's month-over-month chart. Any agent trending significantly upward without a corresponding increase in use?
- Review `AGENTS.md`. Has every entry been touched in the last 30 days? If not, mark it for review.

### Quarterly

- Run the dream pipeline for memory consolidation: `python -m atomic_agents.dream <agent>`. Review the report. Apply or discard proposed consolidations — don't auto-accept.
- Run the tuning analyzer: `python -m atomic_agents.tuning <agent>`. Look at what patterns it surfaced. Decide whether any proposed edits to persona or memory are worth applying.
- Review each agent's persona files (`IDENTITY.md`, `SOUL.md`, `USER.md`). Do they still accurately describe what you want the agent to do? Personas drift in the operator's head before they drift on disk.
- Review each agent's skills with `atomic-agents skills <agent>`. Any warnings unresolved from last quarter?

### Annually

- Re-read every deployed agent's complete persona. A year's worth of incremental edits can compound into something that no longer makes sense read top-to-bottom.
- Check for stale memory notes with `last_seen` more than a year ago that aren't pinned. Archive candidates per spec/05.
- Consider whether any agents should be consolidated, split, or retired.

---

## Audit and observability

Every autonomous run produces an audit trail without any operator action required.

**Run-level logs.** Every invocation appends a JSONL line to `<agent>/log/YYYY-MM/YYYY-MM-DD.jsonl`. Minimum fields: `ts`, `trigger`, `model`, `input_tokens`, `output_tokens`, `status`, `summary`. Full records include `cost_usd`, `cache_hit_tokens`, `latency_ms`, and error detail when status is `error`.

**Tool call logs.** Custom tool invocations each get their own JSONL line (`"trigger": "tool_call"`) with `tool_name`, `latency_ms`, `error`, and a `parent_run_id` linking back to the parent run. Same format for delegate calls (`"trigger": "delegate"`) and helper calls (`"trigger": "helper"`).

**Memory versioning.** Every overwrite or merge of a memory note creates an immutable snapshot in `<agent>/memory/.versions/<note-stem>/`. Versioning events append JSONL lines to the per-day log (`memory_version_created`, `memory_version_restored`, `memory_version_redacted`). This means every memory mutation is traceable — who wrote it, when, what changed.

**Cost dashboard.** `python -m atomic_agents.dashboard render` aggregates all JSONL logs into an HTML dashboard at `<agents_root>/_dashboard/index.html`. Per-agent drill-downs at `<agent>/dashboard.html`. No daemon, no server required for static viewing.

**Compliance and PII.** If a memory note contains sensitive content that shouldn't persist, use `redact_version()` to replace the body while preserving the frontmatter audit trail. The frontmatter stays (timestamps, type, sources) and the body becomes the replacement string you provide (e.g., `"[REDACTED — PII removed]"`). This is the right tool for compliance situations where you need audit evidence that a note existed but can't retain its content.

**Log retention.** JSONL files accumulate in monthly directories. There's no auto-expiry — the operator manages retention. For a 5-agent system running daily, a year of logs is typically under 10MB. Keep at least 3 months. Keep more if you're running evals, since eval scoring needs log history to compute trends.

---

## Cross-surface considerations

Unlike Anthropic's platform (which has multiple distinct surfaces — API, console, claude.ai, Claude Code), Atomic Agents has one surface: the vault on disk. But the same agent can be invoked from different triggers, and those differences matter.

**One agent, multiple invocation paths.** A `cron`-triggered run, a `/atomic-agents-run` skill call in Claude Code, and a direct `atomic-agents run` CLI invocation all hit the same agent folder with the same persona, memory, and tools. The trigger type is logged (`"trigger": "cron"` vs `"trigger": "skill"` vs `"trigger": "manual"`), but the agent's behavior is the same. There's no "separate cron deployment" vs "separate skill deployment" — it's the same files.

**No automatic propagation across agents.** A skill added to `agent-a/skills/` does not appear in `agent-b/skills/`. If you want two agents to share a skill, copy the folder to both. Copying is a deliberate decision — you're committing to maintaining that skill in two places, or you set up a symlink and accept that both agents will see every change.

**Git as the sync mechanism.** If the vault runs on more than one machine, git is how changes move. Conflicting writes (two machines wrote different captures to the same note) are merge conflicts — resolve them the same way you'd resolve any markdown conflict. Memory versioning helps: you can read both versions' history before deciding which to keep.

**Claude Code skill wrappers are thin.** The wrappers in `extras/claude-code-skills/` are bash invokers around the CLI — they don't add logic, state, or separate configuration. They follow the same `ATOMIC_AGENTS_ROOT` environment variable the CLI uses. If a skill isn't finding your agents, check the environment, not the skill wrapper.

---

## What this framework intentionally does NOT do

These are scope decisions, not bugs.

- **No org-level skill registry.** Skills live per-agent. If you want an org-wide catalog of skills, that's a convention in git (`agents/_shared_skills/`) that you enforce yourself.
- **No usage analytics beyond the cost dashboard.** The dashboard aggregates token and cost data. There's no "who invoked what" audit across users, no usage frequency tracking per feature, no engagement reporting. JSONL files are the raw data — aggregate them yourself if you need more.
- **No formal RBAC or access control.** File system permissions are the access control layer. Who can read and write which agent's folder is an OS-level concern, not a framework concern.
- **No automatic skill propagation across agents.** See above.
- **No managed multi-tenant deployment.** Every deployment is operator-managed. There's no hosted runtime, no tenant isolation layer, no SLA.
- **No cross-agent budget pooling.** Cost caps are per-agent. There's no "all agents combined: $X/month" enforcement in v1.

These choices keep the framework portable and simple for the target use case. For organizations that need these features, see [Anthropic's enterprise agent skills guide](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/enterprise) as a reference for what enterprise-scale governance looks like.

---

## Red flags that should stop you

Stop and fix before continuing if any of these apply.

**Cost guardrails disabled, agent scheduled on cron.** An unguarded cron agent has no cost ceiling. A loop, a misconfigured schedule, or a runaway tool call can burn through daily API budget before you notice. Enable guardrails first.

**Write path includes `/`, `~/`, or any path broader than `<agents_root>/`.**  This means the agent could write anywhere on the filesystem the process has access to. Scope write paths to the agent's own subfolder.

**Custom tool handler calls `eval()` or `subprocess.run()` on model-supplied input without sanitization.** The model output is not controlled input. A crafted work item could pass a malicious string to a handler that executes it. This is a real code-execution risk.

**Skill content references a file outside its skill directory.** `load_skill_file` blocks path traversal (`../`) via the `SkillFileTraversal` exception, but manual review is still worth doing. A skill body that references `../../persona/IDENTITY.md` won't load the file, but it's a sign the skill was authored incorrectly.

**Roster includes an agent whose `tools.md` you haven't reviewed.** You're delegating LLM calls to that agent. Its write paths and hard NOs are now part of your execution surface. Review them before including it in any roster.

**`memory/` contains secrets or PII in plain text.** API keys, passwords, SSNs, or medical information in memory notes are a problem: they land in context, they show up in logs (via `summary` fields), and they persist indefinitely. Move secrets to their designated stores (environment variables, macOS Keychain, `~/.config/atomic_agents/keys.json`). Use `redact_version()` to clean up any notes that already contain sensitive content.

**The agent has been modified and no one ran through the pre-deploy checklist.** Changes to `tools.md`, `roster.md`, or custom tool handlers are the most common sources of unexpected behavior after initial deployment. Treat every material change as a re-deployment and run the checklist again.
