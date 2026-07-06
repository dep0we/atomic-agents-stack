---
spec: 55
title: Fleet Management CLI — the "manage" layer
status: DRAFT
created: 2026-06-25
issue: 624
---

# spec/55 — Fleet Management CLI: the "manage" layer

**Status:** DRAFT — foundation + first verb (`govern`, #609) ship with issue #624 (epic #606); locked after the foundation conformance tests pass and the first verb is wired.

---

## Purpose

The Agent Fleet Platform grows in three layers on one shared foundation (epic #606): **observe → manage → interact**. The console (#614/#615/#616) is the OBSERVE layer — read-only; it shows what every agent is doing and what an operator *could* change ("swap Opus→Haiku, save $Y/mo"). This spec defines the MANAGE layer: the authorized, audited **write** path onto the same backend protocols the console reads. It closes the loop the console opens — the console recommends, the management CLI applies.

The management CLI is **not a new tool.** It is the missing middle of the single `atomic-agents` binary, which already spans most of an agent's lifecycle (`init` creates, `deploy` (spec/49) stands up and supervises, `serve` exposes, `doctor` checks). The management verbs let an operator *change a live agent's configuration* through that same surface, instead of hand-editing markdown files.

The pain it removes: today, changing an agent means opening its plain-text config files in a text editor — no validation (you can typo any value), no preview, no rollback, no record of who changed what. For a home user with one agent that is tolerable. For an operator tending a fleet it drifts and is unauditable. The management CLI is a validated, audited editor for those same files.

This spec covers:

- The **foundation** — the architecture every management verb conforms to (the spine, the safety routine, the copilot properties, the authorization posture, the markdown-config rule).
- The **first verb**, `manage govern <agent>` (#609) — a `governance.md` frontmatter editor written through `AgentRegistryBackend`. The reference verb that establishes the scaffold the rest inherit.

**Out of scope in this arc (deferred, tracked):**

- `manage set-model` — change an agent's model (`model.md`); highest console-loop value; forces Policy-composition + `_costs.PRICING`/caps checks. The next verb after `govern`.
- `manage apply-rec <id>` — apply a #616 console recommendation; re-validates the no-quality-cost guard *at apply time*. After the primitive verbs exist.
- `manage set-goal` (`goal.md` / GoalBackend #425, has its own history append) and lifecycle verbs (`pause`/`resume` — ambiguous in a stateless runtime; own design pass).
- An interactive guided "fleet manager" menu — a human-facing front-end layered ON TOP of the verbs, not built first (the verbs are what the copilot and the console drive).
- An in-framework operator-permission / role-based authorization layer — see "Authorization posture"; deferred (Principal PR 4).

---

## What the management CLI is and is NOT

The management CLI is a set of **verbs**, not a config layer, not a runtime, not a state store, and not an authorization system.

- It MUST NOT introduce a new config-file format or a new state store. An agent's identity and configuration stay in its markdown (CLAUDE.md rule 7). A management write is a *validated edit to an existing markdown file* (`governance.md`, `model.md`, `goal.md`), reconstructable by hand. The durable record of *what was changed, by whom, when* is the existing JSONL audit trail (principle #5), not a bespoke management-state file.
- It MUST NOT grant new authority. It is a safer path to edits the operator could already make by hand; it does not let anyone do something they could not already do with filesystem access (see "Authorization posture").
- It MUST NOT reimplement discovery. Every verb resolves its target agent through `AgentRegistryBackend` (#607) — the same directory the console and the future chat UI consume.
- It MUST respect (compose with) the agent-runtime gates where relevant. A management write that would set a value the runtime would reject MUST refuse at write time rather than write a config the agent cannot honor (e.g. a model forbidden by `policy.md`, or absent from `_costs.PRICING`). Composition, not bypass.

The boundary, stated plainly: **the management CLI owns making a validated, audited, reversible change to an agent's config; the operator (home user) or the perimeter (org) owns who is allowed to run it.**

---

## The spine (what keeps "manage everything" coherent)

The north star is a single CLI for the whole lifecycle of a fleet. The risk of "one tool does everything" is sprawl — seven tools in a trench coat that nobody can learn. The framework's aesthetic ("it feels like one thing") survives only with a strong spine. Four invariants form it; they are cheap to commit to now and expensive to retrofit, so they are foundational and conformance-tested.

### S1 — The registry is the single agent selector

Every management verb **resolves and loads** its target agent through `AgentRegistryBackend` (#607) — the same backend the console reads. This keeps discovery uniform across observe and manage, and means a verb works against the filesystem default today and a Postgres registry (#608) later without per-verb changes. A verb MUST NOT walk the filesystem directly to find or load an agent's config when the registry exposes that surface.

**The registry is a selector, not the config writer (decided — arc #624 fork `registry-write-seam`).** The registry resolves *which* agent and returns its location; the verb then writes the canonical config file (`governance.md`, etc.) via `_io.atomic_write` to that resolved location. The write does NOT flow through a registry-backend method, and the `AgentRegistryBackend` protocol is NOT extended with a governance-write method. Rationale: governance is **config**, not state (T15) — it is operator-authored, read-mostly, version-controlled, and has *no runtime consumer* (verified: nothing in `agent.call()`, the cost engine, the policy layer, or the mandate layer reads governance; only the registry/dashboard/`doctor` read it, all for display/aggregation). Per T15, config stays file-canonical in every deployment; routing config *writes* through the discovery backend (the surface that becomes Postgres at scale) is the "human-authored config shoved into Postgres" error T15 explicitly warns against. A future Postgres registry MAY *index/cache* governance for fast fleet reads, but the file remains the thing an operator edits, and reconstruction across stores is the spec/40 export contract's job — not a write seam on the discovery protocol.

### S2 — One five-step safety routine for every verb

Every write verb performs exactly these steps, in order. An operator (or a copilot) learns the shape once and it holds for every verb:

1. **Resolve + validate.** Resolve the target via the registry (S1). Validate the proposed change against the relevant schema (enum/field/format checks) AND against composition gates (e.g. PRICING/caps, `policy.md`). Refuse invalid input before anything is written — no silently-wrong config.
2. **Preview.** Render the before→after of the affected markdown as a readable artifact. Nothing is written. (`--dry-run` stops here; see C3.)
3. **Confirm.** Require explicit confirmation: an interactive y/n on a TTY, or `--yes` non-interactively. The default is NOT to apply.
4. **Snapshot + atomic write.** Snapshot the file for instant rollback, then write through `_io.atomic_write` (temp + fsync + rename), preserving the markdown body (only the targeted fields change). No half-written config (principle #8).
5. **Audit.** Append a management event to the JSONL audit trail through `LogBackend` carrying *who* (principal_id), *when*, *which agent*, *which field*, and *before→after*. A management change is a stewardship event and MUST be recorded (principle #5).

### S3 — Copilot-first

The primary intended driver is an AI copilot, not a human memorizing commands. Three properties, baked into the scaffold from the first verb, make every verb copilot-drivable:

- **No required interactive prompts.** Everything a verb needs MUST be expressible as flags. A verb MUST NOT *require* a human at a TTY to complete (an interactive confirm is allowed only as the TTY branch of S2 step 3, always satisfiable by `--yes`).
- **Structured output (`--json`).** Every verb MUST support `--json` emitting machine-readable output (the preview diff, the result, and on refusal the structured reason) so a copilot can read current state, read a recommendation, and read the outcome. Same convention as `doctor --json`.
- **Preview-as-artifact (`--dry-run`).** The preview (S2 step 2) MUST be obtainable without applying, so a copilot can run the preview, show the operator the diff, and apply only after approval.

`--yes` is the "the operator already approved" signal. It is NEVER the default. This makes the human and copilot flows the *same mechanism*: a human reads the preview and types `y`; a copilot runs `--dry-run`, shows the diff, the operator says "go", and the copilot re-runs with `--yes`. A copilot can therefore never silently change state, but can apply once approved.

### S4 — The `manage` command group

Write verbs live under a single `manage` group: `atomic-agents manage <verb> <agent> [options]`. Grouping every state-changing verb under one word makes the observe/manage (read/write) line visible in the CLI surface itself, and fits the existing group-then-verb idiom (`persona`, `corpus`, `mcp-registry`, `secrets`, `deploy`). Read-only inspection stays where it is (`info`, `doctor`, `corpus list`, etc.); `manage` is for changes.

---

## Lifecycle map (where `manage` sits)

```
 discover   →   create   →   deploy   →   manage   →   observe        →   retire
 (registry)     init         deploy       (this spec)  doctor + console     deploy down
 #607           shipped      spec/49      spec/55      shipped              spec/49
```

The management CLI does not rebuild any neighbor. `deploy` (spec/49) already owns standing an agent up and tearing it down; `manage` owns changing its config while it exists. Both sit on the same spine (S1–S4) so the experience is consistent across the lifecycle. A later console/`manage` surface MAY display `deploy status` inline; this spec does not require it.

---

## Front-ends — one engine, terminal-primary

The management verbs are one **engine**, driven by several **front-ends**. The engine is: the backend protocols (read), the management verbs (write, this spec), and `agent.call()` (interact). Every human-facing surface is a *thin presentation* over that engine — it renders protocol reads and invokes verbs; it holds **no business logic of its own**. This is what lets the framework grow multiple surfaces and still "feel like one thing" (the aesthetic): each new surface is a rendering, not a re-implementation, and each inherits the S2 safety routine + S3 copilot properties for free.

**The surfaces, positioned (decided — operator ruling, 2026-07-05):**

| Capability | Terminal habitat | Web habitat |
|---|---|---|
| Observe | TUI dashboard | Web console (spec/52–54) |
| Manage | CLI verbs (this spec) + TUI actions | CLI verbs / copilot |
| Interact | TUI chat pane | web chat (later) |

- **The terminal (TUI + CLI) is the primary deployment and management driver.** This is the surface an operator uses to deploy (`deploy`, spec/49) and manage (this spec) a fleet — keyboard-driven, SSH-friendly, copilot-adjacent.
- **The web console (spec/52–54) is the visual monitoring layer** — the shareable, remote, graphical *observe* surface. It reads the same protocols; it is not the primary act-on-the-fleet surface.
- **The CLI and a copilot are complementary manage drivers, not competitors:** the CLI verbs are the scriptable, structured (`--json`), preview-then-`--yes` surface; a copilot drives those same verbs from natural-language intent (S3). The TUI is the interactive, live cockpit that folds observe + manage (+ later interact) into one keyboard-driven surface.

**The TUI is a named future front-end, sequenced after the verbs.** It is *not* specified here and adds *no* engine concept: every TUI view is a protocol read, every TUI action is a verb run through the same preview→confirm→audit path (S2), and any chat pane is `agent.call()`. Because the verbs it drives must exist first, the TUI is built after the first management verbs land; it earns its own spec at that time. The design consequence for *this* spec is only that the verb layer must stay clean and fully flag-drivable with structured output (S3) so the TUI (like the copilot) can drive it without special-casing. The TUI ships as an optional extra (`atomic-agents-stack[tui]`), lazy-loaded so the base CLI stays lean (principle #6), mirroring the `[serve]` pattern. Tracked as a future front-end under epic #606.

---

## Authorization posture

The management CLI does NOT decide who is allowed to make a write. **Access is authority:**

- **Home user** — whoever can run the CLI already owns the agent folder. A management write grants no authority beyond what `vim model.md` already grants; the CLI is the safer path.
- **Org / cloud** — the perimeter (who can invoke the CLI or reach a future management endpoint) is the gate, exactly as `serve` (spec/37) delegates request auth to the perimeter.

Principal (spec/48) supplies the **audit identity** for S2 step 5 (the `principal_id` stamped on the management event), NOT a gate. In the home-user case that is `LOCAL_PRINCIPAL`. The HARD-REFUSE gate of spec/48 is conversation-scoped and does not apply to management writes.

Mandate (spec/29) and Policy (spec/32) gate the **agent's runtime behavior**, not the operator's management actions. They are *composed with* (a management write refuses a value the agent's policy would reject — see "is and is NOT") but they are NOT the operator-write gate, and the management CLI MUST NOT repurpose them as one.

In-framework, role-based operator authorization ("this operator may pause but not change models") is explicitly deferred — it is the deferred Principal PR 4 (role-based authz). The spine leaves room for it: if added later, it slots in as a gate before S2 step 4 without changing any verb's shape.

---

## First verb — `manage govern <agent>` (#609)

A frontmatter editor for an agent's `governance.md`, written through `AgentRegistryBackend`. It is the reference verb: it establishes the S2 safety routine and the S3 copilot properties that every later verb inherits.

### Surface

```
atomic-agents manage govern <agent> --set <field>=<value> [--set ...] [options]
atomic-agents manage govern <agent> --show
```

- `--set <field>=<value>` — set a governance field (repeatable). Field names map to the spec/51 `GovernanceRecord` schema (`owner`, `permission-tier`, `customer-data`, `writes-sor`, `lifecycle-status`, and the review/risk sub-fields as the schema allows). Hyphenated CLI field names map to the underscore schema keys.
- `--show` — print the current resolved governance record (read-only convenience; respects `--json`).
- `--dry-run` — preview only (S2 step 2), no write.
- `--yes` — apply without the interactive confirm (S2 step 3).
- `--json` — structured output (S3).
- `--agents-root` / agent resolution consistent with the rest of the CLI; the target is loaded through the registry (S1).

### Behavior (the S2 routine, concretely)

1. **Resolve + validate.** Load the target through `AgentRegistryBackend`. Validate every `--set` field name against the `GovernanceRecord` schema and every value against its enum/format (`permission-tier`, `customer-data`/`writes-sor` tri-states, date fields). Refuse unknown fields and invalid enums with a clear, named error before writing.
2. **Preview.** Show the before→after of `governance.md`'s frontmatter block (the prose body is preserved verbatim).
3. **Confirm.** Interactive y/n on a TTY, or `--yes`.
4. **Snapshot + atomic write.** Snapshot the existing `governance.md` to the dedicated config-snapshot location (M3), then write the updated block through `_io.atomic_write` per the M2 surgical preservation contract. **Creating `governance.md` where absent** (a fleet operator authoring governance for the first time) is allowed and MUST render the **init governance.md template** so there is one canonical governance.md shape in the wild (decided — arc #624 fork `create-absent-governance`), then apply the `--set` values on top. The canonical-shape invariant is enforced by a **byte-identity lint test** across the template copies: `govern` renders the top-level copy through the shared `render_governance_stub` renderer, and `init`/wizard renders its per-type copy; the lint test asserts they stay byte-identical, so the "one shape" guarantee holds even though the two sites do not literally share a single call. The write still goes through validation + audit, and MUST NOT clobber an existing file (consistent with init's preserve-on-re-run contract, spec/51).
5. **Audit.** Append a `PRIMITIVE_MANAGE_GOVERN` `RunRecord` event (principal_id, ts, agent, changed_fields, before→after) through `LogBackend` to the per-agent and fleet scopes (M8) — two copies when they are physically distinct stores (the Filesystem default), collapsing to a single append when both scopes resolve to one shared store (a URL-backed distributed backend) to avoid a duplicate `run_id` row that would double-count fleet aggregations.

### Why governance first

`governance.md` holds labels and metadata (owner, risk tier, review date, lifecycle status). Setting them changes nothing about how the agent *behaves* or what it *costs* — so it is the safest possible place to build and prove the write scaffold. The schema already exists (spec/51 `GovernanceRecord`), `init` already writes a stub, and the AgentRegistryBackend already reads it. The verb adds the safe write path.

---

## CLI surface grammar (normative — decided, arc #624 fork `cli-surface-flags-naming`)

Every name/format here is a public operator surface (a compatibility contract) and a copilot-prompt surface, so the **full grammar is pinned now** even though PR1 implements only the flat-scalar slice. Deferring implementation of low-traffic fields is fine; deferring the *contract* is the trap.

- **Group + verb:** `atomic-agents manage govern <agent> ...` (S4).
- **Scalar fields:** `--set <field>=<value>`, repeatable. CLI field names are hyphenated; they map to the underscore `GovernanceRecord` schema keys (`permission-tier` → `permission_tier`). Split on the first `=` (values may contain `=`).
- **Nested fields:** addressed with **dotted paths** (`--set review.reviewer=...`), NOT hyphen-joined — the dot disambiguates a nested path from a hyphenated scalar name.
- **List fields** (`sources.*`, `actions.*`): mutated with `--add <path>=<item>` / `--remove <path>=<item>` for element-level changes and `--set-json <path>=<json-array>` for full-list replacement. Comma-separated values are explicitly NOT used (they break on items containing commas).
- **Fleet-scoped flag:** `--agents-root` (matching the existing `init`/registry convention; the registry is agents_root/fleet-scoped — NOT the per-agent `--agent-root`).
- **Read/safety flags:** `--show` (print current resolved record), `--dry-run` (preview only, S2 step 2), `--yes` (apply without TTY confirm, S2 step 3), `--json` (structured output, S3).
- **`--json` output** exposes canonical **underscore** schema keys (not CLI hyphen spellings), so a copilot reads the schema, not the typing convention.
- **PR1 scope:** flat scalar `--set` only. `--add`/`--remove`/`--set-json` and dotted nested paths are reserved-and-documented; an unimplemented path returns a clean `"not yet settable via CLI; edit governance.md directly"` refusal — never a parser error whose meaning shifts in a later PR.

## Implementation notes (Tier B — agent decides at build, with justification)

- **Code location:** a new `atomic_agents/manage/` package holding the reusable S2 safety-routine helper + per-verb modules, with thin dispatch in `cli.py` — matching the `secrets`/`mcp_registry` packaging idiom. The five-step routine MUST be a genuine shared helper, not copy-paste per verb.
- **Error taxonomy:** a typed exception ladder — validation guards (unknown-field / invalid-enum / invalid-date / control-char / nested-path / list-mutation / governance-invalid) each with a distinct message + structured `--json` reason, so the negative-control tests have a distinct, strip-testable failure per guard. Two ladder members named in the arc are general manage-layer taxonomy the `govern` verb does NOT implement as raised exceptions: **composition-refused** arrives only with the first runtime-effective verb (set-model, per M9 — govern's composition set is empty by design), and **audit-dropped** is deliberately NOT an exception — it is surfaced as a non-fatal `(ok, warnings)` tuple return of the shared audit helper (a warn that still exits 0 on an applied write, per M8), not a raised error. Validation/composition failures → non-zero exit, no write; audit-drop → exit 0 with a `warn` audit status.

## Implementer Contract (MUSTs)

**M1 — No new config format or state store.** A management verb MUST write only to existing agent markdown files in their existing formats, and MUST record management events only through `LogBackend`. It MUST NOT introduce a management-state sidecar file.

**M2 — Preservation contract (decided — arc #624 fork `governance-yaml-block-format`).** governance.md is a fenced ` ```yaml ` block (root key `governance:`) surrounded by operator-authored prose sections, and the block carries instructional inline comments operators rely on. A `--set` write MUST be **surgical**, satisfying this normative contract:
- (a) the prose body outside the yaml block survives **byte-for-byte**;
- (b) untargeted keys inside the block — including their inline comments and authored key order — survive **byte-for-byte**;
- (c) only the targeted key's scalar **value** is rewritten in place.

**`updated_at` auto-stamp (named exception to (c)).** On every applied write, the verb ALSO stamps `updated_at` to today's date (ISO-8601) unless the operator set `updated_at` explicitly in the same invocation. This is the single implicit field change permitted beyond the operator-targeted keys; it is a genuine write (it appears in the preview and in the audit before→after), and on a file that omits `updated_at` it is INSERTED as a new top-level scalar under `governance:` (consistent with the create-absent / partial-file fill behavior). It is named here so it is never a silent surprise and so later verbs inherit a documented "freshness stamp" contract.

The implementation MUST be a line-aware in-block value editor. PyYAML is the **reader/validator only** (enum/format validation per M4) and MUST NEVER be the writer — a parse→`safe_dump` round-trip is forbidden because it strips comments, reorders keys, and is non-idempotent against a hand-authored file. No comment-preserving YAML dependency (e.g. ruamel) is added; the codebase's existing line-aware config-edit idiom (`_model.py`, `_roster.py`) is the model. If a `--set` ever targets a **list** field (`sources.*`, `actions.*`), full re-emission of that one list is the single documented exception (spec/55 names it so it is never a silent surprise) — PR1 does not implement list mutation (see CLI grammar).

**M3 — Atomic write + restorable snapshot (decided — arc #624 fork `snapshot-mechanism`).** Every write MUST go through `_io.atomic_write` (a crash mid-write MUST NOT leave a half-written config, principle #8) and MUST first snapshot the prior **file** content to a **dedicated config-snapshot location** so the change is rollback-able to byte-faithful prior content. The snapshot MUST NOT reuse memory's `.versions/` machinery (that surface is memory-scoped per spec/02/spec/20; governance is config, not a memory note), and the JSONL audit line MUST NOT be treated as the rollback source (an audit record is not restorable file content, and recoverability MUST NOT depend on the observability stream being readable — cf. #497/#498 degraded-read posture). The snapshot is taken BEFORE overwrite; the audit (M8) is appended AFTER, so rollback never depends on the audit having been written.

**M4 — Validate before write.** A verb MUST validate field names AND values (schema + enums + formats) AND applicable composition gates BEFORE step 2 (preview). Invalid input MUST be refused with a clear, named error and a non-zero exit, and MUST NOT write or partially write.

**M5 — Confirm-by-default; `--yes` to apply.** A verb MUST NOT apply a change without either an interactive confirmation (TTY) or `--yes`. `--yes` MUST NOT be the default. `--dry-run` MUST stop after preview and never write.

**M6 — Copilot properties.** Every verb MUST support `--json` (structured output incl. the structured refusal reason) and MUST be fully drivable with flags (no required interactive prompt). The only interactive element permitted is the TTY confirm of M5, which `--yes` always satisfies.

**M7 — Registry-resolved target; verb-side write.** A verb MUST resolve and load its target agent through `AgentRegistryBackend`, not by direct filesystem walking, so it works against any registered registry backend. The **write** then goes to the registry-resolved location via `_io.atomic_write` (M3); the `AgentRegistryBackend` protocol stays read-only discovery and is NOT extended with a governance-write method (see S1; arc #624 fork `registry-write-seam`).

**M8 — Audited with identity, on the existing audit shape (decided — arc #624 fork `audit-record-shape`).** Every applied write MUST append a management event through `LogBackend` as a `RunRecord` — NOT a new event dataclass (which would require widening the LOCKED `LogBackend.append()` signature) — following the established non-LLM-event precedent (`policy_decision`, `mandate_reservation`). The record uses a dedicated primitive `PRIMITIVE_MANAGE_GOVERN = "manage_govern"`, `model="n/a"`, `input_tokens=0`/`output_tokens=0`, `status="applied"`, and carries `principal_id` (from the resolved Principal — `LOCAL_PRINCIPAL` for the home user), `changed_fields`, `before`, and `after` in `extra{}`, plus `snapshot_path` (the restorable pre-write snapshot, relative to the agent folder; `null` on a create-absent write) and `created` (`true` when the write CREATED the file, so there is no restorable prior state). `snapshot_path` is a generic per-verb key every snapshotting verb emits; `created` is govern-verb-specific (it records that governance.md did not previously exist). The status vocabulary is pinned NORMATIVELY: an applied management write emits `status="applied"`; refusals and dry-runs do NOT emit a management RunRecord at all (they are refused before the audit step, so there is no `status="refused"`/`"dry_run"` value — a management RunRecord existing implies the write was applied). These `extra{}` key names + the primitive id + the `status="applied"` value are pinned NORMATIVELY here so every later verb (set-model, set-goal, apply-rec) emits the same shape.

**Backend-aware dual-scope append (decided — arc #624 fork `logbackend-scope`; refined at convergence).** The event is appended to the target agent's per-agent `LogBackend` (`get_default_log_backend(agent_dir)`) AND a fleet-level management `LogBackend` at `agents_root/_manage` — but the two-copy shape is correct ONLY when the two scopes resolve to physically **distinct** stores. Under the default Filesystem backend they are two separate `log/` dirs (and SQLite-no-URL is two separate db files), so BOTH scopes are appended: two append-only copies of one immutable event — the home user sees it in the agent's own log with zero extra machinery, and the fleet stream survives the agent's deletion. Under a **URL-backed distributed `LogBackend`** (Postgres, or SQLite with a shared `ATOMIC_AGENTS_LOG_BACKEND_URL`) `scope_root` is ignored and BOTH scopes resolve to the **same central table**; there the event is appended **exactly once** and that single central-table row IS the fleet stream (it already survives agent-folder deletion and is queryable by `primitive=manage_govern`). A second append under a shared store would be a **duplicate row with an identical `run_id`** and would double-count every fleet `COUNT` / `GROUP BY agent` / cost aggregation (audit integrity is structural, principle #5), so it MUST NOT be written. A conforming implementation MUST therefore collapse to a single append when the two scopes share one physical store. If the backend's store identity cannot be determined (an unrecognised custom backend), the implementation appends once (no duplicate-row risk) and MUST surface a non-fatal warning so the possibly-skipped fleet copy is observable, not silent. A dropped audit write MUST surface a non-fatal warning, never fail silently, and (per M3) MUST NOT compromise rollback — this includes a **construction-time** failure of a swapped/misconfigured backend, not only an `append()`-time failure. Before/after values are operator-authored config (not new secrets), kept inline; the verb MUST redact known-secret-shaped fields at the echo site (the project's redact-at-echo rule) and cap string length the way `summary` is capped.

**M9 — Compose, never bypass (per-field scoped; decided — arc #624 fork `composition-gates-for-govern`).** A verb that sets a value the agent runtime would reject MUST refuse at write time (e.g. a model not in `_costs.PRICING`/caps, or disallowed by `policy.md`). A management write MUST NOT produce a config the agent cannot honor. **For `govern` specifically, the composition set is EMPTY by design:** every governance field is descriptive metadata with no runtime-rejection counterpart (verified — no runtime gate reads governance), so S2 step 1's composition check resolves to enum/format validation (M4) with zero additional gates. This is a documented intentional empty set, NOT an unimplemented step. The composition set becomes non-empty for runtime-effective verbs (set-model composes with PRICING/caps/policy); if a future GovernanceRecord field ever becomes runtime-enforced, its non-empty composition set is recorded in this per-field scoping.

**M10 — No authority escalation.** A verb MUST NOT perform any action the operator could not already perform with direct write access to the agent folder. The CLI is a validated path to those edits, not a privilege grant.

---

## Conformance test outline

- **Spine:** target resolution goes through a stub `AgentRegistryBackend` (S1/M7); a verb run against a backend that does not expose the agent fails cleanly.
- **Safety routine:** `--dry-run` writes nothing (M5); a non-`--yes` non-TTY run does not apply (M5); a confirmed run writes atomically and preserves the body (M2/M3); a mid-write crash leaves the original intact (M3); snapshot-then-rollback restores prior content (M3).
- **Validation:** unknown field, invalid enum, malformed date each refuse before write with a non-zero exit and write nothing (M4); a value rejected by a composition gate (PRICING/caps or `policy.md`) refuses (M9).
- **Copilot:** `--json` emits structured success and structured refusal (M6); every path is reachable without a TTY prompt given `--yes` (M6).
- **Audit:** an applied write appends exactly one management event with principal_id, before→after, changed fields (M8); a `LogBackend` append failure warns but does not fail the (already-applied) write (M8).
- **govern verb:** `--set` round-trips through `GovernanceRecord`; hyphenated CLI names map to underscore schema keys; creating `governance.md` where absent works and is audited; the prose body survives an update.
- **Negative controls:** strip each independent guard (validation, confirm, snapshot, audit, composition) and assert a distinct failure — no false-green (per the project's per-invocation negative-control discipline).

---

## Cross-references

- **Epic #606** — Agent Fleet Platform (observe → manage → interact); #624 (this foundation), #609 (the `govern` verb).
- **spec/51** AgentRegistryBackend (#607) — the agent selector (S1) + the `GovernanceRecord` schema the first verb writes.
- **spec/48** PrincipalBackend (#556) — the audit identity stamped on management events (not a gate).
- **spec/49** `atomic-agents deploy` — the adjacent lifecycle verb under the same spine.
- **spec/29** Mandate / **spec/32** Policy — gate the agent's runtime; composed-with (M9), not the operator-write gate.
- **spec/22** LogBackend — the management-event audit stream (M8).
- **CLAUDE.md** rule 7 (markdown config), rule 5 (audit is structural), rule 8 (atomic + idempotent), rule 14 (backward compatibility — `manage` is additive; existing CLI unchanged).
