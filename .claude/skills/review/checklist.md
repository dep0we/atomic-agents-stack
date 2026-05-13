# Pre-Landing Review Checklist — atomic-agents-stack

## Instructions

Review the `git diff origin/main` output for the issues listed below. Be specific — cite `file:line` and suggest fixes. Skip anything that's fine. Only flag real problems.

**Two-pass review:**
- **Pass 1 (CRITICAL):** Backend Protocol Invariants, Cost Gate Placement, Audit Trail Shape, Atomic Write Discipline, LLM Output Trust Boundary, Schema/API Break Detection. Highest severity.
- **Pass 2 (INFORMATIONAL):** Spec/Impl Drift, Public Surface Hygiene, Markdown-Config Aesthetic, Progressive Disclosure, Deprecation Shim Freshness, Test Conformance, CHANGELOG Hygiene, Documentation Match-to-Reality, One-Level Constraints.

All findings go through Fix-First Review: obvious mechanical fixes are applied automatically; genuinely ambiguous issues are batched into a single user question.

**A note on confidence calibration.** Several categories below are *semantic* (require reading the spec, the architecture doc, or `_costs.py` to judge correctness), not *mechanical* (a regex won't catch them). Treat these as informational-tier and tune confidence to 5-7 unless you've explicitly verified by reading the relevant source.

**Output format:**

```
Pre-Landing Review: N issues (X critical, Y informational)

**AUTO-FIXED:**
- [file:line] Problem → fix applied

**NEEDS INPUT:**
- [file:line] Problem description
  Recommended fix: suggested fix
```

If no issues found: `Pre-Landing Review: No issues found.`

Be terse. One line describing the problem, one line with the fix. No preamble, no "looks good overall."

---

## Review Categories

### Pass 1 — CRITICAL

#### Backend Protocol Invariants

The framework's load-bearing abstraction. See `CLAUDE.md` §"Protocols, not subclassing" and `docs/spec/20-memory-backend.md` for the pattern.

- **Provider-specific shapes leaking into the agent layer.** `agent.py`, `_capture.py`, `_costs.py`, `_locks.py` should never reference `anthropic.types.*`, `openai.types.*`, Anthropic's `tool_use` block shape, OpenAI's JSON-string `arguments`, etc. All cross-boundary shapes go through canonical types in `atomic_agents/llm/types.py` (`LLMToolDefinition`, `LLMToolUse`, `LLMToolResult`, `CacheDirective`, `LLMCapabilities`).
- **New backend not registered at framework import.** A new Protocol implementation (e.g., a new `MemoryBackend` or `LLMBackend`) must be registered via the canonical `register_*_backend()` API in the corresponding `__init__.py`, with lazy default init at framework import. Direct conditional dispatch (`if model.startswith("gemini")`) is the anti-pattern.
- **`if backend_type == "postgres":` style branching in core.** The whole point of the Protocol-pattern is to remove this. Refuse the diff and ask for a protocol method instead.
- **New backend without conformance test additions.** Every new backend Protocol gets ~25 conformance tests (parameterized across all impls) + ~10 impl-specific tests. See `tests/test_*_backend_conformance.py` for the template.

#### Cost Gate Placement

Per `CLAUDE.md` §"Cost is first-class, not bolted on" — every code path that calls an LLM has a cost gate. The discipline is structural, not per-feature.

- **New LLM call path without a cost guardrail.** Any new code that calls `_llm.call_*` or any `LLMBackend.call()` must be preceded by `_check_cost_guardrails(...)` (in `agent.call()`) or `_check_cap(...)` (in pipelines like `dream.py`, `eval.py`, `tuning.py`).
- **Cost check after a subprocess spawn or session establish.** Cost must be checked *before* paying the overhead, not after.
- **`critical=True` as a new default.** This bypasses cost gates and should remain explicit per-call, never plumbed as a default.
- **Helper batch dispatch without worst-case reservation.** `helper_call_parallel()` must reserve worst-case cost before dispatch, not after.
- **Delegate without tree-cap clamp.** Delegate spawns must clamp budget to `MIN(child_remaining, parent_remaining)`.

#### Audit Trail Shape

Per `CLAUDE.md` §"Audit trail is structural" — JSONL streams are the source of truth.

- **New event type without `run_id`.** Every agent run writes a JSONL line with `run_id`. Helper, tool, and delegate sub-events carry `parent_run_id` linking back.
- **Side-channel logging that bypasses the JSONL stream.** New surfaces (LogBackend, dashboards, alerts) translate from JSONL; they don't replace it. If a feature has events the existing shapes can't represent, extend the right shape (don't add a parallel stream).
- **Memory mutation without `.versions/` snapshot.** `write_note`, `restore_version`, `redact_version` write atomic version snapshots. New memory-mutating tools follow the same pattern.

#### Atomic Write Discipline

Per `CLAUDE.md` §"Atomic + idempotent everywhere".

- **New file write that doesn't go through `_io.atomic_write` (or equivalent fsync+rename).** Direct `open(path, "w")` for stateful files (not throwaway tmp) is the anti-pattern.
- **Teardown that isn't idempotent.** MCP pool close, tool deregistration, lock release must all run safely on the exception path *and* the success path. Multiple invocations should be no-ops, not errors.
- **Capture without `(type, name, body_hash)` dedupe key.** Memory captures dedupe on this triple; new capture-shaped paths follow suit.

#### LLM Output Trust Boundary

- LLM-generated values (paths, URLs, identifiers) written to disk without canonicalization. Use `_io.safe_resolve_under()` before any vault-relative write.
- Structured tool output (dicts, lists) accepted without `dataclass` validation before persistence.
- LLM-generated URLs fetched without allowlist — SSRF risk on agents that may reach internal networks.
- LLM output stored in memory/notes without sanitization — stored prompt-injection risk.

#### Schema / API Break Detection

Per `docs/deployment/versioning.md` — schema breaks and API breaks are Major-shaped (pre-1.0: Minor with `### BREAKING` callout).

- Frontmatter contract change in any spec doc (renamed field, type flip, dropped recognized value) without a `migrate.py` script + `CURRENT_SCHEMA_VERSION` bump.
- Public symbol removed/renamed from `atomic_agents/__init__.py` (`__all__`) without a deprecation shim with documented sunset date.
- Default backend change (e.g., default `MemoryBackend` switches from filesystem to something else) without a `### BREAKING` callout draft.

### Pass 2 — INFORMATIONAL

#### Spec / Implementation Drift

Per `CLAUDE.md` §"The spec is the product" — code without spec is incomplete; spec without matching code is aspirational.

- New backend implementation without a matching `docs/spec/NN-*.md` doc (or pending RFC issue tracked).
- Spec doc with status `locked` whose claimed types/contract no longer match the implementation surface.
- **Targeted divergence check (only if the diff narrows the scope to one or two spec docs).** If the diff touches a module whose top-of-module docstring or surrounding `__init__.py` references a specific `docs/spec/NN-*.md`, read that one spec doc and check for divergence on the surfaces this diff modifies. Otherwise skip — a full 22-spec-doc cross-check during pre-landing review is not feasible and would get rubber-stamped.

#### Public Surface Hygiene

- New Protocol / canonical type / public exception missing from `atomic_agents.__all__`.
- Test count claims in `CLAUDE.md`, `README.md`, or `docs/architecture.md` not updated to match the new test count.
- Spec count claims (e.g., "22 spec docs today") not updated when a new spec doc lands.
- README "What's shipped" table or protocol badges not updated when a new backend Protocol lands.

#### Markdown-Config Aesthetic

Per `CLAUDE.md` §"Markdown config or no config".

- New operator-facing config file in YAML / TOML / JSON instead of markdown (the `cost_guardrails:` embedded-YAML pattern in `model.md` is fine; pure-YAML files are not).
- New config field that doesn't fit the markdown shape. Ask whether the field is right before changing the shape.

#### Progressive Disclosure

Per `CLAUDE.md` §"Progressive disclosure by default".

- New context-bearing feature that loads its full body into the system prompt instead of advertising metadata + lazy-loading via tool.
- New MCP server registered eagerly instead of at the start of `call()` (with teardown in `finally`).
- Memory recall that returns full bodies for an unbounded set instead of INDEX-guided selective load.

#### Deprecation Shim Freshness

Per `CLAUDE.md` §"Backward compatibility by default".

- New deprecation shim without a documented sunset date in the shim's docstring.
- Existing deprecation shim now past its sunset date and not removed.
- New module that re-exports without `DeprecationWarning` emission on import.

#### Test Conformance

- New Protocol added without a parameterized conformance suite.
- New CLI subcommand without an integration test covering happy path + at least one error path.
- Migration-shaped PR (`atomic_agents.migrate`) without fixture tests across the relevant backend protocol.

#### CHANGELOG Hygiene

Per `docs/deployment/versioning.md` and `docs/deployment/release-runbook.md` — every **PR-level update** adds at least one bullet to `## [Unreleased]`. **Release-cut PRs** promote `## [Unreleased]` to a dated header; the substantive change IS the promotion, so no new bullet is required (a release-cut diff that *only* promotes the header is correct).

- PR-level update PR has substantive changes but no `## [Unreleased]` bullet added.
- `[Unreleased]` bullet that overstates or understates the change (e.g., calls a bug fix a "feature").
- Breaking change (schema, API, default) shipped without a draft `### BREAKING` callout in the PR body.
- Release-cut PR that promotes `## [Unreleased]` without leaving a fresh empty `## [Unreleased]` above for the next cycle.

#### Documentation Match-to-Reality

Per `CLAUDE.md` §"Documentation matches reality, not aspirations".

- Doc claim about CLI / runbook / behavior that does not match the implementation. Either fix the implementation or fix the doc — aspirational claims belong in issues, not docs.
- Docstring on a public symbol that describes a different signature than the one in code.
- Cross-reference to a spec doc by number that no longer exists or has been renumbered.

#### One-Level Constraints

Per `CLAUDE.md` §"One-level constraints stay".

- Two-level delegation (a delegate spawning its own delegates). Spec/15 refuses this.
- Skill referenced files nesting more than one level deep.
- Goal hierarchy growing past the one-level depth limit.

#### Concurrency, Atomicity, Error Handling

- Empty `except:` blocks around file ops, process kills, or storage mutations. Use the project's `_io` helpers or pass `errors=...` to the specific exception class.
- TOCTOU patterns: check-then-set that should be atomic. Use the `_locks` module.
- Signal/exception safety in cleanup paths — `finally` blocks must not raise.
- `time.sleep()` inside async functions — use `asyncio.sleep()`.

#### LLM Prompt Issues

- 0-indexed lists in prompts (LLMs reliably return 1-indexed).
- Prompt text listing tools/skills that don't match what's actually wired into `tool_classes` / loaded skills.
- Token / word limits stated in multiple places that could drift.

---

## Fix-First Heuristic

For each finding, classify as **AUTO-FIX** or **ASK**:

- **AUTO-FIX**: mechanical correctness fix where the right answer is unambiguous (test count update, missing `__all__` entry, deprecation-shim sunset date, missing `[Unreleased]` bullet for a clear change, missing fsync on a stateful write where atomic-write is the established pattern in the same module).
- **ASK**: anything requiring judgment about semantics (does this match the spec? is this the right Protocol shape? is the cost-gate placement correct for this new code path?), or fixes that would alter shipped behavior.

Critical findings lean toward **ASK** unless the fix is purely mechanical. Informational findings lean toward **AUTO-FIX** unless they require semantic judgment.
