# Backend protocols shipped

Twelve backend protocols are locked for v1.0. A thirteenth, SecretBackend (#340), is in progress for v1.5 — its PR-1 section (filesystem reference impl + DRAFT spec/38) is included below; the spec LOCKs and the GCP Secret Manager backend land at PR 2. Each section captures the reference implementations shipped, the operator override surface, the doctor coherence check, the Implementer Contract location, and the architectural cliff the protocol closes.

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
- `ATOMIC_AGENTS_MEMORY_BACKEND_URL` companion var deferred to [#258](https://github.com/dep0we/atomic-agents-stack/issues/258).

**Doctor checks** (two-check pair mirrors LockBackend):

- `check_memory_backend_config` — coherence: known id, constructs for non-filesystem ids. Doctor-reuses-factory invariant.
- `check_memory_backend` — liveness: factory resolves and `stats()` returns.

**Override tests:** `tests/test_memory_operator_override.py` (29 tests — factory env-var path, kwarg-wins, lock threading, registry helpers, uniform construction contract + registry conformance, doctor coherence + liveness checks including registered-backend PASS).

**Implementer Contract:** 7 MUSTs in spec/20 §"Implementer Contract" — uniform construction, `lock_backend` threading, impl identifiability, write-4-case semantics, `WritePolicy` enforcement, atomic writes, capability advertisement.

**Closes:** the memory config→backend wiring seam (T5; gates Phase 2 Postgres/pgvector scale-out). Default stays filesystem; zero behavior change for existing deployments.

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

`CorpusBackend` becomes the source of truth for `wiki/` and `raw/` per spec/34 while `MemoryBackend` retains exclusive ownership of `memory/` and `journal/` (spec/24 Decision 7 addendum).

Operator override via `ATOMIC_AGENTS_CORPUS_BACKEND` + optional `ATOMIC_AGENTS_CORPUS_BACKEND_URL` env vars (when `=sqlite` without URL, defaults to `<agent_root>/.corpus.db` with `agent_scope=quote_plus(agent_root.name)` so single-host operators get a working SQLite default by flipping one env var) OR `AtomicAgent(..., corpus_backend=...)` constructor kwarg + per-runner kwargs on OutcomeRunner (threads at `outcome.py:255`) / EvalRunner (at `eval.py:363`) / DreamRunner (stores as `self._corpus_backend` for API parity; no internal `AtomicAgent` construction site in v1).

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

Conformance suite covers 10 MUSTs (name charset, side-effect-free construction, capability honesty, credential redaction, per-agent scoping, backend_id stability + close idempotency, transient-vs-permanent failure honesty, env-var resolution at load time, install/uninstall atomicity + idempotency, load_all consistency).

Capability flag evolution: PR 1-4 static False/False on HTTP (unconditional NIE on write paths); PR 5 dynamic True/True on tier-2+ probed backends (install/uninstall now live).

405 mid-session tier regression handler: re-probes then raises `NotImplementedError` with tier-change message + updates cache; if re-probe fails raises `MCPRegistryUnavailable` with "Capability cache may be stale" message.

Test count ~3,319-3,325 at PR 5 (delta +12 to +18 vs post-PR-4 3,307).

**Closes the v1.0 Protocol surface**: operators with a managed MCP catalog or a private HTTP catalog registry can now install/uninstall MCP servers from the same `agent.call()` flow as home-user filesystem operators.

---

## SecretBackend (#340, DRAFT spec/38, PR 1 of 2 — the thirteenth)

The first backend protocol added after the v1.0 twelve. Abstracts credential resolution behind a Protocol so alternate secret substrates (GCP Secret Manager, AWS Secrets Manager, HashiCorp Vault) drop in without forking.

PR 1 ships `FilesystemSecretBackend` — resolves env vars → macOS Keychain → `~/.config/atomic_agents/keys.json` in a fixed, non-configurable priority; credentials are machine-scoped, never vault-relative (spec/38 MUST 2). The `atomic_agents/secret_backend/` package carries the `@runtime_checkable SecretBackend` Protocol + `SecretError` / `SecretNotFound` / `SecretBackendNotRegistered` hierarchy (`backend.py`), the `SecretRef` + `SecretCapabilities` frozen dataclasses (`types.py`), and the registry + `get_default_secret_backend()` factory (`__init__.py`). Package named `secret_backend/` (not `secrets/`) to avoid shadowing the stdlib `secrets` module.

`_get_key()` is **superseded**: the credential cascade now lives in the backend; all six live callers route through it, with `_get_key()` / `_get_anthropic_key()` / `_get_openai_key()` / `_get_moonshot_key()` kept as thin redirect wrappers so doctor and runtime resolve credentials through one code path.

Operator surface: `atomic-agents secrets check <KEY>` (present/absent + source label, never the value), `secrets which <KEY>` (source label only), `secrets validate` (capability snapshot). `ATOMIC_AGENTS_SECRET_BACKEND` (default `filesystem`) + `ATOMIC_AGENTS_SECRET_BACKEND_URL` (reserved for the GCP backend) env vars. `doctor.check_secret_backend()` validates backend instantiation + capability honesty.

DRAFT spec/38 ships 9 normative MUSTs (charset validation, machine-scoped sources, capability honesty, no value in exceptions, no value in `locate()`, no value in CLI output, empty-string-as-absent, `has()` delegation, no caching). Spec LOCK + the GCP Secret Manager reference impl land at PR 2.

**Closes the credential-portability cliff:** the same agent runs on a laptop (Keychain / keys.json) and behind a fleet HTTP service (GCP Secret Manager, PR 2) with no code change — only the registered secret backend differs.

---

## Why twelve protocols, summarized

A person at home runs filesystem-everything with one agent. An organization runs the same agents over Postgres, behind an HTTP service, with a fleet of orchestrated roles. **Same agent definitions, same `call()` flow, same audit trail. Different backends.**

That property is the moat. Each Protocol is one Implementer Contract away from a new substrate, and every reference impl follows the same shape established by `docs/spec/20-memory-backend.md` + PR #57.

Going forward: **the elegance is the product.** Protect it.
