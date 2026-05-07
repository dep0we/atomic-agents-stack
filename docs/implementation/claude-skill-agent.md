# Claude-skill-version Atomic Agent

How to build the interactive version of an Atomic Agent — a Claude Code skill (e.g., `/caldwell`, `/harper`, `/paul`) that loads the agent's vault folder and chats with Dan in-session.

This is the second of two runtime forms. The other is the [cron-agent](cron-agent.md) for autonomous scheduled work. Both read/write the same vault folder, so the agent stays consistent across runtimes.

---

## When to use the skill version

✅ Dan wants to chat with the agent interactively (`/caldwell` "should I prepay this card?")

✅ The interaction needs back-and-forth exchange, not a one-shot run

✅ The agent's job benefits from Dan's iterative input (clarifying questions, exploring tradeoffs)

❌ The work is autonomous and scheduled — that's cron

❌ The work is one-shot text-in-text-out with no need for context — just call the API directly

---

## Where the code lives

User-level Claude Code skills:

```
~/.claude/skills/
├── caldwell/
│   └── SKILL.md
├── highland/                              ← Harper
│   └── SKILL.md
├── dpic/                                  ← Paul
│   └── SKILL.md
└── ...
```

Each skill is just a `SKILL.md` file with frontmatter + instructions. Claude Code loads it when Dan types `/caldwell`.

---

## SKILL.md structure

```yaml
---
name: caldwell
description: Caldwell — Dan's personal financial advisor. Loads the agent's vault folder
  and provides direct, calm financial counsel on debt strategy, income planning, and
  spending decisions. Calm posture, no judgment, never leaves Dan stuck.
---

# Caldwell — financial advisor skill

You are Caldwell. Load this agent's persona and memory from the vault before responding.

## Setup (do this every invocation)

Read these files in order, IN A SINGLE PARALLEL TOOL CALL where possible:

1. `~/docs/agents/caldwell/persona/IDENTITY.md`
2. `~/docs/agents/caldwell/persona/SOUL.md`
3. `~/docs/agents/caldwell/persona/USER.md`
4. `~/docs/agents/caldwell/tools.md`
5. `~/docs/agents/caldwell/memory/INDEX.md`
6. `~/docs/agents/caldwell/wiki/INDEX.md`

After reading these, you embody Caldwell. The persona files define who you are; the
INDEX files tell you what additional memory exists.

## Recall pattern

When Dan asks a question:

1. Identify which atomic notes from `memory/INDEX.md` are relevant.
2. Read those specific note files.
3. Identify which wiki pages from `wiki/INDEX.md` are relevant (if any).
4. Read those specific wiki pages.
5. If the question requires current dollar amounts, read
   `~/docs/finance/balance_sheet.md` AND any relevant `~/docs/finance/accounts/*` files.
6. Now reason and respond per Caldwell's persona.

Do NOT load the entire `memory/` folder. The INDEX is the routing layer; load atomic
notes by name on demand.

## Capture rule

If during the conversation Dan says something durable that should be remembered
(corrections, locked decisions, validated approaches, new user-profile observations),
emit a capture marker in your response:

<atomic_capture>
type: feedback
name: <title>
description: <one-line hook>
confidence: <high|medium|low>
sources: [conversation_<date>]
body: |
  <body content>
</atomic_capture>

The skill harness will detect the marker and write the file. You don't write the
files yourself — that's the harness's job.

Apply the capture rules from `~/docs/Atomic Agents/spec/05-capture-rules.md`. When
in doubt, capture less rather than more.

## End of session

Before exiting, emit a journal block in the same helper-mediated pattern:

```
<journal_entry>
{
  "what_happened": "Bonus allocation question — applied locked debt priority",
  "captures": ["feedback_debt_priority_order.md (last_seen updated)"],
  "open_questions": ["Q3 progress check due end of June"],
  "lint_observations": "No drift, no contradictions"
}
</journal_entry>
```

The harness writes the journal file. The skill does NOT use the Write tool directly — same atomicity / validation / path-enforcement reasoning as captures.

For sessions where a harness isn't available, journal entries can be Dan-curated post-hoc. Don't have the agent freelance writes to the vault.

## Hard rules

- Never write outside `~/docs/agents/caldwell/` — see tools.md
- Never recommend specific securities by ticker
- Never take external actions (email, transfer, login)
- Bottom line first in every response (per pinned feedback memory)
- Match Dan's communication preferences (per persona/USER.md)
```

---

## How invocation works

User types: `/caldwell Should I prepay the mortgage with the Q1 bonus check?`

Claude Code:
1. Loads `~/.claude/skills/caldwell/SKILL.md` into the system prompt
2. The SKILL.md instructions tell Claude to read the vault files first
3. Claude reads persona + INDEXes (parallel tool calls — fast)
4. Claude identifies relevant memories, reads them
5. Claude reads `~/docs/finance/balance_sheet.md` if dollar amounts are needed
6. Claude responds *as Caldwell*

Dan sees the response. He can keep chatting; Claude maintains context within the session.

---

## How captures work

V1 of Atomic Agents requires **helper-mediated writes only**. The agent does NOT write atomic notes directly via the Write tool. The reasons:

1. **Atomicity** — note write + INDEX update must be one logical operation. Direct LLM writes can succeed on the note and fail on the INDEX update, leaving orphans.
2. **Validation** — frontmatter schema must be checked before commit. The helper validates; freeform Write tool calls don't.
3. **Path enforcement** — the helper enforces `tools.md` write paths. Direct Write tool calls bypass that boundary.
4. **Locking** — concurrent cron + skill writes need file locking. Helper handles it; direct writes race.

### How it actually works in v1

Two pathways depending on what the runtime supports — see [../spec/05-capture-rules#capture-markers-the-robust-format](../spec/05-capture-rules.md#capture-markers-the-robust-format) for the full specification.

**Path 1 (preferred where available):** the agent emits a structured **tool call**. Claude Code's skill runtime supports tool definitions — the skill declares an `atomic_capture` tool with a strict JSON schema, and the agent invokes it with typed arguments. SDK-level validation catches malformed inputs before they touch the helper.

**Path 2 (fallback):** the agent emits a fenced markdown code block tagged `atomic_capture` containing JSON:

````markdown
```atomic_capture
{
  "type": "feedback",
  "name": "Q1-bonus-allocation reaffirmation",
  "description": "Dan reaffirmed bonuses route to credit-cards-first per locked priority order",
  "confidence": "high",
  "sources": ["conversation_2026-05-06"],
  "supersedes": null,
  "merge_into": "feedback_debt_priority_order.md",
  "body": "Dan reaffirmed today (re: Q1 bonus question) that bonus checks route to highest-rate credit card first.\n\nThis isn't a new rule — it's the existing locked debt priority being applied to bonus income specifically. The merge_into directive tells the helper to update last_seen and append to sources on the existing note rather than creating a duplicate."
}
```
````

The fenced-code-block format **replaces the deprecated `<atomic_capture>...</atomic_capture>` XML-style tags from the v0 spec.** Triple-backtick fences with the `atomic_capture` language tag are unambiguous in markdown, parser-friendly, and don't have the closing-tag-collision problem the XML format had.

The skill **does not** write the file. The skill harness or the runtime's post-processor extracts capture markers (or tool calls) and calls the shared helper, which handles validation, locking, atomic write, and INDEX update.

### Where this leaves "without a harness" usage

If you want to use the skill without the helper installed, captures are **non-functional** — the agent can still emit markers in its responses, but no writes happen until you connect the helper. This is by design: better to skip captures than to corrupt the vault.

A future v2 may allow direct-write mode with a strict locking + validation discipline, but it's deferred. v1 is helper-only.

Apply the capture *rules* (when to write, when not to) from [../spec/05-capture-rules](../spec/05-capture-rules.md) regardless of mode.

---

## Skill writing best practices

### Keep SKILL.md tight

The whole SKILL.md gets loaded into context every invocation. Don't pad it. Specific instructions > generic prose.

### Don't duplicate persona content in SKILL.md

The persona is in `IDENTITY.md`/`SOUL.md`/`USER.md`. The skill just says "go read those." If you copy persona content into SKILL.md, you have to update two places when the persona changes.

### Reference the spec, don't restate it

```markdown
Apply capture rules from `~/docs/Atomic Agents/spec/05-capture-rules.md`.
```

This way, spec updates propagate to all skills automatically.

### Use parallel reads aggressively

Caldwell reads 5-10 small files at startup. They should all happen in one parallel tool call. Token cost: trivial. Latency: ~1 second instead of 5+.

### Make the capture marker explicit

Don't rely on Claude to "intuit" when to capture. Spell out the format. Spell out when to use it.

### End-of-session journal write

Without it, the agent forgets what happened today. With it, tomorrow's session sees yesterday in the recent-journal load.

---

## Cross-runtime consistency

The cron version and the skill version of Caldwell share the same vault folder. This means:

- An atomic note captured in a skill session is loaded by the next cron run
- A journal entry written by cron is loaded by the next skill session
- Persona edits propagate immediately to both

This is the whole point of "vault is source of truth, runtime is just the loader." A learning anywhere becomes a learning everywhere.

The only caveat: don't run cron and skill *simultaneously* writing the same file. The "writes stay single-threaded" rule applies even within an agent — sequential writes are fine, concurrent writes risk conflicts. In practice this is unlikely (cron runs once a day, skills are interactive), but worth knowing.

---

## Bishop's skill version

Bishop already has Telegram as its interactive surface, so a Claude Code skill version isn't necessary. But if Dan ever wants to chat with Bishop from a Claude Code session, a `/bishop` skill could be added that reads from Bishop's openclaw paths instead of `~/docs/agents/bishop/`.

For all other agents (Caldwell, Harper, Paul, Muse), the skill version is the primary interactive surface.

---

## Comparison: cron vs skill

| | Cron | Skill |
|---|---|---|
| **Trigger** | LaunchAgent on schedule | Dan types `/agent` |
| **Code** | Python script in automations repo | SKILL.md in `~/.claude/skills/` |
| **Conversation** | Single shot | Multi-turn |
| **Load order** | Same canonical order | Same canonical order |
| **Capture** | Parsed from response by Python | Parsed by skill harness OR written by Claude directly |
| **Journal** | Written by Python wrapper | Written by Claude at end-of-session |
| **Log** | Written by Python wrapper | Optional (skill harness can log invocation) |
| **Cost tracking** | Per-run in log JSONL | Per-session, harder to track without harness |
| **Failure handling** | `lib.logger.run()` Telegram alerting | Visible to Dan in chat — failures can't hide |

Most agents have BOTH a cron version (autonomous scheduled work) and a skill version (interactive chat). They share the same vault folder; the runtimes are just different doors into the same agent.

---

*See also: [cron-agent](cron-agent.md) for autonomous runtime, [chatgpt-skill-agent](chatgpt-skill-agent.md) for the same skill running on Codex CLI / ChatGPT, [shared-helper](shared-helper.md) for the Python library shape.*
