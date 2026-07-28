# CLAUDE.md — atomic-agents-stack

This file loads in every session for this repo. It captures the *design ethos* of the framework — what to protect as it scales, what to refuse, how to make decisions that keep the codebase coherent. Project-specific tactical rules (test commands, branch shape) sit alongside.

For broader context, read these in order on a fresh session:
- `docs/architecture.md` — the mental model in diagrams
- `~/ObsidianVault/Atomic Agents/ROADMAP.md` — strategic narrative + live issue links
- `docs/TENSIONS.md` — architectural tensions that must survive scaling
- `~/ObsidianVault/Atomic Agents/RESUME-NEXT-SESSION.md` — current session-state pointer

---

## What this is

Atomic Agents is a vault-native AI agent framework: agents live as plain markdown files, the runtime is stateless, and storage is moving toward swappable protocols layer by layer. **Twelve backend protocols shipped through v1.0; twenty-two total as of v2.0.0** — see `docs/protocols-shipped.md` for the per-protocol summary (reference impls, capabilities, operator overrides, doctor checks, Implementer Contracts, and what cliff each closes).

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
                  SecretBackend ✅ (#340 PR 2, LOCKED spec/38 — the 13th, v2.0.0)
                  GoalBackend ✅ (#425 PR 1 + #448 PR1/PR2/PR3 arc-closer + #483 PR1 clock-injection/shim/agent_root + #496 PR1 backend-universe alignment + #642 PR1 multi-goal/create_goal RE-LOCK + #581 PR2 gate-status RE-LOCK + #582 PR3 expected_decision_id CAS RE-LOCK, LOCKED spec/41 — the 14th, v2.0.0)
                  OutcomeBackend ✅ (#426 PR 1 + #448 PR2 write-path, LOCKED spec/42 — the 15th, v2.0.0)
                  JournalBackend ✅ (#427 PR 1 + LOCK PR, LOCKED spec/43 — the 16th, v2.0.0)
                  QueueBackend ✅ (#428 PR 1 + LOCK PR + #582 PR3 enqueue() RE-LOCK, LOCKED spec/44 — the 17th, v2.0.0)
                  IdempotencyBackend ✅ (#520 PR 1 + PR 2 arc-closer, LOCKED spec/45 — the 18th, v2.0.0)
                  EmbeddingBackend ✅ (#200 PR 2 + #544 PR1/PR2a/PR2, LOCKED spec/46 — the 19th, v2.0.0)
                  ConversationBackend ✅ (#535 PR 1 + LOCK PR, LOCKED spec/47 — the 20th, v2.0.0)
                  PrincipalBackend ✅ (#556 PR 1 + LOCK PR, LOCKED spec/48 — the 21st, v2.0.0)
                  AgentRegistryBackend 🟡 (#607 PR 1, DRAFT spec/51 — the 22nd, v2.0.0)
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

For any non-trivial PR — especially backend protocols, framework refactors, anything touching `agent.call()`, `_capture.py`, `_costs.py`, `_locks.py`, the protocol surfaces, AND even docs-only PRs — run **2-5 Opus adversarial rounds pre-merge**, not one thorough pass. **Which models review is set in `.gstack/arc.config.jsonc`, not here.** That file is what the loop actually executes, so it is the authority; this section describes the practice, never the roster. Read `crossFamily` there for the current answer. The full reviewer-roster rationale is in `docs/methodology.md` §"Reviewer roster — what the project actually does".

The non-obvious property: **each round catches different things.** Not because the reviewer "tries harder" the second time. Because each fix changes the diff and exposes new edges. Recent track record: PR #75 (`doctor`) — 3 rounds, 9 P2 findings closed. PR #76 (SemVer policy) — 5 rounds, 11 P2 findings closed. Round 5 of #76 was the only round that flagged the `No migrations needed` claim — earlier rounds had cleared the diff that contained it. **PR #206 (#64 PR 4, docs-only) — 2 rounds, 11 findings + 1 new successor issue (#207). Round 2 caught a count-drift the round-1 fix commit itself introduced.** Rounds-not-passes holds even when the diff has no code.

A different-model-family reviewer catches what same-family blind spots miss, which is why `crossFamily` exists in the config. If the configured cross-family CLI is unreachable, the loop falls back to a fresh same-family read and flags `crossFamilyReviewed:false` rather than pretending the round was cross-family. **The wrong version of this practice is "ask Claude to imagine being a reviewer." That's prompting; this is verification — the subagent gets a fresh context, reads the diff itself, and runs its own commands.**

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

Run `uv run pytest --collect-only -q | tail -1` for the live test count (last refresh: 6,259 tests collected, 2026-06-28). New backend protocols add ~25 conformance + ~10 impl-specific tests. New features ship with tests. Migration-shaped PRs need parameterized fixture tests across the backend protocol — the conformance suite is what keeps the protocol honest.

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
| `docs/protocols-shipped.md` | Per-protocol summary of the twenty-two shipped backends — reference impls, capabilities, operator overrides, doctor checks, Implementer Contracts, and the cliff each closes. |
| `docs/spec/01-...57-agent-detail.md` | Locked spec (56 docs today, 44 locked + 12 DRAFTs/RFCs at spec/26 (cascade bundle), spec/30 (responsibility audit), spec/37 (serve), spec/39 (otel-export), spec/49 (deploy), spec/51 (agent-registry-backend), spec/52 (fleet-console), spec/53 (fleet-console-scoring), spec/54 (fleet-console-recommendations), spec/55 (fleet-management-cli), spec/56 (fleet-monitor), and spec/57 (agent-detail)). The product. |
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

**v2.0.0, stable, PUBLIC.** Core runtime stable. Test suite: run `uv run pytest --collect-only -q | tail -1` for the live count (last refresh: 6,259 tests collected, 2026-06-28). Full CI runs against `uv sync --extra dev --extra openai --extra validation --extra redis`. **Twelve backend protocols locked at v1.0; SecretBackend (#340), the thirteenth, shipped in v2.0.0 (both PRs merged, spec/38 LOCKED, Filesystem + GCP Secret Manager reference impls); GoalBackend (#425 + #448 PR1/PR2/PR3 + #483 PR1 + #496 PR1 + #642 PR1 RE-LOCK + #581 PR2 RE-LOCK + #582 PR3 RE-LOCK), the fourteenth, shipped in v2.0.0 (LOCKED spec/41 — RE-LOCKed at 14 MUSTs; 13 in #642 PR1, MUST 14 in #582 PR3; FilesystemGoalBackend reference impl + goal-outcome coordinator + fail-closed cost gate + CAS guard + clock injection + GoalManager thin shim + agent_root resolution + backend-universe alignment: coordinator threads log/policy/profile backends into OutcomeRunner; #642 PR1 RE-LOCK: create_goal()/list_goals() on GoalBackend Protocol, AddressableGoalBackend Protocol + for_goal() scope handle, GoalCapabilities.supports_multi_goal, GoalAlreadyExists exception, export() fail-loud guard while addressed goals present (#643 deferred), GoalManager.for_goal() scope-binding handle; 74 new conformance tests TEST 60–129 (TEST 113 parametrized ×5, so 70 TEST-number labels = 74 collected functions); #582 PR3 RE-LOCK: apply_transition() expected_decision_id CAS under goal lock (MUST 14), save_goal() per-goal lock (#655 closed), SubGoal.held_conflict_keys; +1 conformance test TEST 64); OutcomeBackend (#426), the fifteenth, shipped in v2.0.0 PR 1 + #448 PR2 write-path adoption (LOCKED spec/42, FilesystemOutcomeBackend reference impl); JournalBackend (#427), the sixteenth, shipped in v2.0.0 PR 1 + LOCK PR (LOCKED spec/43, FilesystemJournalBackend reference impl, ADOPT-NOW all three read sites wired at PR 1); QueueBackend (#428 + #582 PR3 RE-LOCK), the seventeenth, shipped in v2.0.0 PR 1 + LOCK PR + #582 PR3 RE-LOCK (LOCKED spec/44 — RE-LOCKed at 13 MUSTs in #582 PR3; FilesystemQueueBackend reference impl; closes TENSIONS T4; enqueue() producer primitive (MUST 13) wired in conductor conflict-queue advisory; SCAFFOLDING-ONLY designation RETIRED; 143 tests (68 conformance + 75 filesystem-specific), 13 MUSTs; runtime adoption deferred to #469); IdempotencyBackend (#520), the eighteenth, shipped in v2.0.0 PR1+PR2 arc-closer (LOCKED spec/45, FilesystemDedupLedger reference impl; PR2 wires agent.call(idempotency_key=...) with two-phase gate (W1–W7): lookup() BEFORE lock → COMPLETED short-circuit; begin() AFTER cost gate → COMPLETED short-circuit (W7, the lookup→commit→begin race), IN_FLIGHT raise, or FRESH claim; commit() after JSONL write; release_lease() in finally; spec/22 versioned normative addendum: cost_usd ABSENT on deduped/in_flight records; RunRecord idempotency_key+replayed_run_id audit fields persisted+queryable on all 3 reference log backends (Filesystem, SQLite, Postgres); SQLite + Postgres v1→v2 migration; serve HTTP 200 deduped (result_ref, no inlined output) / HTTP 409 in_flight; cron_tick_key (tz-aware-required) + extract_queue_idempotency_key trigger helpers; dedup_body_hash_enabled opt-in; full agent.call() two-phase-gate integration test suite with per-invariant negative controls); EmbeddingBackend (#200), the nineteenth, shipped in v2.0.0 PR 2 + #544 PR1/PR2a/PR2 (LOCKED spec/46, OpenAIEmbeddingBackend reference impl; EMBEDDING_PRICING table + calc_embedding_cost() isolated from chat PRICING; 9-MUST Implementer Contract (backend) + 4 gate-site normative MUSTs (caller) including MUST-NOT-RAISE + len(out)==len(in) conformance invariants; registry + pgvector wiring + input_type kwarg shipped PR3 (#200); #544 PR1: PRIMITIVE_EMBED = 'embed' + 4 trigger mappings, batch embed cost gate at capture-commit site in agent.call() — gate resolves the pricing model_id from MemoryCapabilities.embedding_backend_resolved.model_id (NOT the embedding_provider provider-label), estimates tokens on UTF-8 BYTE length (never under-reserves multibyte/CJK), and ENFORCES the cap (raises CostGuardrailBlocked when worst-case reservation > remaining headroom via _embed_remaining_headroom, not audit-only); _has_effective_embed_cap() with full Policy+tree-cap resolution, check_embedding_backend() doctor check WIRED into run_doctor() (tri-state api_key_probe), spec/20+spec/22+spec/34 versioned normative addenda (embedding_provider documented as a provider LABEL, consistent with spec/46 + backend.py), embed-gate refusal writes a terminal CostGuardrailBlocked audit record (status=error, embed_batch_blocked marker, carries the chat cost_usd so the spend lands in the ledger — Principle #5 + the #495/#497/#498 under-counting guard), 39 new tests with per-invocation strip negative controls; #544 PR2 shipped the query-embed gate at the CLI corpus-query path (#564), NOT an agent.call() interceptor); #544 PR2a: dedicated embed_cost JSONL record (cost_usd=actual_usd, cost_source='actor', model, cost_estimated, parent_run_id, parent_agent) emitted after embed_batch_release conditioned on actual_usd > 0 — now sum_cost_for_period folds prior embed spend across calls ('embed_cost': PRIMITIVE_EMBED in _PRIMITIVE_BY_TRIGGER, double-count guard tested); merge-write pre-read reservation now calls read_note(merge_into) before the write loop and sizes from the PRESERVED TARGET body so an over-cap merge is refused before billing (Principle #4); spec/22 versioned normative addendum §"embed_cost cross-call accounting record"; 18 new PR2a tests (57 total in test_embed_cost_gate.py, incl. empty-/missing-merge-target fragment-fallback compound-boolean strip controls); /ship cross-family review corrected the #566 docstring (same-batch under-count NOT bounded by the 2x fan-out buffer — sub-cent but a genuine ledger under-count) + filed #567 (embed headroom excludes in-flight chat spend) / #568 (crash-window between release and embed_cost records); #544 PR2 shipped the CLI corpus-query query-embed gate (_corpus_query: embed_reservation/embed_release/embed_cost in try/finally + --critical bypass + fail-closed headroom check; single-call ceil(utf8_bytes/3), no fan-out, model_id resolved off EmbeddingCapabilities.embedding_backend_resolved) + 4 gate-site normative MUSTs (backend Implementer Contract stays at 9) + direct-caller gate boundary (#586) + pgvector conformance fold (@requires_postgres, StubEmbeddingBackend, CI-only) + spec/46 DRAFT→LOCKED); ConversationBackend (#535), the twentieth, shipped in v2.0.0 PR 1 + LOCK PR (LOCKED spec/47, FilesystemConversationBackend reference impl; `atomic_agents/conversation/` package; 10-MUST Implementer Contract; model-aware token budget derivation (max(1000, context_limit - sys_tokens - max_output - 2000), fail-soft to 8000); write-back gated on not _response_deferred (#553 fix); model.md Conversation Backend field LOCKED; check_conversation_backend() doctor dual-probe wired; spec/22 conversation_id addendum; spec/40 ConversationBackend export addendum; 142 conversation tests (wiring/conformance/filesystem) + 11 doctor tests = 153 total; PostgresConversationBackend deferred to Phase 3); PrincipalBackend (#556), the twenty-first, shipped in v2.0.0 PR 1 + LOCK PR (LOCKED spec/48, LocalPrincipalBackend + StaticClaimsPrincipalBackend reference impls; HARD-REFUSE gate in agent.call() placed BEFORE the idempotency dedup lookup (MUST 10) so an unverified caller cannot replay a victim's cached run via a guessed idempotency_key; serve HYBRID flow is fail-closed opt-in — raw identity header NOT trusted by default (ServeConfig.identity_is_perimeter_verified ANDed with non-loopback bind), present-but-untrusted identity yields is_verified=False → HARD-REFUSE; doctor check WIRED into run_doctor() with fail-closed negative probe + DSN-credential redaction (safe_backend_id at all echo sites); 12-MUST Implementer Contract; security review (Claude + cross-family Codex) added four fail-closed fixes (cross-tenant collapse guard when an is_local_only backend runs under perimeter-trust; storage-key ASCII-whitespace-only strip so Unicode-space subjects don't collide, MUST 11; full-untruncated raw_identity as the authz sub not the 512-char audit copy; header-absent-under-perimeter → unverified not LOCAL_PRINCIPAL), each strip-RED negative-control-tested; LOCK ceremony: derive_storage_key made private; full 12-MUST conformance-test map (MUST 2/3/9 object-identity tests); explicit run_id in principal-refused JSONL record (Principle #5); raw-token self-verifying backend + role-based authorization deferred); AgentRegistryBackend (#607), the twenty-second, shipped in v2.0.0 PR 1 (DRAFT spec/51, FilesystemAgentRegistryBackend reference impl; `atomic_agents/agent_registry/` package; fleet-root agent enumeration by the spec/37:314 model.md-present predicate + governance.md schema (embedded YAML block: owner/permission_tier/customer_data/writes_sor/lifecycle_status + review/risk/sources/actions sub-records; five-state parse ABSENT/PRESENT_VALID/PRESENT_INVALID/PRESENT_NO_BLOCK/PRESENT_UNREADABLE, fail-soft PRESENT_INVALID carries only parse_errors); ATOMIC_AGENTS_AGENT_REGISTRY_BACKEND env override fails LOUD on unknown id; FULL ADOPT-NOW — dashboard discover_agents() rewired through registry.list_agents(include_governance=False) with read-only fail-soft fallback to the filesystem default, check_agent_registry_backend() WIRED into run_doctor() with a bidirectional reconcile sub-probe against the profile backend, atomic-agents init writes a governance.md stub (placeholder fixed to the locked ${agent_name} brace form — the templates shipped a bare $AGENT_NAME that safe_substitute left literal); 37 conformance + 39 filesystem + 2 dashboard-costs predicate-change guards + 1 init-template-render-regression test (79 new), incl. two MUST-5 per-entry fail-soft negative controls + an unquoted-tristate YAML-boolean-coercion happy-path guard + a get_agent `_`/`.`-prefix-exclusion guard (MUST 3 consistency with list_agents) (#607)). Conductor — durable, resumable playbook orchestration (LOCKED spec/50, #575 design): PR1 (#580) shipped `atomic_agents/conductor/` package: `run()` free-function (re-entrant: same `conductor_run_id` resumes from ledger), `discover_playbooks()`, `validate_playbook_manifest()`, `ConductorState`/`PlaybookManifest`/`StageSpec` types; PLAYBOOK.md vault-native schema; sequential automated stages; goal-ledger resume cursor (full C2 predicate: complete+resolvable-result skipped, blocked-normalized, abandoned-is-terminal, in_progress/(re-)dispatched); run-cap tree-cap pinned at create; 56 tests. PR2 (#581) shipped: `resume()` public entry point (answers a suspended gate and continues); `GateDecision` frozen dataclass (the ONE genuinely new artifact — spec/50 §63: decision_id, stage_id, prompt, options, context_ref, held_conflict_keys, disposition, answer, answered_by, answered_at, rationale); gate suspension path in `run()` (is_gate=True stages transition sub-goal to `awaiting_decision`, return ConductorState(status='awaiting_decision', pending_decision=GateDecision(...))); stale/duplicate-answer rejection (CAS via apply_transition expected_from_status='awaiting_decision' + gate_decision_id field check — c5-stale-duplicate-rejection ruling); disposition typed field NOT magic-word-sniffed; CLI `resume` subcommand; spec/41 RE-LOCKed (gate statuses addendum: `awaiting_decision` + `skipped` added to VALID_SUB_GOAL_STATUSES, `gate_decision_id` added to SUB_GOAL_TRANSITION_FIELDS, TEST 60–63 in test_goal_backend_conformance.py); spec/12 amended (new statuses in sub-goal status enum); 73 tests collected (net +17 vs PR1's 56). PR3 (#582) shipped: per-run LockBackend lease (run() acquires conductor_run_id lease non-blocking, finally releases); conflict_keys on gate StageSpec (validated, included in playbook fingerprint, rejected on automated stages); conductor-conflict-scan shared lease serializes scan+suspend/queue decision; queue-behind-decision: run with overlapping conflict keys calls queue_backend.enqueue(role=<blocking_decision_id>, ...) + appends conductor_run_queued goal-ledger event → returns ConductorState(status='deferred', queued_behind_decision_id=...) (status value 'deferred', not 'queued', to avoid the QueueBackend queue-dir vocabulary collision); self-release: next run() checks _is_decision_still_pending(), appends conductor_queue_released, continues; resume() does NOT push-release queued runs; spec/41 RE-LOCKed (MUST 14: apply_transition expected_decision_id CAS under goal lock, closes #660; save_goal per-goal lock, closes #655; SubGoal.held_conflict_keys; +1 conformance test TEST 64); spec/44 RE-LOCKed (MUST 13: enqueue() producer primitive, SCAFFOLDING-ONLY RETIRED; leading-dot item_name rejection; +6 conformance tests TEST 57–62; 143 tests total, 13 MUSTs); 96 tests collected in tests/test_conductor.py (net +40 vs PR1's 56). PR4 (#583) shipped: C7 launder-guard upgraded from warn to ConductorLaunderRefused(NestedDelegationRefused) hard raise on both run() and resume() for trigger=='delegate' — fires BEFORE any durable work (no goal created in run(), no gate mutated in resume()); structural call-depth deferred to #665; check_conductor(agent_root) orchestration-layer doctor check wired into run_doctor() (dual-probe: light scan for conductor_run_started events + heavy read-only cursor walk on most-recent run, PASS-with-0-runs not SKIP, PASS/WARN/FAIL); ConductorLaunderRefused exported at top-level atomic_agents package; 4 C7 conformance tests in test_conductor.py + 7 doctor tests in test_doctor.py; spec/50 DRAFT→LOCKED (LOCK ceremony: 111 tests collected, C1–C9 all normative, no inline TODOs, addendum added); the C7 complete-stage doctor probe distinguishes an answered gate from an automated stage by the durable conductor_gate_answered audit (NOT the cleared gate_decision_id), so a human-gated happy path does not false-WARN; check_conductor most-recent selection orders by a (has_ts, ts) key so a ts-bearing run always outranks a ts-less one (mtime is a ts-less tiebreaker only); 111 tests collected in tests/test_conductor.py (net +55 vs PR1's 56). Reference playbook (#584, 2026-06-28): `docs/samples/dev-lifecycle/skills/dev-lifecycle-playbook/PLAYBOOK.md` (18 conductor stages — 10 automated + 8 gates — encoding kit stages 1–11; discoverable via `discover_playbooks()`; TEST 57 parse-guard, TEST 58 e2e integration, TEST 59 merge:main conflict serialization; non-normative §Reference playbook addendum in spec/50; code-comment updates in run.py/types.py retargeting `deferred to #584` references to tracking issues #671/#672); 114 tests collected in tests/test_conductor.py (net +58 vs PR1's 56). #668 (2026-06-28): per-stage actor-model dial wired end-to-end — `StageSpec.model` applied at dispatch via the four-link chain: `_dispatch_stage` → `dispatch_sub_goal_as_outcome(actor_model=)` → `OutcomeRunner(actor_model=)` → `agent.call(model_override=actor_model)`; gate stages reject `model:` at parse (hard error, symmetric with `conflict_keys`); WARN-not-block for unknown model at parse (runtime LLMBackend is authoritative); model in fingerprint conditionally (non-None only, same pattern as `conflict_keys`); 8 gate `model:` lines stripped from PLAYBOOK.md; UserWarning 'NOT YET APPLIED' block removed from run.py; C10 added, spec/50 RE-LOCKed (C1–C10 all normative); 122 tests collected in tests/test_conductor.py (net +66 vs PR1's 56); test_goal_coordinator.py +1 (actor_model wiring guard), test_outcome.py +1 (per-iteration tree-cap under model_override). NOT counted in the 22-backend protocol table — conductor is an orchestration layer, not a backend protocol. Fleet Observability Console PR1 (#614, DRAFT spec/52) — Operator Attention Queue + three-axis Cost/Quality/Reliability trend panels + ack/snooze POST endpoints + alert state JSONL sidecar; `atomic_agents/dashboard/alert_state.py` + `attention.py`; 8-MUST console contract; 63 new tests. Fleet Console PR2: Health Scoring Engine (#615, DRAFT spec/53) — pure-compute Fleet Health Score (0-100) decomposed into Cost/Quality/Reliability sub-scores; critical-axis cap; `atomic_agents/advisor/` package (`score.py`, `targets.py`); 10-MUST Implementer Contract; 97 new tests. Fleet Console PR3: Recommendations Engine (#616, DRAFT spec/54) — pure-compute OBSERVE-ONLY recommendations (`atomic_agents/advisor/recommend.py`); savings_cost / quality_report / governance rec kinds; composite conjunctive no-quality-cost guard; same-family downgrade candidate map; point-impact counterfactual scoring; 11-MUST Implementer Contract; 64 new tests; folds #623 display-integer band consistency + equal-length 7d WoW window fixes (spec/53 MUSTs 11-13). Cost-read fail-closed posture (#495 PR1, internal refactor — `sum_cost_for_period` returns `CostReadResult(total_usd, degraded, dropped_records)`, two-tier blind-detect, spec/09 amended); LogBackend read-failure contract (#497 PR1 — typed `LogBackendReadError` raised by all 3 reference backends from query/tail/aggregate on unrecoverable read failure, empty/absent → [] boundary preserved, cost-reader + mandate spend-gate fail-closed, dashboard/dream degrade gracefully, spec/22 versioned normative read-failure addendum outside the 8-MUST count); dashboard degraded-read banner (#498 — `cost_data_degraded: bool` field on `GlobalSummary` + `AgentDashboardData`, OR-composed across every cost-view read; non-blocking "data may be incomplete" banner on global + per-agent cost views; spec/09 LOCKED reworded to describe shipped path); workflow_id cross-run correlation field (#622 PR1 — `RunRecord.workflow_id` + `LogQuery.workflow_id` spec/22 versioned normative addendum; stamps all 9 terminal JSONL sites + helper + embed_cost records; threads through delegate into the child's call(); coordinator mirror record NOT stamped (count-once enforcement); `WorkflowSummary` + `aggregate_workflow(agents_root, workflow_id)` added to `dashboard/costs.py` + re-exported from `dashboard/__init__`; `'workflow_id'` added to `_CANONICAL_FIELDS`; SQLite + Postgres bumped to v4 (v3→v4 migration adds workflow_id TEXT column + idx_workflow_id partial index); 24 new tests in test_workflow_aggregate.py + 2 new conformance tests in test_log_protocol_conformance.py + SQLite/Postgres v3→v4 migration isolation tests). Fleet Console home rebuilt as a composable panel-registry cockpit (#635, DRAFT spec/52 §16-§18) — panel-registry layout engine composing registered self-contained panels; STATUS/ACT/EXPLORE zone layout (KPI strip with Fleet-Health tile, Operator Attention Queue, 3-axis Advisor scorecard, fleet-status summary with navigable deep-links to the Monitor); layered recommendation tags (spec/52 §17.3); shared `status_for_agent()` (spec/52 §17.1); pure-compute, zero LLM spend. Fleet Monitor — NOC-wall roster page (#653, DRAFT spec/56) — the console's second surface (Cockpit home → Monitor → detail); every agent as a monitored entity, problems-first; dual List/Cards view; OK/WARN/ERROR/STALE summary bar + filters; cost sparklines; freshness stamp + full-page auto-reload (MUST 13); `?status=` deep-link arrival; Monitor tab in shared top nav; per-row fail-soft; 39 conformance tests (13 MUSTs). Per-Agent Detail Cockpit (#637, DRAFT spec/57) — the console's third surface (home → Monitor → detail); Fable "Briefing" layout in B7 palette; banner + spec/51 governance block (five states) + agent-tab telemetry tabs (Overview/Cost/Activity/Quality/Memory/Goals/Dreaming/Efficiency) composed via registered `agent-tab` panels through `compose_agent_detail()` (spec/52 §16); restores Dreaming view (#684) rendering real `dreams/*/manifest.json` + `report.md` fields; shared `status_for_agent()` + FleetHealth co-rendered off the same snapshot as the Monitor (spec/57 MUST 5); replaces `<agent>/dashboard.html` content (path + route kept); 43 conformance tests (10 MUSTs). Cost-optimization advisory change (#687, spec/53 MUST 14) — premium/verbose agents no longer scored as ERROR; `savings_cost` recs are advisory, not health-degrading (spec/52 MUST 14 added). Recs standalone zone + eval-score as percentage (#689/#690/#694) — recs zone moved to always-visible between governance block and tabs (B7 fidelity); empty-state "No recommendations right now"; eval score rendered as percentage on all surfaces (None → "—"). Fleet Management CLI DRAFT (spec/55, #624, #609) — the MANAGE layer of the Agent Fleet Platform (epic #606): four spine invariants S1–S4 (registry-as-selector, five-step safety routine validate→preview→confirm→atomic-write+snapshot→audit, copilot-first `--json`/`--dry-run`/`--yes`, `manage` command group); first verb `manage govern <agent>` (#609) — governance.md frontmatter editor written through `AgentRegistryBackend`, surgical line-aware preservation (M2), restorable snapshot (M3), LogBackend audit with `PRIMITIVE_MANAGE_GOVERN` (M8); 10-MUST Implementer Contract; PR1 implements flat-scalar `--set` slice (nested/list paths return a documented clean refusal). Implementation + conformance tests ship in the #624 PR1 build.** — see `docs/protocols-shipped.md` for the per-protocol summary (reference impls, capabilities, operator overrides, doctor checks, Implementer Contracts, and what cliff each closes):

| # | Protocol | Issue / Lock | Reference impls |
|---|----------|--------------|-----------------|
| 1 | MemoryBackend | #57 + #382 PR 1 (override surface) + #258 PR 1 (Postgres FTS) | Filesystem + Postgres (FTS, PR1 of 4-PR arc) |
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
| 14 | GoalBackend | #425 PR 1 + #448 PR1/PR2/PR3 arc-closer + #483 PR1 cleanup + #496 PR1 backend-universe alignment + #642 PR1 RE-LOCK (LOCKED spec/41) | Filesystem |
| 15 | OutcomeBackend | #426 PR 1 + #448 PR2 write-path (LOCKED spec/42) | Filesystem |
| 16 | JournalBackend | #427 PR 1 + LOCK PR (LOCKED spec/43) | Filesystem |
| 17 | QueueBackend | #428 PR 1 + LOCK PR (LOCKED spec/44) | Filesystem |
| 18 | IdempotencyBackend | #520 PR 1 + PR 2 arc-closer (LOCKED spec/45) | Filesystem (FilesystemDedupLedger) |
| 19 | EmbeddingBackend | #200 PR 2 + #544 PR1/PR2a/PR2 (LOCKED spec/46) | OpenAI (OpenAIEmbeddingBackend) |
| 20 | ConversationBackend | #535 PR 1 + LOCK PR (LOCKED spec/47) | Filesystem (FilesystemConversationBackend) |
| 21 | PrincipalBackend | #556 PR 1 + LOCK PR (LOCKED spec/48) | Local (LocalPrincipalBackend) + StaticClaims (StaticClaimsPrincipalBackend) |
| 22 | AgentRegistryBackend | #607 PR 1 (DRAFT spec/51) | Filesystem (FilesystemAgentRegistryBackend) |

MCP client support shipped (PRs #55 + #56). All twelve backend protocols shipped; v1.0.0 released 2026-06-04. SecretBackend (#340) shipped in v2.0.0 (both PRs merged, spec/38 LOCKED, Filesystem + GCP Secret Manager reference impls). GoalBackend (#425 + #448 PR1/PR2/PR3 + #483 PR1 + #496 PR1 + #642 PR1 RE-LOCK + #581 PR2 RE-LOCK + #582 PR3 RE-LOCK) shipped in v2.0.0 (LOCKED spec/41 — RE-LOCKed at 14 MUSTs; 13 in #642 PR1, MUST 14 in #582 PR3; FilesystemGoalBackend reference impl + goal-outcome coordinator + fail-closed cost gate + CAS guard — arc-closer PR3 merged 2026-06-13; clock injection + GoalManager thin shim + agent_root resolution + spec/41 addendum — #483 PR1 merged 2026-06-13; backend-universe alignment: coordinator now threads gate agent's log/policy/profile backends into OutcomeRunner so the runner's internal AtomicAgent spends in the same cost universe the pre-dispatch gate checked — #496 PR1 merged 2026-06-14; multi-goal addressing + create_goal RE-LOCK: create_goal()/list_goals() on GoalBackend Protocol, AddressableGoalBackend Protocol, GoalCapabilities.supports_multi_goal, GoalAlreadyExists exception, export() fail-loud guard while addressed goals present (deferred to #643), GoalManager.for_goal() scope-binding handle, 74 new conformance tests TEST 60–129 (TEST 113 parametrized ×5, so 70 TEST-number labels = 74 collected functions) — #642 PR1 merged 2026-06-26; expected_decision_id CAS under goal lock (MUST 14) + save_goal per-goal lock (#655 closed) + SubGoal.held_conflict_keys + 1 new conformance test TEST 64 — #582 PR3 RE-LOCK merged 2026-06-28). OutcomeBackend (#426 + #448 PR2) shipped in v2.0.0 (LOCKED spec/42, FilesystemOutcomeBackend reference impl; write-path adopted — OutcomeRunner routes through outcome_backend.write_result(); custom-output_dir result.json relocation fix; AtomicAgent.outcome_backend is the per-agent coordinator/inspection handle). JournalBackend (#427) shipped in v2.0.0 PR 1 + LOCK PR (LOCKED spec/43, FilesystemJournalBackend reference impl — ADOPT-NOW: all three read sites wired at PR 1). QueueBackend (#428 + #582 PR3 RE-LOCK), the seventeenth, shipped in v2.0.0 PR 1 + LOCK PR + #582 PR3 RE-LOCK (LOCKED spec/44 — RE-LOCKed at 13 MUSTs in #582 PR3; FilesystemQueueBackend reference impl; closes TENSIONS T4; enqueue() producer primitive (MUST 13) wired in conductor conflict-queue advisory; SCAFFOLDING-ONLY designation RETIRED; 143 tests (68 conformance + 75 filesystem-specific), 13 MUSTs; LOCK hardening: iterdir()-walk in export() (#477), no-replace mkdir + O_EXCL claim probe + non-FNF-rename placeholder cleanup (#478), O_NOFOLLOW sidecar writes (#479), MUST 10 fail-soft de-vacuoused + strip-RED (#476); runtime adoption deferred to #469). IdempotencyBackend (#520) shipped in v2.0.0 PR1+PR2 arc-closer (LOCKED spec/45, FilesystemDedupLedger reference impl; PR2 wires agent.call(idempotency_key=...) two-phase gate, RunRecord audit fields, spec/22 addendum, serve/queue/cron trigger helpers, dedup_body_hash_enabled, SQLite v1→v2 migration, ~90 new tests). EmbeddingBackend (#200) shipped in v2.0.0 PR 2 + #544 PR1/PR2a/PR2 (LOCKED spec/46, OpenAIEmbeddingBackend reference impl; `atomic_agents/embedding/` package; EMBEDDING_PRICING table + calc_embedding_cost() isolated from chat PRICING; 9-MUST Implementer Contract; reachable typed provider-unavailable branch + server-side dimension reduction honored against the model's NATIVE dimension (3-large defaults to 3072, not a global 1536) + MUST-1 construction validation; 112 new tests; registry + pgvector wiring shipped PR3 (#200); #544 PR1: PRIMITIVE_EMBED = 'embed' + 4 trigger mappings in _PRIMITIVE_BY_TRIGGER, batch embed cost gate at post-loop capture-commit site in agent.call() with _has_effective_embed_cap() (full Policy+tree-cap resolution, NOT model.md-only), fail-closed gate (degraded AND cap), release in finally with actual_usd = full per-written-note byte-token estimate (NOT conditioned on embed()-None; write_note() does not surface that signal, so a successfully-written note is charged the full per-note estimate — see the spec/22 + spec/46 release addenda; actual_usd is 0 only when no note was written), check_embedding_backend() doctor check (SKIP/PASS/WARN/FAIL) WIRED into run_doctor(), embed-gate refusal writes a terminal CostGuardrailBlocked audit record (status=error + embed_batch_blocked marker carrying the chat cost_usd, so a cost block is never silent in the ledger), spec/20+spec/22+spec/34 versioned normative addenda, 39 new tests; #544 PR2a: dedicated embed_cost JSONL record (cost_usd=actual_usd visible to sum_cost_for_period across calls) + merge-write pre-read reservation (sized from PRESERVED TARGET body, Principle #4 enforce-before-pay) + spec/22 versioned normative addendum + 16 new tests (57 total in test_embed_cost_gate.py); #544 PR2: CLI corpus-query embed gate (_corpus_query: embed_reservation/embed_release/embed_cost in try/finally + --critical bypass + fail-closed headroom check + single-call ceil(utf8_bytes/3) no-fan-out estimate priced off EmbeddingCapabilities.embedding_backend_resolved) + 4 gate-site normative MUSTs (backend contract stays at 9) + direct-caller gate boundary (#586) + pgvector conformance fold (PgvectorMemoryBackend + PgvectorCorpusBackend into shared @_parametrize_backends suites via deterministic StubEmbeddingBackend, @requires_postgres CI-only) + spec/46 DRAFT→LOCKED + 24 new tests in test_corpus_query_embed_gate.py (per-invocation negative controls strip each of the three fail-closed predicate parts)). ConversationBackend (#535) shipped in v2.0.0 PR 1 + LOCK PR (LOCKED spec/47, FilesystemConversationBackend reference impl; `atomic_agents/conversation/` package; Principal primitive + LOCAL_PRINCIPAL; Turn schema; 10-MUST Implementer Contract; model-aware token budget derivation (max(1000, context_limit - sys_tokens - max_output - 2000), fail-soft to 8000); write-back gated on not _response_deferred (#553 fix); model.md Conversation Backend field LOCKED; check_conversation_backend() doctor dual-probe wired into run_doctor(); spec/22 conversation_id normative addendum; spec/40 ConversationBackend export normative addendum; 142 conversation tests (wiring/conformance/filesystem) + 11 doctor tests = 153 total; PostgresConversationBackend deferred to Phase 3). PrincipalBackend (#556) shipped in v2.0.0 PR 1 + LOCK PR (LOCKED spec/48, `atomic_agents/principal/` package; LocalPrincipalBackend home-user default (always returns LOCAL_PRINCIPAL, is_local_only=True) + StaticClaimsPrincipalBackend serve-layer impl (sha256 NUL-separator key derivation); PrincipalBackendError + UnverifiedPrincipalConversationAccess (conversation_id + principal_id attributes, serve → HTTP 401) in exceptions.py; HARD-REFUSE gate in agent.call() placed BEFORE the idempotency dedup lookup (MUST 10), BEFORE lock, BEFORE cost gate — keys on is_verified ONLY; the BEFORE-dedup placement closes a cross-principal cached-run replay (an unverified caller supplying conversation_id + a guessed/replayed idempotency_key mapping to a COMPLETED record is refused, not served the victim's result_ref); principal_backend kwarg on AtomicAgent.__init__(); serve HYBRID flow in _runner.py + _app.py is fail-closed opt-in (ServeConfig.identity_is_perimeter_verified default False, ANDed with non-loopback bind; present-but-untrusted caller_identity → a directly-constructed is_verified=False Principal, NOT LOCAL_PRINCIPAL, so the posture is backend-independent); stable-subject guidance for the sub claim (MUST 11); check_principal_backend() in doctor.py WIRED into run_doctor() with dual probe + fail-closed negative probe (skipped for is_local_only) + DSN-credential redaction (safe_backend_id at all echo sites); ATOMIC_AGENTS_PRINCIPAL_BACKEND fails LOUD on unknown id (not silently LocalPrincipalBackend) with redacted error message; 12-MUST Implementer Contract (8 PersonaBackend base + 4 named security-boundary MUSTs: fail-closed-at-gate, never-trust-raw-header, storage-key-stability+non-reassignability, is_verified-honesty); spec/37 MUST 6 gets a non-normative clarifying NOTE (LOCKED normative text untouched); security review (Claude adversarial + cross-family Codex) added four fail-closed fixes to the serve/principal seam — (1) cross-tenant collapse guard (perimeter-trust + is_local_only registered backend → runner mints serve_local_backend_misconfig unverified Principal so the gate fires, not LOCAL_PRINCIPAL collapsing every tenant); (2) storage-key ASCII-whitespace-only strip (`_ASCII_WS`, not str.strip()'s Unicode set, so leading-NBSP subjects don't collide — MUST 11); (3) full untruncated raw_identity as the authz sub (not the 512-char-capped caller_identity, which is audit-log-only); (4) header-absent-under-perimeter → unverified not LOCAL_PRINCIPAL (case 1 guard now `caller_identity is None AND not identity_perimeter_verified`) — each with a strip-RED-verified negative-control test; LOCK ceremony: derive_storage_key made private (_derive_storage_key, underscore-only); full 12-MUST conformance-test map (MUST 2/3/9 new tests added); explicit run_id in principal-refused audit record (Principle #5); 129 new PR1 collected tests + 7 net-new at LOCK (6 conformance: MUST 2/3/9 + derive_storage_key privatization-lock; 1 doctor DSN-redaction); raw-token self-verifying backend + role-based authorization deferred to later PRs). Fleet Observability Console PR1 (#614, DRAFT spec/52) shipped (Operator Attention Queue + three-axis Cost/Quality/Reliability trend panels + ack/snooze POST endpoints + alert state JSONL sidecar; 63 new tests). Fleet Console PR2 Health Scoring Engine (#615, DRAFT spec/53) shipped (`atomic_agents/advisor/score.py`; pure-compute Fleet Health Score 0-100; critical-axis cap; 10-MUST Implementer Contract; 97 new tests). Fleet Console PR3 Recommendations Engine (#616, DRAFT spec/54) shipped (`atomic_agents/advisor/recommend.py`; savings_cost / quality_report / governance rec kinds; composite conjunctive no-quality-cost guard; 11-MUST Implementer Contract; 64 new tests; #623 display-integer + WoW window fixes folded as spec/53 MUSTs 11-13). Cost-read fail-closed posture (#495 PR1) — `sum_cost_for_period` and `_sum_via_backend` return `CostReadResult(total_usd, degraded, dropped_records)` instead of a bare `float`; two-tier error handling (whole-file OSError / majority-corrupt current-day file → fail-closed, per-line below 50% → skip+warn+degraded); `CostCheckResult.cost_data_degraded` field added; all gate sites map degraded → fail-closed; spec/09 §"Cost-read error posture" added. LogBackend read-failure contract (#497 PR1) — typed `LogBackendReadError` raised by all 3 reference backends on unrecoverable read failure; empty/absent → [] boundary preserved; cost-reader + mandate spend-gate fail-closed; dashboard/dream degrade gracefully; spec/22 versioned normative read-failure addendum. Dashboard degraded-read banner (#498) — `cost_data_degraded: bool` on `GlobalSummary` + `AgentDashboardData`; OR-composed across every cost-view read; non-blocking "data may be incomplete" banner on global + per-agent cost views; spec/09 LOCKED §"Cost-read error posture" reworded to describe shipped path. Single-developer project; reference implementation that anyone can use, fork, or extend.

Going forward: **the elegance is the product.** Protect it.
