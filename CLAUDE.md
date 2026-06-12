# CLAUDE.md — atomic-agents-stack

This file loads in every session for this repo. It captures the *design ethos* of the framework — what to protect as it scales, what to refuse, how to make decisions that keep the codebase coherent. Project-specific tactical rules (test commands, branch shape) sit alongside.

For broader context, read these in order on a fresh session:
- `docs/architecture.md` — the mental model in diagrams
- `~/ObsidianVault/Atomic Agents/ROADMAP.md` — strategic narrative + live issue links
- `docs/TENSIONS.md` — architectural tensions that must survive scaling
- `~/ObsidianVault/Atomic Agents/RESUME-NEXT-SESSION.md` — current session-state pointer

---

## What this is

Atomic Agents is a vault-native AI agent framework: agents live as plain markdown files, the runtime is stateless, and storage is moving toward swappable protocols layer by layer. **Twelve backend protocols shipped through v1.0; sixteen total as of v1.5 in progress** — see `docs/protocols-shipped.md` for the per-protocol summary (reference impls, capabilities, operator overrides, doctor checks, Implementer Contracts, and what cliff each closes).

The spec is the central artifact. The Python package is one conforming reference implementation. Anyone can build agents to the spec without using this code — and eventually, alternate implementations will.

## The throughline

> **A home user with one agent and an org with a fleet experience the same framework — graceful, coherent, self-explanatory at every scale.**

This is the property to protect. Everything in this file exists to keep it true.

The trap to refuse: optimizing for one shape and calling the other a degraded mode. *Both shapes are first-class.* Caldwell-on-a-Mac-mini is not a toy version of the SaaS shape. The SaaS shape is not a power-user lift on the home shape. They're the same agents, run through the same code, with different backends registered.

When you can't tell whether a design move helps both — stop, name the tradeoff, write it down. Don't pick one silently.

## Architecture in one breath

```
                  agent.call() — the orchestrator
                                │
       Composition layers   Execution layers   Observability
       ─────────────────    ─────────────────  ──────────────
       Persona (cascade)    Outcomes           Dashboard (5 tabs)
       Tools                Dreams             JSONL logs
       Skills (lazy)        Evals              Helper provenance
       MCP (external)       Tuning             Delegation rollup
       Memory (Notes+Wiki)  Goals              Cost tracking
       Journal              Delegation (tree-cap) OTel trace export 🟡 (#341 PR1: call span)
                            Helpers (cheap parallel)
                                │
                  Backend Protocols (the moat)
                  Memory ✅  LLM ✅  Judge ✅ (locked at #112 PR 4)
                  Lock ✅ (locked at #60 PR 4)  Log ✅ (locked at #61 PR 4)
                  AgentProfile ✅ (locked at #63 PR 4)
                  ToolRegistry ✅ (locked at #64 PR 4)
                  Mandate ✅ (locked at #124 PR 4)
                  Policy ✅ (locked at #89 PR 4)
                  Persona ✅ (locked at #62 PR 4)
                  Corpus ✅ (locked at #65 PR 4)  MCPServerRegistry ✅ (locked at #201 PR 5)
                  SecretBackend ✅ (#340 PR 2, LOCKED spec/38 — the 13th, v1.5)
                  GoalBackend 🟡 (#425 PR 1, DRAFT spec/41 — the 14th, v1.5)
                  OutcomeBackend 🟡 (#426 PR 1, DRAFT spec/42 — the 15th, v1.5)
                  JournalBackend 🟡 (#427 PR 1, DRAFT spec/43 — the 16th, v1.5)
                                │
                  Storage substrate — swappable
                  Filesystem (today)  →  Postgres / pgvector / Redis (later)
```

Read `docs/architecture.md` for the diagrams. Read `docs/spec/20-memory-backend.md` for the protocol pattern that every future backend follows.

---

## Design principles — the taste rules

These are the rules that, followed, keep the elegance intact as the framework grows. When a proposed change would violate one, *stop and write down why before proceeding*. Often the answer is "the rule still holds; rework the change." Sometimes it's "the rule needs to flex here, and TENSIONS.md gets a new entry." Either is fine. Silently breaking them is not.

### 1. The vault is the source of truth

Anything stateful lives in the agent folder — primarily markdown + JSONL, with structured JSON sidecars for pipeline state (dream `manifest.json`, outcome `result.json`, cascade `.lease.json`, dashboard data files). Backends translate vault state into other forms (database rows, vector indexes); they are never authoritative. Kill the runtime, restart it, swap from cron → Claude skill → MCP server → HTTP service — the agent is the same agent because the files are the same.

If a feature requires state that *only* exists in a backend, reconsider it. The portability win is load-bearing.

**This rule flexes for backend-swapped deployments** (cloud/enterprise) — see `docs/TENSIONS.md` T15. The short version: "backends are never authoritative" holds for the default filesystem deployment and for *config* (persona/model/tools/goal) in every deployment; *state* run through a swapped backend (Postgres, native cloud store) is authoritative-in-that-deployment, with the canonical vault file *shape* preserved as a tested round-trip export contract (#379) rather than a guaranteed location. Portability is "the file shape is always reconstructable," not "the files are always the truth." (See TENSIONS.md T15 for the Position B ruling and `docs/spec/40-canonical-export.md` for the LOCKED implementation contract.)

### 2. Protocols, not subclassing

When a primitive touches storage, it gets a protocol. The template is `docs/spec/20-memory-backend.md` + PR #57: **Protocol + dataclasses + WritePolicy + capability advertisement + filesystem-default + spec doc + ~25 conformance tests + ~10 fs-specific tests**. Apply this shape to LockBackend (#60), LogBackend (#61), PersonaBackend (#62), AgentProfileBackend (#63), ToolRegistryBackend (#64), CorpusBackend (#65), MandateBackend (#124), and any future backend.

Don't bolt on Postgres support as `if backend == "postgres": ...`. Define the protocol; ship filesystem-default; let alternate impls register at import time.

### 3. Layers compose; they don't merge

Persona ≠ memory. Notes ≠ Wiki. atomic_capture ≠ helper_call ≠ delegate ≠ tool. Each has its author, cadence, lifecycle, and review process. They live in different files for a reason.

If you're tempted to collapse two layers — write down why first. Read `docs/architecture.md` §"Why this shape (the load-bearing decisions)". Most layer-merge ideas dissolve once the rationale is reread.

### 4. Cost is first-class, not bolted on

Every `agent.call()` path checks `_check_cost_guardrails` before the first LLM call and re-checks each iteration of the multi-turn loop. Every helper batch reserves worst-case before dispatch. Every delegate clamps to `MIN(child_remaining, parent_remaining)` — the **tree-cap** is what makes "running an army" not bankruptcy. Long-running pipelines outside `call()` (`dream.py`, `eval.py`, `tuning.py`) have their own cost gates (`_check_cap`, batch reservations) — the discipline is that *every code path that calls an LLM has a cost gate*, even when it's not the same gate.

Don't add a code path that escapes its appropriate guardrail. Don't make `critical=True` the default anywhere. Don't move the cost check after a subprocess spawn or a session establish — refuse the call before paying the overhead.

### 5. Audit trail is structural

Every agent run writes a JSONL line with a `run_id`. Helper, tool, and delegate calls write child JSONL lines carrying `parent_run_id` linking back. The parent run record rolls up `helper_provenance`, `delegations`, `tool_calls` inline. Memory mutations carry their own audit shape — version snapshots in `.versions/` for `write_note` / `restore_version` / `redact_version`. Goal lifecycle changes append to `goal.md` history. New surfaces (LogBackend, dashboards, alerts) translate these streams — they don't replace them.

If a new feature has events the existing audit shapes can't represent, extend the right shape; don't side-channel.

### 6. Progressive disclosure by default

Don't load what you don't need yet. Skills metadata in the system prompt; bodies lazy-loaded via `load_skill` tool. MCP tools registered at the start of `call()`, torn down in `finally`. Atomic notes recall: 2K-token INDEX guides selective loading of 1-3 notes (~1-2K tokens) instead of dumping 30K+ tokens of full memory.

The principle: the LLM pays context tokens for *capability awareness*, not capability content. When a new feature wants context space, ask "what's the metadata that lets the model decide to load the rest?"

### 7. Markdown config or no config

Operator-facing config (`tools.md`, `model.md`, `mcp.md`, `roster.md`, `SKILL.md`, `goal.md`, `persona/IDENTITY.md|SOUL.md|USER.md`) stays markdown. Editable in any text editor or Obsidian. Same aesthetic as the agent's content.

Resist YAML / TOML / JSON for human-edited config. Embedded YAML inside markdown (the `cost_guardrails:` block in `model.md`) is fine for structured fields. Pure-YAML config files are not.

If a config field doesn't fit the markdown shape, ask whether the field is right before changing the shape.

### 8. Atomic + idempotent everywhere

Writes go through `_io.atomic_write` (temp + fsync + rename + parent dir fsync). Teardown is idempotent (MCP pool, tool registrations, lock release all run safely on exception paths). Captures dedupe by `(type, name, body hash)`. Tool registry refuses overwrite by default. Schema migrations snapshot before, validate after, rollback if invalid.

No half-finished state. Crashes leave recoverable artifacts, not corruption.

### 9. One-level constraints stay

Delegation is one-level (a coordinator delegates to specialists; specialists don't delegate). Skill referenced files are one-level deep. These are guardrails against complexity creep — they bound the call tree, the file tree, the reasoning a future contributor has to do.

When a feature wants to lift a one-level constraint, the burden of proof is high. Two-level delegation looks like flexibility; in practice it's how systems become unauditable.

### 10. The spec is the product

Code without spec is incomplete. New backend = new spec doc, numbered, in `docs/spec/`. Spec drift erodes the conformance-suite play (ROADMAP #9) and the framework's portability story.

Spec lock cadence: spec gets locked when the implementation matches and tests pass. Spec changes that imply implementation changes get filed as issues. Spec docs are not aspirational; they describe what's true today.

### 11. Adversarial review in rounds, not passes

For any non-trivial PR — especially backend protocols, framework refactors, anything touching `agent.call()`, `_capture.py`, `_costs.py`, `_locks.py`, the protocol surfaces, AND even docs-only PRs — run **2-5 Opus adversarial rounds pre-merge**, not one thorough pass. The Opus adversarial subagent is the default reviewer; Codex is skipped per the standing project rule (corroborated across 5 arcs: #112, #60, #61, #63, #64). The full reviewer-roster rationale is in `docs/methodology.md` §"Reviewer roster — what the project actually does".

The non-obvious property: **each round catches different things.** Not because the reviewer "tries harder" the second time. Because each fix changes the diff and exposes new edges. Recent track record: PR #75 (`doctor`) — 3 rounds, 9 P2 findings closed. PR #76 (SemVer policy) — 5 rounds, 11 P2 findings closed. Round 5 of #76 was the only round that flagged the `No migrations needed` claim — earlier rounds had cleared the diff that contained it. **PR #206 (#64 PR 4, docs-only) — 2 rounds, 11 findings + 1 new successor issue (#207). Round 2 caught a count-drift the round-1 fix commit itself introduced.** Rounds-not-passes holds even when the diff has no code.

A different-model-family reviewer would catch what same-family blind spots miss — Codex is the deferred-not-deleted cross-family backup; re-instate when a session can verify Codex is responsive on a small probe first. **The wrong version of this practice is "ask Claude to imagine being a reviewer." That's prompting; this is verification — the subagent gets a fresh context, reads the diff itself, and runs its own commands.**

Empirically, 2-3 rounds is sufficient for most diffs (2 is the minimum because round 2 catches what round 1's fix commit introduces). Most rounds run as background tasks while you're doing something else — wall-clock cost stays low, token cost amortizes against the compounding correctness payoff.

Don't merge without it. The full retrospective is in `docs/methodology.md`.

### 12. Verify before claim, empirically

When Codex (or anyone) says "your docs are wrong about this CLI flag" — **reproduce the failure before accepting the finding.** Don't accept by plausibility.

Recent examples in this project:
- `python -m atomic_agents.migrate --dry-run` (without `--to`) — Codex asserted exits 1; ran it, confirmed exit 1, fixed the runbook
- `migrate --to vN` against an already-current vault — Codex asserted raises with `Target version vN is not above current vN`; ran it, got exactly that text, matched the docs to actual behavior

The rule: most code review is "your reviewer asserts a thing; you accept or reject based on plausibility." This project mechanizes "you accept or reject by reproducing." Slow per-finding. **Eliminates rumor-driven changes.** The cost is not as high as it sounds because most claims are trivially reproducible.

This applies in both directions — when you make a claim about behavior, run the command and confirm. Documentation, docstrings, error messages all stay accurate this way.

### 13. Documentation matches reality, not aspirations

The upgrade runbook says "scripts must be copied into `<vault>/_migrations/`" because that is the actual interface today. The ideal interface is `atomic-agents migrate <agent>` as a packaged command. **The docs were not "fixed" to match the ideal — the docs were made to match the implementation, and a follow-up issue was filed for the future.**

This is unusual. Most docs describe an aspirational world or a partial truth that drifts. By matching docs to *current behavior + linking to the issue for future improvement*, neither future-readers nor present-operators get misled.

**Pre-merge expectation:** if a doc claim does not match the implementation, fix the implementation or fix the doc — never let them diverge. Aspirational claims are a feature backlog item, not a docs entry.

### 14. Backward compatibility by default

Backends default to filesystem; no config changes required for existing deployments. New backends are opt-in. Deprecation wrappers (`_capture.py`, `_versioning.py`) re-export from new locations and emit `DeprecationWarning` planned for v1.0. Sunset dates go in the shim's docstring.

When a breaking change is necessary, it's a major-version event with a `### BREAKING` callout in the CHANGELOG entry, an upgrade runbook, and a migration script.

---

## Aesthetic — what "graceful" means here

This framework should *feel* like one thing, not a stack of seven libraries glued together. Borrowing the Apple analogy: great technology, very advanced, but the surface stays coherent. The complexity is layered — you can use it without understanding all of it, but if you peek behind, the structure is honest.

Concrete tells of grace, in this codebase:

- **Defaults are right.** A new agent runs with sensible cost guardrails, sensible context layout, sensible memory recall — without operator tuning.
- **Things that should be hard are easy.** Adding a custom tool is ~10 lines. Adding an MCP server is one section in `mcp.md`. Swapping a memory backend will be a config field.
- **Things that should be impossible are also easy.** Capturing a memory takes one tool call. Delegating to a specialist takes one method call. The audit trail rolls up automatically.
- **Things you can't do, you can't do for a documented reason.** Two-level delegation refused per spec/15. Path traversal refused per `_io.safe_resolve_under`. Cost-cap escape refused unless `critical=True`. Each refusal has a rationale a contributor can read.
- **Names are honest.** `agent.memory.write_note(capture, policy)` reads as English. `assemble_system_prompt()` does what it says. `helper_call_parallel()` is what it sounds like.
- **The README sells the same thing the code delivers.** No oversold features, no hidden seams, no "actually you have to also configure X" footnotes.

When a change makes one of these worse, push back on it — even if it adds a feature. **Refusal is a feature.** Restraint is what keeps the framework feeling like one thing.

---

## Conventions

### Issues + backlog

All work tracked in **GitHub Issues at dep0we/atomic-agents-stack**. Title prefixes: `[backend]`, `[deployment]`, `[polish]`, `[v0.X]`. Labels: `enhancement`, `documentation`, `infrastructure`, `polish`, `backend`, `deployment`, `spec`, `bug`. **Don't track atomic-agents work in Todoist.**

When scope creep / follow-up / future enhancement appears mid-task, **file the issue inline as part of completing the parent task**. Don't ask the maintainer to file it. The pattern from PR #57: "I noticed X; filed #58 to track it; continuing on parent scope."

ROADMAP.md (in vault) is the strategic narrative; GitHub Issues are the executable backlog. The two stay in sync via the ROADMAP's "Live issue backlog" table.

### Branches + PRs

Default workflow: feature branch → PR → adversarial review (Opus subagent, in rounds per rule 11) → self-review → merge. **Never push to main directly.** This applies even on solo work — the PR body is the audit trail and CI runs there.

The `/ship` skill handles the full pipeline (branch, test, CHANGELOG, VERSION bump, PR). Use it.

### Tests

Test suite is the conformance gate. Run before pushing:

```bash
uv run pytest                            # full suite
uv run pytest tests/test_<module>.py -v  # one module
```

Run `uv run pytest --collect-only -q | tail -1` for the live test count (last refresh: 4,252 tests collected, 2026-06-12). New backend protocols add ~25 conformance + ~10 impl-specific tests. New features ship with tests. Migration-shaped PRs need parameterized fixture tests across the backend protocol — the conformance suite is what keeps the protocol honest.

### Releases + SemVer

Pre-1.0, **Minor releases may contain breaking changes** — read release notes before upgrading. v1.0 lock is when the protocol surface is stable.

Every release: `vX.Y.Z` git tag + GitHub Release with CHANGELOG entry verbatim. Breaking changes get `### BREAKING` callout. See `docs/deployment/versioning.md` and `docs/deployment/upgrading.md` for the full SemVer policy.

---

## Working methods

These are the methods that have produced this codebase's quality (7 published tags through v1.0.0, ~160 merged PRs, ~3.3k tests, no production rollback events). Captured here to survive the session that produced them. Full retrospective in `docs/methodology.md`.

### Always run `/ship` end-to-end — never bypass

**Every push goes through the `/ship` skill.** Not "merge directly," not "push and PR manually," not "I'll just bump the version this once." `/ship` is an 18-step pipeline that handles branch, tests, CHANGELOG, VERSION, commit, push, PR — and **Step 18 runs `/document-release` as a subagent** to keep the README's "What's shipped" table, package metadata, and surface docs in sync with what actually shipped.

This matters because **bypassing `/ship` once causes drift that's caught only by accident later.** The v0.10.0 release was cut without `/ship` — the README's "What's shipped" table drifted and was caught only because the maintainer noticed. Workflows are correct when run end-to-end; manual shortcuts lose the consistency check.

Treat the 18 steps as load-bearing. If a step is wrong, fix `/ship`, don't skip it.

### Bisectable commits, not save-points

Every merged PR splits into multiple logical commits when the work is non-trivial:
- PR #75 — one commit for `doctor.py + tests`, one commit for `spec doc + getting-started + CHANGELOG`
- PR #76 — one commit for `versioning.md + upgrading.md`, one commit for CHANGELOG conventions + README link

Future operators running `git bisect` on a regression have clean atoms to bisect against, not a 1873-line wall. The shape works retroactively too — when v0.1.0 and v0.9.0 were tagged retroactively, `git log --oneline -- CHANGELOG.md` surfaced the right commits in seconds because commits had been sized for navigability all along.

Don't squash multi-concern PRs into one commit "to keep history clean." Clean history is *bisectable* history.

### CHANGELOG is the single source of truth

GitHub Release notes come from the CHANGELOG entry **verbatim** (via `awk` extraction with `gh release create --notes-file`), never from auto-generated commit summaries. Operators reading the GitHub Releases page see narrative notes — including `### BREAKING` callouts — that match what they read in the file.

Sounds obvious but most projects have the Releases page diverge from CHANGELOG within a few releases, and recovery is hard. The convention was baked in at v0.1.0 by writing the release procedure into `docs/deployment/versioning.md` *before* any release went out.

**Corollary:** every PR adds its own bullets to `[Unreleased]` as part of the diff. There is no "release notes meeting" to remember. The PR body, the CHANGELOG entry, and the git tag annotation are the same prose, written once.

### Self-dogfood the work as it ships

Wrote the SemVer release runbook, then immediately ran it on the retroactive v0.1.0 + v0.9.0 tags. The `awk` extractor was the first thing tested — **the runbook was operator-validated before any external operator existed.**

The doctor's `check_provider_keys` reuses `_llm._get_key()` so doctor's verdict and runtime behavior cannot disagree. **The correctness ratchet runs through the test suite.**

Pattern: when shipping operator-facing tooling (CLI commands, runbooks, doctor checks), the first operator to validate it is *you*, immediately, on real artifacts. Codex found bugs IN the SemVer docs as they were being written by reading them cold — caught months earlier than an external operator would have caught them.

### Scope discipline — file inline, don't accumulate

When something surfaces that isn't the current task — a missing top-level subcommand, personal references that need cleanup, a follow-up to a fix — file it as a separate GitHub Issue and keep the current PR clean.

**File inline as part of completing the parent task.** Don't ask the maintainer to do it. By the time they next look, the scope-creep has a bug number. There is no "we should track that" debt — there is "issue #N has it queued."

### Reversible vs irreversible — different gates

Local edits, branches, commits, running tests against tmp dirs — all reversible, all auto-shipped without confirmation. **Pushing tags, merging PRs, creating GitHub Releases, force-pushes — all require explicit approval.**

The line is action-reversibility, not user-friction-minimization. Auto mode does not override it. When tags are created locally for retroactive versioning, the distinction "created locally; not yet pushed" is load-bearing — it means the operator can still rename, retag, or abandon.

### The handoff is intentional

`~/ObsidianVault/Atomic Agents/RESUME-NEXT-SESSION.md` exists because the previous session wrote it. The next session that opens this repo doesn't have to reconstruct context — it has a self-contained brief pointing at the four key files (CHANGELOG, ROADMAP, the spec doc establishing the protocol pattern, the GitHub issue list filters), explaining the conventions established this session, and recommending a starting point.

**The handoff cost is paid by the session that's leaving, not the session that's arriving.**

When you finish a session that established a convention, shipped a non-trivial PR, or made architectural decisions — update RESUME-NEXT-SESSION.md before closing out. It's not optional polish; it's how institutional memory survives across the maintainer's sessions and eventually beyond them.

### What this method does NOT optimize for

Naming the trade-offs explicitly so future sessions don't try to "optimize" by skipping:

- **Maximum velocity.** A 5-round review cycle is slower than a 1-round cycle. The compensation is shipped correctness, not raw throughput.
- **Cheap reviews.** Each adversarial round is real spend (Opus subagent tokens; Codex tokens if re-instated). The compensation is 9-11 P2 findings closed pre-merge per non-trivial PR — issues that would otherwise be field bugs.
- **Brevity.** PR bodies are large. CHANGELOG entries are detailed. Spec docs are exhaustive. The compensation is durable institutional memory.

If the project ever needs to optimize differently, `docs/methodology.md` is the honest description of the current trade-offs being accepted. **Don't quietly drop one of these to ship faster.** Name it, and write down the decision.

---

## Where things live

| Doc | Purpose |
|-----|---------|
| `docs/architecture.md` | Mental model in diagrams. Read first. |
| `docs/protocols-shipped.md` | Per-protocol summary of the sixteen shipped backends — reference impls, capabilities, operator overrides, doctor checks, Implementer Contracts, and the cliff each closes. |
| `docs/spec/01-...43-journal-backend.md` | Locked spec (42 docs today, 34 locked + 8 DRAFTs/RFCs at spec/26 (cascade bundle), spec/30 (responsibility audit), spec/35 (init wizard), spec/37 (serve), spec/39 (otel-export), spec/41 (goal-backend), spec/42 (outcome-backend), and spec/43 (journal-backend)). The product. |
| `docs/implementation/` | Build guides per runtime (cron, Claude skill, dashboard) |
| `docs/deployment/versioning.md`, `upgrading.md` | SemVer + operator runbook |
| `docs/deployment/release-runbook.md` | Maintainer `/ship` runbook: two-mode workflow + manual surface check |
| `docs/methodology.md` | Working-methods retrospective; the methods that produced this codebase |
| `docs/TENSIONS.md` | Architectural tensions; protect against silent drift |
| `docs/GOVERNANCE.md` | Solo / small-team operator guide |
| `~/ObsidianVault/Atomic Agents/ROADMAP.md` | Strategic narrative + live backlog table |
| `~/ObsidianVault/Atomic Agents/RESUME-NEXT-SESSION.md` | Cross-session state pointer |
| GitHub Issues | Executable backlog |

---

## When in doubt

**Before proposing an architectural change** — read TENSIONS.md. The tension you're about to resolve may already be tracked, with the why-it's-load-bearing already articulated. Either build on that thinking or update the entry.

**Before adding a new code path that touches storage** — ask "should this be a protocol method?" The answer is usually yes if it's the second time the framework needs to do this thing.

**Before adding a config field** — ask "does this fit the markdown-as-config aesthetic, or am I leaking complexity outward?" If outward, push back on the feature shape, not the config shape.

**Before optimizing for the home user** — ask whether the change makes the org case worse.

**Before optimizing for the org case** — ask whether the change makes the home user case worse.

**Before merging a non-trivial PR** — Opus adversarial review on scope, then implementation, in rounds (rule 11). Both pre-merge.

**Before claiming a feature is done** — verify against the spec doc, run the test suite, check the dashboard renders correctly if observability is touched.

**When two paths look equally good** — pick the one that adds fewer concepts. The framework already has plenty.

---

## What we're explicitly NOT building

From `ROADMAP.md` §"What I'd NOT pursue", repeated here so future-Claude doesn't propose them as new ideas:

- **Agentic graph workflows** (LangGraph's territory). We don't compete on flexibility there.
- **TypeScript port.** One ecosystem at a time. Port later if community demand justifies.
- **Domain-specific agents shipped officially** (e.g., "atomic-finance-advisor" as a first-party package). Marketplace's job, not ours.
- **Multi-modal capture by us** (voice, image). MCP servers provide this. We'd duplicate the ecosystem.
- **Graphify-style knowledge-graph integration** ([graphify.net](https://graphify.net/)'s territory). INDEX-driven recall is the load-bearing memory pattern (spec/02 + spec/04); "visualize the whole graph" is the opposite aesthetic. Wrong substrate too — graphify's strength is code semantics (Tree-sitter + LLM extraction), not YAML + JSONL. Three lightweight in-framework dashboard improvements are filed as alternatives (#138 goal timeline, #139 delegation cost treemap, #140 spec cross-reference Mermaid diagram).

These are not forbidden forever — they're explicitly deferred with rationale. If someone proposes one, the burden of proof is "what changed since the rationale was written."

---

## Status

**v1.1.0, stable, PUBLIC.** Core runtime stable. Test suite: run `uv run pytest --collect-only -q | tail -1` for the live count (last refresh: 4,252 tests collected, 2026-06-12). Full CI runs against `uv sync --extra dev --extra openai --extra validation --extra redis`. **Twelve backend protocols locked at v1.0; SecretBackend (#340), the thirteenth, shipped for v1.5 (both PRs merged, spec/38 LOCKED, Filesystem + GCP Secret Manager reference impls); GoalBackend (#425), the fourteenth, shipped for v1.5 (DRAFT spec/41, FilesystemGoalBackend reference impl); OutcomeBackend (#426), the fifteenth, shipped for v1.5 PR 1 (DRAFT spec/42, FilesystemOutcomeBackend reference impl); JournalBackend (#427), the sixteenth, shipped for v1.5 PR 1 (DRAFT spec/43, FilesystemJournalBackend reference impl, ADOPT-NOW all three read sites)** — see `docs/protocols-shipped.md` for the per-protocol summary (reference impls, capabilities, operator overrides, doctor checks, Implementer Contracts, and what cliff each closes):

| # | Protocol | Issue / Lock | Reference impls |
|---|----------|--------------|-----------------|
| 1 | MemoryBackend | #57 + #382 PR 1 (override surface) | Filesystem |
| 2 | LLMBackend | #87 | Anthropic + OpenAI + Moonshot + Vertex Gemini |
| 3 | JudgeBackend | #112 PR 4 | PolicyJudge + LLMJudgeBackend |
| 4 | LockBackend | #60 PR 4 | Filesystem + Redis |
| 5 | LogBackend | #61 PR 4 | Filesystem + SQLite + Postgres |
| 6 | AgentProfileBackend | #63 PR 4 | Filesystem + SQLite |
| 7 | ToolRegistryBackend | #64 PR 4 | Filesystem + SQLite |
| 8 | PolicyBackend | #89 PR 4 | Filesystem |
| 9 | MandateBackend | #124 PR 4 | Filesystem |
| 10 | PersonaBackend | #62 PR 4 | Filesystem |
| 11 | CorpusBackend | #65 PR 4 | Filesystem + SQLite (FTS5) |
| 12 | MCPServerRegistryBackend | #201 PR 5 | Filesystem + HTTP (tier-1/2/3) |
| 13 | SecretBackend | #340 PR 2 (LOCKED) | Filesystem + GCP Secret Manager |
| 14 | GoalBackend | #425 PR 1 (DRAFT spec/41) | Filesystem |
| 15 | OutcomeBackend | #426 PR 1 (DRAFT spec/42) | Filesystem |
| 16 | JournalBackend | #427 PR 1 (DRAFT spec/43) | Filesystem |

MCP client support shipped (PRs #55 + #56). All twelve backend protocols shipped; v1.0.0 released 2026-06-04. SecretBackend (#340) shipped for v1.5 (both PRs merged, spec/38 LOCKED, Filesystem + GCP Secret Manager reference impls). GoalBackend (#425) shipped for v1.5 (DRAFT spec/41, FilesystemGoalBackend reference impl). OutcomeBackend (#426) shipped for v1.5 PR 1 (DRAFT spec/42, FilesystemOutcomeBackend reference impl — write-path wiring deferred to #448). JournalBackend (#427) shipped for v1.5 PR 1 (DRAFT spec/43, FilesystemJournalBackend reference impl — ADOPT-NOW: all three read sites wired this PR). Single-developer project; reference implementation that anyone can use, fork, or extend.

Going forward: **the elegance is the product.** Protect it.
