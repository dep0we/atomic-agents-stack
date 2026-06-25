---
spec: 54
title: Fleet Management CLI — the "manage" layer
status: DRAFT
created: 2026-06-25
issue: 624
---

# spec/54 — Fleet Management CLI: the "manage" layer

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

Every management verb targets its agent through `AgentRegistryBackend` (#607). The same backend the console reads is the backend the management CLI writes through. This keeps discovery uniform across observe and manage, and means a verb works against the filesystem default today and a Postgres registry (#608) later without per-verb changes. A verb MUST NOT walk the filesystem directly to find or load an agent's config when the registry exposes that surface.

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
 #607           shipped      spec/49      spec/54      shipped              spec/49
```

The management CLI does not rebuild any neighbor. `deploy` (spec/49) already owns standing an agent up and tearing it down; `manage` owns changing its config while it exists. Both sit on the same spine (S1–S4) so the experience is consistent across the lifecycle. A later console/`manage` surface MAY display `deploy status` inline; this spec does not require it.

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
4. **Snapshot + atomic write.** Snapshot the existing `governance.md`, then write the updated frontmatter through `_io.atomic_write`, preserving the markdown body. Creating `governance.md` where absent is allowed (a fleet operator authoring governance for the first time); the write still goes through validation + audit.
5. **Audit.** Append a `manage_govern` management event (principal_id, ts, agent, changed fields, before→after) through `LogBackend`.

### Why governance first

`governance.md` holds labels and metadata (owner, risk tier, review date, lifecycle status). Setting them changes nothing about how the agent *behaves* or what it *costs* — so it is the safest possible place to build and prove the write scaffold. The schema already exists (spec/51 `GovernanceRecord`), `init` already writes a stub, and the AgentRegistryBackend already reads it. The verb adds the safe write path.

---

## Implementer Contract (MUSTs)

**M1 — No new config format or state store.** A management verb MUST write only to existing agent markdown files in their existing formats, and MUST record management events only through `LogBackend`. It MUST NOT introduce a management-state sidecar file.

**M2 — Markdown body preservation.** A frontmatter-field write MUST preserve the file's prose body and any fields not targeted by the write. Only the targeted fields change.

**M3 — Atomic write + snapshot.** Every write MUST go through `_io.atomic_write` and MUST snapshot the prior file content before overwriting, so the change is rollback-able. A crash mid-write MUST NOT leave a half-written config (principle #8).

**M4 — Validate before write.** A verb MUST validate field names AND values (schema + enums + formats) AND applicable composition gates BEFORE step 2 (preview). Invalid input MUST be refused with a clear, named error and a non-zero exit, and MUST NOT write or partially write.

**M5 — Confirm-by-default; `--yes` to apply.** A verb MUST NOT apply a change without either an interactive confirmation (TTY) or `--yes`. `--yes` MUST NOT be the default. `--dry-run` MUST stop after preview and never write.

**M6 — Copilot properties.** Every verb MUST support `--json` (structured output incl. the structured refusal reason) and MUST be fully drivable with flags (no required interactive prompt). The only interactive element permitted is the TTY confirm of M5, which `--yes` always satisfies.

**M7 — Registry-resolved target.** A verb MUST resolve and load its target agent through `AgentRegistryBackend`, not by direct filesystem walking, so it works against any registered registry backend.

**M8 — Audited with identity.** Every applied write MUST append a management event through `LogBackend` carrying the principal_id (from the resolved Principal — `LOCAL_PRINCIPAL` for the home user), timestamp, agent name, changed fields, and before→after values. A dropped audit write MUST surface a non-fatal warning, never fail silently.

**M9 — Compose, never bypass.** A verb that sets a value the agent runtime would reject MUST refuse at write time (e.g. a model not in `_costs.PRICING`/caps, or disallowed by `policy.md`). A management write MUST NOT produce a config the agent cannot honor.

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
