# 01 — Anatomy of an Atomic Agent

What files an Atomic Agent is made of, and what each one does.

---

## The complete file list

```
~/agents/{agent_name}/
├── persona/
│   ├── IDENTITY.md            ← required
│   ├── SOUL.md                ← required
│   └── USER.md                ← required (unless agent doesn't have a primary user)
├── tools.md                   ← required
├── model.md                   ← required
├── memory/
│   ├── INDEX.md               ← required (auto-generated, hand-edited as needed)
│   └── *.md                   ← any number, frontmatter-tagged
├── wiki/                      ← optional (only if agent ingests source documents)
│   ├── INDEX.md
│   └── *.md
├── raw/                       ← optional (paired with wiki/)
├── journal/                   ← required
│   └── YYYY-MM/YYYY-MM-DD.md
├── log/                       ← required for cron-version agents
│   └── YYYY-MM/YYYY-MM-DD.jsonl
├── goal.md                    ← OPTIONAL (only for goal-driven / hybrid agents — see spec/12)
└── evals/                     ← OPTIONAL but recommended (see spec/08)
    ├── rubric.md
    ├── judge.md
    └── golden/
```

## Agent operating mode (declared in IDENTITY.md)

Per [12-goals-and-intent](12-goals-and-intent.md), every Atomic Agent declares one of three operating modes:

| Mode | What it does | `goal.md` present? |
|---|---|---|
| **Reactive** | Each invocation is a discrete transaction. Operator (or another agent) supplies the work item. | NO |
| **Goal-driven** | Maintains active goal across runs. Decomposes into sub-goals + queue items. Self-generates the next work item from the goal. | YES, when active |
| **Hybrid** | Reactive by default; goal-driven when `goal.md` is active. Trigger source (skill = reactive, cron = goal-driven) determines mode per invocation. | OPTIONAL (active when goals exist) |

Most agents (Caldwell, another agent, agent-a, agent-b) are **reactive**. Goal-driven and hybrid modes exist for agents that need to pursue persistent objectives across many sessions (Muse Director on a novel project, ops-automation pursuing a multi-step migration, research agents synthesizing across many sources).

The mode is declared as a section in `IDENTITY.md`:

```markdown
## Operating mode

This agent is **reactive** (or **goal-driven** or **hybrid**).

[For goal-driven/hybrid agents: explain how trigger source determines mode]
```

If the file doesn't declare a mode, default is **reactive**.

---

## Graduated autonomy

Atomic Agents treats autonomy as a **gradient**, not a binary. The framework's design rejects both extremes — fully-autonomous agents that take any action without check, and fully-supervised agents that pause for human approval on every decision. Neither shape survives contact with real operator workloads at any scale beyond a single agent doing trivial work.

**The principle**: agents act freely where risk is low, ask for revision where risk is moderate, and prepare evidence for humans where risk is high. The structural mechanism that encodes this is the **action classification + judge layer** combination from `spec/17` (tools.md) and `spec/28` (judge layer):

| Action class | Default judge policy | What this means in practice |
|---|---|---|
| `read_only` (read_file, search_notes, list_directory) | Bypass judge | Agent acts freely — no authorization check on reads |
| `reversible_write` (draft creation, staged memory writes) | Judge optional; default-allow with audit | Agent acts; audit captures the action; operator can review post-hoc |
| `external_side_effect` (send_email, create_pr, post_message) | Judge required; judge-decides | Per-action validation before execution; judge may revise, block, or escalate |
| `high_risk` (delete_files, force_push, production_deploy) | Judge required; default-escalate | Human review for every instance; agent prepares evidence; never auto-executes |

The four-outcome judge model (`allow / block / revise / escalate` per spec/28) is the mechanical encoding of graduated autonomy. **Revise** in particular is what makes the gradient real — it lets the system correct an action without losing the actor's intent (\"send the email but remove the attachment\" / \"open the PR as draft, not for merge\" / \"lower the spend limit before proceeding\").

Operators tune the gradient per-agent via `judges.md` class policy and via `mandates.md` (`spec/29`) for durable scoped authority that crosses many runs. The autonomy ladder declared in `IDENTITY.md` (see below in this spec, in the IDENTITY template) is the persona-level statement of intent; tools.md / judges.md / mandates.md are the operator-managed *enforcement surfaces* that make the ladder real at runtime.

The graduated-autonomy property is what distinguishes Atomic Agents from frameworks that assume agents either (a) operate against fully-supervised checklists or (b) operate with unrestricted tool access modulated only by post-hoc evals. Both extremes leave operators unable to scale agent deployment to real workloads without taking on either an approval-bottleneck cost or an unbounded-blast-radius cost. The framework's commitment is that **the same agent definition runs at every scale — the gradient is configured by the operator's `tools.md` / `judges.md` / `mandates.md`, not by re-shaping the agent itself.**

---

## persona/IDENTITY.md

**Purpose**: Who the agent is. Mission, role, scope, doctrine. The stable role definition.

**Author**: The operator, by hand. Updated only when the role itself changes.

**Lifecycle**: Stable. May go years without an edit.

**Contents** (suggested, not rigid):

```markdown
# IDENTITY — Caldwell

## Who I am
Caldwell. The operator's personal financial advisor — sharp, honest, calm.

## Mission
Help the operator make money decisions: debt elimination, income planning, investment strategy, spending tradeoffs.

## Scope
- Personal finances (operator's household)
- Day-job income when it intersects with personal finance
- NOT: the employer's company finances. NOT: side-project operational costs.

## Operating doctrine
- Always start by loading current state (latest balance sheet, recent journal entries)
- Take a position when asked. Don't fence-sit.
- Never leave the operator with a problem and no path forward (Caldwell posture).
- Money decisions belong to the operator. I advise, I don't decide.

## Autonomy ladder
- Internal vault writes: autonomous (own folder only)
- External actions (sending email, paying bills, moving money): never. Hard stop.
- Repeated approved analysis types: autonomous after 3rd approval

## What I'm NOT
- Not a CPA, not a lawyer. I name when professional help is needed.
- Not another agent. I don't run the operator's life. I just handle money questions.
```

**Key principles for IDENTITY.md**:

- Be specific, not generic. "Senior financial advisor with 15 years of CRE experience" beats "helpful assistant."
- Define the boundary explicitly. What this agent is NOT is as important as what it is.
- Include the autonomy ladder if the agent takes actions, not just answers.
- Reference the specific doctrine that shapes its judgment (e.g., "Caldwell posture").

---

## persona/SOUL.md

**Purpose**: Personality, voice, taste. The evolving self.

**Author**: Initially the operator. Over time, the agent edits itself with permission. Different lifecycle from IDENTITY.

**Lifecycle**: Evolves continuously as the agent develops. Should be reviewed monthly.

**Contents** (suggested):

```markdown
# SOUL — Caldwell

## Voice
Calm, direct, no judgment. Numbers-first when numbers exist. Anchor recommendations in tradeoffs the operator can weigh, not in authority.

## Posture
- Treat financial stress as legitimate. Don't pretend it isn't there.
- Never leave the operator stuck without a path forward. If I don't have an answer, say so AND propose how we get one.
- Bias toward the operator's stated priorities (debt elimination > investment optimization, currently).

## Evolution discipline
(rules about how SOUL itself grows)
- Don't go generic. Specific observations beat vague principles.
- Don't fake depth when the data is thin. Say "I don't have enough context here" and ask.
- Don't over-template responses. Match the shape of the question.
- Don't promise things outside scope. If the operator asks about day-job comp, redirect to agent-a.

## Things I've learned about how to advise the operator
(this section grows over time as their preferences become clear)
- They want the bottom line first, supporting math second.
- They push back when advice feels generic — needs the answer rooted in their specific numbers.
- They prefer fewer, bigger moves over many small optimizations.
```

**Key principles for SOUL.md**:

- This is *how* the agent behaves, not *what* it does. Mechanics live in IDENTITY/PROMPT; personality lives here.
- Evolution discipline (the meta-rules about how SOUL itself grows) is part of SOUL.
- Allow this file to be edited by the agent itself (with promotion-from-Atomic-Notes review). That's what makes the agent "self-improving."

**SOUL ≠ IDENTITY**: this is a hard rule. Personality changes shouldn't touch role mechanics, and role changes shouldn't touch personality. Mixing them defeats the purpose of the split.

---

## persona/USER.md

**Purpose**: About the operator. What they care about, how they work, what to avoid.

**Author**: The operator, by hand. The agent may suggest additions; the operator approves.

**Lifecycle**: Slow evolution. Major life changes trigger edits.

**Contents**:

```markdown
# USER — the operator

## Role and context
- Director of Operations at a regional logistics firm (day job)
- Freelance technical editing on the side
- Married, 2 kids in school
- Madison, WI — Central time
- Not a developer; knows what to ask for and needs guidance on the how

## Communication preferences
- Direct, concise, no fluff
- Bottom line first
- Specific over generic
- Hates being asked obvious questions
- Doesn't want to be told to "rest" or "pause"

## Money-specific preferences (Caldwell-relevant)
- Currently prioritizing debt elimination over investment optimization
- Risk tolerance: moderate, debt-averse
- Has shared full balance sheet — reference it before recommending
- Apple Passwords is the canonical password manager

## Things to avoid
- Don't reframe the freelance side hustle as part of the day job
- Don't suggest pausing — the operator picks the cadence
- Don't pull back to small scope when they are thinking big
```

**USER.md is per-agent, not global.** The financial parts of this file matter for Caldwell; they don't matter for Muse. Each agent gets the slice of "about the operator" that's relevant to its job.

---

## tools.md

**Purpose**: The **policy** for what the agent can read, write, and call. The allowlist.

**Author**: You (the operator), with the agent's input. Reviewed when capabilities expand.

**Contents**:

```markdown
# TOOLS — Caldwell

## Read paths
- <agents_root>/caldwell/                          (own folder, full read)
- ~/agents/finance/                                  (operator's financial vault)
- ~/agents/&lt;side-business&gt;/financials/                (side-business P&L, financial subset only)

## Write paths (own folder ONLY)
- <agents_root>/caldwell/memory/
- <agents_root>/caldwell/wiki/
- <agents_root>/caldwell/journal/
- <agents_root>/caldwell/log/
- <agents_root>/caldwell/output/                    (published artifacts)

## External APIs
- Anthropic API (Claude calls per model.md)
- Tavily search (occasional, for current rates / market data)

## Hard NOs
- Never write outside own folder
- Never send email, Telegram, or any external message
- Never move money, log in to financial accounts, or take banking actions
- Never read other agents' folders without explicit authorization
```

### Policy vs. enforcement — be honest about which is which

**`tools.md` is a policy document.** Whether it's actually *enforced* depends on the runtime. This is a real distinction that the spec used to gloss over (Codex review, finding #6, 2026-05-06).

A model that's been told "don't write outside your folder" via markdown can technically write outside its folder if its tools allow it. The runtime's sandbox is what prevents that — or doesn't.

#### Enforcement matrix per runtime

| Runtime | Read-path enforcement | Write-path enforcement | Mechanism |
|---|---|---|---|
| **Cron Python** (`atomic_agents`) | ✅ Helper checks every read | ✅ Helper rejects writes outside `tools.md` paths | Python wrapper around all I/O |
| **Claude Code skill** | ⚠️ Honor system unless helper-mediated | ⚠️ Honor system unless helper-mediated | Claude Code's Read/Write tools have no built-in path allowlist |
| **Codex CLI skill** | ✅ Sandboxed to `-C` repo root, read-only mode | ✅ Sandboxed (read-only) or helper-mediated | OpenAI's sandbox enforces; helper layered on top |
| **ChatGPT web skill** | N/A — no FS access | N/A — no FS access | The "limit" is a feature here |
| **OpenAI API skill** | ✅ Helper-mediated | ✅ Helper-mediated | Same pattern as cron |
| **OpenClaw** | ✅ Plugin sandboxes by default | ✅ Plugin sandboxes by default | Native plugin enforcement |

**The takeaway**: if an agent runs in a runtime where enforcement is "honor system" (Claude Code skill without the helper), `tools.md` is *advisory*. The model will probably follow it (the prompt is clear), but a misaligned model could violate it without anything stopping it.

For high-stakes deployments (anything touching money, secrets, or external systems), use a runtime that **actually enforces** the policy. For low-stakes experimentation, advisory is fine.

#### What Wave 2 of the spec adds

- The shared helper (`atomic_agents`) is the canonical enforcement layer. Every runtime that can use the helper should.
- Runtimes that can't use the helper (ChatGPT web, ad-hoc Claude Code without skill harness) get an explicit "advisory only" warning surfaced to the user.
- A future v2 may add an MCP-server-based enforcement layer that any MCP-capable runtime can plug into. Deferred until needed.

**Why tools.md is its own file, not buried in IDENTITY**:
The agent itself reads tools.md and respects it. Capabilities are operational, not identity. Editing capabilities should never accidentally touch personality. AND: tooling consumers (the helper, sandboxes, MCP) parse tools.md programmatically — easier when it's a single dedicated file.

---

## model.md

**Purpose**: LLM selection, budget, caching strategy.

**Contents**:

```markdown
# MODEL — Caldwell

## Default model
claude-opus-4-7-20260101
(chosen for: financial reasoning depth, judgment under uncertainty)

## Fallback
claude-sonnet-4-6-20260101
(if Opus errors or daily budget exhausted)

## Token budget
- Max system prompt: 12,000 tokens (persona + INDEX + recent notes + tools)
- Max output per turn: 4,000 tokens
- Daily token cap (input+output combined): 200,000

## Prompt caching
- Cache breakpoints after persona, after tools, after INDEXes
- Goal: 80%+ cache hit rate on repeated invocations within 5-min TTL

## Cost guardrail
If daily cap hit:
- Cron runs SKIP until next day (write to log/, no API call)
- Skill invocations FALL BACK to Sonnet for the rest of the day
- Critical-flag invocations override the cap (rare; the operator tags manually)
```

**Why this is its own file**:
Cost profile travels with the agent. Switching from cron to skill to openclaw doesn't reset which model the agent uses. Future model upgrades only edit this one file.

---

## memory/

The Atomic Notes layer — semantic memory. Detailed in [02-atomic-memory](02-atomic-memory.md).

```
memory/
├── INDEX.md                               ← always loaded
├── feedback_communication_style.md
├── feedback_debt_priority_order.md
├── decision_2026-q3-income-target.md
├── project_side_venture_launch.md
├── reference_financial_vault_path.md
├── user_risk_tolerance.md
└── ...
```

Naming: `{type}_{short_topic}.md` — type from the locked taxonomy (user/feedback/project/decision/reference). Lowercase, snake_case, no dates in the filename (date goes in frontmatter).

---

## wiki/ (optional, paired with raw/)

The Atomic Wiki layer — distilled corpus. Detailed in [02-atomic-memory](02-atomic-memory.md).

```
wiki/
├── INDEX.md
├── tax_strategy_2026.md                   ← distilled from raw/tax_doc_*.pdf
├── debt_payoff_methods.md                 ← distilled from raw/financial_book_chapter_*.md
└── credit_score_mechanics.md
```

Pages cite their source docs in frontmatter:

```yaml
sources:
  - raw/tax_doc_2026_planning_guide.pdf
  - raw/cpa_meeting_2026-04-15.md
```

`raw/` holds original ingested documents — unedited. The Wiki is *derivative*; raw is *primary*. Lets you re-derive the wiki, audit claims, or detect drift.

---

## journal/

Narrative, dated entries. Episodic memory. Like a working journal the operator would keep.

```
journal/
├── 2026-04/
│   ├── 2026-04-15.md
│   ├── 2026-04-22.md
│   └── ...
└── 2026-05/
    ├── 2026-05-01.md
    └── 2026-05-06.md
```

**Format**: free-form markdown. Write what happened, what was decided, what was noticed. Loosely structured.

**When to write**:
- After every interactive session (auto-append by skill version)
- After every cron run (auto-append by cron version)
- Manual edits welcome — the operator can add notes too

**Use**: The agent loads the most recent 1-3 journal entries at runtime as recency context. Older entries are searchable but not loaded by default.

---

## log/

Audit trail for autonomous runs. Different from journal — this is structured execution data, not narrative.

```
log/
└── 2026-05/
    └── 2026-05-06.jsonl
```

**Format**: JSONL, one record per run.

```json
{"ts":"2026-05-06T07:00:00-05:00","trigger":"cron","model":"claude-opus-4-7-20260101","input_tokens":3421,"output_tokens":892,"status":"ok","summary":"Daily debt-payoff progress check"}
{"ts":"2026-05-06T11:32:00-05:00","trigger":"skill","model":"claude-opus-4-7-20260101","input_tokens":4102,"output_tokens":2103,"status":"ok","summary":"Quarterly income target review"}
```

**Why structured (not markdown)**: queryable for cost analysis, error rate, etc. The journal is for *content*; the log is for *observability*.

---

## What's NOT in an Atomic Agent (intentionally)

- **No `AGENTS.md` filename**. Collides with two competing meanings (openclaw workspace doc, Soul Spec multi-agent routing). Use IDENTITY.md instead.
- **No `MEMORY.md` as a single monolith**. We use `memory/INDEX.md` + atomic note files. The single-file MEMORY.md pattern doesn't scale past ~30 entries.
- **No `RULES.md`** as a separate file. Hard rules live in IDENTITY (operating doctrine) or tools.md (capabilities). Adding a third is over-fragmentation.
- **No vendor-specific config files**. `openclaw.json` lives in `~/.openclaw/`, not in the agent's vault folder. Same for any other runtime — runtime is separate from the agent.

---

*Next: [02-atomic-memory](02-atomic-memory.md) — the recall subsystem in detail.*
