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
