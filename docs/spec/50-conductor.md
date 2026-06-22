# spec/50 — Conductor: durable, resumable orchestration of multi-stage playbooks

> **Status:** **DRAFT** (design spec, issue [#575](https://github.com/dep0we/atomic-agents-stack/issues/575)). No reference implementation and no conformance tests yet — this document fixes the *shape* (what a conductor is, how it composes the five shipped primitives, and the one genuinely new artifact it adds). The five Tier-A forks were ruled by the maintainer on 2026-06-22 (see §"Ruled decisions"); this DRAFT reflects those rulings. The Conductor Contract MUSTs below are **proposed**, not LOCKED; they become normative only after an implementation matches and the conformance suite passes.
>
> **Cross-links:** spec/12 (Goals and Intent — the durable run anchor + history), spec/14 (Outcomes — per-stage iterate-to-rubric result), spec/18 (Skills — the playbook *definition* layer), spec/41 (GoalBackend), spec/42 (OutcomeBackend), spec/44 (QueueBackend — conditional: role-team fan-out + conflict serialization behind a gate), spec/45 (IdempotencyBackend — resume safety), spec/15 (Delegation — the one-level bound this spec must not circumvent).

---

## Overview

A **conductor** runs a multi-stage **playbook** — an ordered, human-gated process — to completion, and **never loses its place**. If a run stops mid-process (crash, restart, or a gate left awaiting a human decision for hours or days), it resumes exactly where it left off with all prior state and rulings intact.

The conductor is **not** a backend protocol and **not** a graph/workflow engine (LangGraph stays out of scope per ROADMAP §"What we're NOT building"). It is an **orchestration layer** in the spirit of `agent.call()` and the goal-outcome coordinator (`goal/coordinator.py`, spec/41): a thin sequencer that composes primitives the framework already shipped, plus **one new artifact** — the gate-decision record.

The conductor's durable state **is a decision ledger**. The resume cursor and the audit record are the same artifact: every stage entered/completed and every human gate decision is written as *what was decided, by whom, why, when, and what is still pending*. That single artifact gives both:

- **suspend and resume** mid-playbook (the protection property), and
- later **query** — "what did we decide at the security gate for feature X, and why" (the audit property).

This is a direct application of Principle #5 ("audit trail is structural"): a rationale-bearing, append-only, rolls-up record is exactly the structural audit the framework already values. The conductor does not invent a new audit shape — it **leans on the goal ledger**, which is already append-only, CAS-guarded, and exportable (spec/41).

---

## Why now (the gap is known, not speculative)

Durable pause/resume across a long, gated run is table-stakes for the org half of the throughline ("a home user with one agent and an org with a fleet"). The framework already *committed* to resumable runs when it shipped **Goal (#425)**, **Outcome (#426)**, **Queue (#428)**, and **Idempotency (#520)** — those backends only make sense if long runs survive interruption. The conductor is the missing layer that ties them into one named, coherent capability. Leaving it unnamed forces every operator to re-wire it by hand, which violates the gracefulness aesthetic ("things that should be easy are easy; defaults are right").

Independent corroboration: the **dev-process-kit** repo (the first real playbook the conductor must run — see §"Reference playbook") scatters its gate decisions across three disconnected places (`docs/DECISIONS.md`, per-build Tier-A rulings JSON, and per-gate receipts) with **no unified, durable, queryable record of every human gate decision with its rationale**. Different vantage, same gap reasoned to abstractly. The conductor's decision ledger is the unified record that closes it.

Per [[feedback_dont_defer_known_architectural_gap]]: durable resume is a *known, load-bearing* org requirement, not a speculative cliff. The shipped primitives (Goal/Outcome/Queue/Idempotency) only fully make sense *because of* the conductor. So the conductor is designed first-class now, knowing the resume + ledger requirement from line one — not retrofitted.

---

## Vocabulary

| Term | Meaning |
|------|---------|
| **Playbook** | An ordered definition of stages: what to run at each step, which model, and where execution must stop for a human decision. A vault-native markdown artifact (see §"Relationship to spec/18"). |
| **Stage** | One step of a playbook. Either an **automated stage** (an `agent.call()` / `outcome` run / single `delegate`) or a **gate**. |
| **Gate** | A stage that halts execution awaiting an external (human) decision before the run may continue. |
| **Conductor run** | One execution of a playbook against one work subject (e.g. "run the dev-process-kit playbook for feature #1234"). Has a stable `conductor_run_id`. |
| **Decision ledger** | The durable record of every stage transition and every gate decision for a run. **It is the goal ledger** (spec/41) — `goal.md` history + `goal_history.jsonl` — plus the new gate-decision event type. Resume cursor and audit record in one. |
| **Gate-decision record** | The one genuinely new artifact: a structured record of a pending-or-answered human decision (`{decision_id, prompt, options, context_ref, status, answer, answered_by, answered_at, rationale}`). |

---

## The composition — how the conductor maps onto the five shipped primitives

The conductor is a sequencer. Every piece of durable state it needs **already exists** in a shipped primitive. The design discipline (Principle #3 "compose, don't merge"; "pick fewer concepts") is to add the *minimum* new concept — one record type — and route everything else through existing stores.

| Conductor concept | Shipped primitive it maps to | Notes |
|-------------------|------------------------------|-------|
| The run's durable anchor + ordered stages + append-only history | **Goal (spec/41)** | One conductor run **is a goal**. `goal.intent` = "run playbook X for subject Y". Each stage **is a sub-goal** (`pending → in_progress → complete/blocked/abandoned`). `goal_history.jsonl` is the append-only ledger; `apply_transition()` (CAS-guarded, MUST 10) is the atomic stage-transition primitive. `archive_goal()` retires a finished run; `list_archived()` enumerates past runs. |
| Each automated stage's persisted result | **Outcome (spec/42)** | An automated stage that iterates-to-rubric persists its terminal result as `result.json` (write-once, MUST 9). The **goal-outcome coordinator** (`dispatch_sub_goal_as_outcome`, spec/41) already composes Goal+Outcome for the *single*-stage case with a fail-closed cost gate + CAS terminal transition. **The conductor is that coordinator generalized across N stages.** |
| Don't re-run a stage that already completed on resume | **Idempotency (spec/45)** | `agent.call(idempotency_key=…)` is already wired (W1–W8). The per-stage key is deterministic, so a resume that re-dispatches a completed stage short-circuits (COMPLETED → deduped) instead of re-spending. See §"Resume semantics" for the exact role idempotency plays vs the ledger. |
| (a) The pending stages when a stage fans out to a role team, AND (b) serializing a conflicting run behind a suspended gate | **Queue (spec/44)** — **conditional composition** | Two uses. (a) *Role-team fan-out:* a stage dispatching work to multiple roles uses the project-scoped QueueBackend (org shape). (b) *Conflict serialization:* a new run that needs a resource a gate-suspended run holds enqueues behind that decision and is released on resume (see §"Concurrency and conflict serialization"). Queue is absent only when a playbook declares no role-team dispatch AND no conflict keys (the simplest single-job-at-a-time home case); the moment overlapping runs can conflict, Queue is the serialization mechanism. |
| Cost ceiling across the whole (possibly multi-day) run | **Tree-cap** | The conductor run is the cost root. Each stage's `agent.call()` / `outcome` / `delegate` clamps to `MIN(stage_remaining, run_remaining)` — identical to delegation (spec/15) and the goal-outcome coordinator's pre-dispatch gate. The multi-day run cannot exceed its ceiling no matter how many stages it spans. |

**The conductor itself is a thin orchestrator** — a free function / module (like `goal/coordinator.py`), **not** a backend Protocol and **not** an agent. It holds no authoritative state of its own; every durable fact lives in Goal / Outcome / (optional) Queue / Idempotency. Kill it mid-run, restart it, and it reconstructs its place entirely from those stores.

---

## The one genuinely new artifact — the gate-decision record

Everything above is reuse. The single new concept is the **gate-decision record**: how a run durably says "I am blocked here, awaiting *this* human decision, and here is how to resume when the answer arrives."

Goal carries history but **not** a "blocked on external input, resume with this value" state. The existing sub-goal `blocked` status means *blocked by a sibling sub-goal* (`blocked_by` references another sub-goal that must reach `complete`) — it is not "awaiting a human answer to inject." That is the gap the gate-decision record fills.

### Shape (DRAFT — storage RULED OD1a: in the goal ledger)

```python
@dataclass(frozen=True)
class GateDecision:
    decision_id: str              # stable id for this gate within the run
    stage_id: str                 # the sub-goal/stage this gate guards
    prompt: str                   # the question put to the human (plain language)
    options: list[str]            # offered choices (may be empty for free-text)
    context_ref: str              # opaque ref to the run state the human reviews
    status: Literal['pending', 'answered']   # REQUIRED — no default
    answer: str | None            # the human's ruling (None while pending)
    answered_by: str | None       # principal id of the human who ruled (spec/48)
    answered_at: str | None       # ISO 8601 wall-clock of the ruling
    rationale: str | None         # WHY — the load-bearing audit field
    held_conflict_keys: list[str] # declared resources this suspended run holds
                                  # (e.g. ['merge:main']) — drives conflict
                                  # serialization; see §"Concurrency and conflict
                                  # serialization". Empty when the run declares none.
```

`answered_by` ties into PrincipalBackend (spec/48) so the ledger records *who* ruled, not just *what* — the org-shape audit property. For the home shape it is `LOCAL_PRINCIPAL`.

### Suspension and resume (the new control-flow bit)

A conductor run that reaches a gate:

1. Writes a `GateDecision(status='pending', …)` into the ledger (a new `goal_history` event type — RULED OD1a/OD2), and transitions the gate stage's sub-goal to a new **`awaiting_decision`** suspension status (RULED OD1a).
2. **Returns control.** `conductor.run(...)` returns a `ConductorState` carrying `status='awaiting_decision'` and the pending `GateDecision`. No thread is parked; no resource is held across the (possibly multi-day) wait. The run is *durably suspended*, not *blocking*.
3. When the human answers, the operator calls `conductor.resume(conductor_run_id, decision_id, answer, rationale, principal)`. The conductor:
   - re-reads the ledger to confirm the run is still suspended on `decision_id` (rejects a stale/duplicate answer),
   - appends the `GateDecision(status='answered', answer=…, answered_by=…, rationale=…)` event (audit + the injected value, in one record),
   - releases any runs queued behind this decision's `held_conflict_keys` (see §"Concurrency and conflict serialization"),
   - transitions the gate stage off `awaiting_decision`, injects the answer at exactly that stage, and continues the playbook from the next stage.

The answer is injected *at the gate point* because the ledger records *which* `decision_id` the run was suspended on — the resume cursor and the injected value are the same record. This is the property the issue calls out: the resume cursor and the audit record are one artifact.

---

## Resume semantics — the ledger is primary, idempotency is the safety net

Resume must be correct across two failure modes, and they use different mechanisms. Getting this boundary right is the load-bearing design statement of the spec.

**The goal ledger is the authoritative resume cursor.** On (re)start, the conductor reads the run's goal:

- A stage whose sub-goal is `complete` **and** whose result is present in the durable store (Outcome `result.json`, or the sub-goal `output` field) → **skip**; reuse the stored result. The result comes from the store, never re-computed.
- A stage that is `awaiting_decision` → re-surface the pending `GateDecision`; wait for `resume()`.
- A stage that is `pending` or `in_progress` → (re-)run it.

**Idempotency is the safety net for one narrow window** — a stage that committed its result but crashed *before* the ledger transition landed (the commit-then-crash-before-ledger-update window). A deterministic per-stage idempotency key (`conductor:<conductor_run_id>:<stage_id>`) means the re-dispatched stage's `begin()` sees COMPLETED and short-circuits (W7) instead of double-spending the LLM.

**Two correctness facts that must be stated, not glossed:**

1. **A deduped Response carries no output** (spec/45 W2/W7 — `deduped_response()` returns `text=''`). So the conductor MUST take a replayed stage's *result* from the durable store (Outcome / goal sub-goal), **never** from the deduped Response. Idempotency is the *guard against re-spending*; the Goal/Outcome store is the *source of the result*. (This is a second reason the result lives in the shipped stores, not in a new conductor-invented store — RULED OD2.)

2. **The single-host hard-crash gap is real and bounded.** `FilesystemDedupLedger` is `supports_ttl=False` (spec/45). A *hard* crash mid-stage leaves the idempotency lease wedged IN_FLIGHT with no TTL sweep, while the goal sub-goal reads `in_progress`. The conductor's resume relies on the **ledger** (re-run an `in_progress` stage), and Outcome write-once (MUST 9, fresh `run_id` per attempt) keeps the re-run from corrupting state — at the cost of one extra stage run in this rare window (audit-visible, never silent). **Exactly-once across hard crashes requires the deferred IdempotencyBackend TTL sweep (spec/45 §"TTL sweep") or a multi-host Redis/Postgres IdempotencyBackend.** This is named here as a known limitation with a named dependency — the home/single-host shape accepts the bounded duplicate-spend; the org shape closes it by registering a TTL-capable backend. The conductor does not build the TTL sweep ahead of that need.

---

## Concurrency and conflict serialization

A gate-suspended run can be parked for hours or days. During that window the agent must keep doing its other work, and a *conflicting* second run must not barge past the pending decision. Three rules, in order of how often they fire:

1. **Suspension holds no agent lock (the common case).** A run suspended on a gate parks no thread and holds no `AgentLock` across the wait (Contract C8). So a new scheduled trigger (a cron tick, an HTTP call) for the *same agent* starts a fresh, independent conductor run and **proceeds normally**. The home expectation — "the agent is waiting on me for job one, but its scheduled job two still runs" — is the default behavior, not a special case.

2. **Non-conflicting concurrent runs proceed freely.** Each conductor run is its own goal (one run = one goal), so two runs never collide on the ledger itself. Absent a shared declared resource, they run fully in parallel under the agent's normal per-run locking.

3. **A conflicting run queues behind the decision (the new bit).** A run declares the resource(s) it needs — a **conflict key**, an opaque string the playbook author chooses (e.g. `merge:main`, `deploy:prod`, or a path the gated stage will write once the human answers). When a new run needs a conflict key that a *gate-suspended* run currently holds (recorded in that run's `GateDecision.held_conflict_keys`), the new run does **not** block-wait (that would wedge it for the multi-day gate) and does **not** barge. It **enqueues behind the decision** via the project-scoped QueueBackend (spec/44) — the conflict key maps to a queue bucket keyed by the blocking `decision_id`. When `resume()` answers that decision, the conductor releases the queued waiters for that key, which then run in order. This is the literal "flagged or queued up to run right after the human decision is made" behavior.

**Why a held lock is the wrong mechanism, and Queue is the right one.** Holding a `LockBackend` lock across the gate would satisfy mutual exclusion but violate rule 1 — it wedges the agent for the entire (multi-day) human wait, starving every other job. Queue-behind-decision gives the same exclusion *without* holding a resource across the wait: the suspended run owns the conflict key by *record* (in the ledger), not by a *held lock*, and the waiter parks in durable queue state, not on a live thread. The suspension stays resource-free (C8) and the conflict still serializes correctly.

**Honest limits (named, not hidden):**
- **Conflict keys are declared, not inferred.** The framework cannot magically know two arbitrary jobs collide; the playbook author names the resource. An undeclared real conflict is not serialized — same discipline as any lock-scope design. The cost of the chosen behavior (vs. flag-only) is this declaration burden; the benefit is automatic clean sequencing instead of a hand-resolved collision.
- **This is the second reason Queue is load-bearing, not just org-shape fan-out.** Even a single home agent with two overlapping scheduled jobs hits conflict serialization, so the spec frames Queue as *conditional* (present when conflict/fan-out is possible), not *org-only*.

---

## Cost — the tree-cap across a multi-day run

Per Principle #4 ("cost is first-class"), every conductor stage runs behind a cost gate, and the *whole run* is capped. The conductor run is the cost root; each stage clamps to `MIN(stage_remaining, run_remaining)`, exactly as delegation (spec/15) and the goal-outcome coordinator's pre-dispatch gate already do. A multi-day, many-stage run cannot exceed its ceiling. A gate-suspended run that resumes days later resumes *under the same run-level ceiling* — the ledger carries cumulative spend, so the cap survives the suspension. No conductor code path runs an LLM without passing through the stage's cost gate first (the gate fires *before* the stage's first LLM call, never after — same discipline as `_check_cost_guardrails`).

---

## Relationship to spec/18 (Skills) — "a skill that leans on goals," refined

The design conversation framed the conductor as "a skill that leans on goals." That holds for **one** layer and diverges for the **other** — naming the split keeps a future contributor from collapsing them.

- **The playbook *definition* IS a skill-shaped artifact (the framing HOLDS).** A playbook — the ordered stages, each stage's prompt, the gate questions, the model dial per stage — is authored as vault-native markdown, progressively disclosed (Principle #6), editable in any text editor (Principle #7). It can live as a `SKILL.md`-shaped file the agent loads to know *what the process is*. "A skill that describes a multi-stage process" is exactly right for the definition.
- **The conductor *runtime* is NOT a skill (the framing DIVERGES).** The thing that *executes* the playbook — sequences stages, writes the ledger, suspends and resumes across days — is an orchestration layer in code, invoked programmatically (like `agent.call()` / the goal-outcome coordinator). A skill is *instructions loaded into a turn*; it cannot "return control and resume in three days." Only the runtime can.

This mirrors the issue's execution-model note exactly: the **kit-side** conductor (a Claude Code skill) is necessarily a *stateful guided checklist*, because in the Claude Code harness skills are instructions loaded into the turn, not functions that return control. The **atomic-agents** conductor is a *true orchestration engine*, because `agent.call()` is programmatic, `load_skill` is a real call, and goals/queue/outcomes are real state. The two conductors differ in **execution model**, not just "human present vs human remote." So: playbook definition = skill (markdown, vault-native, progressively disclosed); conductor runtime = orchestration layer (code). RULED OD4.

---

## One-level delegation (#9) — the conductor is not a delegation level

A conductor runs stages; a stage may be an `agent.call()` that itself delegates to a specialist (spec/15). Does "a conductor running stages that delegate" stack onto the one-level delegation bound?

**No — and the bright line must be stated normatively.** One-level delegation bounds a single `delegate()` call tree: a coordinator delegates to a specialist; the specialist cannot delegate further. The conductor is **not** a delegation level. It is an orchestration layer that *sequences independent, fresh top-level agent runs* — there is no live parent call frame held across stages. Each stage starts a fresh call tree. Within a stage, an agent may delegate one level (coordinator → specialist). The conductor sequencing stage₁, stage₂, … does not deepen the call graph, exactly as cron running a delegating agent does not create two-level delegation, and the goal-outcome coordinator dispatching an `OutcomeRunner` (whose inner `agent.call()` may delegate) does not.

**The bright line (proposed normative MUST — RULED OD3: document + best-effort guard now):** the conductor is invoked at the orchestration top — by an operator, cron, serve, or a coordinator agent *before* it has delegated. A stage that is *itself* a delegated, depth-1 specialist call MUST NOT start a conductor run. Allowing that would *launder* two-level delegation through the conductor (specialist → conductor → more delegating stages), defeating the #9 bound. Per OD3, this ships as a documented normative constraint plus a best-effort guard for the obvious case; full structural call-depth enforcement (refuse to start from within a delegate frame) is deferred until a call-depth signal exists.

---

## Reference playbook — the dev-process-kit (design against the real workload, don't build ahead of it)

The conductor is designed against **one concrete playbook**: the dev-process-kit lifecycle (`~/Projects/dev-process-kit/PLAYBOOK.md`). The kit-side conductor (a human-present Claude Code skill / stateful checklist) ships *first* in that repo as the version we port; the atomic-agents conductor is designed knowing the resume + ledger requirement from line one. The spec is scoped to what this playbook actually needs — nothing speculative.

The kit playbook is 13 stages (0–12), each with a model dial and explicit human gates:

| Kit stage | Conductor mapping |
|-----------|-------------------|
| 1 `/office-hours` (go/no-go) | **gate** — human rules go/no-go; answer injected, run continues or terminates |
| 2 `/spec` (scope boundaries) | automated stage + **gate** on scope approval |
| 3 `/autoplan` (which concerns matter) | automated stage (4 review angles) + **gate** |
| 4 design (visual direction) | optional automated stages + **gate**; skipped-with-approval = a recorded gate ruling, not a silent skip |
| 5 `/arc discovery` (rule every Tier-A fork) | automated stage emitting Tier-A forks → **one gate per fork**, each ruling a `GateDecision` with rationale |
| 6 `/arc build` (unforeseen Tier-A) | automated `outcome`-style stage; an unforeseen Tier-A fork mid-build = a **new gate** mid-run (suspend → resume) |
| 7 verify / qa | automated stages |
| 8 security (accept/avoid findings) | automated stage + **gate** per real finding |
| 9 `/ship` (merge approval) | automated stage + **merge gate** (irreversible — the gate is mandatory) |
| 10–11 deploy / document | automated stages + rollback **gate** |
| 12 ongoing | out of a single run's scope |

Two properties of the kit playbook the conductor must honor, both already first-class in the design:

- **"Default flow every time; ask before skipping; never skip silently."** A skipped stage is a **recorded gate ruling** (a `GateDecision` with the skip rationale), never an absent stage. The ledger's "what is still pending" field makes a silent skip structurally impossible.
- **The scattered-decisions problem the kit has today** (`docs/DECISIONS.md` + Tier-A rulings JSON + per-gate receipts) collapses into **one** queryable ledger: every gate ruling, with its rationale and ruler, in the goal history of that run.

**Do not build ahead of this playbook.** The kit needs sequential, gated stages with per-stage automated runs, human gates with injected answers, durable resume, and a run-level cost ceiling. It does **not** need parallel stage DAGs, conditional branching, sub-playbooks, or a playbook-authoring GUI — those are explicitly out of scope until a real playbook needs them (see §"What this is NOT").

---

## Throughline — the SAME conductor runs a tiny home playbook and a big org playbook

The same conductor code runs both shapes. A 3-stage personal playbook (home — "draft → my review gate → publish") and the 13-stage dev-process-kit playbook (org) are the *same* stage-sequencing loop over the *same* Goal + Outcome + Idempotency stores. The differences are **backend registration** and **playbook size**, never a code fork.

The tradeoffs to name (per the throughline rule — neither shape is a degraded mode):

- **Queue is optional composition, so the home shape is not taxed.** At org scale, a stage that fans work to a role team uses the project-scoped QueueBackend (spec/44). At home scale, stages run inline on the one agent and Queue is entirely absent — the home conductor never instantiates queue machinery, leases, or claim logic. A stage declares a role-team dispatch or it doesn't; the conductor only reaches for Queue when it does. This keeps the home shape graceful (no concept it doesn't use) and the org shape first-class (real cross-host claim when it does fan out), rather than making one an inline-only degraded version of the other.
- **The ledger scales via the shipped GoalBackend swap, not a new store.** One conductor run = one goal. The home shape runs it on `FilesystemGoalBackend` (a few sub-goals, a short history — graceful). A big org playbook (many stages, possibly concurrent runs, long history) stresses the single-goal-per-agent, single-file model — and that is *exactly* what the GoalBackend Protocol's Postgres swap exists to absorb (spec/41 operator override; T15 / Position B). So the org shape scales by registering a Postgres GoalBackend, not by the conductor growing a parallel ledger store. The honest cost: a very large run's `goal.md` is bigger than a home goal — acceptable on filesystem for home, absorbed by the backend swap for org. This is the throughline working as designed: same conductor, different backend registration.
- **Exactly-once resume favors the org shape only where the org shape pays for it.** The single-host hard-crash duplicate-spend window (§"Resume semantics") is bounded and audit-visible for the home shape; the org shape closes it by registering a TTL-capable / multi-host IdempotencyBackend. Neither shape is broken; the stronger guarantee rides on the stronger backend the org shape already wants for other reasons.

---

## Proposed Conductor Contract (DRAFT — normative only after an implementation matches)

These are the requirements an implementation will have to satisfy. The forks are ruled (§"Ruled decisions"); these MUSTs reflect those rulings but are **not** LOCKED until an implementation matches and the conformance suite passes.

- **C1 — The conductor holds no authoritative state of its own.** Every durable fact (run anchor, stage status, stage result, gate decision, cumulative cost) lives in a shipped primitive (Goal / Outcome / Idempotency / optional Queue). Killing and restarting the conductor MUST reconstruct the run's place entirely from those stores.
- **C2 — The goal ledger is the authoritative resume cursor.** Resume MUST consult stage status in the goal, not the idempotency lease, to decide skip-vs-rerun. A `complete` stage with a stored result is skipped (result from the store); an `in_progress`/`pending` stage is (re-)run; an `awaiting_decision` stage re-surfaces its pending `GateDecision`.
- **C3 — A replayed stage's result comes from the durable store, never from a deduped Response.** (Deduped Responses carry no output — spec/45 W2/W7.)
- **C4 — Every gate writes a rationale-bearing `GateDecision`; a skipped stage is a recorded ruling, never an absent stage.** "What is still pending" MUST be queryable from the ledger so a silent skip is structurally impossible (Principle #5; the kit's "never skip silently" rule).
- **C5 — `resume()` is idempotent and stale-safe.** A second answer to an already-answered `decision_id`, or an answer to a run no longer suspended on that decision, MUST be rejected without advancing the run (CAS against the ledger, reusing goal `apply_transition`'s `expected_from_status` guard, MUST 10).
- **C6 — Every stage runs behind a cost gate; the run is tree-capped.** No conductor code path runs an LLM without the stage's cost gate firing first; the cap survives gate suspension across days (cumulative spend carried in the ledger).
- **C7 — The conductor is not a delegation level.** A stage may delegate one level (spec/15); a depth-1 delegated specialist MUST NOT start a conductor run (the #9 launder-guard — RULED OD3: documented constraint + best-effort guard; structural depth-tracking deferred).
- **C8 — Suspension holds no live resource.** A gate-suspended run parks no thread, holds no lock across the wait, and is reconstructable purely from durable state. A new scheduled trigger for the same agent MUST be able to start and run an independent conductor run while a prior run is gate-suspended.
- **C9 — Conflicting runs serialize by record, not by held lock.** A run owns its declared conflict keys by ledger record (`GateDecision.held_conflict_keys`), never by a lock held across the gate. A new run needing a key held by a gate-suspended run MUST enqueue behind that decision (QueueBackend) rather than block-wait or barge; `resume()` MUST release the queued waiters for the answered decision's keys. A run that declares no conflict keys is never serialized this way.

---

## What this is NOT (scope discipline — do not build ahead of the reference playbook)

- **Not a backend protocol** — the conductor adds no 22nd Protocol; it composes existing ones. (The gate-decision record is a new *event type in the goal ledger*, not a new store — RULED OD2.)
- **Not a graph/workflow engine** — no DAGs, no conditional branching, no parallel stage topologies. Stages are an ordered sequence. (LangGraph's territory, ROADMAP §"What we're NOT building".)
- **Not sub-playbooks / nested conductors** — a playbook is one level of stages. (Mirrors the one-level constraints, Principle #9.)
- **Not a playbook-authoring GUI** — playbooks are markdown (Principle #7).
- **Not the TTL sweep** — exactly-once-across-hard-crash rides on the deferred spec/45 TTL sweep or a multi-host backend; the conductor does not build it ahead of the org shape needing it.

---

## Ruled decisions (maintainer, 2026-06-22)

The five Tier-A forks were ruled by the maintainer. The DRAFT above reflects these rulings.

**OD1a — Pending-decision record placement → RULED: in the goal ledger.** The `GateDecision` record is a new event type in the run's goal history (one store, alongside every stage transition). Rejected: a separate sidecar file next to `goal.md`, which would split "what's pending" from "what happened" and start down the scattering path the conductor exists to fix.

**OD1b — Conflicting-run handling → RULED: queue behind the decision.** A run declares the resources it needs (conflict keys). A new run needing a key a gate-suspended run holds enqueues behind that decision (QueueBackend) and is released automatically on `resume()`. Non-conflicting runs proceed freely; the suspended run holds no lock across the gate. Rejected: flag-only/optimistic (let both run, flag the collision for manual resolution) — it leaves a hand-resolved collision and a wasted spend instead of clean automatic sequencing. Rejected (off the table): a held lock across the gate — wedges the agent for the multi-day wait. This ruling is why Queue is *conditional*, not *org-only* composition — see §"Concurrency and conflict serialization".

**OD2 — Ledger storage → RULED: reuse Goal history + Outcome.** No 22nd backend, no parallel store. Goal history is already append-only, CAS-guarded, exportable, and doctored; big org runs scale via the shipped GoalBackend Postgres swap (spec/41 operator override), not a new store.

**OD3 — One-level delegation enforcement → RULED: document + best-effort guard now.** The bright line (a depth-1 delegated specialist MUST NOT start a conductor run — Contract C7) is stated normatively with a best-effort guard for the obvious case; full structural call-depth enforcement is deferred until a call-depth signal exists. The launder path is narrow and the guard catches the common case.

**OD4 — spec/18 boundary → RULED: confirm with a refinement.** The playbook *definition* is a skill-shaped artifact (markdown, vault-native, progressively disclosed); the conductor *runtime* is an orchestration layer in code, not a skill, because a skill cannot return control and resume across days. See §"Relationship to spec/18".

---

## Cross-references

- spec/12 (Goals and Intent) — the run anchor + history narrative the ledger leans on.
- spec/14 (Outcomes) — the per-automated-stage iterate-to-rubric result.
- spec/15 (Delegation) — the one-level bound the conductor must not circumvent.
- spec/18 (Skills) — the playbook-definition layer.
- spec/41 (GoalBackend) — the goal-outcome coordinator the conductor generalizes; the Postgres swap the org shape scales on.
- spec/42 (OutcomeBackend) — write-once per-stage result.
- spec/44 (QueueBackend) — optional role-team fan-out (org shape).
- spec/45 (IdempotencyBackend) — resume safety; the TTL-sweep dependency for exactly-once.
- spec/48 (PrincipalBackend) — `answered_by` on a gate decision (who ruled).
- TENSIONS T15 / Position B — the backend-swap authority model the throughline scaling leans on.
- ROADMAP §"What we're NOT building" — graph workflows stay out of scope.
- Issue #575 — the design conversation and maintainer framing this spec implements.
