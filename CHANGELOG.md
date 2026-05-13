# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
See [`docs/deployment/versioning.md`](docs/deployment/versioning.md) for the full
SemVer policy and what counts as a Major / Minor / Patch change for this project.

## Conventions

Each released version uses `## [MAJOR.MINOR.PATCH] - YYYY-MM-DD`. Within a
release, entries are grouped under these section headings (Keep a Changelog
v1.1.0, in this canonical order):

- **Added** — new user-visible features, modules, or CLI surfaces.
- **Changed** — non-breaking changes to existing behaviour or wording.
- **Deprecated** — APIs slated for removal in a future Major; still functional.
- **Removed** — APIs deleted in this version. Always paired with a Major bump
  unless pre-1.0 (see versioning.md §Pre-1.0 caveat).
- **Fixed** — bug fixes that don't change documented behaviour.
- **Security** — vulnerabilities fixed; reference CVE / advisory if any.

### Marking breaking changes

Any change that would force operators to do work to upgrade — schema rename,
CLI flag removal, default-value flip, default-backend swap, frontmatter
contract change — gets a `### BREAKING` callout under the version's section.
The callout names the symptom an operator would observe, the migration
path (typically a `python -m atomic_agents.migrate --to vN` invocation),
and a one-line rationale. Example:

```
## [0.2.0] - 2026-06-01

### BREAKING

- **Memory schema bumped 2 → 3.** Frontmatter `confidence` field is now an
  enum (`high`/`medium`/`low`) rather than a freeform string.
  Migrate: copy `docs/migrations/v2_to_v3.py` into `<vault>/_migrations/`,
  then run `python -m atomic_agents.migrate --to v3`.
  Why: enables typed conformance tests in the eval runner.
```

The pre-merge expectation is: if a PR's diff would trigger a `### BREAKING`
callout, the PR title starts with `feat!:` / `refactor!:` and the PR body
includes the callout text verbatim, ready to paste into the next release's
CHANGELOG entry.

## [Unreleased]

### Changed

- **`_costs.sum_cost_for_period()` — optional `source` + `mandate_id` filters ([#122](https://github.com/dep0we/atomic-agents-stack/issues/122)).** Two new keyword-only parameters: `source` (`"actor"` / `"judge"` / `"audit"` / `None`) filters cost events by their origin per spec/28's actor/judge ledger split and spec/30's audit-cost extension; `mandate_id` filters by the authorizing mandate per spec/29. Filters AND together; both default to `None` (legacy behavior — sum every record). Cost-event JSONL records now carry `cost_source: "actor"` explicitly; legacy records without the field are treated as actor spend on read so existing logs continue to read correctly. The 8 existing actor cost-guardrail callers (6 in `agent.py`, 2 in `dream.py`) now pass `source="actor"` defensively — zero behavior change today, but ensures the actor cap won't double-count judge or audit spend when the judge ([#112](https://github.com/dep0we/atomic-agents-stack/issues/112)) and audit ([#125](https://github.com/dep0we/atomic-agents-stack/issues/125)) runtimes start writing to the same log files. Foundation for spec/28 judge ledger, spec/29 mandate budgets, and spec/30 audit ledger. 13 new unit tests covering each filter combination, legacy-record compat, multi-source aggregation, and pinned behavior on unrecognized `cost_source` values. No breaking changes — all existing call sites and log files continue to work unchanged.

- **`CLAUDE.md` §"What we're explicitly NOT building"** — added Graphify-style knowledge-graph integration to the list of explicitly-deferred ideas, with rationale (INDEX-driven recall is the load-bearing memory pattern per spec/02 + spec/04; graphify's "visualize the whole graph" aesthetic is the opposite; substrate mismatch — graphify's strength is code semantics, not YAML + JSONL). Mirrors the canonical entry in `~/ObsidianVault/Atomic Agents/ROADMAP.md` §"What I'd NOT pursue". Three lightweight in-framework alternatives filed as separate issues ([#138](https://github.com/dep0we/atomic-agents-stack/issues/138) goal timeline, [#139](https://github.com/dep0we/atomic-agents-stack/issues/139) delegation cost treemap, [#140](https://github.com/dep0we/atomic-agents-stack/issues/140) spec cross-reference Mermaid diagram).

### Added

- **`atomic-agents review --backend kimi` subcommand ([#134](https://github.com/dep0we/atomic-agents-stack/issues/134)) — cross-family adversarial code review via Moonshot.** Closes the gap that surfaced during PR #117 + #118 spec reviews when Codex hung mid-round and the only fallback was an Opus subagent (same model family as the author). New `atomic_agents/review.py` module with a `ReviewRequest` / `ReviewResult` dataclass surface and a built-in system prompt that enforces CLAUDE.md rule #12 (verify before claim — every finding must quote file:line evidence). CLI accepts `--prompt` or `--prompt-file`, optional `--target` (primary file under review), `--read-files` (comma-separated grounding context), `--working-dir`, `--model`, `--max-tokens` (default 16000 — reasoning-style Moonshot models like Kimi K2.x consume a large slice for internal `reasoning_content`). All operator-supplied paths flow through the framework's canonical `_io.safe_resolve_under` guard — `..` segments and absolute paths that escape `--working-dir` raise `PathTraversalError` (matches the discipline used everywhere else paths cross a trust boundary). Empty/whitespace-only `--prompt` and `--prompt-file` are rejected with exit 1 before the LLM call (no silent paid no-op). When a reviewer returns empty visible content (the documented K2.x thinking-model failure mode — see [#146](https://github.com/dep0we/atomic-agents-stack/issues/146)), a WARNING precedes the cost summary so operators don't pay for blank reviews silently. Review output writes to stdout; cost summary to stderr so piping to a file doesn't pollute the artifact. Default model is `moonshot/moonshot-v1-128k` (non-thinking, produces visible content reliably); Kimi K2.6 / K2.5 are priced in the table but require [#146](https://github.com/dep0we/atomic-agents-stack/issues/146)'s reasoning_content extraction work before they become the recommended reviewer. 26 unit tests covering prompt assembly, path-traversal refusal (relative, absolute, target + read-files), file resolution, backend dispatch, model overrides, cost summary stream routing, empty-content warning, empty-prompt guards, and CLI integration. Live-validated against open PR #145 ($0.005 / 21s per review). `docs/methodology.md` §"Codex as a real outside voice" gains a new "Reviewer roster" sub-section explaining when to use Codex vs Opus subagent vs Kimi, including the honest empirical note that today's Kimi default model is a weaker reviewer than Opus or Codex (use as third opinion alongside, not as substitute) and a security caveat that `MOONSHOT_BASE_URL` determines where the API key + prompt + read-files contents are sent.

- **`_llm._call_moonshot` reads `MOONSHOT_BASE_URL` / `ATOMIC_AGENTS_MOONSHOT_BASE_URL` env vars for endpoint override.** Operators with keys issued via the international portal (`api.moonshot.ai`) can now use the framework; default behavior unchanged (`api.moonshot.cn`). Stopgap until proper per-region routing lands with the LLMBackend protocol ([#87](https://github.com/dep0we/atomic-agents-stack/issues/87)).

- **`_costs.PRICING` extended with Moonshot model entries.** `moonshot/moonshot-v1-{8k,32k,128k}` (non-thinking), `moonshot/kimi-k2.6` + `moonshot/kimi-k2.5` (thinking, api.moonshot.ai naming), and `moonshot/kimi-k2-0905-preview` + `moonshot/kimi-k2-0711-preview` (thinking, api.moonshot.cn naming — also referenced by the existing `tests/test_llm_tool_uses.py` fixture). All entries at placeholder rates of $0.30 / $1.20 per Mtok in/out; verify against Moonshot's current published pricing before depending on dashboard cost totals.

- **README hero diagram** — at-a-glance SVG at the top of the README showing the three core value claims (agent-as-folder, stateless cost-capped runtime, grepable JSONL audit trail) and the four shipped runtime shapes (`cron` · `launchd` · Claude Code skill · embedded Python). Light + dark variants wired via `<picture>` element so each viewer's system color scheme is matched. Source SVGs at `docs/assets/atomic-agents-hero.svg` and `docs/assets/atomic-agents-hero-dark.svg`. Every in-diagram claim — `Response` field names, JSONL top-level log shape, the runtime list — verified against shipped code (`atomic_agents/types.py` for the dataclass surface, `docs/samples/caldwell/log/` for real log shape, `extras/` for the runtime ports) per CLAUDE.md taste rules #12 + #13; an Opus subagent stood in for the rate-limited Codex round and caught four drift claims (invented `run_id`/`captures` JSONL fields, mistyped `Response(text, cost, run_id)`, aspirational `MCP server`/`HTTP service` runtime chips, drifted "21 locked spec docs" count) before commit.

- **`docs/spec/30-responsibility-audit.md`** — design spec for the responsibility audit primitive: a scheduled or on-demand offline-reflection surface that reads cross-cutting state (tools.md + judges.md + mandates.md + recent run logs + escalation queue) and produces a structured per-action-class coverage report with gap analysis as the primary output. Status: RFC. Origin: [#116](https://github.com/dep0we/atomic-agents-stack/issues/116). Implementation pending in follow-up issues filed after spec merges. Defines the six-row coverage model (Discovery / Authorization / Action execution / Evidence / Reversibility / Escalation) generalized from commerce; per-agent vs project-level scope; CLI surface (`atomic-agents audit responsibility`) with on-demand + scheduled + doctor-triggered modes; audit-output file format and frontmatter schema; rule-engine vs LLM enrichment two-mode operation; new `cost_source: "audit"` ledger value (sibling of `actor` and `judge`); 4 audit event types (audit_started / audit_completed / audit_failed / audit_budget_exhausted); composition rules with the eval framework (sibling, not collapsed), the dream pipeline (sibling shape, different layer), the doctor (bidirectional cross-reference), and the future PolicyBackend (#89) (fleet-scale composition); 6 doctor checks (check_responsibility_audit_age, check_responsibility_audit_gap_count, check_responsibility_audit_stale_policy, check_responsibility_audit_unused_mandates, check_responsibility_audit_escalation_drift, check_audit_budget_exhausted); backward compat as opt-in (audit never runs automatically; operators schedule explicitly); 5 open questions documented for impl-PR resolution.

- **`docs/spec/29-mandates.md`** — design spec for the mandate primitive: durable, operator-granted scoped authority records that live in `mandates.md`, are referenced by side-effectful action proposals via `mandate_id`, and validated by the judge layer's new `MandateCheck` specialist. Status: RFC. Origin: [#115](https://github.com/dep0we/atomic-agents-stack/issues/115). Implementation pending in follow-up issues filed after spec merges. Defines the `Mandate` + `MandateConstraints` + `TargetPattern` + `TimeWindow` dataclasses; the `mandates.md` file format with parser rules; per-agent vs project-root resolution (with can-only-tighten discipline mirroring spec/28's judge-policy floors); the `Authorization` shape extension (`granted_by: "mandate:<id>"` + `mandate_id` field); the `MandateCheck` judge specialist's 8-step validation order; cost-event ledger split (`cost_source: "mandate:<id>"`) with reservation pattern for concurrent-action TOCTOU defense; 7 mandate lifecycle event shapes; `judges.md`'s new `## Mandates` configuration section; 5 doctor checks (`check_mandate_health`, `check_mandate_no_expiry`, `check_mandate_id_collisions`, `check_mandate_relaxation_violations`, `check_mandate_source_hash_drift`); read-only CLI surface (`atomic-agents mandate list / show / usage` — no `grant`/`revoke` subcommands because the file IS the operator's grant); backward-compat as opt-in.

- **`docs/spec/01-anatomy.md` §"Graduated autonomy"** — new framework-level section naming graduated autonomy as a stated property of the framework, with the four-class action taxonomy table from spec/28 and the principle that the same agent definition runs at every scale by configuring `tools.md` / `judges.md` / `mandates.md` rather than re-shaping the agent. `docs/spec/28-judge-layer.md` Overview gains a one-paragraph cross-link to the new section, framing the judge layer as the mechanism that encodes the principle.

- **`docs/spec/28-judge-layer.md`** — design spec for the judge layer: a pre-action validation surface that gates side-effectful tool calls behind a `JudgeBackend` returning one of four outcomes (allow / block / revise / escalate). Defines action proposal schema, four-outcome model, `JudgeBackend` protocol surface + capability advertisement, default `LLMJudgeBackend` reference implementation, action classification (read-only / reversible-write / external-side-effect / high-risk), specialist judge composition pattern, memory provenance label extension (observed / inferred / generated / confirmed / disputed / superseded), `judges.md` operator config aesthetic, cost treatment per design rule #4, JSONL judgment-event audit shape per design rule #5, eval-suite shape for judges, five named failure modes with mitigations (correlated judgment / specification gaming / escalation drift / latency-cost / policy drift), backward-compat opt-in stance per design rule #14, and five open questions to resolve at spec lock. Status `planned`. Origin: RFC [#110](https://github.com/dep0we/atomic-agents-stack/issues/110). Implementation pending in follow-up issues filed after spec merges.

### Planned (issue filed, implementation pending)

- **`[design] Mandate primitive — durable scoped authority records (RFC)`** ([#115](https://github.com/dep0we/atomic-agents-stack/issues/115)) — closes when `docs/spec/29-mandates.md` merges. Follow-up implementation issues filed after spec lock: `Mandate` dataclass + `mandates.md` parser + `JudgeBackend` integration; mandate event audit shape + cumulative budget tracking via `cost_source`; mandate operator CLI; doctor checks.
- **`[design] Responsibility audit — periodic coverage audit (RFC)`** ([#116](https://github.com/dep0we/atomic-agents-stack/issues/116)) — closes when `docs/spec/30-responsibility-audit.md` merges (drafting after mandate spec lands). Follow-up implementation issues filed after spec lock: audit runtime + coverage detector + report writer + CLI; LLM gap-recommendation enricher; doctor checks; example.
- **`[design] Judge layer — pre-action validation at side-effect boundaries (RFC)`** ([#110](https://github.com/dep0we/atomic-agents-stack/issues/110)) — closes when `docs/spec/28-judge-layer.md` merges. Follow-up implementation issues filed after spec lock: `JudgeBackend` protocol + filesystem-default reference implementation; memory-provenance frontmatter extension; reference judge wired to `tools.md` enforcement.
- **`[backend] LLMBackend Protocol — PRs 1+2 of 4: canonical types + AnthropicLLMBackend + Claude dispatch routing`** ([#87](https://github.com/dep0we/atomic-agents-stack/issues/87)) — establishing the LLM provider abstraction layer in the protocol-pattern series alongside #60–#65 / #82–#84. **PR 1** shipped scaffolding (canonical types, `SyncLLMBackend` Protocol, registry primitives) under the same `[Unreleased]` entry. **PR 2** lands the first concrete backend and routes Claude models through it: new `atomic_agents/llm/anthropic.py` with `AnthropicLLMBackend` implementing all 7 Protocol methods (provider_id, supports_model, capabilities per family with conservative defaults for forward-compat against future Claude releases, pricing delegating to `_costs.PRICING`, count_tokens via SDK with heuristic fallback, call wrapping the SDK with canonical-tools translation, format_tool_results building Anthropic's assistant-echo + user-tool_result message sequence). Lazy default registration in `atomic_agents/llm/__init__.py` — backends register on first `find_backend_for_model` call rather than at import time so subprocess spawn (e.g. `multiprocessing.Process` in tests) doesn't pay anthropic-import latency. `_llm.call_llm` for `claude-*` models now dispatches through `find_backend_for_model(model).call(...)`; `_call_anthropic` deleted (dead code after the routing change). Legacy dict-shape tools converted to canonical at the entry boundary (per-element shape check) so external callers passing Anthropic-shape dicts continue to work; legacy openai-shape preserved by reference when every item is a dict (preserves test object-identity assertions for the OpenAI/Moonshot path that still flows through procedural dispatch). Protocol's `format_tool_results` signature extended from single-arg to three-arg (`tool_uses` + `tool_results` + `assistant_text`) — Anthropic's tool loop needs all three to build the assistant-echo turn. New `_capture.canonical_tool_definition()` + `ToolRegistry.to_canonical_definitions()` helpers in place ready for the PR 2.5 `agent.py` tool-dispatch refactor (descoped from PR 2 after the bundled attempt triggered 25 test failures across the suite). 26 new unit tests for AnthropicLLMBackend (`test_llm_anthropic_backend.py`) covering family resolution, per-model capabilities, pricing delegation, count_tokens SDK/heuristic paths, call() text/tool_use/cache/tools-translation, format_tool_results assistant-echo + atomic_capture skip + is_error + dict-content-json-serialization, forward-compat property on unknown Claude families, and the legacy-dict cache_control drop pinned as a known gap (issue #150 tracks closing it). Opus subagent adversarial review caught 2 P1s (forward-compat regression on future Claude model ids + silent per-tool `cache_control` drop) + 3 P2s (heterogeneous-list crash, broad `except Exception` swallowing init bugs, redundant `except (AttributeError, Exception)`) + cheap P3s (docstring drift, dead-code removal) — all addressed pre-merge with verifying tests. #150 (per-tool cache_breakpoint canonical-type extension) filed inline. Follow-up PRs in this arc: **PR 2.5** `agent.py` tool-dispatch refactor; **PR 3** `OpenAICompatibleLLMBackend` + `MoonshotLLMBackend` + `model.md provider:` parser; **PR 4** `docs/spec/31-llm-backend.md` + ~30-test conformance suite + README/CLAUDE.md/spec cross-refs. Will land across a future Minor release (additive; no `### BREAKING` callout). #148 (top_p/stop_sequences parity) + #146 (Kimi K2.x reasoning_content extraction) + #150 (per-tool cache_breakpoint) tracked as follow-ups.
- **`[deployment] Configuration wizard + deploy assistant for new operators`** ([#94](https://github.com/dep0we/atomic-agents-stack/issues/94)) — `/atomic-init` Claude Code skill that walks new operators conversationally from "I want to build an agent" to "my agent ran its first call." Drafts plausible IDENTITY/SOUL/USER from a role description, picks defaults for `tools.md` / `model.md`, configures secrets, and prints the invocation command. Post-public-flip work.
- **`[polish] Public API surface lock — promote missing exceptions to __all__ + CLI exit-code parity`** ([#99](https://github.com/dep0we/atomic-agents-stack/issues/99)) — 9 exception classes are raised inside the package but not advertised via `atomic_agents/__init__.py`'s `__all__`. Promote the clearly user-facing ones (`NestedDelegationRefused`, `CaptureParseError`, `GoalCorrupted`, `ToolNameCollision`, the MCP triplet). Also map CLI exit codes 1:1 to exception classes so shell scripts can react differently to different failure classes.
- **`[deployment] Add per_call_usd field to cost_guardrails YAML schema`** ([#100](https://github.com/dep0we/atomic-agents-stack/issues/100)) — today's `cost_guardrails` supports `daily_usd` / `monthly_usd` but no per-call cap. Runaway-loop protection and predictable per-call billing both want this. New `per_call_usd` + `per_call_cap_action` fields, pre-call cost estimation, tree-cap interaction, `PerCallCapBlocked` exception in `__all__`.

## [0.12.0] - 2026-05-11

Public-flip-readiness Minor. Documentation-only — no framework behavior changes from v0.11.0; runtime ships unchanged. Drop-in upgrade from v0.11.0 — no `### BREAKING` callouts.

This is the **launch shape** release: the README rewrite that anchors the public-flip narrative, the repo-surface kit standard for an OSS project (CONTRIBUTING / CODE_OF_CONDUCT / SECURITY / issue + PR templates), and a LICENSE consistency fix that closes the final maintainer-name drift left from the personal-references scrub. Codex adversarial review caught six factual errors / overclaims in the original README draft before merge; all six were applied.

### Changed

- **README rewritten for the public flip.** Framework-first positioning replaces the prior feature-list opener. New tagline lands as the hero — *"AI agents that live in your folder, not someone else's database"* with subtitle *"Vault-native, MIT-licensed, Markdown-source-of-truth."* New `Why this exists` opener names "agent state ends up in app databases / vector stores / hosted trace systems / bespoke glue code" as the precise enemy rather than naming specific competitors as universally hosted. New `Current limits` section makes the alpha / single-maintainer / macOS-Linux-primary / only-MemoryBackend-shipped / log-only-alerts state explicit before the comparison matrix. New honest comparison matrix names Letta, Mem0, LangGraph + LangSmith, and direct-SDK with narrower defensible claims (Markdown-source-of-truth, no required server, spec-level file layout) — and a `Where the alternatives win` paragraph names where each does better than Atomic Agents. Backend-protocol scaling section now labels org-scale-over-Postgres as `v1 direction` rather than implying shipped today. 6 deployment runbooks linked. Spec count corrected (13 → 21). Caldwell description sharpened to surface the 5 days of real JSONL logs, the helper-pattern day with ~76% cost savings, and the evals across happy/edge/adversarial/decline categories. Caldwell appears as one sample among future samples, not as the headline.
- **README badge URLs corrected** from `github.com/user/*` (broken) to `github.com/dep0we/*`. Version badge added.
- **README default `ATOMIC_AGENTS_ROOT` corrected** from `~/agents/agents` (duplicated path, typo) to `~/docs/agents` per `atomic_agents/_platform.py:DEFAULT_AGENTS_ROOT`.
- **README "spec docs are not aspirational" softened** to "spec docs separate shipped behavior from explicit future/deferred boundaries" — closer to ground truth (some spec sections mark future work explicitly).
- **Status section** updated v0.10 → v0.11.0; protocol-pattern v1.0 expectation named.

### Added

- **`CONTRIBUTING.md`** — reading order before opening a PR (CLAUDE.md / TENSIONS.md / methodology.md), branch + commit shape, test expectations, review-in-rounds practice, what lands cleanly vs what needs an issue first.
- **`CODE_OF_CONDUCT.md`** — condensed Contributor-Covenant-shaped policy with the project's actual maintainer contact and an enforcement table; not vendored boilerplate.
- **`SECURITY.md`** — 90-day disclosure window, in-scope vs documented-honest-limitations (best-effort path-traversal check in MCP args, advisory-only cost guardrails without the shared helper, plain-markdown-no-encryption-at-rest), operator hygiene checklist.
- **`.github/ISSUE_TEMPLATE/{bug,feature,question}.md`** — structured templates matching the project's existing issue conventions (title prefixes, env/repro/scope sections).
- **`.github/pull_request_template.md`** — mirrors the project's existing PR shape (Summary / Why / Test plan / Design alignment self-check against the 14 CLAUDE.md design rules).

### Fixed

- **`LICENSE` copyright line** flipped from `Copyright (c) 2026 Dan Powers` to `Copyright (c) 2026 atomic-agents-stack contributors`. Matches the `pyproject.toml` author field from the personal-references scrub (issue #77 / PR #92). The Codex adversarial review of the launch README flagged the inconsistency between LICENSE and `pyproject.toml` as small but visible drift — the only place the maintainer name still surfaced after the scrub.

### Process notes (operator-visible context, not behavior changes)

- **Codex adversarial review run against the launch README before the public flip** (per the `codex_reviews_mandatory` operator preference). Six findings caught and applied: factual errors in the comparison matrix (Letta has self-hosted Docker; Mem0 OSS exists; LangGraph has filesystem-backed memory via Deep Agents and LangSmith for observability — original matrix overclaimed all four), `runs anywhere markdown does` softened to `Markdown-source-of-truth` because `atomic_agents/_locks.py` imports POSIX `fcntl` unconditionally, `spec docs are not aspirational` softened to `separate shipped behavior from explicit future/deferred boundaries`, dangling "Your first agent below" reference removed, and the LICENSE consistency fix above. Recommendation: Revise before public flip — adopted.

- **Public-flip launch shape work shipped** (closes [#93](https://github.com/dep0we/atomic-agents-stack/issues/93)). The launch-shape design doc from the `/office-hours` session lands as the README + repo-surface kit above.

## [0.11.0] - 2026-05-11

Documentation-heavy Minor focused on **public-flip readiness**. Closes the operator-doc gaps that were blockers for a credible public release: every supported deployment shape now has its own runbook, every public exception is cataloged, and the Caldwell sample is unambiguously fictional. No new framework features; the runtime ships unchanged from v0.10.0. Drop-in upgrade from v0.10.0 — no `### BREAKING` callouts.

### Added

- **`docs/deployment/obsidian.md` — Obsidian-backed deployment guide** (833 lines, closes [#67](https://github.com/dep0we/atomic-agents-stack/issues/67)). The operator runbook for running an Atomic Agent on top of an Obsidian-synced vault. Covers recommended vault layout, `.obsidian/sync.ignore` patterns with per-line rationale, `.obsidian/` config dir handling, `AgentLock` race conditions vs Sync writes, `_dashboard/index.html` self-containment, conflict copy recovery, a 9-step worked first-run example, and cross-platform read/edit-vs-run boundaries. Cites concrete code paths (`_locks.py:54-77`, `_io.atomic_write`, `_platform.py:get_agents_root`, `mcp.py:616`, `memory/filesystem.py` walker, `migrate.py:find_content_files`, `dashboard/render.py:7-9`).
- **`docs/deployment/programmatic.md` — Programmatic invocation guide + complete public exception table** (761 lines, closes [#69](https://github.com/dep0we/atomic-agents-stack/issues/69)). The Python-embedded path for using the framework inside another app. Covers when to use programmatic vs CLI, `Agent` + `call()` public surface, cost-guardrail handling, memory API (canonical `agent.memory.*` + deprecated re-exports), helper/delegate semantics with the one-level constraint, concurrency model, three worked examples (single-shot cron embed, custom orchestrator, subprocess-safe gunicorn-shaped worker), and a complete public exception table covering 18 exported classes plus a "raised but not yet in `__all__`" subsection for 9 internal ones (follow-up tracked in [#99](https://github.com/dep0we/atomic-agents-stack/issues/99)).
- **`docs/deployment/disaster-recovery.md` — Disaster recovery runbook** (741 lines, closes [#72](https://github.com/dep0we/atomic-agents-stack/issues/72)). Symptom-organized runbook for first-response when something goes wrong. Nine scenarios covering stale-lock recovery (`lsof` diagnosis, why `.lock` persists after flock release), mid-run crash recovery via atomic-write guarantees, corrupted INDEX repair via `FilesystemBackend.list_orphans()`, migration rollback flow, memory-write races, Obsidian Sync conflict recovery, `.versions/` snapshot management, doctor failure-mode mapping, and the git-as-canonical-backup pattern. Every recovery shows the exact command to run plus how to verify the fix.
- **`docs/deployment/cost-guardrail-sizing.md` — Cost guardrail sizing guidance** (521 lines, closes [#73](https://github.com/dep0we/atomic-agents-stack/issues/73)). How to pick numbers for daily/monthly caps and cap action. Includes a current pricing-per-MTok snapshot from `_costs.py`, the 14-day observe-then-apply pattern from spec/09, and **seven role archetypes with recommended starting caps**: personal financial advisor, daily-brief cron, interactive skill-mode, helper-heavy summarizer, goal-driven autonomous, high-stakes single-call modeling, and multi-role coordinator. Names a real schema gap (no `per_call_usd` field today) that's tracked as a follow-up in [#100](https://github.com/dep0we/atomic-agents-stack/issues/100).
- **Cross-references between the four new deployment docs** so a reader landing on any one of them can fan out to the others without scrolling back to a directory listing.

### Changed

- **Caldwell sample `model.md` cost guardrails backfilled to real Archetype A values** — replaced placeholder `daily_cap_usd: 5.00 / monthly_cap_usd: 100.00 / enabled: false` block with the recommended personal-financial-advisor archetype from the new sizing guide (`daily_cap_usd: 0.50`, `monthly_cap_usd: 7.00`, `daily_cap_action: fallback`, `enabled: true`). Operators copying the sample now see a real worked example matching documented recommendations rather than a placeholder block.

- **Personal-reference scrub for public release** (issue [#77](https://github.com/dep0we/atomic-agents-stack/issues/77)). Stripped maintainer-identifying details ahead of the public flip:
  - **Maintainer-name references removed** from code and tests — `pyproject.toml` author field, the `_platform.py` env-var docstring, two test-fixture description strings (`tests/test_capture.py`, `tests/test_schema.py`), and three doc-side mentions (`CLAUDE.md`, `docs/TENSIONS.md`, `docs/methodology.md`). One narrative sentence in the Caldwell financial-modeling sample skill that anchored on the maintainer was rewritten to be context-neutral. The package author field now reads `atomic-agents-stack contributors`.
  - **Sample-persona name genericized in spec docs.** Every reference to the Caldwell sample's user-persona name outside `docs/samples/caldwell/` was rewritten as "the operator" / "the user" / "you" / placeholder — covering `docs/architecture.md`, `docs/README.md`, `docs/GOVERNANCE.md`, `docs/appendix/portability.md`, `docs/methodology.md`, and every spec doc (`spec/01`–`spec/13`). The persona name still appears inside the Caldwell sample, where the framing as "the sample's fictional user" is correct. The example USER.md content in `spec/01-anatomy.md` is now generic so the spec no longer depends on the sample's persona.
  - **Caldwell sample reshape.** The sample's surface details rewritten so the persona reads as unambiguously synthetic: Director of Operations at Atlas Logistics (was Head of IT at a fictional industrial conglomerate), freelance technical editing on the side (drops the prior consulting-practice angle), married + 2 kids in school (was 4 kids + 5 grandkids), Madison, WI (was a Tennessee location). The spouse name was removed throughout; the spouse-side-business project was replaced with `project_freelance_editing_growth.md` plus a renamed `project_freelance_retainers.md`. Downstream sample artifacts (journal entries, evals, dashboard sample data, log JSONL summaries) updated to match.
  - **Canonical example agent names genericized.** Two named example-agents in the docs (placeholders for the maintainer's actual advisor agents) were replaced with `agent-a` / `agent-b` placeholders across ~15 doc files (`docs/architecture.md`, `docs/README.md`, `docs/appendix/portability.md`, every spec doc that listed example agents, every implementation guide).
  - **Real project-name leaks removed.** Personal-vault example paths in `spec/01-anatomy.md` and `samples/caldwell/tools.md` were rewritten with generic placeholders. Folder-name examples in `implementation/claude-skill-agent.md` and `implementation/chatgpt-skill-agent.md` that had used the maintainer's real project names were replaced with `agent-a/` / `agent-b/`. Also caught and fixed three "Maya 2026" → "May 2026" date typos in `spec/07` and `implementation/chatgpt-skill-agent.md`.
  - Acceptance criteria from the issue verified — case-insensitive greps for the maintainer name, the sample-persona name outside `docs/samples/`, and the four real project-name patterns all return zero hits across `.md` / `.py` / `.toml`. Full test suite (720 tests) passes. Sample remains internally consistent.

### Fixed

- README "What's shipped" table refreshed to add v0.10.0 rows (MCP client, MemoryBackend, doctor, deployment docs) and consolidate the inflated `v0.2`–`v0.8` labels — those modules all actually shipped in v0.9.0, the leading-zero `v0.X` labels were aspirational milestone numbers from the build sequence. Versions in the table are now real release tags.
- README "What's shipped" table swept again post-cluster to add four new deployment-doc rows (`obsidian.md`, `programmatic.md`, `disaster-recovery.md`, `cost-guardrail-sizing.md`) and broaden the `docs/deployment/` description in the Repository structure block. The four rows ship as `✅ v0.11.0`.
- Public-API audit (filed as follow-up [#99](https://github.com/dep0we/atomic-agents-stack/issues/99)) — discovered while documenting the programmatic invocation path that 9 exception classes are raised inside the package but not in `atomic_agents/__init__.py`'s `__all__`. The new `programmatic.md` documents current behavior honestly and surfaces the gap; the actual `__all__` promotion + CLI exit-code parity work is queued for a future Minor.

## [0.10.0] - 2026-05-09

First Minor release after the spec-completion v0.9.0. Adds the MCP client, the
MemoryBackend protocol (the watershed for the protocol-pattern scaling
roadmap), the `atomic-agents doctor` preflight CLI, and the SemVer/upgrade
documentation that turns "what version are you running" into an answerable
question. No `### BREAKING` changes — drop-in upgrade from v0.9.0.

### Added

**`atomic-agents doctor` preflight CLI** (issue #66, PR #75)

- New CLI subcommand: `atomic-agents doctor [--agent <name>] [--agents-root <path>] [--json] [--no-mcp]`. Runs nine independent checks (env, python, vault, provider-keys, model, mcp, locks, memory-backend, write-paths) and reports each as `pass` / `fail` / `skip`.
- Each failing check emits a `fix_hint` containing the literal command needed to resolve it (e.g. `security add-generic-password ... -s atomic-agents-anthropic -w '<key>'` for a missing Keychain entry).
- Provider-keys check reuses the production lookup chain (`_llm._get_key()`) so doctor's verdict can never disagree with runtime behaviour. Provider inference follows `_costs.PRICING` keys: `claude-*` → anthropic, `gpt-*` → openai, `moonshot/*` → moonshot. Also verifies the optional provider SDK is importable — `gpt-*` and `moonshot/*` selections require the `openai` extra; doctor fails fast instead of letting the runtime hit `ImportError` on first call.
- MCP check exercises the real stdio handshake (`session.initialize` + `list_tools`) per declared server, threading `tools.md` `read_paths` through `parse_mcp_md` so the same `PathTraversalError` that runtime would raise surfaces at install time. Bounded by a 10-second default wall-clock timeout — a server that starts but never replies fails the check instead of hanging the CLI. Skipped via `--no-mcp` or when `mcp.md` is absent.
- Cascade-aware: when the agent path matches `<system>/projects/<project>/agents/<role>` (spec/06), `model.md` and `tools.md` are resolved via `_cascade.resolve_*` so role-level config satisfies the vault check and downstream parsers see the same config the runtime would.
- Locks check uses `flock(LOCK_NB)` to distinguish a lingering lock file (normal) from an actively-held lock (problem); flags stale when held + mtime > 300s.
- Write-paths check verifies the agent's `memory/` directory falls inside at least one `write_path`, is NOT shadowed by a `read_only_path`, and is itself `os.W_OK` writable on disk — `FilesystemBackend.write_note()` enforces all three at runtime, so a misalignment would otherwise fail after the agent has already spent tokens.
- Malformed config (bad YAML in `model.md`, etc.) is reported as a FAIL CheckResult, not as an exit-2 doctor crash. Exit-2 is reserved for genuine bugs in doctor itself.
- Output formats: human-readable aligned table by default, machine-readable JSON via `--json` (intended for Cloud Run liveness probes / launchd preflight).
- Exit codes: `0` all-pass, `1` any-fail, `2` doctor itself crashed.
- Spec: `docs/spec/27-doctor.md`. Getting-started gains a "Verify your install" step (§9).
- Codex review across three pre-merge rounds: 9 P2 findings closed (cascade resolution, parse-error containment, optional SDK detection, MCP read_paths enforcement, memory-in-write_paths verification, YAML-syntax detection, empty-write_paths-in-agent-scope FAIL, MCP handshake timeout, direct memory-dir `os.W_OK`).
- 54 new tests in `tests/test_doctor.py` covering each check's PASS + FAIL paths, every codex-fix scenario, and CLI integration (exit codes, JSON shape, crash → exit 2).

**SemVer policy + upgrade runbook** (issue #68, PR #76)

- New: [`docs/deployment/versioning.md`](docs/deployment/versioning.md) — full SemVer policy with project-specific Major/Minor/Patch definitions (schema break vs new feature vs bug fix), pre-1.0 caveat, and the release-cutting procedure (extract CHANGELOG section via awk, tag with annotation, create GitHub Release with `--notes-file`).
- New: [`docs/deployment/upgrading.md`](docs/deployment/upgrading.md) — operator runbook: read release notes → pull → copy migration script(s) into `<vault>/_migrations/` → `python -m atomic_agents.migrate --status` → `--to vN --dry-run` → `--to vN` → verify (`atomic-agents doctor` in v0.10.0+; pre-v0.10 falls back to `info` + `run` smoke check) → restart LaunchAgents.
- Updated: [`README.md`](README.md) gains a "Versioning & upgrades" section linking both docs.
- Updated: `CHANGELOG.md` header now documents Keep-a-Changelog section conventions (Added / Changed / Deprecated / Removed / Fixed / Security) and the `### BREAKING` callout convention for any change that forces operator work to upgrade.
- Tagged historical releases v0.1.0 and v0.9.0 retroactively at the commits where their CHANGELOG entries landed, so `git tag -l` and the GitHub Releases page now match the CHANGELOG history.
- Codex review across five pre-merge rounds: 11 P2 findings closed (pre-1.0 bump-rule consistency, migrate `--to vN` requirement, migration scripts location (`<vault>/_migrations/`), GitHub Release notes from CHANGELOG (not `--notes-from-tag`), doctor reference gating to v0.10.0+, no-op migrate behavior, rollback semantics, single-snapshot-per-`--to`, `CURRENT_SCHEMA_VERSION` lives in the package not the vault).

**MCP (Model Context Protocol) client support** (PR #55, follow-up #56)

- New module `atomic_agents/mcp/` enables agents to consume tools from external MCP servers (stdio transport).
- Server registry parsed from `<agent>/mcp.md`; tool collision detection across MCP + custom tools (`ToolNameCollision`).
- Validator integration end-to-end so server-side schema rejections surface as `MCPValidationError` to the agent.
- Env merge semantics: agent-level + per-server env vars compose without leaking parent process env.
- Codex review (6 findings) closed before merge; covers env merge, validator wiring, collision detection, server lifecycle.

**MemoryBackend protocol + FilesystemBackend default** (PR #57, follow-up #58/#59)

- New `atomic_agents.memory` package with `MemoryBackend` Protocol, `Note`/`NoteRef`/`VersionRef`/`WritePolicy`/`StagedMemory`/`MemoryStats` dataclasses, and `FilesystemBackend` as the default registered backend.
- Boil-the-lake refactor of the memory layer: 9 call sites (agent.py, dream.py, tuning.py, both dashboards, cli.py, _capture.py, _versioning.py) route through `agent.memory` instead of direct filesystem operations.
- `WritePolicy` enforced at `write_note`, `apply_staging`, and inside staged writes — security-equivalent to the prior write-path enforcement, now backend-pluggable.
- Atomic dir-swap in `apply_staging` is rollback-safe (microsecond-precision archive name + restore-on-failure).
- New exceptions: `BackendNotRegistered`, `VersionNotFound`, `StagingNotApplied`.
- Spec doc: `docs/spec/20-memory-backend.md`.
- Test count: 626 → 668 (35 conformance tests in `test_memory_protocol_conformance.py` + 10 fs-specific in `test_memory_filesystem_backend.py` + 4 live-agent integration in `test_memory_integration.py`).
- Two scoped follow-ups deferred to issues #58 (dream → `staging.write_note(capture, policy)`) and #59 (CLI → `agent.memory` instead of direct backend).
- Codex reviewed the scope (10 findings → rev 2) and the implementation diff (4 P1 + 7 P2 + 3 P3 → all closed in fix commit).

### Issue tracking convention

All scoped follow-ups, codex-deferred items, and future enhancements now go to GitHub Issues at dep0we/atomic-agents-stack with label conventions: `enhancement`, `documentation`, `infrastructure`, `polish`, `backend` (new — protocol abstractions), `deployment` (new — install / upgrade / runbooks), `spec`, `bug`. Title prefix `[scope]` (e.g. `[backend]`, `[deployment]`, `[v0.X]`, `[polish]`).

### Roadmap as live backlog

- 6 backend-protocol scaling issues filed (#60–#65): LockBackend (urgent — multi-process cliff), LogBackend, PersonaBackend, AgentProfileBackend, ToolRegistryBackend, CorpusBackend.
- 8 deployment-readiness issues filed (#66–#73): doctor CLI, Obsidian guide, SemVer + release pipeline, programmatic API docs, cost alert webhook, launchd template stamper, recovery runbook, cost guardrail sizing.
- Filter via `gh issue list --label backend` or `gh issue list --label deployment`.

## [0.9.0] - 2026-05-07

Spec-completion release. The full v0.x build sequence is landed: every deferred spec module from v0.1 plus operational extras and an in-repo copy of the spec.

### Added

**Eval runner** (`atomic_agents.eval`, was issue #1, PR #12)

- `EvalRunner` class with `run_test`, `run_suite`, and category/test filters.
- Cross-family LLM-as-judge: Claude scores OpenAI agents, OpenAI scores Claude agents — never self-judge. Same-family fallback when no cross-family judge is available; raises `NoJudgeAvailable` if none.
- Rubric weighting: per-dimension weights from `evals/rubric.md` frontmatter; weighted score in [0,5]; threshold-based pass/fail.
- Hard-fail override: any rubric dimension marked `hard_fail: true` in the rubric forces a failed verdict regardless of weighted score.
- Malformed-judge-JSON retry: one retry with stricter "JSON only" reminder before recording `judge_error`.
- Run logs land in `evals/runs/YYYY-MM-DD.jsonl`; long agent responses persisted separately under `evals/runs/responses/` and referenced from the JSONL line.
- CLI: `python -m atomic_agents.eval <agent> [--category|--test|--all|--summary-only|--no-write]`.

**Tuning analyzer** (`atomic_agents.tuning`, was issue #2, PR #22)

- Eval-driven self-improvement per spec/11. Detects four pattern types from recent eval runs: recurring persona-fidelity miss, recurring hard-fail, stale memory reference, promotable hot memory.
- `EditProposal` dataclass: each detected pattern emits a concrete proposed edit with the eval evidence inline.
- Optional LLM polish (~$0.02 per proposal) to improve report wording without changing recommendations.
- Reports land in `evals/tuning_reports/YYYY-MM-DD_proposal.md`. Operator approves/rejects in the report file.
- `--apply` writes approved diffs to the target persona/memory/tools files via `atomic_write`, respecting `tools.md` write_paths. Diffs that are instructional (multi-step, comment-only) are flagged as manual-apply with a skip reason; all decisions (applied, skipped, rejected, deferred) land in `evals/tuning_history.jsonl`. Use `--dry-run` with `--apply` to preview what would change without writing.
- CLI: `python -m atomic_agents.tuning <agent> [--since|--apply|--polish|--dry-run]`.

**Goal manager** (`atomic_agents.goal`, was issue #3, PR #14)

- Goal + sub-goal lifecycle for goal-driven and hybrid agents per spec/12.
- `GoalManager`: load/save `<agent>/goal.md`, dispatch logic (`next_sub_goal` filters by `blocked_by` chain), status transitions with sanity enforcement, history JSONL.
- Pacing analysis in `progress_report`: planned vs. elapsed days, on-track / behind / ahead verdict.
- Non-destructive abandon and complete (archives `goal.md` to `goal_archive/<date>_<slug>.md`).
- Operating modes: reactive, goal-driven, hybrid — manager works the same across all three.
- CLI: `python -m atomic_agents.goal <agent> {status|next|advance|abandon|complete|report}`.

**Schema migration runner** (`atomic_agents.migrate`, was issue #4, PR #16)

- Vault-wide schema migrations with mandatory snapshot + automatic rollback on validation failure.
- `MigrationScript` Protocol: declares `FROM_VERSION`, `TO_VERSION`, `applies_to`, and a pure `migrate(content_dict)` function.
- Snapshot format: gzipped tarball under `<vault>/_migrations/snapshots/<timestamp>.tar.gz` — small for typical vaults, restorable with `--rollback`.
- Migration plan walks the script chain `from_current → to_target`; refuses to skip versions.
- Post-validation re-parses every changed file against the target schema; any failure rolls back the entire batch.
- Safety property: package's `CURRENT_SCHEMA_VERSION` and the migration ladder ship together — until both are present, post-validation rejects new-schema files and rolls back, so the vault can never silently land in an unsupported state.
- CLI: `python -m atomic_agents.migrate [--to|--dry-run|--status|--rollback|--list-snapshots]`.

**Tool-call captures (Path 1)** (`atomic_agents._capture`, was issue #5, PR #16)

- Structured tool-call extraction alongside the existing fenced-JSON parser per spec/05. Provider SDKs validate inputs against schema before they reach the helper, eliminating the malformed-JSON failure mode.
- `CAPTURE_TOOL_SCHEMA`: shared JSON Schema; identical taxonomy + required fields as the fenced-block validator.
- `anthropic_tool_definition()` and `openai_tool_definition()`: provider-specific format wrappers.
- `extract_tool_call_captures()` and `extract_all_captures()`: combined Path 1 + Path 2 extractor with priority-aware dedup (tool calls win on collisions).
- `_RawLLMResponse.tool_uses` field — normalized across Anthropic and OpenAI/Moonshot.
- `AtomicAgent._capture_tool_definitions(model)` picks the right per-provider formatter; `agent.call()` passes the capture tool to every LLM call and extracts captures from both paths.

**Multi-agent project cascade loader** (`atomic_agents._cascade`, was issue #6, PR #23)

- Three-layer cascade per spec/06: role / project / instance. When an agent path resolves like `<system>/projects/<project>/agents/<role>/`, the loader walks up to find the role and project layers.
- `CascadePaths` dataclass; `detect_cascade(agent_root)` returns `None` for single-agent layouts (full backwards compat).
- Layer-1: `<role>/PROMPT.md`. Layer-2: `<project>/{canon.md, style_guide.md, goal.md, policy/*.md}`. Layer-3: instance persona/memory/wiki/journal/log + optional `tools.md` / `tools.override.md` / `model.md` overrides.
- `tools.md` resolution: `tools.override.md` (additive merge with role) > instance `tools.md` (replaces role) > role `tools.md` (base).
- Queue mechanics: `claim_next_queued` (atomic POSIX rename), `release_claim`, `move_to_dead_letter` (with reason file), `recover_stale_claims` (mtime-based lease expiry).
- `assemble_system_prompt()` extended for cascade order: role PROMPT → instance persona → tools → project canon/goal/style_guide/policy → memory/wiki/notes/journal.
- `parse_tools_md` and `parse_model_md` split into path-based wrappers + text-based core (so cascade-merged content can be parsed without writing to disk).

**Helper provenance preservation** (was issue #7, PR #20)

- Per spec/10 Wave 8: helper output must preserve attribution back to source so the parent can cite it.
- `helper_call(..., sources=...)` and `helper_call_parallel(..., sources=... | sources_per_prompt=...)`. When sources are passed, the helper's system prompt prepends a citation instruction + source bullet list.
- `_detect_provenance(text, sources)` heuristic: bracketed citations (`[§2, p3]`, `[page 5]`), inline phrases (`according to`, `per memo`, `§3`), or verbatim source-basename mention. Conservative — prefers false-positive over false-negative.
- `HelperResult.sources` echoes the input list; `HelperResult.provenance_preserved` reports the heuristic verdict.
- Run record JSONL gains `sources` and `provenance_preserved` fields when sources are passed; omitted otherwise (log shape unchanged for backwards compat).

**Research integrity Layers 2 + 3** (was issue #8, PR #21)

- **Layer 2 — source-grounded eval.** When a golden test declares `expected_facts`, `_build_judge_prompt` appends a "Factual accuracy check" section instructing the judge to verify each fact (`stated_in_response`, `value_correct`, `cited`) and emit a `factual_checks` array. `compute_factual_accuracy_from_checks` derives a 1–5 dimension score from the checks (full credit when verified + cited, half credit when stated correctly but uncited). When the rubric weights `factual_accuracy` but the judge omits a numeric score for it, the runner derives one from the checks; judge's numeric score takes priority when present.
- **Layer 3 — research log per response.** `_helpers_this_run` rollup tracks helper calls during a parent run; `agent.call()` embeds it as `helper_provenance` in the parent's run log record. Field is omitted when no helpers were called, so log shape stays unchanged for reactive agents.

**Spec import** (`docs/`, was issue #9, PR #18)

- All 13 spec docs (`docs/spec/01-anatomy` → `13-research-integrity`), `architecture.md`, `docs/README.md`, the 7 implementation guides, `appendix/portability.md`, and the complete Caldwell sample agent (persona, memory, wiki, journal, log, evals/rubric+judge+5 golden tests) imported from the source vault.
- 122 Obsidian wikilinks converted to relative markdown links across 27 files.
- 38 dangling cross-references (filename examples like `[[feedback_communication_style]]`) converted to inline code so the intent reads correctly. Zero broken markdown links remain.
- Stale `lib/atomic_agents.py` references updated to `atomic_agents` (the package name in this repo) across 6 files.

**Operational extras** (`extras/`, was issue #11, PR #19)

- Seven Claude Code skill wrappers: `atomic-agents-{run,info,eval,tune,goal,dashboard,migrate}` — each is a portable `SKILL.md` with action-oriented instructions, invocation, output reading, and troubleshooting.
- Three macOS LaunchAgent plist templates: daily run, daily eval suite, hourly dashboard refresh. All three validate with `plutil -lint`. README walks through substitution, loading, and the Keychain alternative for keys.
- Linux cron templates: `crontab.example` + `run-atomic-agent.sh` portable shell wrapper handling env loading, key sourcing from a chmod-600 file, and per-command logging.
- `__KEY__` placeholder syntax (double-underscore) for textual placeholders so plist templates remain valid XML during review.

### Changed

- Top-level README's "What's shipped" table refreshed to mark every shipped module, including the test count (296).
- `docs/README.md` status table refreshed to show all shipped modules with their module names.
- Repository structure section in the top-level README expanded to surface `docs/` and `extras/` trees.

### Tests

- 296 total (was 67 in v0.1). New tests by module: eval +27, tuning +25, goal +39, migrate +32, tool-call captures +32, cascade +35, helper provenance +23, research integrity +16.

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
