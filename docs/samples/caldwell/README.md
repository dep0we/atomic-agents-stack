# Caldwell — sample Atomic Agent

This folder is a complete worked example of a single-agent Atomic Agents deployment. It's used throughout the spec as a reference.

> **About the names in this sample.** Caldwell is a fictional financial-planning agent for a fictional user named **Sam** (with spouse **Maya**). Sam works at **Acme Industries** and runs a side consulting practice called **SamCo** with two retainer clients (**ClientCo A** and **ClientCo B**). All of these are illustrative — pick your own names when you build your own agent. Specs throughout `docs/spec/` use Caldwell + Sam as a worked example for the same reason.

---

## What you're looking at

Caldwell is the example financial-planning assistant for the example user, Sam. The persona, memory, and journal entries reflect a believable household financial situation — debt-averse posture, a Q3 income target, retainer income, a side launch by the spouse — to show the spec in use rather than the spec in the abstract.

A generic "DemoAgent" with placeholder content wouldn't show what a real, populated Atomic Agent looks like. Caldwell does.

## If you're not Sam (which you aren't)

Use this as a template structure, not as your agent's content. To adapt for yourself:

1. Copy the folder structure to `<your_agents_root>/<your_agent_name>/`
2. **Rewrite `persona/IDENTITY.md`** — your agent's role, scope, doctrine
3. **Rewrite `persona/SOUL.md`** — your agent's voice and posture
4. **Rewrite `persona/USER.md`** — about *you*, not about Sam
5. **Replace `tools.md`** read/write paths with your actual paths
6. **Replace `model.md`** with your model and budget
7. **Empty `memory/`** except for `INDEX.md` (start with an empty index; populate as you use the agent)
8. **Empty `journal/`** — populate as the agent runs
9. **Empty `wiki/` and `raw/`** if you'll use them

The structure is the spec; the content is yours.

## What this agent's job is (Caldwell-specific)

Personal financial planning assistant for Sam. Helps with:
- Debt elimination strategy and execution
- Income planning across day job + side hustle + spouse's launch
- Investment philosophy and allocation framing
- Spending tradeoffs

NOT a CPA, NOT a lawyer, NOT a securities advisor. Recommends licensed professionals when their domain applies. See `persona/IDENTITY.md` for full scope.

## Files in this sample

```
samples/caldwell/
├── README.md                                    ← you are here
├── persona/
│   ├── IDENTITY.md                              ← who Caldwell is
│   ├── SOUL.md                                  ← personality
│   └── USER.md                                  ← about Sam (the slice Caldwell needs)
├── tools.md                                     ← what Caldwell can read/write
├── model.md                                     ← LLM + budget + cost guardrails
├── memory/                                      ← Atomic Notes
│   ├── INDEX.md                                 ← always-loaded routing
│   ├── feedback_*.md                            ← behavioral corrections + validated
│   ├── decision_*.md                            ← locked architectural choices
│   ├── user_*.md                                ← about Sam
│   ├── project_*.md                             ← active project state
│   └── reference_*.md                           ← pointers to external systems
├── wiki/                                        ← Atomic Wiki (distilled corpus)
│   ├── INDEX.md
│   └── avalanche_vs_snowball.md                 ← one example wiki page
├── raw/                                         ← source documents (would feed wiki/)
├── journal/                                     ← episodic narrative log
│   ├── 2026-05/2026-05-06.md                    ← bonus allocation question
│   └── 2026-05/2026-05-07.md                    ← first run with Atomic Helpers
├── log/                                         ← run audit JSONL (5 days of sample data)
│   └── 2026-05/*.jsonl
├── dashboard.html                               ← hand-built sample dashboard from log data
└── evals/                                       ← Wave 4 evaluation framework
    ├── rubric.md                                ← 5 dimensions, weights, hard fails
    ├── judge.md                                 ← LLM-as-judge prompt template
    └── golden/
        ├── happy/                               ← 2 happy-path tests
        ├── edge/                                ← 1 edge case (stale data)
        ├── adversarial/                        ← 1 jailbreak attempt
        └── decline/                             ← 1 should-refuse test
```

## Internal consistency

This sample passes the validators defined in spec/03-file-formats:
- Every INDEX entry points to an existing file ✓
- Every memory file has full required frontmatter ✓
- All `last_seen` dates are accurate ✓
- No persona/memory duplication remaining ✓
- Financial reasoning is internally consistent ✓

If you find an inconsistency, file it — the sample is meant to be walkable end-to-end.

## What this sample also demonstrates

- **Cost dashboard data**: `log/2026-05/*.jsonl` contains 5 days of realistic run records, including a model-switch day (May 3, Sonnet fallback) and a helper-using day (May 7). Open `dashboard.html` in a browser to see the cost dashboard rendered from this data.
- **Helper pattern (Wave 3.5)**: The May 7 journal entry walks through a parallel-helper run — 3 Haiku calls in parallel + 1 Opus reasoning call, ~76% cost savings vs. all-Opus. See `journal/2026-05/2026-05-07.md`.
- **Eval framework (Wave 4)**: `evals/` contains a populated rubric, judge prompt, and 5 golden tests across all 4 categories (happy, edge, adversarial, decline). Run them via `python -m atomic_agents.eval caldwell` once the runner is built.
