# Backend protocols shipped

Twelve backend protocols are locked for v1.0. A thirteenth, SecretBackend (#340), shipped for v1.5 with two reference implementations (FilesystemSecretBackend + GCPSecretManagerBackend) and LOCKED spec/38. A fourteenth, GoalBackend (#425 + #448 PR1/PR2/PR3 + #483 PR1 + #496 PR1), shipped for v1.5 with FilesystemGoalBackend and LOCKED spec/41 (arc-closer #448 PR3 locked the spec; #483 PR1 added clock injection + GoalManager thin shim + agent_root resolution + spec/41 normative addendum; #496 PR1 added backend-universe alignment — coordinator threads gate agent's log/policy/profile backends into OutcomeRunner). A fifteenth, OutcomeBackend (#426), shipped for v1.5 with FilesystemOutcomeBackend; write-path adopted in #448 PR2 and LOCKED spec/42. A sixteenth, JournalBackend (#427), shipped for v1.5 with FilesystemJournalBackend and DRAFT spec/43. A seventeenth, QueueBackend (#428), shipped for v1.5 with FilesystemQueueBackend and DRAFT spec/44. An eighteenth, IdempotencyBackend (#520), shipped for v1.5 (PR 1 + PR 2 arc-closer) with FilesystemDedupLedger and LOCKED spec/45 — PR 2 wired the two-phase dedup gate into agent.call() (idempotency_key kwarg, lookup-before-lock COMPLETED short-circuit, begin-after-cost-gate, serve/queue/cron trigger integration, RunRecord audit fields, spec/22 versioned normative addendum). A nineteenth, EmbeddingBackend (#200), shipped for v1.5 PR 2 with OpenAIEmbeddingBackend and DRAFT spec/46 — ships the Protocol + OpenAI reference impl + EMBEDDING_PRICING cost table isolated from chat PRICING; registry + pgvector wiring ship in PR 3. A twentieth, ConversationBackend (#535), shipped for v1.5 PR 1 with FilesystemConversationBackend and DRAFT spec/47 — ships the Protocol + filesystem reference impl + full agent.call() wiring (three-channel backend selection, prior-turn injection into messages[], turn write-back AFTER JSONL log, continuity_persisted=False on failure, conversation_id tagged on all 7 terminal JSONL sites) + SQLite/Postgres v2→v3 migration; PostgresConversationBackend + doctor check deferred to later PRs. Each section captures the reference implementations shipped, the operator override surface, the doctor coherence check, the Implementer Contract location, and the architectural cliff the protocol closes.

This file is the canonical reference for what the framework's storage seam looks like today. CLAUDE.md links here instead of inlining the detail so the session prompt stays under its char budget.

For the Protocol-pattern template every backend follows, read `docs/spec/20-memory-backend.md` + PR #57.

---

## MemoryBackend (#57, operator override surface added at #382 PR 1)

`FilesystemBackend(agent_root, *, lock_backend=None)` reference impl — the Protocol-pattern template every later backend follows. All memory reads and writes go through `MemoryBackend`; call-site code never touches disk paths directly.

**Protocol conformance:** `tests/test_memory_protocol_conformance.py` (26 named behavioral tests, parameterized fixture accepts any `MemoryBackend` impl) + `tests/test_memory_filesystem_backend.py` (10 filesystem-specific tests — `.versions/` layout, INDEX.md format, path enforcement, staging lifecycle).

**Operator override surface** (added in #382 PR 1 — mirrors LockBackend / LogBackend):

- `ATOMIC_AGENTS_MEMORY_BACKEND` env var (default `"filesystem"`) — unknown ids fail-fast at agent construction with `BackendNotRegistered` + full known-id list; no silent fallback.
- `AtomicAgent(..., memory_backend=...)` constructor kwarg — always wins over env var.
- `get_default_memory_backend(agent_root, *, lock_backend=None)` public factory exported from `atomic_agents.memory` — one selection seam for all state-writing construction sites (`agent.py`, `dream.py`, `tuning.py`, `_capture.py`, `_versioning.py`).
- Uniform construction contract: every registered `MemoryBackend` MUST accept `(agent_root: Path, *, lock_backend=None)` — normative spec/20 MUST + registry conformance test.
- `lock_backend` threading: factory threads through to registered backend so `apply_staging` and `agent.call()` share the same lock backend instance.
- **Delegate threading deliberately absent**: memory is per-agent state (each agent owns its own `memory/` directory). `delegate()` never threads the coordinator's memory backend to children — each child resolves independently via the process-global env selection (distinct from fleet-scoped persona/corpus backends which are threaded). Cross-agent memory sharing is a Tier A design fork; the kwarg escape hatch on each agent's own construction is the expressible path.
- `DreamRunner` guards against non-filesystem backends at construction (`NotImplementedError` rather than silent failure at apply time). Routing through a backend-agnostic staging-adopt path deferred to [#396](https://github.com/dep0we/atomic-agents-stack/issues/396).
- `ATOMIC_AGENTS_MEMORY_BACKEND_URL` companion var ships in [#258](https://github.com/dep0we/atomic-agents-stack/issues/258) PR 1 (Postgres). Set alongside `ATOMIC_AGENTS_MEMORY_BACKEND=postgres`.

**Doctor checks** (two-check pair mirrors LockBackend):

- `check_memory_backend_config` — coherence: known id, constructs for non-filesystem ids. Doctor-reuses-factory invariant.
- `check_memory_backend` — liveness: factory resolves and `stats()` returns.

**Override tests:** `tests/test_memory_operator_override.py` (29 tests — factory env-var path, kwarg-wins, lock threading, registry helpers, uniform construction contract + registry conformance, doctor coherence + liveness checks including registered-backend PASS).

**Implementer Contract:** 7 MUSTs in spec/20 §"Implementer Contract" — uniform construction, `lock_backend` threading, impl identifiability, write-4-case semantics, `WritePolicy` enforcement, atomic writes, capability advertisement.

**Closes:** the memory config→backend wiring seam (T5; gates Phase 2 Postgres/pgvector scale-out). Default stays filesystem; zero behavior change for existing deployments.

**`PostgresMemoryBackend` (#258 PR 1):** Non-semantic Postgres reference impl (FTS/tsvector recall, `supports_semantic_search=False`). Ships alongside `FilesystemBackend` as the second MemoryBackend reference impl. Targets multi-host deployments (Cloud Run, shared Postgres). `ATOMIC_AGENTS_MEMORY_BACKEND_URL` companion env var. Tier B field-lossless export. Schema `_SCHEMA_VERSION=2` (v1→v2 `display_name` migration for cross-backend `Note.name` parity) independent of `PostgresLogBackend`. Advisory lock key distinct from log backend. EmbeddingBackend Protocol (#200) shipped as `atomic_agents/embedding/` (DRAFT spec/46, PR 2); pgvector wiring in `PostgresMemoryBackend` deferred to #258 PR 2/PR 3.

---

## LLMBackend (#87)

Anthropic + OpenAI + Moonshot + Vertex Gemini reference impls, registered lazily on the first `find_backend_for_model` lookup (not at module import — spec/31; Vertex Gemini behind the optional `[vertex]` extra, skipped with a DEBUG log when `google-genai` is absent); conformance suite parametrizes across all four.

---

## JudgeBackend (#112, locked at PR 4)

`tests/test_judge_protocol_conformance.py` parametrizes across registered backends. PolicyJudge (rule engine) + LLMJudgeBackend reference impls; ESCALATE + REVISE state machines; `judges.md` operator config with cascade-aware project floor; operator-driven resolution flow (Approved / Denied / Redacted / Revised / Auto-decided); body-integrity check + O_EXCL sidecar de-dup + CAS-safe auto-decide.

**PR 5a (unreleased):** `escalation.fallback_on_timeout` widens to per-class dict form; auto-decide resolves policy from PENDING frontmatter `action_class`. **PR 5b (unreleased):** strict JSON-Schema validation of amended `tool_arguments` via the opt-in `[validation]` extra (`validation: strict` in `judges.md`); default remains `weakened` (PR 3c behavior), so operators upgrading without flipping the field see no behavior change. Concludes the #112 arc-with-amendments.

Dispatch opt-in via `judges.md` in the agent root or `AGENT_JUDGE_ENABLED=1` — existing deployments see no judge invocation by default.

---

## LockBackend (#60, locked at PR 4)

`tests/test_lock_protocol_conformance.py` parametrized across both backends.

`FilesystemLockBackend` (POSIX `fcntl.flock` advisory; preserves the legacy `<agent>/.lock` on-disk artifact byte-for-byte) + `RedisLockBackend` (single-instance Redis advisory lock + atomic Lua release/renew + daemon heartbeat at TTL/3 + `LockLost` lease-expiry detection) reference impls.

`scope(sub_path)` Protocol method lets operators pass ONE backend; framework re-scopes for dream + memory paths internally.

Operator override via `ATOMIC_AGENTS_LOCK_BACKEND` + `ATOMIC_AGENTS_LOCK_BACKEND_URL` env vars (deployment path) OR `AtomicAgent(..., lock_backend=...)` constructor kwarg (programmatic path — always wins). `doctor.check_lock_backend` validates operator-config coherence with PASS/WARN/FAIL ladder + credential-redacted URL output.

`_locks.AgentLock` preserved as a deprecation shim (sunset planned for v1.1; deferred from v1.0 per #201 PR 5 release decision).

**Closes the multi-host cliff** that motivated the entire arc: atomic-agents now runs on Cloud Run / Kubernetes / gizmo without forking the framework.

---

## LogBackend (#61, locked at PR 4)

`tests/test_log_protocol_conformance.py` parametrized across all three backends.

`FilesystemLogBackend` (JSONL-on-disk; preserves the legacy `<agent>/log/YYYY-MM/YYYY-MM-DD.jsonl` artifact byte-for-byte via `_io.atomic_append_jsonl`) + `SQLiteLogBackend` (stdlib `sqlite3`, no optional extra; six indexes covering dashboard + cost-guardrail query patterns; WAL journal mode + per-thread connections for multi-process append safety on local filesystems; aggregation pushdown via SQL `GROUP BY` for canonical columns + SQLite JSON1 `json_extract` for primitive-specific `extra`-field group_bys with alphanumeric-identifier SQL injection guard; index-driven `delete_older_than`; schema version tracking with idempotent `INSERT OR IGNORE` cold-start init for multi-replica deployments) + `PostgresLogBackend` (opt-in `[postgres]` extra, psycopg 3; bounded connection pool with an operator-tunable ceiling; advisory-lock cold-start schema init for multi-replica safety; full DSN credential redaction; `cost_usd`/`latency_ms` stored as `DOUBLE PRECISION` for cross-backend value parity with filesystem/SQLite; real-Postgres conformance gated by a CI service container).

Operator override via `ATOMIC_AGENTS_LOG_BACKEND` + optional `ATOMIC_AGENTS_LOG_BACKEND_URL` env vars OR `AtomicAgent(..., log_backend=...)` / `OutcomeRunner(..., log_backend=...)` / `DreamRunner(..., log_backend=...)` constructor kwargs (programmatic path — always wins; threads through to internal sub-agents).

`LogQuery.agent_name` filter (added in PR 3 review-pass per Step 11 P0 #1) for shared-backend cross-agent isolation with lenient match for legacy records (records without `agent_name` match any filter — filesystem per-agent-dir scoping is the natural isolation primitive).

`doctor.check_log_backend` validates operator-config coherence with PASS/WARN/FAIL ladder + stats probe (records_today / records_this_month) + URL-credential redaction.

Implementer contract for queryable backends documented in `docs/spec/22-log-backend.md` §"Implementer contract for queryable backends" — future Postgres / Datadog / Loki / Cloud Logging adapters mirror the SQLite reference's shape.

**Closes the dashboard-perf cliff** + remote-shipping requirement: operators on Cloud Run / Kubernetes with N replicas can pin SQLite for O(log N) indexed queries + indexed retention; the same Protocol seam admits future Datadog / Loki / Postgres-with-pgvector backends without forking the framework.

---

## AgentProfileBackend (#63, locked at PR 4)

`tests/test_profile_protocol_conformance.py` parametrized across both backends — 46 tests × 2 backends = ~92 invocations.

`FilesystemAgentProfileBackend` walks `<agent>/persona/IDENTITY.md|SOUL.md|USER.md` + `<agent>/{model,tools,judges,roster,mcp,goal}.md` + `<agent>/skills/<name>/SKILL.md` via the existing parsers; preserves byte-for-byte on-disk artifacts via `_io.atomic_write`; cascade-aware via `_cascade.detect_cascade`; JSON-based snapshot trio at `<scope_root>/.snapshots/<agent_id>/<snapshot_id>/{profile,metadata}.json` with `_validate_snapshot_id` path-traversal refusal + `relative_to(snapshots_root)` path-scope check + `metadata.agent_id` cross-check.

`SQLiteAgentProfileBackend` (stdlib `sqlite3`, no optional extra; JSON blob + indexed scalars approach — `agents(name PK, agent_mode indexed, profile_json, updated_at)` + `profile_snapshots(snapshot_id PK, agent_id+created_at composite indexed, label, profile_json)` + `meta(key PK, value)` with schema_version tracking via idempotent `INSERT OR IGNORE` cold-start init; `threading.local` connection pool + WAL journal mode + `synchronous=NORMAL` for multi-process append safety on local filesystems; cross-agent snapshot isolation enforced via `WHERE snapshot_id = ? AND agent_id = ?` AND-clause).

`supports_skills` capability dimension — filesystem=True (walks skill dirs), SQLite=False (skills stay filesystem-only in v1; future `save_skill` Protocol method lands when SaaS UI editing requires DB-backed skill bodies).

48-bit snapshot id random tail (Step 11 adversarial F-8) makes same-second collision at 4K snapshots/sec ~6e-8.

Operator override via `ATOMIC_AGENTS_PROFILE_BACKEND` + optional `ATOMIC_AGENTS_PROFILE_BACKEND_URL` env vars (when `=sqlite` without URL, defaults to `<scope_root>/.profile.db` so single-host operators get a working SQLite default by flipping ONE env var) OR `AtomicAgent(..., profile_backend=...)` / `OutcomeRunner(..., profile_backend=...)` / `EvalRunner(..., profile_backend=...)` / `DreamRunner(..., profile_backend=...)` constructor kwargs (programmatic path — always wins; threads through to internal sub-agents and `delegate.py`).

`AgentProfile` carries typed shadow + raw text for every config file (spec/24 Decision 1 — `mcp_md_raw` preserves `$VAR` env refs verbatim so save paths never bake resolved secrets into on-disk state). `save_profile` re-derives `agent_mode` from `persona_identity` on every write (spec/24 Decision 6 — single source of truth).

`doctor.check_agent_profile_backend` validates operator-config coherence with PASS/WARN/FAIL ladder + capability snapshot (incl. `supports_skills` disclosure) + agent-count probe + URL-credential redaction.

Implementer contract for registry-backed backends documented in `docs/spec/24-agent-profile-backend.md` §"Implementer contract for registry-backed backends" (8 normative MUSTs covering path-traversal refusal at API boundary, cross-agent snapshot isolation at storage layer, agent_mode re-derivation discipline, raw-text round-trip preservation, idempotent schema init across processes, snapshot id entropy budget, thread-life-tied connection management, supports_skills capability honesty) — future Postgres / git / SaaS-database adapters mirror the SQLite + filesystem references' shapes.

**Closes the SaaS-shape cliff**: SaaS / database-backed / git-backed agent registries are now ONE Protocol implementation away from the framework's existing operator-config surface. Same agent definitions, same `agent.call()` flow, same audit trail — different substrate.

---

## ToolRegistryBackend (#64, locked at PR 4)

`tests/test_tool_registry_protocol_conformance.py` parametrized across both backends — 43 conformance test functions running on filesystem + SQLite, 18 skips on capability gates.

`FilesystemToolRegistryBackend(agent_root)` walks `<agent>/tools/<name>.md` for descriptors + `<agent>/tools/<name>.py` for handler modules via `importlib.util.spec_from_file_location`; refuses path-traversal in `name` at API boundary; refuses control characters; 256 KB descriptor size cap defending against YAML alias-bomb DoS (PR 1 Step 11 REPRODUCED at 33 GB RSS pre-fix); treats `chmod-000 tools/` as empty rather than `PermissionError`-crashing every agent construction (PR 2 Step 11 P1 REPRODUCED); `validate()` is static-only — descriptor parse + handler import + signature check, NO handler execution.

`SQLiteToolRegistryBackend(db_path, agent_scope, *, handlers_root=None)` (stdlib `sqlite3`, no optional extra; hybrid storage shape — SQLite stores metadata only (descriptor JSON + handler path + version + classification + scope + timestamps), handler **bodies** live on disk as `.py` files under `<handlers_root>/<agent_scope>/<name>.py` and load via the same `importlib.util.spec_from_file_location` path the filesystem reference uses; base64-exec'd-source design was rejected at the plan-subagent stage because it silently breaks closures + module-level imports + `session = requests.Session()` patterns; schema `tools(agent_scope, name, descriptor_json, handler_path, version, classification, created_at, updated_at, PRIMARY KEY (agent_scope, name))` — composite PK so two scopes can both have a tool named the same; `meta(key PK, value)` schema-version with idempotent `INSERT OR IGNORE` cold-start race fix; `PRAGMA busy_timeout=5000` BEFORE `PRAGMA journal_mode=WAL` resolves the multi-process WAL race REPRODUCED 3/5 pre-fix in PR 3 Step 11; `threading.local` connection pool + WAL journal mode + `synchronous=NORMAL` for multi-process append safety on local filesystems; cross-scope isolation enforced via `WHERE agent_scope = ?` on every query; URL factory `make_sqlite_tool_registry_backend_from_url` honors `sqlite:///path?agent_scope=<name>` and refuses non-sqlite scheme / netloc / fragments / duplicate query params / unknown query params — credential redaction across all 5 `ValueError` sites via `_redact_url` helper resolves the PR 3 Step 11 P1 REPRODUCED postgres-URL credential leak; `:memory:` mode is single-threaded test-only — `check_same_thread=True` + per-instance `tempfile.mkdtemp()` for `handlers_root` honoring the non-persistent promise; `handlers_root` refuses `<= 1`-component paths defending against root-write on misconfigured Linux).

`install()` is TOCTOU-safe via **INSERT-first + atomic_write-on-success-only** ordering (PR 3 Step 11 REPRODUCED 50/50 pre-fix — original handler-atomic_write-first order caused concurrent installs to destroy the winner's handler file via the loser's rollback `unlink()`); losers see `rowcount=0` and raise `ToolAlreadyInstalled` WITHOUT touching disk. `install()` rejects non-callable handler at install time. `install()` rejects non-None `version` when `supports_versioning=False` (capability honesty).

Operator override via `ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND` + optional `ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND_URL` env vars (when `=sqlite` without URL, defaults to `<agent_root>/.tools.db` with `agent_scope=<agent_root.name>` so single-host operators get a working SQLite default by flipping ONE env var) OR `AtomicAgent(..., tool_registry_backend=...)` / per-runner kwargs on OutcomeRunner/EvalRunner/DreamRunner (programmatic path — always wins; threads through to internal sub-agents — `delegate.py` deliberately does NOT thread because tool registry is per-agent scoped per spec/25 Decision 9, distinct from the fleet-scoped `profile_backend` which IS threaded).

Backend tools register into `agent.tool_registry` AFTER operator-supplied `tools=ToolRegistry()` kwarg with `allow_overwrite=False` so collisions surface loudly as `ToolNameCollision`; **empty / missing `<agent>/tools/` yields zero registrations** — all 115 `AtomicAgent(...)` construction sites in the test suite see byte-identical pre-#64 behavior.

`doctor.check_tool_registry_backend` validates operator-config coherence with PASS/WARN/FAIL ladder + capability snapshot + tool-count probe + URL-credential redaction.

Implementer contract for registry-backed tool backends documented in `docs/spec/25-tool-registry-backend.md` §"Implementer contract for registry-backed tool backends" (8 normative MUSTs covering path-traversal refusal at API boundary, cross-scope isolation at storage layer, atomicity on install via INSERT-first + atomic_write-on-success-only, two-tier descriptor round-trip — raw-text-preserving for filesystem-shape backends, lossy-parse-documented for structured-storage backends, idempotent schema init + busy_timeout before WAL pragma, capability honesty, trust-model framing for shared-catalog backends, connection / handler lifecycle).

Protocol seam in place; two reference impls (filesystem + SQLite) shipped; 43 conformance test functions across both backends pin the contract. Future PyPI / git / company-internal-HTTP / SaaS-database adapters slot in via `register_tool_registry_backend(...)` without forking core — same agent definitions, same `agent.call()` flow, same audit trail, different tool catalog.

---

## PolicyBackend (#89, locked at PR 4)

`tests/test_policy_protocol_conformance.py` parametrized across registered backends + `tests/test_policy_filesystem_backend.py` + `tests/test_policy_integration.py` + `tests/test_policy_cost_cap_consumption.py` + `tests/test_policy_noncap_log_only.py` + `tests/test_policy_noncap_integration.py`.

`FilesystemPolicyBackend(<project_root>)` reference impl: markdown + embedded YAML at `<project_root>/policy.md` (per Premise 5 + plan-eng-review D4 — fleet-wide single source of truth, no per-agent `policy.md`); mtime+size composite cache key catches same-second edits on 1s-granularity filesystems via the size proxy; `cache_ttl_s=0` capability declaration — operators observe edits within 0 seconds of mtime change (the framework-side mtime stat is the staleness contract; SaaS / Postgres backends declare their real internal TTL); `agent_name` charset `[a-zA-Z0-9_.+@-]+` enforced at API boundary with path-traversal / control-char / newline / leading-dot refusal; side-effect-free construction (lazy parse on first method call so the 115 existing `AtomicAgent(...)` construction sites stay byte-identical when no `policy.md` exists).

Only reference impl in v1; future Postgres / SaaS / org-admin-console adapters register via `register_policy_backend(...)` per /office-hours 2026-05-19 D2 (full Protocol seam from day 1).

`PolicySnapshotForCall` frozen at `agent.call()` entry per Premise 3 — every consumption site reads the SAME snapshot for the duration of the call; operator edits to `policy.md` mid-call defer to the next `agent.call()`.

Cost-cap MIN composition in `_check_cost_guardrails` (`effective_daily = MIN(policy.daily, model_md.daily)`; `effective_monthly = MIN(...)`; per-call `cost_cap` ceiling bounds same-dimension cap arithmetic); `MandateCheck` steps 7-9 consume pre-composed effective caps so Policy and Mandate cost-cap checks share the same arithmetic (PR 3a — cost caps enforce immediately and ignore the env-var flag).

Non-cap surfaces (tool allowlist, MCP server allowlist, model selection) consumed at the three matching call sites with `ATOMIC_AGENTS_POLICY_ENFORCE_NONCAP` env-var-gated enforcement — PR 3b shipped in log-only mode (flag default `false`); **PR 4 flipped the default to `true` so non-cap surfaces enforce by default; operators wanting log-only set `ATOMIC_AGENTS_POLICY_ENFORCE_NONCAP=false` explicitly**.

Tool dispatch: blocked tool yields a synthesized `policy_blocked` `ToolCallResult` mirroring the judge_blocked shape so the LLM sees a refusal on the next turn. MCP discovery: denied servers filtered BEFORE `MCPClientPool` construction so the framework doesn't pay the subprocess startup cost. Model selection: Policy's `get_effective_model()` return replaces the pre-Policy effective model in enforce mode.

Unified `policy_decision` event family with `decision_kind: deny | override` discriminator + `axis: cost_cap | tool_allowlist | mcp_allowlist | model_selection` + `enforced: bool` so SaaS / Postgres adapters target a frozen schema (Premise 4 — one event family answers "was this Policy or Mandate?" via `denying_layer`; cost-cap denials with `cap_action ∈ {alert, fallback}` emit `enforced=False`).

`PolicyDecision.model_from_per_call_override` (#274) captures the `agent.call(model=...)` kwarg when Policy supersedes it so the caller can detect the silent override; fleet-config-wins precedence documented in `AtomicAgent.call()` docstring + spec/32 §"Composition math". Per-call dedup set bounds tool-allowlist denial emissions to one event per `(tool_name, call)` (#273).

`policy.md` parser handles fleet-default `cost_caps` / `tools.{allow,deny}` / `mcp_servers.{allow,deny}` / `model` fields at top level + nested `agents: { <agent_name>: { ... } }` per-agent overrides with field-level MERGE for caps + UNION+deny-wins for allowlists + REPLACE for model selection; per-dimension MIN cap math (`daily` and `monthly` independently; cumulative deferred to v1.1).

Cross-host cap-overrun bound `(replica_count) × (per-call ceiling)` documented in `docs/spec/32-policy-backend.md` §"Cross-host bound" for shared-FS deployments (Postgres / SaaS adapters with linearizable state get exact-cap semantics through their own consistency layer).

Operator override via `ATOMIC_AGENTS_POLICY_BACKEND` env var OR `AtomicAgent(..., policy_backend=...)` / per-runner kwargs (programmatic path — always wins; threads through to internal sub-agents) + `delegate.py` threading per spec/32 D1 (Policy is fleet-scoped — a delegate inheriting the coordinator's pinned Postgres backend doesn't silently fall back to the filesystem default and bypass the operator's fleet cap; distinct from `mandate_backend` which is per-agent scoped and deliberately NOT threaded).

`doctor.check_policy_backend` validates operator-config coherence with PASS/WARN/FAIL ladder + capability snapshot + URL credential redaction.

Implementer contract for policy backends documented in `docs/spec/32-policy-backend.md` §"Implementer contract for policy backends" (7 normative MUSTs covering `agent_name` validation at API boundary, per-agent storage isolation, `cache_ttl_s`-bounded staleness, side-effect-free construction, capability honesty, URL credential redaction in factory `ValueError` sites, `PolicyDecision` event schema compliance).

**Closes the cross-agent configuration cliff**: operators with a fleet of agents stop hand-syncing `model.md` / `tools.md` / `mcp.md` across N agents; a single project-root `policy.md` is the audit-trail source of truth, fleet-default + per-agent overrides compose with most-restrictive-wins semantics, and SaaS / Postgres / org-admin-console adapters are ONE Protocol implementation away.

---

## MandateBackend (#124, locked at PR 4)

`tests/test_mandate_protocol_conformance.py` parametrized across registered backends + `tests/test_mandate_check.py` + `tests/test_mandate_reservations.py` + `tests/test_mandate_filesystem_backend.py` + `tests/test_mandate_integration.py`.

`FilesystemMandateBackend(scope_root)` reference impl: markdown + embedded YAML descriptors at `<scope_root>/mandates.md` (project scope) or `<scope_root>/<agent>/mandates.md` (agent scope); state at `<scope_root>/.judge-state/mandates.json` via `_io.atomic_write`; refuses path-traversal in `mandate_id` at API boundary; source-hash recomputation on every `load_mandate`; derived-EXPIRED state computed at load time.

Only reference impl in v1; future SaaS / mobile / Slack-bot adapters register via `register_mandate_backend(...)` per /office-hours 2026-05-17 Option 2 decision (build the seam upfront, don't retrofit later).

`MandateCheck` judge specialist (~730 LOC) implements validation steps 1-9: existence + source-hash binding + state + tool allowlist + target allowlist via per-agent named `TargetExtractorRegistry` (7 built-in heuristic extractors pre-registered at agent construction; MCP tools prefix extracted target with `mcp:<server>:`) + time window + token-cost projection with stale-baseline defense (if most-recent matching event's `ts` is before current iteration's start, fall back to `expected_cost_per_call_usd` so stale-baseline drift doesn't compound across multi-iteration runs) + external-cost projection via `CostEstimatorRegistry` fail-closed to spec-stable `mandate_external_cost_unprojectable` BLOCK reason + escalation thresholds with ESCALATE-preempts-BLOCK precedence.

Reservation pattern (`MandateReservationManager.create / commit / rollback / _expire` lifecycle with `threading.Timer`-driven TTL watchers + `threading.Lock`-serialized in-process state; `compute_outstanding(log_backend, scope, mandate_id)` four-clause definition — created AND NOT committed/rolled_back/expired/committed_on_recovery AND no cost event with matching `proposal_id` AND age < ttl_s — closes the cost-event-landed-without-_committed window; cost events for mandate-citing actions carry `mandate_id` + `proposal_id` so cumulative budget defense `_sum_prior_token_cost` matches against the right ledger).

Crash recovery via `MandateBackend.recover_orphan_reservations(log_backend, scope, *, lock_backend=None)` with `LockBackend.acquire(scope='mandate-recovery:<scope>')` scan-inside-lock discipline (pessimistic over-report > silent under-bill — token orphans emit `mandate_reservation_committed_on_recovery`; external orphans emit BOTH `_committed_on_recovery` AND `mandate_reservation_external_unverified` so operators verify in Stripe / vendor via the `atomic-agents mandate reconcile <reservation-id> --action {committed|rolled_back}` CLI).

Post-action verification event family (`mandate_action_verified` / `mandate_action_diverged` / `mandate_action_verification_unavailable` emitted exactly once per `external_side_effect` / `irreversible` action after cost commit; operator-facing audit signal, NOT a refund mechanism in v1).

Suspicious-rebind throttle (60s default; closes the source-hash-before-state edit window for prompt-injection-style threats; persisted on-disk in `MandateBackend.read_state` shape under `throttles` key — in-memory-only forbidden because crash-restart loop would defeat the prompt-injection defense).

`mandates.md` parser + `judges.md ## Mandates` operator config with cascade-aware project floor (floor-wins where stricter for safety: longer throttle, "block" beats "escalate") + constraint enforceability discipline (mandates without enforceable constraints AND without `unconstrained: true` + non-empty justification are rejected at load time).

Structural write protection: `mandates.md` excluded from default WritePolicy alongside `tools.md` / `judges.md` / `model.md` / `persona/IDENTITY.md` / `persona/SOUL.md` / `persona/USER.md` — even a malicious actor with a write-capable tool cannot grant itself authority; the WritePolicy is the authoritative protection, the `## Only operators grant mandates` discipline is the behavioral story.

Operator override via `ATOMIC_AGENTS_MANDATE_BACKEND` env var OR `AtomicAgent(..., mandate_backend=...)` / per-runner kwargs on OutcomeRunner/EvalRunner/DreamRunner (programmatic path always wins; threads through to internal sub-agents; `delegate.py` deliberately NOT threaded — per-agent scoping per spec/29 + spec/15 delegate isolation).

`doctor.check_mandate_backend` validates operator-config coherence.

Implementer contract for mandate backends documented in `docs/spec/29-mandate-backend.md` §"Implementer contract for mandate backends" (8 normative MUSTs covering path-traversal refusal at API boundary, per-scope isolation enforced at storage layer, state persistence via `read_state` / `write_state` Protocol methods (NOT filesystem-path contract), source-hash recomputation per load, lifecycle event emission via `LogBackend.append(record)`, reservation event discriminator shape, pessimistic crash recovery semantics, capability honesty).

Operator CLI surface ships with the impl: `atomic-agents mandate list` / `show` / `usage` / `reconcile`.

**Closes the durable-authorization cliff**: operators authoring `cumulative_external_usd: 6000` on a procurement mandate now have that cap defended against concurrent action races + crash-restart; post-hoc divergence audits surface when an action's executed target differed from authorization at proposal time; mandate revocation is operator-editable in `mandates.md` with immediate effect on the next agent run.

The Mandate primitive is orthogonal to the v1.0 Protocol queue (Corpus / MCPServerRegistry remained after PersonaBackend locked at #62 PR 4; Mandate primitive ships its OWN `MandateBackend` seam from day 1).

---

## PersonaBackend (#62, locked at PR 4)

`tests/test_persona_protocol_conformance.py` parametrized across registered backends + `tests/test_persona_filesystem_backend.py` + `tests/test_persona_composition.py` + `tests/test_profile_composition_snapshot.py` + `tests/test_profile_composition_restore.py`.

`FilesystemPersonaBackend(personas_root)` reference impl: persona records at `<scope_root>/.personas/<persona_id>/{IDENTITY,SOUL,USER}.md` + `metadata.json` sidecar (hidden namespace mirrors `.snapshots/` so `list_agents()` skips dot-prefixed entries and personas don't surface as agents).

Only reference impl in v1; future Postgres / SaaS / git adapters register via `register_persona_backend(...)` per the established Protocol-pattern seam.

`persona_id` charset `[a-zA-Z0-9_.+@-]+` enforced at API boundary with path-traversal / control-char / newline / leading-dot refusal. Side-effect-free construction (lazy walk on first method call so the 166 existing `AtomicAgent(...)` construction sites stay byte-identical when no `persona.link.md` exists).

Group-atomic `save_persona`: `mkdir(exist_ok=False)` claims the persona dir exclusively before any file write for race-free fresh-create (`overwrite=False` losers raise `PersonaExists` WITHOUT touching disk); `overwrite=True` uses swap-and-delete via a sibling temp directory with a 20-iteration retry bound sized for 16-thread contention on macOS APFS `ENOTEMPTY` semantics; PR 1 Round 3 closed an orphan-backup leak via best-effort `shutil.rmtree(backup, ignore_errors=True)`.

Snapshot trio (`snapshot` / `restore` / `list_snapshots`) flipped `supports_snapshot=False → True` in PR 3 with nested storage `<personas_root>/<persona_id>/.snapshots/<snapshot_id>/{IDENTITY,SOUL,USER}.md + metadata.json` (D-PP-10 — geometric cross-persona isolation: a snapshot record always resides under its parent persona's directory, so `rm -rf <personas_root>/<persona_id>/` removes the persona AND its full history cleanly without an explicit `persona_id` cross-check on the snapshot record).

`snap_<YYYY-MM-DDTHHMMSS>_<12hex>` snapshot ID format with 48-bit `secrets.token_hex(6)` random tail matches AgentProfile spec/24 Implementer Contract #8 (D-PP-11 — cross-Protocol uniformity enables a shared `_validate_snapshot_id` path-security guard; same-second collision probability at 4K snapshots/sec is ~6e-8).

`_save_persona_group_atomic` merges backup `.snapshots/` entry-by-entry on `overwrite=True` so a concurrent `snapshot()` racing the persona-dir replace cannot destroy snapshot history (PR 3 Round 1 P1 adversarial — the original single-directory-rename approach lost the full snapshot history under contention). `list_snapshots` defense-in-depth symlink-escape guard via `entry.resolve().relative_to(snapshots_root.resolve())` (PR 3 Round 1 P2 adversarial — matches `restore()`'s confinement check).

URL factory `make_filesystem_persona_backend_from_url("filesystem:///path")` handles `filesystem:///absolute/path` URLs and refuses non-filesystem schemes, netloc, fragments, duplicate / unknown query params, and relative paths; credentials redacted from all `ValueError` sites via `_redact_url`.

### Composition with AgentProfileBackend (D1 + D3 + D6 + D-PP-13)

`<agent>/persona.link.md` is the ownership trigger (YAML in a code block with two scalar fields: `kind: shared` + `persona_id: customer-support-v3` per D-ER-4 — the colon-prefixed single-scalar `shared:customer-support-v3` was rejected at /plan-eng-review because the colon violates D4's `persona_id` charset).

`AgentProfileBackend.external_persona_ref(agent_id) -> str | None` (D-PP-3 — supersedes D-ER-1's original boolean signature because the architecturally-right Optional[str] returns the persona_id the framework needs in one Protocol call) gives the bootstrap path the persona_id to look up without importing PersonaBackend.

`AgentProfileBackend.load_profile()` repopulates persona fields via `persona_backend.load_persona(persona_id)` and re-derives `agent_mode` from the loaded persona text (D-PP-4 — `agent_mode` is derived from `persona_identity` and would otherwise be stale because the persona fields are empty at `load_profile` return time when externally owned).

`save_profile()` ignores `profile.persona_identity / soul / user` when externally owned (D6 — mirrors spec/24 Decision 6's `agent_mode` ignore-on-save pattern; writes go through `persona_backend.save_persona()` only). `snapshot()` drops persona fields when externally owned (persona has its own snapshot history via PersonaBackend).

`restore()` drops snapshot's persona fields when restoring a pre-PersonaBackend snapshot (carrying full persona text) into an agent that is NOW externally owned; the framework emits a one-time `agent_profile_restore_dropped_persona_fields` warning per `(agent_id, snapshot_id)` via thread-safe per-process dedup with `threading.Lock`-guarded check-and-add (D-PP-13 migration-window event).

`<agent>/persona.link.md` AND `<agent>/persona/IDENTITY.md` both present raises `PersonaOwnershipConflict` at filesystem-backend `load_profile()` (D2a + D-PP-8 — filesystem-only loud refusal because two files on disk is a visible operator mistake the framework must surface; SQLite uses silent-drop with the equivalent `agent_profile_save_dropped_persona_fields` event for cross-backend uniformity).

SQLite v1→v2 schema migration adds the `agents.persona_id` column via forward-only upgrade routine with explicit race-loser handling (catches `sqlite3.OperationalError "duplicate column name"` then re-reads `schema_version`).

D-PP-1 sentinel sweep (`_is_agent_dir(agent_root)` predicate admits either `persona/IDENTITY.md` OR `persona.link.md`) updated at `load_profile`, `list_agents`, `exists`, AND extended to `list_skills` + `load_skill_body` in PR 3 (D-PP-12 — externally-owned agents now succeed at skill operations end-to-end).

### Operator surface

`atomic-agents persona list / show / snapshot --label "..." / list-snapshots / restore / clone` CLI exposes the full PersonaBackend lifecycle with zero LLM calls; catches `PersonaError` subclasses (including `PersonaNotFound`, `PersonaCorrupted`, `PersonaLinkInvalid`, `PersonaOwnershipConflict`, `PersonaSnapshotNotFound`) + `OSError` + `PermissionError` cleanly with `Error: <message>` on stderr + exit 1.

Default backend resolves to `FilesystemPersonaBackend(<scope_root>/.personas)`. Operator override via `ATOMIC_AGENTS_PERSONA_BACKEND` + optional `ATOMIC_AGENTS_PERSONA_BACKEND_URL` env vars OR `AtomicAgent(..., persona_backend=...)` / per-runner kwargs on OutcomeRunner/EvalRunner/DreamRunner (programmatic path always wins; threads through to internal sub-agents).

`delegate.py` threads `persona_backend` ONLY when the operator supplied it explicitly via the constructor kwarg (D-ER-2 — mirrors Policy's `_policy_backend_was_explicit` precedent at `agent.py:401`; default-resolved backends do not leak the coordinator's `personas_root` to delegates because persona is per-agent semantic context).

`doctor.check_persona_backend` validates operator-config coherence with PASS/WARN/FAIL ladder + capability snapshot + URL credential redaction.

Implementer contract for persona backends documented in `docs/spec/33-persona-backend.md` §"Implementer contract for persona backends" (8 normative MUSTs covering `persona_id` charset validation at API boundary, side-effect-free construction, capability honesty, URL credential redaction in factory `ValueError` sites, group-atomic save with the 20-iteration retry bound + last-writer-wins semantics, snapshot id determinism + cross-persona isolation, `backend_id` property stability, and `snap_<YYYY-MM-DDTHHMMSS>_<12hex>` snapshot ID format with `metadata.json` schema).

D5 retires spec/24's `TemplateProfileBackend` reservation entirely — `PersonaCapabilities.supports_templates` is the canonical home; a future persona-template marketplace (`pip install atomic-personas-starters` or a curated GitHub registry) is a v1.1+ distribution surface that the Protocol seam already accommodates without a forking change.

**Closes the shared-persona cliff**: a team running 5 customer-support agents stops maintaining 5 separate `SOUL.md` files that drift; one canonical persona record (`shared:customer-support-v3`) serves all 5 regional agents with consistent identity, versioning, snapshot/restore lifecycle, and operator-editable markdown. Home users with one agent running the legacy `<agent>/persona/{IDENTITY,SOUL,USER}.md` layout see byte-identical pre-#62 behavior because the legacy layout works forever through AgentProfile's existing filesystem walk; PersonaBackend reads activate only when an operator explicitly creates a `persona.link.md` shared-reference.

---

## CorpusBackend (#65, locked at PR 4)

`tests/test_corpus_protocol_conformance.py` parametrized across registered backends + `tests/test_corpus_filesystem_backend.py` + `tests/test_corpus_sqlite_backend.py` + `tests/test_corpus_registry.py` + `tests/test_corpus_composition.py` + `tests/test_corpus_wiring.py` + `tests/test_corpus_migration_regression.py` + `tests/test_corpus_doctor.py`.

`FilesystemCorpusBackend(agent_root)` reference impl reading `<agent_root>/wiki/` (distilled knowledge per the Karpathy style) + `<agent_root>/raw/` (operator-ingested source documents) with per-page `_io.atomic_write` safety + `render_index_summary(corpus)` Protocol method that returns the routing INDEX the agent loads at step [7] of the canonical load order per spec/04.

`SQLiteCorpusBackend` with FTS5 (stdlib `sqlite3`, no optional extra; hybrid storage shape with metadata in SQL + bodies on disk matching ToolRegistryBackend precedent; WAL journal mode + `PRAGMA busy_timeout=5000` before WAL pragma mirroring the multi-process race fix from #64; FTS5 virtual table for O(log N) indexed full-text query on page bodies + frontmatter titles; cross-agent isolation enforced at the SQL layer via `WHERE agent_scope = ? AND corpus = ?` double discriminator; `BEGIN IMMEDIATE` transaction discipline wrapping the read-validate-UPSERT-FTS sequence in `write_page`; INSERT-first + atomic_write-on-success-only atomicity for hybrid storage half-failure recovery; idempotent `INSERT OR IGNORE` cold-start schema init for multi-replica deployments).

Page name charset `[a-zA-Z0-9_.+@-]+` enforced at API boundary with path-traversal / control-char / leading-dot refusal. Side-effect-free construction (empty or missing `wiki/` + `raw/` yields zero registrations so all 166 existing `AtomicAgent(...)` construction sites stay byte-identical when no corpus is configured; IRON RULE byte-identity regression suite at `tests/test_corpus_migration_regression.py` pins the contract across 5 explicit assertions covering the wiki INDEX read path and bundle rendering).

Parametrized conformance suite across both backends pins the Protocol contract so future `PgvectorCorpusBackend` + Postgres adapters register via `register_corpus_backend(...)` without forking core (the semantic-search seam is deferred to the coordinated #258 Postgres-adapter family release so semantic-search coverage stays symmetric across MemoryBackend + CorpusBackend).

Call-site migration: `agent.py:_load_indexes()` routes `wiki/INDEX.md` reads through `corpus_backend.render_index_summary("wiki")` when registered (per spec/04 step [7]; legacy direct-read path catches `OSError` + `UnicodeDecodeError` with logged warning marker for soft-degrade symmetry). `bundle.py:_render_memory_breakpoint` gains a `corpus_backend: CorpusBackend | None = None` parameter threaded three levels through `render_bundle`, with a shared `_render_wiki_index_section(label, path, content)` helper producing byte-identical output between Protocol path and legacy fallback (IRON RULE assertion 4). `bundle.py:_source_paths` migration deferred to v1.1 (filesystem-only function; pinned by the deferral test and tracked at #314).

`CorpusBackend` becomes the source of truth for `wiki/` and `raw/` per spec/34 while `MemoryBackend` retains exclusive ownership of `memory/` only; `journal/` is carved out to `JournalBackend` (spec/43) as of #427 (spec/24 Decision 7 addendum + spec/02 addendum).

Operator override via `ATOMIC_AGENTS_CORPUS_BACKEND` + optional `ATOMIC_AGENTS_CORPUS_BACKEND_URL` env vars (when `=sqlite` without URL, defaults to `<agent_root>/.corpus.db` with `agent_scope=quote_plus(agent_root.name)` so single-host operators get a working SQLite default by flipping one env var) OR `AtomicAgent(..., corpus_backend=...)` constructor kwarg + per-runner kwargs on OutcomeRunner (threads in `outcome/_outcome_impl.py`) / EvalRunner (at `eval.py:363`) / DreamRunner (stores as `self._corpus_backend` for API parity; no internal `AtomicAgent` construction site in v1).

`delegate.py` explicit-only threading via `_corpus_backend_was_explicit` flag mirroring PersonaBackend D-ER-2 at `agent.py:431` (default-resolved backends do not leak the coordinator's `agent_root` to delegates because corpus is per-agent semantic context, distinct from fleet-scoped Policy + AgentProfile which always thread).

`doctor.check_corpus_backend` coherence check with PASS/WARN/FAIL ladder + capability snapshot + page-count performance cliff WARN when `stats().page_count` exceeds 1000 pages on `supports_full_text_search=False` (the WARN hint names `ATOMIC_AGENTS_CORPUS_BACKEND=sqlite` as the remedy, mirroring the LogBackend doctor precedent) + URL credential redaction across operator-facing error paths.

`atomic-agents corpus` CLI (`list`/`show`/`query`/`version`/`restore` subcommands, zero LLM calls, env-var-aware).

Implementer contract for corpus backends documented in `docs/spec/34-corpus-backend.md` §"Implementer contract for corpus backends" (9 normative MUSTs covering page name charset validation at API boundary, side-effect-free construction, capability honesty including `embedding_provider=None` invariant, `query()` capability precedence rule, `write_page()` 4-case behavior table, URL credential redaction across operator-facing error paths, cross-corpus isolation at storage layer, snapshot id determinism + cross-page isolation, `backend_id` stability + `close()` idempotency).

**Closes the GB-scale wiki cliff**: operators with a 10K-page wiki or hundreds of MB of raw documents stop waiting seconds per keyword grep over an unindexed filesystem; `SQLiteCorpusBackend` with FTS5 delivers O(log N) indexed full-text search at stdlib cost (no Postgres operator burden); future `PgvectorCorpusBackend` arrives via the coordinated #258 release for symmetric semantic retrieval across both substrates. Same agent definitions, same `agent.call()` flow, same audit trail, different corpus substrate.

---

## MCPServerRegistryBackend (#201, locked at PR 5 of 5)

`tests/test_mcp_server_registry_conformance.py` parametrized across both backends + `tests/test_mcp_server_registry_http_backend.py`.

`FilesystemMCPServerRegistryBackend(agent_root, read_paths)` reference impl reading `<agent_root>/mcp.md` + optional `read_paths` for shared catalogs.

`HTTPMCPServerRegistryBackend(catalog_url, agent_scope)` reference impl with tier-1/2/3 capability negotiation (OPTIONS probe for tier negotiation, `GET /capabilities` for structured capability body, tier-1 = read-only, tier-2 = read + install/uninstall, tier-3 = read + install/uninstall + audit).

Protocol surface: `list_mcp_servers` / `load_mcp_server` / `load_all_mcp_servers` / `validate_mcp_server` / `install` / `uninstall` / `capabilities` / `refresh_capabilities` / `close`.

### Key decisions

- **D1**: filesystem read-only; catalog server owns transactionality for HTTP.
- **D2**: per-agent scoping via `agent_scope` query param on HTTP.
- **D3**: MCP servers are processes; ToolRegistry is functions. Separate Protocols per spec/25 Decision 3.
- **D4**: tier negotiation — OPTIONS then capabilities endpoint.
- **D5**: `lock_backend` kwarg on filesystem for `.mcp_registry.lock` file distinct from agent main `.lock`.
- **D6**: pre-probe conservative False/False capability default; HTTP dynamic per tier; tier-1 fallback stays False/False.
- **D7**: env-var references resolve client-side at load time; install path must emit unresolved `$VAR` form.
- **D8**: 409 collision maps to `MCPServerAlreadyInstalled`; 405 triggers mid-session tier regression handler with re-probe + cache invalidation.
- **D9**: URL credential redaction via `_safe_catalog_url` in ALL error paths.

Conformance suite covers 10 backend MUSTs (name charset, side-effect-free construction, capability honesty, credential redaction, per-agent scoping, backend_id stability + close idempotency, transient-vs-permanent failure honesty, env-var resolution at load time, install/uninstall atomicity + idempotency, load_all consistency). Two additional framework-security MUSTs were added post-v1.0 (GHSA-xhcr-cqfr-m3hv): **MUST 11** (HTTP transport scheme gate — `HTTPMCPServerRegistryBackend` refuses non-loopback cleartext `http://` without opt-in; asserted in `tests/test_mcp_server_registry_http_backend.py`) and **MUST 12** (spawn gate — `MCPClientPool` validates every command basename against an operator-configurable allowlist before spawning; default set `{npx, uvx, python, python3, node, docker}`; operators replace via `## Allowed commands` in `mcp.md`; asserted in `tests/test_mcp.py`). MUST 11 and MUST 12 are NOT in the backend-parametrized conformance suite — they bind specific layers (`HTTPMCPServerRegistryBackend` and `MCPClientPool` respectively), not every backend implementation.

Capability flag evolution: PR 1-4 static False/False on HTTP (unconditional NIE on write paths); PR 5 dynamic True/True on tier-2+ probed backends (install/uninstall now live).

405 mid-session tier regression handler: re-probes then raises `NotImplementedError` with tier-change message + updates cache; if re-probe fails raises `MCPRegistryUnavailable` with "Capability cache may be stale" message.

Test count ~3,319-3,325 at PR 5 (delta +12 to +18 vs post-PR-4 3,307).

**Closes the v1.0 Protocol surface**: operators with a managed MCP catalog or a private HTTP catalog registry can now install/uninstall MCP servers from the same `agent.call()` flow as home-user filesystem operators.

---

## SecretBackend (#340, LOCKED spec/38, the thirteenth)

The first backend protocol added after the v1.0 twelve. Abstracts credential resolution behind a Protocol so alternate secret substrates (GCP Secret Manager, AWS Secrets Manager, HashiCorp Vault) drop in without forking.

**Reference implementations:** `FilesystemSecretBackend` (PR 1) + `GCPSecretManagerBackend` (PR 2).

`FilesystemSecretBackend` resolves env vars → macOS Keychain → `~/.config/atomic_agents/keys.json` in a fixed, non-configurable priority; credentials are machine-scoped, never vault-relative (spec/38 MUST 2). `GCPSecretManagerBackend` resolves the `latest` version of each secret from GCP Secret Manager live on every `get()` call (no caching, `supports_rotation=True`); installed via `uv add 'atomic-agents-stack[gcp]'`; uses ADC / workload identity; key-to-secret-name mapping: `key.lower().replace('_', '-')` (e.g. `ANTHROPIC_API_KEY` → `anthropic-api-key`).

The `atomic_agents/secret_backend/` package carries the `@runtime_checkable SecretBackend` Protocol + `SecretError` / `SecretNotFound` / `SecretBackendNotRegistered` hierarchy (`backend.py`), the `SecretRef` + `SecretCapabilities` frozen dataclasses (`types.py`), and the registry + `get_default_secret_backend()` factory (`__init__.py`). Package named `secret_backend/` (not `secrets/`) to avoid shadowing the stdlib `secrets` module.

`_get_key()` is **superseded**: the credential cascade now lives in the backend; all six live callers route through it, with `_get_key()` / `_get_anthropic_key()` / `_get_openai_key()` / `_get_moonshot_key()` kept as thin redirect wrappers so doctor and runtime resolve credentials through one code path.

Operator surface: `atomic-agents secrets check <KEY>` (present/absent + source label, never the value), `secrets which <KEY>` (source label only), `secrets validate` (capability snapshot). `ATOMIC_AGENTS_SECRET_BACKEND` (default `"filesystem"`; set to `"gcp"` for GCP) + `ATOMIC_AGENTS_SECRET_BACKEND_URL` (format `projects/<project_id>/secrets` for GCP). `doctor.check_secret_backend()` validates backend instantiation + capability honesty; when `backend_id == "gcp"`, delegates to `check_gcp_secret_backend()` for a non-billable ADC liveness probe (`credentials.refresh(Request())`), WARN when `GOOGLE_CLOUD_PROJECT` absent, mirrors `check_vertex_credentials()`.

LOCKED spec/38 carries 9 normative MUSTs (charset validation, machine-scoped sources, capability honesty, no value in exceptions, no value in `locate()`, no value in CLI output, empty-string-as-absent, `has()` delegation, no caching). 149 secret-backend tests: 56 conformance (25 parametrized x 2 backends + 6 non-parametrized charset tests) + 49 GCP-specific + 11 CLI + 33 filesystem-specific, plus 8 doctor tests in `test_doctor_gcp_secret_backend.py` (counted in the doctor suite, not the 149).

**Closes the credential-portability cliff:** the same agent runs on a laptop (Keychain / keys.json) and behind a managed cloud secret store (GCP Secret Manager) with no code change -- only the registered secret backend differs.

---

## GoalBackend (#425 + #448 PR1/PR2/PR3 + #483 PR1 + #496 PR1, LOCKED spec/41, the fourteenth)

The first agent-scope surface of the #383 four-protocol wave (goals, outcomes, journal, cascade). Carves the flat `goal.py` module — which held GoalManager, Goal/SubGoal dataclasses, CLI dispatch, and schema constants — into a proper Protocol + storage backend, separating storage abstraction from runtime behavior.

**Reference implementation:** `FilesystemGoalBackend` (#425 scaffold + #448 PR1 write-path adoption + #448 PR2 CAS conformance + #448 PR3 coordinator arc-closer + #483 PR1 clock injection + GoalManager thin shim + agent_root resolution + #496 PR1 backend-universe alignment). Reproduces today's exact `goal.md` / `goal_history.jsonl` / `goal_archive/` I/O byte-for-byte (except the intentional archive data-loss fix in #448 PR1 — optional fields are now preserved). Zero behavior change for home users outside that fix.

`FilesystemGoalBackend` stores goal state in `<agent_root>/goal.md` (frontmatter + body including the `## History` prose section) and structured history events in `<agent_root>/goal_history.jsonl` (JSONL, append-only). Archived goals live in `<agent_root>/goal_archive/<slug>.md` (flat `.md` file, no subdirectory). The backend is `@runtime_checkable` and exposes `load_goal` / `save_goal` / `append_history_event` / `archive_goal(agent_id, reason="completed", when=None)` / `list_archived` / `read_schema_version` / `export` / `capabilities` plus the load-bearing `apply_transition(sub_goal_id, to_status, fields, *, expected_from_status=None, when=None)` atomic primitive (the `expected_from_status` CAS guard ships in #448 PR3; the `when: date | None = None` injectable clock on both `apply_transition` and `archive_goal` ships in #483 PR1 — `apply_transition`'s `when` controls the `## History` prose date prefix only, `archive_goal`'s `when` controls all four date-stamped fields, single-resolution).

`apply_transition()` is the defining Protocol method: a single durable sub-goal status flip + optional `output`/`blocked_by` fields + one `goal_history.jsonl` audit line, all under a filesystem file lock. The optional `expected_from_status` parameter adds compare-and-set semantics (MUST 10): if supplied, the backend re-reads the sub-goal status under the lock and raises `GoalConcurrentModification` if it doesn't match — no write, no JSONL append. This is what a Postgres backend delivers via a `WHERE status = ?` SQL guard. Pure computation (legal transition checks, cycle detection via `_would_cycle`, `evaluate_completion`, `next_sub_goal`, `progress_report`) stays in `GoalManager` above the Protocol.

`GoalManager` was relocated to `_goal_impl.py`. `dispatch_as_outcome()` is now a thin shim calling `goal/coordinator.py:dispatch_sub_goal_as_outcome()` — the coordinator composes `GoalBackend` + `OutcomeRunner` with a fail-closed cost gate (`CostGuardrailBlocked`), pending→in_progress pre-transition, locked-free `OutcomeRunner.run()`, and terminal `apply_transition(expected_from_status='in_progress')` CAS guard. The documented public `from atomic_agents.goal import GoalManager` path is preserved as a **supported, non-deprecated** compatibility re-export (no `DeprecationWarning` — intentionally permanent, matching Principle #14).

**Operator override surface:** `ATOMIC_AGENTS_GOAL_BACKEND` env var (default `"filesystem"`) + `get_default_goal_backend(agent_root)` factory + `AtomicAgent(goal_backend=...)` constructor kwarg + `AtomicAgent.goal_backend` public attribute (both wired live as of #448 PR1). Also as of #448 PR1: `GoalManager` routes `load()` / `save()` / `append_history_event()` through the backend; `GoalManager.archive()` data-loss fix (no longer silently drops `deadline`/`related_*` optional fields on archive). As of #483 PR1: `GoalManager.archive()` and `abandon()` are thin shims over `backend.archive_goal(when=self.today)` (backend owns the "goal archived" prose exclusively; no double-write); `GoalManager.agents_root` and `agent_root` resolved via `Path.resolve()` at `__init__` (canonical-path invariant for all GoalManager file paths).

`doctor.check_goal_backend()` uses the dual-probe pattern (`list_archived` liveness + `load_goal` load probe) per `feedback_doctor_dual_probe_pattern`. Located in `atomic_agents/doctor.py`.

**Exportable:** `GoalExport` is a composite `ExportableResult` carrying `(goal_md_bytes, history_records_with_bytes, archived_goals_with_bytes)`. `FilesystemGoalBackend` implements `Exportable` with `supports_canonical_export=True` and is registered in the spec/40 export conformance harness (`tests/test_export_protocol_conformance.py` + `tests/test_export_capability_advertisement.py`).

LOCKED spec/41 carries 10 normative MUSTs (side-effect-free construction, capability honesty, `goal_text()` read-only, `save_goal()` write-what-I-give-you, round-trip fidelity, atomic `apply_transition` with enum-validate-fail-closed, archive write-ordering, collision-safe archive slug, idempotent archive retry, `apply_transition` compare-and-set CAS guard). Test suite: `tests/test_goal_backend_conformance.py` (60 tests, all 10 MUSTs; TEST 56–59 are the #483 PR1 clock-injection + ts-first-reorder + apply_transition prose-vs-ts conformance tests) + `tests/test_goal_filesystem.py` (filesystem-specific) + `tests/test_goal_doctor.py` (dual-probe, PASS/FAIL/SKIP ladder) + `tests/test_goal_dispatch_audit_ordering.py` (MUST 6 regression, 2 tests) + `tests/test_goal_coordinator.py` (coordinator integration, 21 tests — 19 from #448 PR3 + 2 backend-universe alignment tests added in #496 PR1: `test_coordinator_threads_gate_agent_backends_into_runner` instance-identity test + `test_coordinator_forwards_custom_log_backend_to_runner` behavioral test). The 4 pre-existing goal test files kept intact as the zero-behavior-change regression guard.

**Closes the goal-persistence cliff:** the same goal state machine — status transitions, history audit trail, archiving — can run on a filesystem or a Postgres table without forking `GoalManager` or the CLI. `dispatch_as_outcome()` correctness fix (goal.md persisted before `goal_history.jsonl` audit line, spec/41 MUST 6) and the goal-outcome coordinator (spec/41 §"Goal-outcome composition") both ship fully in this arc.

---

## OutcomeBackend (#426 + #448 PR2, LOCKED spec/42, the fifteenth)

The second agent-scope surface of the #383 four-protocol wave (goals, outcomes, journal, cascade). Carves the flat `outcome.py` module — which held `OutcomeRunner`, `OutcomeResult`/`IterationRecord` dataclasses, and the CLI entry point — into a proper Protocol + storage backend, separating the storage abstraction from the runtime behavior.

**Reference implementation:** `FilesystemOutcomeBackend` (PR 1 + PR2 write-path adoption). `OutcomeRunner.run()` now routes its single write site through `self.outcome_backend.write_result(agent_name, run_id, result)` — the backend owns the canonical path `agent_root/outcomes/runs/<run_id>/result.json` from `run_id`. `OutcomeRunner._write_result_json` stays intact as the reference serializer (TEST 30 in `test_outcome_backend_conformance.py` depends on it). Default case is byte-identical-location. **INTENTIONAL relocation fix (A1 ruling):** when an operator passes a custom `--output-dir`, `result.json` now lands at the canonical `outcomes/runs/<run_id>/result.json` (the audit envelope belongs with the run); the custom-output_dir `result.json` was previously invisible to `list_runs`/`read_result`/`export` (orphan bug). Agent ARTIFACT files still go to `output_dir`. Guarded by `tests/test_outcome_adoption_golden.py`.

`FilesystemOutcomeBackend` stores completed-run state in `<agent_root>/outcomes/runs/<run_id>/result.json` (the `OutcomeResult` envelope: iteration history, judge verdicts, artifact paths, aggregate cost/tokens, final status). The backend is `@runtime_checkable` and exposes the THIN envelope-only surface `write_result` / `read_result` / `list_runs` / `export` / `export_all` / `capabilities` + `backend_id`. Artifact-file discovery (output_dir glob-diffing), output_dir resolution, and run_id minting STAY in `OutcomeRunner` above the Protocol (Principle #3 layers-don't-merge). `OutcomeResult`/`IterationRecord` (canonical types in `outcome/types.py`) are **mutable** — deliberate divergence from the frozen-DTO convention, documented in the module docstring exactly like `goal/types.py` (the outcome layer is a state machine during the run). `OutcomeCapabilities` is frozen.

`OutcomeRunner` was relocated to `_outcome_impl.py`. The documented public `from atomic_agents.outcome import OutcomeRunner` / `IterationRecord` path (used in 10+ test files) is preserved as a **supported, non-deprecated** compatibility re-export (no `DeprecationWarning`, matching Principle #14), and `python -m atomic_agents.outcome` is preserved via `outcome/__main__.py`.

**Artifact-reference portability:** on-disk `result.json` stays BYTE-IDENTICAL for the default path (absolute paths); the net-new `export()` emits PORTABLE artifact refs by rebasing absolute paths to relative-to-`agent_root` (via `is_relative_to` guard). This rebases against `agent_root` (the agent's own root, for whole-agent portability), NOT the ruling's literal `agents_root` — a deliberate departure recorded in spec/42 §"Artifact-reference portability". (The #426 PR1 "Option C" write-path design — keep the direct `atomic_write` and leave a custom-`output_dir` `result.json` where the operator pointed it — was superseded by A1 / #448 PR2 `write_result` routing; see spec/42 §"Artifact-reference portability".) `export()`/`export_all()` are deliberately scoped to the single most-recent run (the single-run `OutcomeExport` shape) by deliberate design; a multi-run export shape is filed as #454.

**Operator override surface:** `ATOMIC_AGENTS_OUTCOME_BACKEND` env var (default `"filesystem"`) + `get_default_outcome_backend(agent_root)` factory + `OutcomeRunner(outcome_backend=...)` kwarg (keyword-only, kwarg-wins-over-env, default `get_default_outcome_backend(agent_root)` — added in #448 PR2) + public `AtomicAgent.outcome_backend` attribute (per-agent handle for operator inspection; the coordinator (`goal/coordinator.py`) uses `OutcomeRunner` directly — `AtomicAgent.outcome_backend` is NOT the coordinator's write path; the runner resolves its own backend independently).

`doctor.check_outcome_backend()` uses the dual-probe pattern (`list_runs` liveness + `read_result` only when runs exist) per `feedback_doctor_dual_probe_pattern`, probing the registered factory, not raw files. Located in `atomic_agents/doctor.py`.

**Exportable:** `OutcomeExport` is an `ExportableResult` carrying `(run_id, result_json_bytes, artifact_refs, backend_id, scope)`, re-exported from the `atomic_agents.export` package root. `FilesystemOutcomeBackend` implements `Exportable` with `supports_canonical_export=True` and `supports_artifact_storage=True`, registered in the spec/40 export conformance harness (`tests/test_export_protocol_conformance.py` + `tests/test_export_capability_advertisement.py`).

LOCKED spec/42 carries 9 normative MUSTs (side-effect-free construction, capability honesty, `list_runs` returns `[]` when absent, atomic writes, round-trip fidelity, `OutcomeCorrupted` on corrupt result.json, `AtomicAgentsError` on absent run, stable `backend_id`, write-once/result-immutability as the unique 9th axis). Conformance suite: `tests/test_outcome_backend_conformance.py` (76 tests, all 9 MUSTs + filesystem-specific) + `tests/test_outcome_adoption_golden.py` (3 tests, routing proof + custom-output_dir relocation + kwarg-wins). The 2 pre-existing outcome tests (`test_outcome.py`, `test_goal_outcome_composition.py`) kept intact as the zero-behavior-change regression guard.

**Closes the outcome-persistence cliff:** the same result.json envelope — iteration history, judge verdicts, artifact refs, final status — can run on a filesystem or a swapped store without forking `OutcomeRunner` or the CLI, and an `export()` carries portable (relative) artifact refs so a whole-agent move survives (T15 / Position B).

---

## JournalBackend (#427, DRAFT spec/43, the sixteenth)

Filed as [#427](https://github.com/dep0we/atomic-agents-stack/issues/427). Shipped in 1 PR (ADOPT-NOW ruling — all three read sites wired in the same PR as the Protocol definition).

**Reference implementation:** `FilesystemJournalBackend` (PR 1 of 1). Reproduces today's exact `journal/YYYY-MM/YYYY-MM-DD.md` month-bucketed I/O byte-for-byte. Zero behavior change for home users — the three legacy rglob callers in bundle.py, agent.py, and dream.py are replaced with single `JournalBackend` calls, but the rendered output is byte-identical (frozen by golden tests).

`FilesystemJournalBackend` stores journal entries in `<agent_root>/journal/YYYY-MM/YYYY-MM-DD.md` (month-bucketed markdown files, one per calendar date). The backend is `@runtime_checkable` and exposes `append_entry` / `query_by_date` / `list_entries` / `export` / `export_all` / `capabilities` + `backend_id`. The `.journal.lock` sidecar at `agent_root` level serializes concurrent same-day writes via `fcntl.flock` (MUST 9 append atomicity). `JournalEntry` and `JournalCapabilities` are **frozen** dataclasses — deliberate inverse of goal/outcome's mutable exception (journal entries are append-only immutable episodes, not state-machine objects).

**Operator override:** `ATOMIC_AGENTS_JOURNAL_BACKEND` env var (default `'filesystem'`) OR `AtomicAgent(..., journal_backend=...)` (programmatic path always wins). Registry: `register_journal_backend` / `get_journal_backend` / `list_journal_backends` / `unregister_journal_backend` in `atomic_agents/journal/__init__.py`.

**Doctor:** `check_journal_backend()` uses the dual-probe pattern (`list_entries(limit=1)` liveness + `entry.path.read_bytes()` only when entries exist) per `feedback_doctor_dual_probe_pattern`, probing the registered factory, not raw files. Located in `atomic_agents/doctor.py`.

**Exportable:** `JournalExport` is an `ExportableResult` carrying `(entries_with_bytes, backend_id, scope)`, re-exported from the `atomic_agents.export` package root. `FilesystemJournalBackend` implements `Exportable` with `supports_canonical_export=True` and `supports_date_query=True`, registered in the spec/40 export conformance harness (`tests/test_export_protocol_conformance.py` + `tests/test_export_capability_advertisement.py`).

**STORAGE-ONLY boundary:** the backend owns the data layer (find/read/sort/date-window → raw `JournalEntry`). Formatting STAYS at each call site. The divergence between bundle format (WITH backtick path line) and agent format (WITHOUT) is LOAD-BEARING — byte-identity golden tests in `test_journal_backend_conformance.py` (TESTS 44–45) freeze both.

**spec/02 ownership reconciliation:** journal/ ownership carved out from MemoryBackend's prior claim (spec/02 and spec/24 both corrected in PR 1).

DRAFT spec/43 carries 10 normative MUSTs (base-8 PersonaBackend pattern + MUST 9 append atomicity + MUST 10 date-query correctness incl. include-on-unparse fallback). Conformance suite: `tests/test_journal_backend_conformance.py` (60 tests — a parametrized conformance core (MUST 1–10) plus filesystem-impl-specific doctor, golden-render byte-identity, `_source_paths`/`_staleness_paths` selection-parity, and call-site-adoption tests that drive the real bundle/agent/dream functions, incl. the directory-escape FAIL + symlinked-entry PASS doctor pair + a normalized permissions-class fix_hint pair across the list/read probes) + `tests/test_journal_filesystem.py` (20 filesystem-specific tests).

**Closes the journal-persistence cliff:** bundle, agent, and dream all read from one canonical backend; a Postgres or append-log backend can drop in without touching any of the three call sites.

---

## QueueBackend (#428, DRAFT spec/44, the seventeenth)

Filed as [#428](https://github.com/dep0we/atomic-agents-stack/issues/428). Shipped in 1 PR (SCAFFOLDING-ONLY carve — the queue cluster is lifted out of `_cascade.py` into `atomic_agents/queue/` with zero new internal runtime callers wired; the documented spec/06 cron/project-runner free-function API is preserved via a thin non-deprecated re-export shim).

**Reference implementation:** `FilesystemQueueBackend` (PR 1 of 1). Reproduces today's exact `project_root/queue/{queued,claimed,done,dead-letter}/` directory-tree claim mechanics with behavior preserved — runtime semantics, on-disk layout, and POSIX-rename atomicity are unchanged. The one intentional on-disk addition is an additive `role` key in the `.lease.json` sidecar (additive, ignored by legacy readers) so `list_claimed(role=...)` filters on the claimed side.

`FilesystemQueueBackend` is **project-scoped** — its scope token is `project_root` (NOT `agent_root`), the one project-scoped backend in the v1.5 wave, matching spec/06 where the queue is a shared project resource. Construction is side-effect-free. The backend exposes the four atomicity primitives `claim_next` / `release` / `move_to_dead_letter` / `renew_lease` plus the `list_claimed` enumeration read primitive on the Protocol; `recover_stale_claims` is shared free-function code built only from Protocol calls (`list_claimed` + reclaim) so every backend runs the same recovery with no drift. `claim_next` atomicity is the queued→claimed POSIX rename (one winner under a concurrent race; the loser gets `FileNotFoundError` and tries the next candidate). The `.lease.json` sidecar write is best-effort; `recover_stale_claims()` falls back to the work file's mtime for torn/legacy sidecars. `QueueItem` (abstract Protocol type) carries NO `path` field — that filesystem detail lives on the `FilesystemQueueItem` subtype, so future Redis/SQS/DB backends don't fake a path. Symlink containment mirrors the journal/outcome siblings: resolve to CHECK `is_relative_to(project_root)`, then return the UNRESOLVED path for file ops, so `item.path` stays in the caller's representation (byte-identical to the pre-carve cron API). `export()` re-asserts containment on every durable leaf to refuse a symlinked work file exfiltrating host bytes.

**Operator override:** `ATOMIC_AGENTS_QUEUE_BACKEND` env var (default `'filesystem'`). Factory `get_default_queue_backend(project_root)`; registry `register_queue_backend` / `get_queue_backend` / `list_queue_backends` / `unregister_queue_backend` in `atomic_agents/queue/__init__.py`. No operator CLI in PR 1 (deferred to a follow-up that hardens onto a locked spec/44).

**Doctor:** `check_queue_backend(agent_root)` (derives `project_root` internally via `detect_cascade()`) validates the configured backend resolves, then applies the capability-honesty coherence probe — a SKIP/PASS/WARN/FAIL ladder that WARNs (not FAILs) when a `single_host_only=True` backend is run in a declared multi-host deployment (`ATOMIC_AGENTS_MULTI_HOST=true`/`1`). Located in `atomic_agents/doctor.py`. Credential-bearing override values are redacted in error messages.

**Exportable:** `QueueExport` is an `ExportableResult` carrying `(items_with_bytes, backend_id, scope)`, re-exported from the `atomic_agents.export` package root. `FilesystemQueueBackend` implements `Exportable` with `supports_canonical_export=True`, registered in the spec/40 export conformance harness (`tests/test_export_protocol_conformance.py` + `tests/test_export_capability_advertisement.py`). The export embeds the DURABLE subset only — `queued/` (the irreplaceable pending backlog) + `done/` + `dead-letter/` — and structurally EXCLUDES in-flight `claimed/` and all `.lease.json` sidecars (runtime-bound/ephemeral, double-claim hazard on re-import — mirrors the LOCKED `LockExport` precedent where `lock_file_names` is always `[]`). The conformance suite asserts the ephemeral exclusion even when a claim is currently held.

DRAFT spec/44 carries 12 normative MUSTs (base-8 PersonaBackend pattern + 4 queue-unique axes: atomic-claim-via-rename / no-double-claim-under-race; lease-expiry-recovery correctness; dead-letter terminal-transition / dead-work-stays-dead; `single_host_only` capability honesty). Conformance suite: `tests/test_queue_backend_conformance.py` (parametrized conformance core + the claim-race tests + doctor + export + import-equivalence) + `tests/test_queue_filesystem.py` (POSIX-rename + `.lease.json` sidecar + legacy-mtime-fallback specifics).

**Closes the queue-atomicity cliff (TENSIONS T4):** queue-claim atomicity was locked to POSIX `Path.rename()`. With the Protocol carved out, a Redis/SQS/DB backend providing cross-host atomicity can drop in without forking the claim logic, lifting the single-host constraint for multi-host deployments.

---

## IdempotencyBackend (#520, LOCKED spec/45, the eighteenth)

Filed as [#520](https://github.com/dep0we/atomic-agents-stack/issues/520). Shipped in 2 PRs: PR 1 — Protocol + FilesystemDedupLedger + conformance tests + doctor + canonical export (SCAFFOLDING-ONLY); PR 2 — agent.call() wiring (idempotency_key kwarg, two-phase gate, serve/queue/cron trigger integration, RunRecord audit fields, spec/22 addendum, spec/45 LOCKED).

**Reference implementation:** `FilesystemDedupLedger` (PR 1 scaffolding + PR 2 agent.call() wiring). Agent-scoped (`<agent_root>/idempotency/`). Provides at-most-once execution guarantees for agents that may receive the same trigger more than once (serve, queue, cron). A caller-supplied idempotency key gates execution: the first `begin()` for a key succeeds (FRESH); subsequent calls return IN_FLIGHT or COMPLETED without triggering re-execution.

`FilesystemDedupLedger` stores state under `<agent_root>/idempotency/`. `begin()` atomicity is via `os.open(O_WRONLY|O_CREAT|O_EXCL)` on the lease file — exactly one concurrent `open()` succeeds; the loser gets `FileExistsError` (EEXIST) and returns IN_FLIGHT with no TOCTOU window between check and reserve. `commit()` writes a MARKER-ONLY terminal entry via `_io.atomic_write` (temp + fsync + rename): `key + prior_run_id + result_ref + terminal: true`. No result content is stored at any scale — the `result_ref` is an opaque reference string (run_id, path, URI) that the caller owns. `lookup()` is read-only with no side effects.

**DedupDecision value object:** `DedupDecision(is_duplicate, state, prior_run_id, prior_result_ref)` — a frozen dataclass. FRESH, IN_FLIGHT, and COMPLETED are all expressed as `DedupDecision` fields, NEVER raised as exceptions. Only unrecoverable I/O errors (disk failure, symlink escape) raise `IdempotencyBackendError`.

**Canonical-path containment:** every ledger read/write/claim sink applies the same `_require_canonical_source`-style containment invariant as QueueBackend: regular-file invariant + root containment + symlink-leaf + symlinked-parent rejection.

**TTL + sweep:** `supports_ttl=False` — TTL enforcement (periodic sweep, not inline on `begin()`) is deferred to a follow-up PR per spec/45 PR2. The capability axis is advertised as `False` until the sweep is wired.

**Operator override:** `ATOMIC_AGENTS_IDEMPOTENCY_BACKEND` env var OR constructor kwarg. NO markdown config (env-var + constructor-kwarg-only, matching QueueBackend's carve shape).

**Doctor:** `check_idempotency_backend(agent_root)` uses the dual-probe pattern (write temp terminal entry, read back; claim-and-release a temp key) per `feedback_doctor_dual_probe_pattern`. `single_host_only=True` WARN (not FAIL) on `ATOMIC_AGENTS_MULTI_HOST=true`, mirroring `check_queue_backend`.

**Exportable:** `IdempotencyExport` is an `ExportableResult` registered in the spec/40 export conformance harness (`tests/test_export_protocol_conformance.py` + `tests/test_export_capability_advertisement.py`). `FilesystemDedupLedger` implements `Exportable` with `supports_canonical_export=True`. The export emits TERMINAL entries only; in-flight leases are structurally EXCLUDED (same ephemeral-exclusion precedent as `LockExport.lock_file_names=[]` and QueueBackend's `claimed/` exclusion).

LOCKED spec/45 carries normative MUSTs (capability advertisement + marker-only terminal-entry + lease/in-flight atomicity + `begin()` atomicity via O_EXCL + canonical-export terminal-only + release_lease idempotency/best-effort + cron_tick_key bucket stability). PR2 wiring MUSTs (W1–W8) cover call() integration order, release-on-failure lifecycle, deduped/in-flight return contracts, the Phase-2 begin()→COMPLETED race short-circuit (W7), commit-after-JSONL ordering, idempotency_key on every keyed run, and the queue-trigger extractor's no-`id`-fallback rule (W8). Conformance suite: `tests/test_idempotency_backend_conformance.py` (68 conformance tests, incl. release_lease MUST 13) + `tests/test_idempotency_filesystem.py` (44 filesystem-specific tests: O_EXCL race, MARKER-ONLY invariant, symlink containment, export ephemeral exclusion, release_lease symlink-leaf refusal) + `tests/test_idempotency_pr2_wiring.py` (PR 2 agent.call() two-phase gate integration tests with per-invariant negative controls).

**Closes the at-most-once cliff:** agents triggered by serve, queue, or cron had no framework-level dedup primitive. A durable idempotency ledger — swappable to Redis or Postgres for cross-host guarantees — closes this gap without forking the trigger layer. PR 2 wires it into `agent.call()` with the two-phase gate (lookup before lock → COMPLETED short-circuit; begin after cost gate → COMPLETED short-circuit for the lookup→commit→begin race, IN_FLIGHT raise, or FRESH claim), spec/22 RunRecord audit fields, HTTP 200 deduped / HTTP 409 in-flight serve responses, and cron_tick_key + extract_queue_idempotency_key trigger helpers. spec/45 LOCKED.

---

## EmbeddingBackend (#200, DRAFT spec/46, the nineteenth)

Filed as [#200](https://github.com/dep0we/atomic-agents-stack/issues/200). Shipped in PR 2 of a 3-PR arc (PR 1 = `PostgresMemoryBackend` FTS/#258; PR 2 = EmbeddingBackend Protocol + ref impl; PR 3 = pgvector wiring + registry + lock spec/46).

**Reference implementation:** `OpenAIEmbeddingBackend` in `atomic_agents/embedding/openai.py`. Implements the `EmbeddingBackend` Protocol using the OpenAI embeddings API (`text-embedding-3-small` default, configurable via constructor). No subclassing — structural typing.

**Protocol surface (spec/46 Implementer Contract):** `model_id` / `dimensions` / `provider_id` @properties; `capabilities() → EmbeddingCapabilities`; `embed(text) → list[float] | None`; `embed_batch(texts) → list[list[float] | None]`; `close() → None`. Key design decisions:

- **MUST-NOT-RAISE invariant:** `embed()` and `embed_batch()` return `None` on any failure (network, rate-limit, token-length, auth). Callers zip with input texts safely regardless of partial failure.
- **len(out)==len(in) invariant:** `embed_batch()` always returns a list of exactly `len(texts)` elements; failed items produce `None` at their index, not a shorter list. Empty input returns `[]`.
- **No `input_type` parameter in PR 2:** `EmbeddingCapabilities.supports_input_type` flag is advertised (`False` in all PR 2 implementations — honest, since the Protocol surface doesn't include the parameter yet). PR 3 adds the kwarg and flips the flag.

**Cost accounting (EMBEDDING_PRICING, isolated):** `EMBEDDING_PRICING` is a completely separate dict from `PRICING` (chat models). `calc_embedding_cost(model_id, input_tokens) → (cost_usd, cost_estimated: bool)` calls only `_embedding_fallback_rate()`, which scans `EMBEDDING_PRICING` exclusively. Unknown embedding models fall back to ~$0.13/1M (the max known embedding rate) — NOT Opus's $75/1M. The `cost_estimated=True` return flag means "unpriced model, used max known embedding rate" — distinct from `degraded=True` (I/O failure reading cost history). Rates verified 2026-06-17 against OpenAI's authoritative per-model docs pages (`developers.openai.com/api/docs/models/text-embedding-3-small` $0.02, `…/text-embedding-3-large` $0.13, `…/text-embedding-ada-002` $0.10), cross-checked via the embeddings guide's pages-per-dollar derivation.

**Exception hierarchy:** `EmbeddingError(AtomicAgentsError)` and `EmbeddingProviderUnavailable(EmbeddingError)` in `atomic_agents/exceptions.py` — for internal logging only. The `embed()` and `embed_batch()` signatures MUST return `None`, not raise.

**Key resolution:** `_get_key()` delegates to the framework SecretBackend (spec/38) via the same `_llm._get_key` resolver as `OpenAICompatibleLLMBackend` (KeySpec `ATOMIC_AGENTS_OPENAI_KEY` / `OPENAI_API_KEY`, keychain `atomic-agents-openai`, config key `openai`). Whatever backend is registered (Filesystem, GCP Secret Manager, …) resolves the key, so the embedding path is NOT a private cascade that bypasses an operator's `ATOMIC_AGENTS_SECRET_BACKEND` (the split-brain the /ship review army caught). Unresolved → `None` (graceful; embed() degrades to the None-fallback). The SDK-absence message constant is `MSG_NO_OPENAI_SDK` (named for its actual content; NOT `_API_KEY`/`_SECRET`/`_TOKEN`) to avoid CodeQL `py/clear-text-logging` false positives.

**Per-call client construction:** `_build_client()` is called inside `embed()`/`embed_batch()`, not in `__init__`. Required for `sys.modules` patching in tests.

**No production registry in PR 2:** constructor-injected by the consuming backend. Registry functions (`register_embedding_backend`, etc.) ship in PR 3 alongside pgvector wiring.

**Test layout:** `tests/stub_embedding.py` (TEST-ONLY `StubEmbeddingBackend` + `_RaisingStubEmbeddingBackend` for negative controls; never in production imports) + `tests/test_embedding_protocol_conformance.py` (55 conformance tests, parametrized over [stub, openai-mocked]; includes typed-branch + MUST-1 + dimensions + MUST-9 truncation negative controls per feedback lesson, plus a mechanized stub-not-imported-by-production guard) + `tests/test_openai_embedding.py` (53 OpenAI impl-specific tests; 3 skipped pending `ATOMIC_AGENTS_TEST_OPENAI_KEY`) + 21 cost tests added to `tests/test_costs.py`. 129 new tests total.

**Operator override:** no env-var selection in PR 2; constructor-injected only. PR 3 adds `ATOMIC_AGENTS_EMBEDDING_BACKEND` env var.

**Doctor:** deferred to PR 3 (no billable probe in PR 2).

**Closes:** the embedding abstraction cliff — both `PgvectorMemoryBackend` (#258 PR 2) and `PgvectorCorpusBackend` (#258 PR 3) share a single injected backend without duplicating provider logic. DRAFT spec/46 carries 9 normative MUSTs (MUST 1 input validation; MUST 2 side-effect-free construction; MUST 3 capability honesty; MUST 4 embed 4-case MUST-NOT-RAISE; MUST 5 URL/secret redaction; MUST 6 storage/key isolation; MUST 7 snapshot/vector determinism; MUST 8 backend_id stability + close() idempotency; MUST 9 len(out)==len(in) conformance invariant).

---

## ConversationBackend (#535, DRAFT spec/47, the twentieth)

Filed as [#535](https://github.com/dep0we/atomic-agents-stack/issues/535). Shipped in PR 1 of a multi-PR arc.

**Reference implementation:** `FilesystemConversationBackend` in `atomic_agents/conversation/filesystem.py`. Layout: `<agent_root>/conversations/<principal_id>/<conversation_id>/<iso_ts>_<run_id>_<NN>_<role>.json` — one file per turn, per-principal exclusive `fcntl.flock` write lock. The `<NN>` zero-padded per-call `seq` (user=00, assistant=01) disambiguates the two turns a single `call()` writes (they share one `run_id` AND `ts`), so the assistant file does not overwrite the user file.

**Protocol surface (spec/47 Implementer Contract):** `backend_id` @property; `capabilities() → ConversationCapabilities`; `load_turns(principal, conversation_id, budget_tokens) → list[Turn]`; `write_turn(principal, conversation_id, turn) → None`; `export(query=None) → ConversationExport`; `export_all() → ConversationExport`.

**Principal primitive:** `Principal(identifier, derivation_source, is_verified)` — typed authorization key for conversation ownership. `LOCAL_PRINCIPAL = Principal("local", "local", True)` is the home-user zero-config default. NOT the same as spec/37 `caller_identity` (unverified HTTP header). The serve layer MAY derive a `Principal` from a verified JWT/OIDC/mTLS token; it MUST NOT pass `caller_identity` raw.

**Security: two guards defending DIFFERENT classes — only Guard (2) is load-bearing for cross-principal isolation** (per `feedback_containment_reframe_not_whackamole`, the guards are NOT symmetric):
- Guard (1): `safe_resolve_under(principal_dir, conv_root)` — PERIMETER guard for path-escape OUTSIDE `conversations/`. A sibling symlink `conversations/bob -> conversations/alice` PASSES this guard (alice is inside `conversations/`), so Guard (1) does NOT defend principal identity. On the read path Guard (1) is SUBSUMED by Layer 2 (`_require_canonical_turn_path`, the per-entry canonical-path check), so the dedicated Guard-1 strip control NEUTRALIZES Layer 2 (patches it to a no-op) and asserts the asymmetry — escape refused with Guard (1), leak with Guard (1) stripped — rather than relying on a final-`[]` assertion a sibling guard also produces (per `feedback_false_green_test_needs_per_invocation_negative_control`).
- Guard (2): `principal_dir.resolve().name == principal.identifier` — resolved-path basename comparison, the SOLE load-bearing guard for cross-principal isolation. A symlink `conversations/bob -> conversations/alice` resolves to `alice`, which differs from `bob`, so the cross-principal read raises `ConversationAccessDenied`. Stripping Guard (2) makes the attack succeed; the shipped-code conformance test asserts the raise and goes RED on the strip, with a separate documented-vulnerability test pinning the leak (negative control verified empirically).

Conv-dir containment: `safe_resolve_under(conv_dir, principal_dir)` fires BEFORE `conv_dir.mkdir()` to block pre-staged symlink attacks on the conversation directory.

**Token-budget window (MUST 8):** oldest-first eviction; `(len(role) + len(content)) // 4 + 1` character-to-token approximation. One-turn floor: if the newest turn alone exceeds `budget_tokens`, returns `[]`. Agent.call() uses a static 8000-token budget (TODO: model-aware derivation in LOCK PR).

**Agent.call() wiring:**
- Three-channel backend selection: constructor kwarg → `ATOMIC_AGENTS_CONVERSATION_BACKEND` env → model.md `## Conversation Backend` field (PROVISIONAL). All channels resolve to `None` = single-shot (rule #14, backward-compatible by default).
- Prior turns injected into `messages[]` BEFORE `work_item` (TENSIONS T16, approved). NOT flattened into `assemble_system_prompt()`. Normalized before injection (drop empty-content turns, collapse consecutive same-role entries, drop a trailing user turn, drop leading assistant turns — the symmetric case to the trailing-user drop, since newest-first budget eviction can leave an assistant turn oldest) so the provider API never sees a same-role collision, an empty content block, or a leading-assistant message.
- Backend resolution is GATED on `conversation_id is not None` (single-shot calls never resolve a backend) AND fails soft inside channels (2)/(3): a misconfigured `ATOMIC_AGENTS_CONVERSATION_BACKEND` / model.md id degrades to `None` + WARNING rather than raising `BackendNotRegistered` and crashing the call (MUST 9 backward-compat).
- `continuity_persisted` is `True` only when continuity was not requested (`conversation_id is None`) OR a backend exists; it is `False` when a `conversation_id` is supplied but no backend is configured (nothing was persisted) — so the field never falsely tells the caller history was stored.
- Load AND write-back catch BOTH `ConversationBackendError` AND its sibling `PathTraversalError` (raised on a malformed `conversation_id`) — a bad id degrades to single-shot / `continuity_persisted=False` rather than crashing the billed call.
- Turn write-back AFTER `_log(log_record)` (JSONL-first principle, same ordering as idempotency commit).
- `response.continuity_persisted = False` on write-back failure (non-fatal, LLM call succeeded) AND on the mid-loop cost-cap skip (no write-back ran).
- `conversation_id` tagged on ALL seven terminal JSONL record sites (ok, dedup, lock_busy, pre-loop cost-skip, in_flight, mid-loop cost-skip, security-abort).

**SQLite + Postgres v2→v3 migration:** adds `conversation_id TEXT` column + `idx_conversation_id` partial WHERE NOT NULL index. SQLite migration PRAGMA-guarded for crash-resumability (same pattern as v1→v2). `_SCHEMA_VERSION` moves from 2 to 3. `LogQuery.conversation_id` AND-predicate field added AND wired in all three `LogBackend.query()` paths (SQLite/Postgres `WHERE conversation_id = ?/%s`, Filesystem skip-predicate) — the column + index ship WITH a real predicate, guarded by a round-trip conformance test (parametrized across all reference backends) with a strip negative control so the filter cannot regress to silently returning all records (guards the "column + index shipped, predicate forgotten" shortcut). The Postgres schema-version conformance assertion is pinned to the imported `_SCHEMA_VERSION` constant (not a hardcoded `== 2` literal) so the `@requires_postgres` test does not silently drift in CI's Postgres lane on the next bump.

**Exportable companion (spec/40):** `ConversationExport` subclasses `ExportableResult`; re-exported from `atomic_agents.export`. Stale `.tmp` files excluded.

**Exception hierarchy:** `ConversationBackendError`, `ConversationCorrupted`, `ConversationAccessDenied` in `atomic_agents/exceptions.py`. `ConversationCorrupted` is the branch-distinctive typed exception raised from corrupted turn files (KeyError / JSONDecodeError / TypeError) — makes the typed branch load-bearing for per-invocation negative-control tests.

**Spec/47 10-MUST Implementer Contract:** MUST 1 side-effect-free construction; MUST 2 principal-scoped fail-closed isolation (two independent guards); MUST 3 path traversal guard on all inputs; MUST 4 atomic turn writes (temp+fsync+rename); MUST 5 honest capability declaration; MUST 6 assembly slot is messages[], not system prompt; MUST 7 write-back failure is non-fatal; MUST 8 budget-bounded deterministic load; MUST 9 backward-compatible None default; MUST 10 spec/40 Exportable companion.

**Test layout:** `tests/test_conversation_protocol_conformance.py` (67 conformance tests including the Guard (2) cross-principal strip negative control with a real symlink, the Guard (1) perimeter strip control that neutralizes Layer 2 to isolate Guard (1), the MUST 3 `turn.run_id` bare-component guard with a documented strip negative control, ConversationCorrupted branch-distinctive log assertion, budget eviction boundary, export stale-tmp exclusion) + `tests/test_conversation_filesystem.py` (38 filesystem-specific tests: UTC normalization incl. tz-naive WARNING branch + negative control, O(n) eviction, flock lock file creation, conv-dir symlink containment, export scope/relative-paths, symlinked conversations/ root, multiple-principals isolation, 6 I/O-error mapping tests (glob OSError → ConversationBackendError; ENOENT read_text → TOCTOU skip with negative control; non-ENOENT OSError → ConversationBackendError; principal mkdir OSError; lock-file open OSError; atomic_write OSError), export iterdir OSError, os.scandir OSError → PathTraversalError, registry helpers (list_conversation_backends, unregister, get BackendNotRegistered, re-register DEBUG log, get_default registry-dispatch, get_default fail-fast, _redact_for_error_message 4 shapes)) + `tests/test_conversation_agent_wiring.py` (31 agent.call() integration tests: turn injection into messages[], same-call seq survival, messages[] normalization incl. leading-assistant + trailing-user drops, write-back failure / continuity_persisted incl. the conversation-id-requested-but-no-backend case, mid-loop cost-skip conversation_id tagging, three-channel resolution incl. the misconfigured-env-var fail-soft path, idempotency body-hash dedup skip for conversation calls, and 5 tag-site test pairs with per-invocation negative controls: pre-loop cost-skip, lock_busy, dedup/COMPLETED, in_flight, security-abort/MCPCommandNotAllowed — each with a WITH/WITHOUT conversation_id strip control). Plus 2 shared `LogBackend` conformance tests in `tests/test_log_protocol_conformance.py` (the `LogQuery.conversation_id` AND-predicate filter + round-trip, parametrized across all reference backends, with a strip negative control). 138 conversation tests total (107 from PR1 + 31 from issue #557 backfill). Updated: `tests/test_log_sqlite_backend.py` (schema-version assertions pinned to `_SCHEMA_VERSION`, matching the Postgres convention), `tests/test_log_postgres_backend.py`, `tests/test_idempotency_pr2_wiring.py` (schema v2→v3 assertion updates).

**Operator override:** constructor-injected or `ATOMIC_AGENTS_CONVERSATION_BACKEND` env in PR 1; model.md field PROVISIONAL.

**Doctor:** deferred to later PR.

**Closes:** the multi-turn conversation cliff — agents can now maintain context across invocations using the same `call()` interface, with zero behavioral change for callers that don't set `conversation_id`. TENSIONS T16 (raw-transcript injection into messages[] instead of system prompt) approved and committed on `docs/tensions-t16-conversation-flex` (separate branch).

---

## Why twenty protocols, summarized

A person at home runs filesystem-everything with one agent. An organization runs the same agents over Postgres, behind an HTTP service, with a fleet of orchestrated roles. **Same agent definitions, same `call()` flow, same audit trail. Different backends.**

That property is the moat. Each Protocol is one Implementer Contract away from a new substrate, and every reference impl follows the same shape established by `docs/spec/20-memory-backend.md` + PR #57.

Going forward: **the elegance is the product.** Protect it.
