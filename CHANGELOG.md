# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-05-06

Initial release. Core framework + cost dashboard.

### Added

**Core framework** (`atomic_agents/`)

- `AtomicAgent` class — canonical agent runtime per spec/04. Loads persona (IDENTITY/SOUL/USER), tools.md, model.md, memory INDEX + recent + pinned notes, wiki INDEX, and recent journal entries; calls the LLM with cost-guardrail enforcement; extracts captures; logs every run to JSONL.
- Helper-mediated atomic captures — parses fenced ` ```atomic_capture ` JSON blocks (incl. quad-backtick fence), validates against schema, writes new memory notes with INDEX updates using atomic temp+fsync+rename pattern.
- Multi-tier cost guardrails — 50% / 80% / 100% thresholds with `skip` / `fallback` / `alert` actions per `model.md`.
- Helper functions — `helper_call` (sequential) and `helper_call_parallel` (ThreadPoolExecutor fan-out, default 5 concurrent) per spec/10.
- Provider routing — Anthropic primary, OpenAI and Moonshot Kimi as optional extras.
- Per-agent file locking — `flock`-based with stale-lock recovery on process death.
- Frontmatter validation per spec/03, including Wave 6 date-suffix filename pattern.
- Secrets loading via env vars, macOS Keychain, or `~/.config/atomic_agents/keys.json`.
- CLI: `atomic-agents run <agent>` and `atomic-agents info <agent>`.

**Cost & observability dashboard** (`atomic_agents.dashboard/`)

- HTML dashboard renderer per spec/09 — global view (all agents) + per-agent drilldowns.
- Aggregations: per-agent costs, model breakdown, helper savings, cache savings, top expensive runs, daily cost chart, monthly trend (12-month rolling), provider breakdown.
- Suggested cap calculator — after 14 days of observed usage, surfaces recommended `daily_cap_usd` and `monthly_cap_usd` for `model.md` `cost_guardrails`.
- Self-contained HTML output (inline CSS, no external assets, no JavaScript dependencies).
- Optional local web server (`python -m atomic_agents.dashboard serve`, port 8765) with `/regenerate` endpoint for the Refresh button.
- Pure Python aggregation — no LLM calls, no external services, ~30 sec for typical scale.

**Tests** (67 total)

- Atomic file I/O (write, append, cleanup, crash recovery)
- Per-agent flock (acquire/release, busy + wait scenarios)
- Schema validation (all required fields, type taxonomy, date-suffix filenames)
- Capture parsing (fenced JSON, dedup, multi-block, quad-backtick fence, write-path enforcement)
- Cost calculation (cache hits, period sums, malformed line handling)
- tools.md + model.md parsers
- Dashboard aggregation (load, summarize, helper savings, cache savings, suggested caps)
- Dashboard rendering (HTML output, per-agent + global, edge cases)

### Notes

- The Atomic Agents specification (`docs/`) describes a layered system: spec docs, implementation guides, sample agents, portability appendix. The spec is the central artifact; this repo is the reference implementation.
- This release contains core + dashboard. Eval, tuning, goals, and migration runners ship in subsequent releases.
- Designed as an open standard — anyone can build agents to the spec, with or without using this Python implementation.
