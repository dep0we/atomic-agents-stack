# Operational extras

Templates and helpers for running Atomic Agents in production. The Python package itself is the core; these are the layers that wrap it for real-world use — schedules, IDE shortcuts, and shell wrappers.

Everything here is **portable** — no hardcoded paths, no host-specific assumptions. Substitute your own paths where templates use `<placeholders>`.

## Contents

| Directory | What's there | Who it's for |
|---|---|---|
| [`claude-code-skills/`](claude-code-skills/) | SKILL.md files for [Claude Code](https://claude.com/claude-code) | Anyone running agents from inside Claude Code |
| [`launchd/`](launchd/) | macOS LaunchAgent `.plist` templates | macOS users who want agents to run on schedule |
| [`cron/`](cron/) | Crontab examples + a portable shell wrapper | Linux users (or macOS users who prefer cron over launchd) |
| [`gcp/`](gcp/) | Cloud Run + IAP + Cloud Scheduler reference deployment | Operators deploying to Google Cloud (GCP-as-harness pattern) |

## Prerequisites

All of these assume:
1. The `atomic-agents-stack` package is installed (`pip install` from this repo, or `uv sync` in a clone).
2. Your `ATOMIC_AGENTS_ROOT` environment variable points at the directory holding your agent folders. You can also pass `--agents-root` to every CLI command if you'd rather not set the env var.
3. Your provider API key is available (env var, macOS Keychain, or `~/.config/atomic_agents/keys.json`). See the top-level README for the load order.

## Quick orientation by use case

- **"I want to run an agent on a schedule."** → [`launchd/`](launchd/) on macOS, [`cron/`](cron/) on Linux.
- **"I want to run agent commands from Claude Code."** → [`claude-code-skills/`](claude-code-skills/), copy the skills you want into `~/.claude/skills/`.
- **"I want to bake this into my CI."** → look at `claude-code-skills/atomic-agents-eval/SKILL.md` — the underlying CLI invocations work in any CI runner.
- **"I want to deploy an agent to Google Cloud."** → [`gcp/`](gcp/) — Cloud Run + IAP + Cloud Scheduler reference deployment (GCP-as-harness pattern).

## Versioning

These templates target the same package version as the surrounding repo. CLI flags and module names are stable across the v0.x series; if a flag changes in a later release, the corresponding SKILL.md and templates update in the same PR.
