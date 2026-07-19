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
- The **second verb**, `manage set-model <agent>` (#726) — a `model.md` model-swap editor; the first verb with a non-empty M9 composition set and the first surgical `model.md` field-writer. Specified below ("Second verb"); implemented in #726.

**Out of scope in this arc (deferred, tracked):**

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
atomic-agents manage govern <agent> --list-snapshots
atomic-agents manage govern <agent> --restore <snapshot-id> [options]
```

- `--set <field>=<value>` — set a governance field (repeatable). Field names map to the spec/51 `GovernanceRecord` schema (`owner`, `permission-tier`, `customer-data`, `writes-sor`, `lifecycle-status`, and the review/risk sub-fields as the schema allows). Hyphenated CLI field names map to the underscore schema keys.
- `--show` — print the current resolved governance record (read-only convenience; respects `--json`).
- `--list-snapshots` — print the available snapshot ids for `--restore`, oldest first (read-only convenience, symmetric with `--show`; #710).
- `--restore <snapshot-id>` — roll back `governance.md` to a prior snapshot taken by this verb (#710). Runs the FULL S2 five-step routine, not a bypass — restore itself takes a pre-restore snapshot of the state it is about to overwrite, so a restore is always undoable via a second `--restore`. **Mutually exclusive with `--set`** (and, by the same single-primary-action rule, with `--show`/`--list-snapshots`) — exactly one primary action per invocation, refused cleanly if more than one is given.
- `--dry-run` — preview only (S2 step 2), no write.
- `--yes` — apply without the interactive confirm (S2 step 3).
- `--json` — structured output (S3).
- `--agents-root` / agent resolution consistent with the rest of the CLI; the target is loaded through the registry (S1).

### Behavior (the S2 routine, concretely)

1. **Resolve + validate.** Load the target through `AgentRegistryBackend`. Validate every `--set` field name against the `GovernanceRecord` schema and every value against its enum/format (`permission-tier`, `customer-data`/`writes-sor` tri-states, date fields). Refuse unknown fields and invalid enums with a clear, named error before writing.
2. **Preview.** Show the before→after of `governance.md`'s frontmatter block (the prose body is preserved verbatim).
3. **Confirm.** Interactive y/n on a TTY, or `--yes`. The manage lease (M11) is NOT held across this step — see "Exit codes" below.
4. **Snapshot + atomic write.** Acquire the per-agent manage lease (M11), re-read `governance.md` FRESH from disk (not the step-2 preview read — see M11's lost-update note), snapshot that fresh content to the dedicated config-snapshot location (M3), then write the updated block through `_io.atomic_write` per the M2 surgical preservation contract, and release the lease. **Creating `governance.md` where absent** (a fleet operator authoring governance for the first time) is allowed and MUST render the **init governance.md template** so there is one canonical governance.md shape in the wild (decided — arc #624 fork `create-absent-governance`), then apply the `--set` values on top. The canonical-shape invariant is enforced by a **byte-identity lint test** across the template copies: `govern` renders the top-level copy through the shared `render_governance_stub` renderer, and `init`/wizard renders its per-type copy; the lint test asserts they stay byte-identical, so the "one shape" guarantee holds even though the two sites do not literally share a single call. The write still goes through validation + audit, and MUST NOT clobber an existing file (consistent with init's preserve-on-re-run contract, spec/51).
5. **Audit.** Append a `PRIMITIVE_MANAGE_GOVERN` `RunRecord` event (principal_id, ts, agent, changed_fields, before→after) through `LogBackend` to the per-agent and fleet scopes (M8) — two copies when they are physically distinct stores (the Filesystem default), collapsing to a single append when both scopes resolve to one shared store (a URL-backed distributed backend) to avoid a duplicate `run_id` row that would double-count fleet aggregations. Runs AFTER the manage lease is released (M11).

### Restore verb — `--restore <snapshot-id>` (#710)

`--restore` is a `govern` sub-action, not a new verb group — it runs the SAME hoisted S2 spine every `--set` write uses, through the same `atomic_agents/manage/_routine.py` helper (#709 hoist). Concretely:

- **Validate (step 1).** Restore's validation is INTENTIONALLY narrower than `--set`'s: the target snapshot's governance YAML block MUST parse, and the snapshot MUST resolve under the TARGET agent's OWN `.config-snapshots/govern/` tree — a snapshot-id that only exists under a DIFFERENT agent's tree does not resolve here (no cross-agent restore). This is NOT a full re-validation against the current schema — a snapshot is by definition prior known-good content that already passed validation once.
- **Preview (step 2).** Shows the field-level diff between the CURRENT `governance.md` and the snapshot being restored (the same per-field `changed_fields`/`before`/`after` shape `--set` uses — M8).
- **Snapshot + write (step 4).** The restore itself snapshots the CURRENT (about-to-be-overwritten) `governance.md` before writing the restored content — a restore is always itself undoable via a second `--restore`. Restore is a byte-exact rollback: it does NOT re-stamp `updated_at` the way `--set` does (the resulting file is byte-identical to the snapshotted state). When `governance.md` is ABSENT at restore time (no prior state exists to snapshot), the restore takes NO pre-restore snapshot — this is the documented exemption to "restore snapshots current state before overwriting"; that particular restore is undone by deleting the restored file, not by a second `--restore`.
- **Audit (step 5).** Emits exactly ONE `PRIMITIVE_MANAGE_RESTORE = "manage_restore"` `RunRecord` — never an additional `manage_govern` record for the same operation. `extra{}` carries the same `principal_id`/`changed_fields`/`before`/`after` shape as `manage_govern`, PLUS two distinct snapshot references: `restored_from` (the SOURCE snapshot id the operator asked to restore FROM) and `snapshot_path` (the NEW pre-restore snapshot this restore itself just took, of the state it overwrote).

### Exit codes (normative)

Every write verb (govern `--set`, govern `--restore`, and future verbs sharing the S2 spine) returns from this ladder:

| Exit | Meaning |
|---|---|
| `0` | Applied successfully, `--dry-run` preview, or a read-only `--show`/`--list-snapshots`. |
| `1` | Refused (validation, registry, path-containment, `lock_backend_unavailable`, `agent_busy` — see M11) or an unexpected read/write error. |
| `3` | Interactive confirm declined (`'n'` or EOF at the TTY prompt). `error_type` stays `"aborted"`. Deliberately NOT `2` — argparse reserves exit `2` for its own usage errors, so a copilot driver can always tell "bad flags" apart from "operator declined". |
| `130` | `KeyboardInterrupt` (POSIX SIGINT, `128 + 2`). Distinct from `3` — the operator never answered; the process was interrupted. `error_type` stays `"aborted"`. |

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

---

## Second verb — `manage set-model <agent>` (#726)

The model-swap editor for an agent's `model.md`, written through the same hoisted S2 spine `govern` established (`atomic_agents/manage/_routine.py`, #709/#710). It is the first verb whose M9 composition set is non-empty by design — `govern`'s is empty because governance is descriptive metadata with no runtime-rejection counterpart; a model choice is the opposite case, the single field most directly wired to what the runtime actually spends and whether it runs at all. It is also the primitive the future `manage apply-rec <id>` (#727) delegates to: applying a console `savings_cost` recommendation (spec/54) is, mechanically, a `set-model` write with the recommended model id as `--model`, so `apply-rec` MUST NOT reimplement the write path — it resolves a model id and calls this verb's routine.

`set-model` inherits the full spine for free, unchanged: the per-agent manage lease (M11), the snapshot + `--restore <snapshot-id>` / `--list-snapshots` rollback pair (M3/#710), confirm-by-default + `--dry-run` (M5), the copilot flags (M6), and the exit-code ladder (`0` applied/preview/read-only, `1` refused-or-error, `3` declined, `130` SIGINT). None of that is re-specified here; see "First verb" above for the normative text.

### Surface

```
atomic-agents manage set-model <agent> --model <id> [options]
atomic-agents manage set-model <agent> --show
atomic-agents manage set-model <agent> --list-snapshots
atomic-agents manage set-model <agent> --restore <snapshot-id> [options]
```

- `--model <id>` — the new `## Default model` value. MUST pass the full M9 composition chain (below) before anything is written. In PR1 scope regardless of how the open forks below resolve — this is the verb's reason to exist.
- `--fallback <id>` — the new `## Fallback` value, subject to the SAME M9 composition chain as `--model`. **PR1 scope is an open fork** (see "Open forks" below); the flag name and semantics are pinned here either way, matching the CLI-surface-grammar precedent set for `govern` (pin the contract now, defer low-traffic implementation).
- `--provider <id>` — disambiguates `find_backend_for_model` when a requested model id is claimed by more than one registered `LLMBackend`, writing (or updating) the optional `provider:` line that lives OUTSIDE the `cost_guardrails` yaml block (`atomic_agents/_model.py:105-106`). **Whether this flag is required, optional, or PR1-scoped at all is an open fork** (see below).
- `--show` — print the current resolved model config (`default_model`, `fallback_model`, `provider`) as parsed by `parse_model_md`; read-only, respects `--json`. Same convention as `govern --show`.
- `--list-snapshots` — print available `set-model` snapshot ids, oldest first (M3, namespaced `set-model` so they never collide with `govern`'s `.config-snapshots/govern/` tree).
- `--restore <snapshot-id>` — roll back `model.md` to a prior `set-model` snapshot, through the SAME hoisted spine `govern --restore` uses (M3 sub-MUSTs (a)/(b)): resolves only under the target agent's own `.config-snapshots/set-model/` tree, itself takes a pre-restore snapshot, emits exactly one `PRIMITIVE_MANAGE_RESTORE` record. **Restore does NOT re-run the M9 composition chain** — identically to `govern`'s restore (§"Restore verb"), a snapshot is by definition prior content that already passed composition once; re-validating a historical write against TODAY's `_costs.PRICING`/registered backends/`policy.md` would make old snapshots unrestorable the moment a model is deprecated, defeating the point of a rollback path.
- `--dry-run`, `--yes`, `--json`, `--agents-root` — identical semantics to `govern`'s (S2/S3), unchanged.

### Behavior (the S2 routine, concretely)

1. **Resolve + validate.** Load the target through `AgentRegistryBackend` (S1). Unlike `governance.md`, `model.md` is rendered by every `init` template (`atomic_agents/init/templates/*/model.md`) — its absence is not the ordinary "first time an operator configures this" case `govern`'s create-absent path exists for. If `model.md` is absent at resolve time, `set-model` MUST refuse (S2 step 1) rather than render a stub; it does not carry `govern`'s create-absent allowance. Then run the full M9 composition chain against every requested value (`--model`, and `--fallback` if in scope) before anything is previewed — refuse before step 2 on the first failing check, exactly as M4 requires.
2. **Preview.** Show the before→after of ONLY the targeted heading value line(s) (`## Default model`, and `## Fallback` if in scope) — the `cost_guardrails` block, the `provider:` line (unless `--provider` targets it), and every prose paragraph render as unchanged.
3. **Confirm.** Interactive y/n on a TTY, or `--yes`. Identical to `govern` — the manage lease is NOT held across this step (M11).
4. **Snapshot + atomic write.** Acquire the per-agent manage lease (M11), re-read `model.md` FRESH from disk (the lost-update guarantee — never the step-2 preview read), snapshot that fresh content under `.config-snapshots/set-model/` (M3), then perform the surgical write per the "model.md preservation contract" below through `_io.atomic_write`, and release the lease.
5. **Audit.** Append a `PRIMITIVE_MANAGE_SET_MODEL` `RunRecord` (see "Audit primitive" below) through `LogBackend`, dual-scope per M8's backend-aware rule, AFTER the lease is released.

### Composing with the runtime — M9 made concrete

`govern`'s M9 composition set is empty by design (no governance field has a runtime-rejection counterpart). `set-model` is the first verb where M9 does real work: a `--model` (or in-scope `--fallback`) value MUST be refused at write time — before preview, per M4 — unless ALL of the following hold. Each check reuses an existing pure function; `set-model` MUST NOT reimplement pricing, backend resolution, or policy evaluation:

- **(a) Priced.** The value MUST be a key in `atomic_agents._costs.PRICING`. An unpriced model is not a cosmetic gap — a scaffolded agent whose default model falls outside `PRICING` bills through fallback pricing, a real, silent over-bill hazard (the same hazard the project's "bump a model" memory lesson names for registering new models). Checked via `model_id in atomic_agents._costs.PRICING`.
- **(b) Resolvable to exactly one backend.** The value MUST resolve to exactly one registered `LLMBackend` via `atomic_agents.llm.find_backend_for_model(model_id, preferred_provider=<--provider or None>)`. Zero matches raises `UnknownModelError` (refuse: "not a known model id"); more than one match with no `--provider` given raises `AmbiguousBackendError` (refuse: "ambiguous, pass --provider") — this is the exact seam the `--provider` disambiguation flag exists for (see "Open forks" below for whether `set-model` requires it up front or only offers it on the ambiguous-refusal path).
- **(c) Policy-compatible (caps compose is settled; the model-override interaction is an open fork).** The write MUST compose with the agent's effective Policy, not bypass it. `atomic_agents.policy.backend.PolicyBackend.get_effective_caps(agent_name)` MUST be consulted so a model swap composes with whatever `CostCaps` the fleet or agent-level policy has already pinned — a model whose spend profile those caps forbid is a compose failure. Separately, `PolicyBackend.get_effective_model(agent_name)` returns Policy's per-agent model override (or `None`); if Policy's value wins over `model.md`'s at the `agent.call()` consumption site, a `--model` that differs from a non-`None` override would write a `model.md` value the runtime will not honor. WHETHER that is a hard refusal (M9 — don't write a config the agent cannot honor) or an allowed base-layer edit (model.md is the base; Policy is a deliberate override layer) is an **open fork** (see below); the build MUST verify the actual precedence + override semantics in `policy/backend.py` before pinning it.

**Alignment note (#716).** `set-model` composes `find_backend_for_model` for check (b) today. When #716 (typed LLM errors + a doctor model-resolution check) lands, `set-model`'s "backend resolves" check unifies with the doctor-shared resolution path — a reuse tidy-up, not a behavior change; this section's normative chain does not move.

### model.md preservation contract

`model.md` is harder to write surgically than `governance.md` was for `govern`'s M2. `governance.md` has one syntactic region (a single fenced `yaml` block). `model.md` has TWO: the `## Default model` / `## Fallback` heading values, which live in free-form PROSE (not YAML) with operator-chosen markup wrapping the value — `` **`id`** ``, `**id**`, `` `id` ``, or bare, per `_model.py`'s reader regex (`atomic_agents/_model.py:79-91`) — and the fenced `cost_guardrails` yaml block, which `set-model` does not target at all. There is also the optional `provider:` line, which lives OUTSIDE the yaml block (`_model.py:104-111`) specifically so an operator's nested `provider:` key inside `cost_guardrails` is never confused with it.

Normatively, a `set-model` write MUST be surgical and byte-faithful, mirroring M2's discipline for a second syntactic shape:

- (a) the `cost_guardrails` yaml block survives **byte-for-byte** — `set-model` MUST NOT touch it (that is a future `set-guardrails` verb's job, not this one's);
- (b) every prose paragraph, HTML comment, table, and the `provider:` line (when not itself the target) survive **byte-for-byte**;
- (c) only the targeted heading's value span is rewritten in place — the surrounding section headings, blank lines, and any commentary immediately following the value stay untouched.

This makes `set-model` the framework's **first surgical `model.md` field-writer.** Today `atomic_agents/_model.py` is READER-only. The only existing WRITER, `atomic_agents/profile/filesystem.py`'s `save_profile()` (`atomic_write(agent_root / "model.md", profile.model_md_raw)`, ~line 521-524), is a wholesale blob rewrite of the whole file's raw text — correct for its job (profile round-tripping) and NOT reusable here, since it has no concept of "change one heading's value, preserve everything else" and would require the caller to already have the full correct byte content in hand. `set-model` MUST implement a NEW line-aware in-place value editor following the established idiom (`_model.py`/`_roster.py`-style regex-targeted edits, no parse→re-serialize round trip), the same discipline M2 already commits the codebase to for YAML-shaped regions, extended here to a heading-plus-prose-value shape.

Whether the write preserves the operator's EXISTING markup wrapper around the value as found, or normalizes every write to one canonical form, is an open fork (see below) — it does not affect (a)-(c) above, which hold either way.

### Audit primitive

A new `PRIMITIVE_MANAGE_SET_MODEL = "manage_set_model"` (defined alongside `PRIMITIVE_MANAGE_GOVERN` / `PRIMITIVE_MANAGE_RESTORE` in `atomic_agents/logs/types.py`), using the M8-pinned `RunRecord` shape as-is — this section does not re-pin the shape, only names the new primitive and what rides in `extra{}`:

- `model="n/a"`, `input_tokens=0`, `output_tokens=0`, `status="applied"` (refusals and dry-runs emit no record at all, per M8's pinned status vocabulary);
- `principal_id` — the resolved Principal, `LOCAL_PRINCIPAL` for the home user;
- `changed_fields` — the subset of `["default_model", "fallback_model"]` actually written this invocation (e.g. `["default_model"]` for a `--model`-only run);
- `before` / `after` — the changed field(s)' prior and new values, e.g. `before={"default_model": "claude-sonnet-4-6"}`, `after={"default_model": "claude-opus-4-8"}`;
- `snapshot_path` — the restorable pre-write snapshot under `.config-snapshots/set-model/`, relative to the agent folder (M8's generic per-verb key).

`created` (govern-specific — records that `governance.md` did not previously exist) does NOT apply here: per "Behavior" step 1, an absent `model.md` is a resolve-time refusal for `set-model`, not a create-and-fill path, so no `set-model` audit record is ever emitted with `created=true`. Dual-scope append (per-agent `LogBackend` + fleet `_manage` scope, collapsing to one append under a shared distributed store) follows M8's backend-aware rule unchanged.

### Conformance test outline (set-model additions)

Mirrors the shape of the top-level "Conformance test outline" section; these are the set-model-specific additions to it, not a replacement:

- **M9 composition refusals:** an unpriced `--model` (not in `_costs.PRICING`) refuses before write, non-zero exit, `model.md` unchanged; an unknown model id (`UnknownModelError`) refuses distinctly from an ambiguous one (`AmbiguousBackendError`) with a distinguishable structured `--json` reason for each; a Policy-overridden model (`get_effective_model` returns a conflicting non-`None` value) refuses; each of the three checks is strip-tested independently (per-invocation negative control, per the project's discipline) so a partial composition implementation cannot false-green.
- **Surgical preservation invariant:** an applied `--model` write leaves the `cost_guardrails` yaml block byte-for-byte identical (hash-compared before/after); leaves every prose paragraph and HTML comment byte-for-byte identical; leaves the `provider:` line untouched when `--provider` is not the target of the write.
- **Markup-style invariant:** once "Markup-style preservation" (below) is ruled, a conformance test asserts the ruled behavior across all four operator-markup variants the reader accepts (`` **`id`** ``, `**id**`, `` `id` ``, bare).
- **Absence refusal:** `set-model` against an agent with no `model.md` refuses at S2 step 1 (distinct from `govern`'s create-absent path — asserted as a negative control so the two verbs' absence-handling never silently converge).
- **Restore does not re-validate:** `--restore` against a snapshot whose model id is no longer in `_costs.PRICING` (deprecated since the snapshot was taken) still restores successfully — asserting the "restore does not re-run M9" rule in "Surface" above.

### Open forks (arc-discovery #726)

These are the genuinely undecided questions this section deliberately leaves open for arc-discovery to rule on. Nothing above depends on a particular answer to these; each is called out at its point of relevance.

- **PR1 flag scope.** Does PR1 ship `--model` only, or `--model` + `--fallback` + `--provider` together?
- **Provider disambiguation.** When `find_backend_for_model` raises `AmbiguousBackendError`, does `set-model` require `--provider` up front, or only surface the ambiguity as a refusal that tells the operator to re-run with `--provider`? Either way, how is the `provider:` line written surgically, given it lives outside the `cost_guardrails` yaml block and may be entirely absent from the file today?
- **Unpriced-model posture.** Is an unpriced `--model` value a hard refusal always (the recommended default, given M9 and the over-bill hazard in composition check (a)), or is there an escape hatch (`--force` + a loud warning) for an operator who knowingly wants to run an unpriced/custom model?
- **Policy model-override interaction.** If `PolicyBackend.get_effective_model` pins a model that differs from `--model`, does `set-model` refuse (the written value will not be honored at runtime — M9), or write anyway (model.md is the base layer Policy deliberately overrides)? Turns on the actual precedence semantics — confirm in `policy/backend.py` before ruling.
- **Markup-style preservation.** Does the surgical writer preserve the operator's existing value-wrapper markup (`` **`id`** `` vs `**id**` vs `` `id` `` vs bare) exactly as found, or normalize every write to one canonical form (e.g. matching the `init` templates' `` **`id`** `` style)?
- **Scope of the write.** If `--fallback` ships in PR1, does it target the `## Fallback` heading only? Confirmed here regardless: `set-model` never touches the `cost_guardrails` block in any scope — that is reserved for a future `set-guardrails` verb (see "model.md preservation contract" (a)), not this one.

---

## Implementation notes (Tier B — agent decides at build, with justification)

- **Code location:** a new `atomic_agents/manage/` package holding the reusable S2 safety-routine helper + per-verb modules, with thin dispatch in `cli.py` — matching the `secrets`/`mcp_registry` packaging idiom. The five-step routine MUST be a genuine shared helper, not copy-paste per verb. The hoisted spine (#709/#710) lives in `atomic_agents/manage/_routine.py`: a `manage_lease()` context manager (M11) plus a `run_managed_write()` orchestrator taking `read_base`/`apply_edit` callbacks — `govern.py` (and any future verb) supplies the callbacks; the lock-acquire/fresh-read/snapshot/write mechanics live in the shared helper exactly once.
- **Error taxonomy:** a typed exception ladder — validation guards (unknown-field / invalid-enum / invalid-date / control-char / nested-path / list-mutation / governance-invalid) each with a distinct message + structured `--json` reason, so the negative-control tests have a distinct, strip-testable failure per guard. Two ladder members named in the arc are general manage-layer taxonomy the `govern` verb does NOT implement as raised exceptions: **composition-refused** arrives only with the first runtime-effective verb (set-model, per M9 — govern's composition set is empty by design), and **audit-dropped** is deliberately NOT an exception — it is surfaced as a non-fatal `(ok, warnings)` tuple return of the shared audit helper (a warn that still exits 0 on an applied write, per M8), not a raised error. Validation/composition failures → non-zero exit, no write; audit-drop → exit 0 with a `warn` audit status. Spine-scope additions (#709/#710): `ManageAgentBusyError` (`error_type='agent_busy'`) and `ManageLockUnavailableError` (`error_type='lock_backend_unavailable'`) — both raised by the shared `manage_lease()` helper and caught CENTRALLY by the `manage` command dispatcher (`atomic_agents/manage/__init__.py:run_manage`), never per-verb (M11); `ManageSnapshotNotFoundError` (`error_type='snapshot_not_found'`) — raised when a `--restore <snapshot-id>` does not resolve under the target agent's own snapshot tree (also the cross-agent-restore refusal — no separate exception type, by design: the caller must never learn whether an id exists under a DIFFERENT agent).

## Implementer Contract (MUSTs)

**M1 — No new config format or state store.** A management verb MUST write only to existing agent markdown files in their existing formats, and MUST record management events only through `LogBackend`. It MUST NOT introduce a management-state sidecar file.

**M2 — Preservation contract (decided — arc #624 fork `governance-yaml-block-format`).** governance.md is a fenced ` ```yaml ` block (root key `governance:`) surrounded by operator-authored prose sections, and the block carries instructional inline comments operators rely on. A `--set` write MUST be **surgical**, satisfying this normative contract:
- (a) the prose body outside the yaml block survives **byte-for-byte**;
- (b) untargeted keys inside the block — including their inline comments and authored key order — survive **byte-for-byte**;
- (c) only the targeted key's scalar **value** is rewritten in place.

**`updated_at` auto-stamp (named exception to (c)).** On every applied write, the verb ALSO stamps `updated_at` to today's date (ISO-8601) unless the operator set `updated_at` explicitly in the same invocation. This is the single implicit field change permitted beyond the operator-targeted keys; it is a genuine write (it appears in the preview and in the audit before→after), and on a file that omits `updated_at` it is INSERTED as a new top-level scalar under `governance:` (consistent with the create-absent / partial-file fill behavior). It is named here so it is never a silent surprise and so later verbs inherit a documented "freshness stamp" contract.

The implementation MUST be a line-aware in-block value editor. PyYAML is the **reader/validator only** (enum/format validation per M4) and MUST NEVER be the writer — a parse→`safe_dump` round-trip is forbidden because it strips comments, reorders keys, and is non-idempotent against a hand-authored file. No comment-preserving YAML dependency (e.g. ruamel) is added; the codebase's existing line-aware config-edit idiom (`_model.py`, `_roster.py`) is the model. If a `--set` ever targets a **list** field (`sources.*`, `actions.*`), full re-emission of that one list is the single documented exception (spec/55 names it so it is never a silent surprise) — PR1 does not implement list mutation (see CLI grammar).

**M3 — Atomic write + restorable snapshot, AND a restore verb (decided — arc #624 fork `snapshot-mechanism`; expanded #710).** Every write MUST go through `_io.atomic_write` (a crash mid-write MUST NOT leave a half-written config, principle #8) and MUST first snapshot the prior **file** content to a **dedicated config-snapshot location**, namespaced per verb (an explicit `subdir` parameter — e.g. `govern` — so a future verb's snapshots never collide with another verb's), so the change is rollback-able to byte-faithful prior content. The snapshot MUST NOT reuse memory's `.versions/` machinery (that surface is memory-scoped per spec/02/spec/20; governance is config, not a memory note), and the JSONL audit line MUST NOT be treated as the rollback source (an audit record is not restorable file content, and recoverability MUST NOT depend on the observability stream being readable — cf. #497/#498 degraded-read posture). The snapshot is taken BEFORE overwrite; the audit (M8) is appended AFTER, so rollback never depends on the audit having been written.

**A snapshotting verb MUST expose the restorable state through an actual CLI restore path — a snapshot with no way to consume it is not "restorable," it is a write-only artifact.** `govern` satisfies this with `--restore <snapshot-id>` (#710), which MUST satisfy two sub-MUSTs so restore is genuinely safe, not a shortcut around M2–M9:

- **(a) Snapshot-belongs-to-agent.** The verb MUST resolve `<snapshot-id>` ONLY under the TARGET agent's own snapshot namespace (`<agent_dir>/.config-snapshots/<subdir>/`) — a snapshot-id that is valid only under a DIFFERENT agent's tree MUST be refused with the SAME "not found" shape a genuinely-nonexistent id gets (no cross-agent restore; the caller must never learn whether a given id exists under some other agent).
- **(b) Snapshot-before-restore.** A restore MUST itself run the FULL S2 five-step routine through the SAME hoisted spine every other write verb uses (§"The spine"/S2) — NOT a bypass. In particular it MUST take its OWN pre-restore snapshot of the state it is about to overwrite (so a restore is always itself undoable via a second restore) and MUST produce exactly ONE audit record for the operation (M8) — never an additional record under another verb's primitive for the same logical restore.

Snapshot **retention/pruning** is explicitly OUT OF SCOPE for this spec — `.config-snapshots/<subdir>/` grows unboundedly today (growth scales with write frequency × fleet size, not elapsed time). This is a deliberate, accepted consequence pending a dedicated retention design (tracked — #750), not an oversight.

**M4 — Validate before write.** A verb MUST validate field names AND values (schema + enums + formats) AND applicable composition gates BEFORE step 2 (preview). Invalid input MUST be refused with a clear, named error and a non-zero exit, and MUST NOT write or partially write.

**M5 — Confirm-by-default; `--yes` to apply.** A verb MUST NOT apply a change without either an interactive confirmation (TTY) or `--yes`. `--yes` MUST NOT be the default. `--dry-run` MUST stop after preview and never write.

**M6 — Copilot properties.** Every verb MUST support `--json` (structured output incl. the structured refusal reason) and MUST be fully drivable with flags (no required interactive prompt). The only interactive element permitted is the TTY confirm of M5, which `--yes` always satisfies.

**M7 — Registry-resolved target; verb-side write.** A verb MUST resolve and load its target agent through `AgentRegistryBackend`, not by direct filesystem walking, so it works against any registered registry backend. The **write** then goes to the registry-resolved location via `_io.atomic_write` (M3); the `AgentRegistryBackend` protocol stays read-only discovery and is NOT extended with a governance-write method (see S1; arc #624 fork `registry-write-seam`).

**M8 — Audited with identity, on the existing audit shape (decided — arc #624 fork `audit-record-shape`).** Every applied write MUST append a management event through `LogBackend` as a `RunRecord` — NOT a new event dataclass (which would require widening the LOCKED `LogBackend.append()` signature) — following the established non-LLM-event precedent (`policy_decision`, `mandate_reservation`). The record uses a dedicated primitive `PRIMITIVE_MANAGE_GOVERN = "manage_govern"`, `model="n/a"`, `input_tokens=0`/`output_tokens=0`, `status="applied"`, and carries `principal_id` (from the resolved Principal — `LOCAL_PRINCIPAL` for the home user), `changed_fields`, `before`, and `after` in `extra{}`, plus `snapshot_path` (the restorable pre-write snapshot, relative to the agent folder; `null` on a create-absent write) and `created` (`true` when the write CREATED the file, so there is no restorable prior state). `snapshot_path` is a generic per-verb key every snapshotting verb emits; `created` is govern-verb-specific (it records that governance.md did not previously exist). The status vocabulary is pinned NORMATIVELY: an applied management write emits `status="applied"`; refusals and dry-runs do NOT emit a management RunRecord at all (they are refused before the audit step, so there is no `status="refused"`/`"dry_run"` value — a management RunRecord existing implies the write was applied). These `extra{}` key names + the primitive id + the `status="applied"` value are pinned NORMATIVELY here so every later verb (set-model, set-goal, apply-rec) emits the same shape.

**Backend-aware dual-scope append (decided — arc #624 fork `logbackend-scope`; refined at convergence).** The event is appended to the target agent's per-agent `LogBackend` (`get_default_log_backend(agent_dir)`) AND a fleet-level management `LogBackend` at `agents_root/_manage` — but the two-copy shape is correct ONLY when the two scopes resolve to physically **distinct** stores. Under the default Filesystem backend they are two separate `log/` dirs (and SQLite-no-URL is two separate db files), so BOTH scopes are appended: two append-only copies of one immutable event — the home user sees it in the agent's own log with zero extra machinery, and the fleet stream survives the agent's deletion. Under a **URL-backed distributed `LogBackend`** (Postgres, or SQLite with a shared `ATOMIC_AGENTS_LOG_BACKEND_URL`) `scope_root` is ignored and BOTH scopes resolve to the **same central table**; there the event is appended **exactly once** and that single central-table row IS the fleet stream (it already survives agent-folder deletion and is queryable by `primitive=manage_govern`). A second append under a shared store would be a **duplicate row with an identical `run_id`** and would double-count every fleet `COUNT` / `GROUP BY agent` / cost aggregation (audit integrity is structural, principle #5), so it MUST NOT be written. A conforming implementation MUST therefore collapse to a single append when the two scopes share one physical store. If the backend's store identity cannot be determined (an unrecognised custom backend), the implementation appends once (no duplicate-row risk) and MUST surface a non-fatal warning so the possibly-skipped fleet copy is observable, not silent. A dropped audit write MUST surface a non-fatal warning, never fail silently, and (per M3) MUST NOT compromise rollback — this includes a **construction-time** failure of a swapped/misconfigured backend, not only an `append()`-time failure. Before/after values are operator-authored config (not new secrets), kept inline; the verb MUST redact known-secret-shaped fields at the echo site (the project's redact-at-echo rule) and cap string length the way `summary` is capped.

**M9 — Compose, never bypass (per-field scoped; decided — arc #624 fork `composition-gates-for-govern`).** A verb that sets a value the agent runtime would reject MUST refuse at write time (e.g. a model not in `_costs.PRICING`/caps, or disallowed by `policy.md`). A management write MUST NOT produce a config the agent cannot honor. **For `govern` specifically, the composition set is EMPTY by design:** every governance field is descriptive metadata with no runtime-rejection counterpart (verified — no runtime gate reads governance), so S2 step 1's composition check resolves to enum/format validation (M4) with zero additional gates. This is a documented intentional empty set, NOT an unimplemented step. The composition set becomes non-empty for runtime-effective verbs (set-model composes with PRICING/caps/policy); if a future GovernanceRecord field ever becomes runtime-enforced, its non-empty composition set is recorded in this per-field scoping.

**M10 — No authority escalation.** A verb MUST NOT perform any action the operator could not already perform with direct write access to the agent folder. The CLI is a validated path to those edits, not a privilege grant.

**M11 — Per-agent concurrency: the manage lease (spine-wide, decided — #709).** Every write verb — `govern --set`, `govern --restore`, and every future verb that reaches S2 step 4 — MUST acquire a per-agent, NON-BLOCKING manage lease around the read(fresh)→snapshot→atomic-write region of an APPLYING invocation, and refuse with a structured `agent_busy` error on contention. This is a SPINE-scope MUST (binds every verb through the shared S2 routine), not a `govern`-only rule — it is deliberately NOT folded into M3, because it is a concurrency guarantee, not a rollback guarantee. **Concurrency semantics (explicit, not a gap):** within that guarantee, `--set` is LAST-WRITER-WINS against the fresh in-lease base — the lease prevents interleaved corruption and a lying audit trail (two writers' bytes can never physically interleave, and the audit before/after always reflects what was actually on disk), but it does NOT add optimistic-concurrency / compare-and-swap on the targeted field (a second writer's earlier read is not detected or rejected, it simply loses). Whether `--set` should CAS-refuse on a concurrently-changed field is tracked in **#751**, not resolved by M11.

- **Mechanism.** The lease is acquired via the LOCKED `LockBackend` Protocol (spec/21) — `get_default_lock_backend(agent_dir).acquire('manage', timeout=0)` — never a hand-rolled `fcntl.flock` (principle #2). On the Filesystem default this produces a HIDDEN `<agent_dir>/.manage.lock` artifact (matching the `.config-snapshots` invisibility discipline), NOT a visible `manage/` subdir. On a distributed (non-single-host) backend, whose key namespace is deployment-wide rather than per-`scope_root` (spec/21), the implementation MUST fold the resolved agent id into the acquired resource name (e.g. `manage:<agent_id>`) so per-agent isolation holds under that backend too — a bare `'manage'` name would collapse every agent's lease onto one shared key.
- **Scope — NOT the main lock.** The manage lease is a genuinely DISTINCT named lease from the agent's main `''`-named lock (the one `agent.call()` holds during a live run). A management write MUST NOT contend with a live run, and a live run MUST NOT contend with a management write. The accepted, bounded consequence: a display-layer reader (registry/dashboard/doctor) may observe pre-edit governance until the atomic write lands mid-run — acceptable because governance has no runtime consumer (T15 config/state split).
- **Scope — NOT the confirm prompt.** The lease MUST be acquired AFTER the interactive confirm (S2 step 3) resolves — an idle TTY prompt holding the lease would starve every OTHER management write on the same agent indefinitely. `--dry-run` (which exits before confirm) and a declined/interrupted confirm therefore never touch the lease at all.
- **Scope — NOT the audit append.** The lease MUST be released BEFORE the audit append (S2 step 5) runs. Audit is already best-effort/non-fatal (M8); holding the lease through it would turn a deliberately-non-fatal audit hiccup into an availability hazard for every OTHER management write on the agent.
- **Fresh read, not the preview read (the lost-update guarantee).** The read the applying write bases its edit on MUST be taken FRESH, INSIDE the lease — never the earlier, ADVISORY read a verb took for its S2 step 2 preview / `--dry-run` (that earlier read may be stale by the time an interactive confirm resolves, since confirm can block indefinitely). A verb whose applied write reuses a pre-lock read as its write base does NOT satisfy this MUST, even if it also acquires the lease — the lease alone does not prevent a lost update unless the write base is re-read under it.
- **Fail-closed construction.** If the `LockBackend` cannot be constructed (misconfigured env, missing optional extra, unregistered backend id), the verb MUST refuse the write (exit 1) — it MUST NOT silently proceed unlocked. This is a DISTINCT, separately-typed refusal from lease contention (`error_type='lock_backend_unavailable'` vs. `'agent_busy'`) so a copilot driver can tell "someone else is editing this agent, retry shortly" apart from "the lock infrastructure itself is misconfigured, do not retry-loop."
- **Central catch, no retry.** `agent_busy` / `lock_backend_unavailable` MUST be caught at ONE central spine dispatch point (not duplicated per-verb) and emitted as `{ok:false, error_type, reason}` exit 1. Per M8's pinned status vocabulary, a refusal — including `agent_busy` — does NOT emit a management `RunRecord` at all (a `RunRecord` existing implies the write was applied); contention is visible only via the structured refusal, never via an audit line. The spine deliberately does NOT retry on contention (`timeout=0`, non-blocking, by design) — automated/fleet-scale callers (e.g. a future bulk `set-model`/`apply-rec` sweep) are responsible for their own retry-with-backoff.
- **Reads are exempt.** A pure read (`--show`, `--list-snapshots`) MUST NOT acquire the manage lease — reads never contend with writes under this spec.

---

## Conformance test outline

- **Spine:** target resolution goes through a stub `AgentRegistryBackend` (S1/M7); a verb run against a backend that does not expose the agent fails cleanly. (`tests/test_manage_spine.py`)
- **Safety routine:** `--dry-run` writes nothing (M5); a non-`--yes` non-TTY run does not apply (M5); a confirmed run writes atomically and preserves the body (M2/M3); a mid-write crash leaves the original intact (M3); **`--restore <snapshot-id>` restores prior content through the SAME hoisted spine (M3, not a bypass) — including its own pre-restore snapshot, and a cross-agent snapshot-id is refused (m3 restore sub-MUSTs (a)/(b))** (`tests/test_manage_spine.py::test_restore_*`).
- **Concurrency (M11):** a contended per-agent manage lease refuses with `agent_busy`, distinct from a `lock_backend_unavailable` construction failure; the lease does not contend with the agent's main lock; `--dry-run`/`--show`/`--list-snapshots` never touch the lease; the lease is released before the audit append runs; a write based on a stale pre-lock read does not clobber a concurrent write that already landed (the lost-update fix) (`tests/test_manage_spine.py`).
- **Validation:** unknown field, invalid enum, malformed date each refuse before write with a non-zero exit and write nothing (M4); a value rejected by a composition gate (PRICING/caps or `policy.md`) refuses (M9).
- **Copilot:** `--json` emits structured success and structured refusal (M6); every path is reachable without a TTY prompt given `--yes` (M6).
- **Audit:** an applied write appends exactly one management event with principal_id, before→after, changed fields (M8); a `LogBackend` append failure warns but does not fail the (already-applied) write (M8); `--restore` appends exactly one `manage_restore` record and zero `manage_govern` records for the same operation (`tests/test_manage_spine.py::test_restore_full_routine_emits_exactly_one_manage_restore_record`).
- **Exit-code ladder:** applied/preview/read-only exits `0`; refusal exits `1`; a declined confirm (`'n'`/EOF) exits `3`; `KeyboardInterrupt` exits `130` — verified for both `--set` and `--restore` (`tests/test_manage_govern.py` + `tests/test_manage_spine.py::test_restore_exit_code_ladder_*`).
- **govern verb:** `--set` round-trips through `GovernanceRecord`; hyphenated CLI names map to underscore schema keys; creating `governance.md` where absent works and is audited; the prose body survives an update; BOM / unicode / CRLF byte-fidelity survives an applied write (`tests/test_manage_govern.py`).
- **Negative controls:** strip each independent guard (validation, confirm, snapshot, audit, composition, lock) and assert a distinct failure — no false-green (per the project's per-invocation negative-control discipline); an orphan snapshot from a write that fails AFTER the snapshot succeeds is documented-benign, verified for both `--set` and `--restore` (`tests/test_manage_spine.py::test_orphan_snapshot_on_*`).

---

## Cross-references

- **Epic #606** — Agent Fleet Platform (observe → manage → interact); #624 (this foundation), #609 (the `govern` verb).
- **#709/#710** — spine hardening: per-agent manage lease (M11) + hoisted S2 spine helper (`atomic_agents/manage/_routine.py`) + the `govern --restore` verb + the abort exit-code ladder.
- **spec/21** LockBackend — the Protocol the manage lease (M11) is built on; distributed-backend key-namespace scoping.
- **spec/51** AgentRegistryBackend (#607) — the agent selector (S1) + the `GovernanceRecord` schema the first verb writes.
- **spec/48** PrincipalBackend (#556) — the audit identity stamped on management events (not a gate).
- **spec/49** `atomic-agents deploy` — the adjacent lifecycle verb under the same spine.
- **spec/29** Mandate / **spec/32** Policy — gate the agent's runtime; composed-with (M9), not the operator-write gate.
- **spec/22** LogBackend — the management-event audit stream (M8).
- **CLAUDE.md** rule 7 (markdown config), rule 5 (audit is structural), rule 8 (atomic + idempotent), rule 14 (backward compatibility — `manage` is additive; existing CLI unchanged).
