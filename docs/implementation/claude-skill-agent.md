# Claude-skill-version Atomic Agent

How to build the interactive version of an Atomic Agent — a Claude Code skill (e.g., `/caldwell`, `/agent-a`, `/agent-b`) that loads the agent's vault folder and chats with the operator in-session.

This is the second of two runtime forms. The other is the [cron-agent](cron-agent.md) for autonomous scheduled work. Both read/write the same vault folder, so the agent stays consistent across runtimes.

---

## When to use the skill version

✅ The operator wants to chat with the agent interactively (`/caldwell` "should I prepay this card?")

✅ The interaction needs back-and-forth exchange, not a one-shot run

✅ The agent's job benefits from the operator's iterative input (clarifying questions, exploring tradeoffs)

❌ The work is autonomous and scheduled — that's cron

❌ The work is one-shot text-in-text-out with no need for context — just call the API directly

---

## Where the code lives

User-level Claude Code skills:

```
~/.claude/skills/
├── caldwell/
│   └── SKILL.md
├── agent-a/
│   └── SKILL.md
├── agent-b/
│   └── SKILL.md
└── ...
```

Each skill is just a `SKILL.md` file with frontmatter + instructions. Claude Code loads it when the operator types `/caldwell`.

---

## SKILL.md structure

```yaml
---
name: caldwell
description: Caldwell — the operator's personal financial advisor. Loads the agent's vault folder
  and provides direct, calm financial counsel on debt strategy, income planning, and
  spending decisions. Calm posture, no judgment, never leaves the operator stuck.
---

# Caldwell — financial advisor skill

You are Caldwell. Load this agent's persona and memory from the vault before responding.

## Setup (do this every invocation)

Run this Bash command first, then Read the single file it produces:

```bash
atomic-agents bundle --if-stale ~/agents/caldwell
```

Then Read: `~/.cache/atomic-agents/bundles/caldwell.md`

The bundle contains the agent's full cascade (persona, tools, memory INDEX, wiki INDEX,
pinned notes, recent notes, recent journal) in canonical spec/04 order. One Bash call +
one Read replaces 6+ sequential file reads. See [`../spec/26-cascade-bundle.md`](../spec/26-cascade-bundle.md).

If `atomic-agents bundle` is unavailable in the operator's environment, fall back to
reading the cascade files directly, IN A SINGLE PARALLEL TOOL CALL where possible:

1. `~/agents/caldwell/persona/IDENTITY.md`
2. `~/agents/caldwell/persona/SOUL.md`
3. `~/agents/caldwell/persona/USER.md`
4. `~/agents/caldwell/tools.md`
5. `~/agents/caldwell/memory/INDEX.md`
6. `~/agents/caldwell/wiki/INDEX.md`

After reading the bundle (or the fallback files), you embody Caldwell. The persona files
define who you are; the INDEX files tell you what additional memory exists.

## Recall pattern

When the operator asks a question:

1. Identify which atomic notes from `memory/INDEX.md` are relevant.
2. Read those specific note files.
3. Identify which wiki pages from `wiki/INDEX.md` are relevant (if any).
4. Read those specific wiki pages.
5. If the question requires current dollar amounts, read
   `~/agents/finance/balance_sheet.md` AND any relevant `~/agents/finance/accounts/*` files.
6. Now reason and respond per Caldwell's persona.

Do NOT load the entire `memory/` folder. The INDEX is the routing layer; load atomic
notes by name on demand.

## Capture rule

If during the conversation the operator says something durable that should be remembered
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

Apply the capture rules from `~/agents/Atomic Agents/spec/05-capture-rules.md`. When
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

For sessions where a harness isn't available, journal entries can be operator-curated post-hoc. Don't have the agent freelance writes to the vault.

## Hard rules

- Never write outside `~/agents/caldwell/` — see tools.md
- Never recommend specific securities by ticker
- Never take external actions (email, transfer, login)
- Bottom line first in every response (per pinned feedback memory)
- Match the operator's communication preferences (per persona/USER.md)
```

---

## How invocation works

User types: `/caldwell Should I prepay the mortgage with the Q1 bonus check?`

Claude Code:
1. Loads `~/.claude/skills/caldwell/SKILL.md` into the system prompt
2. The SKILL.md instructions tell Claude to read the vault files first
3. Claude reads persona + INDEXes (parallel tool calls — fast)
4. Claude identifies relevant memories, reads them
5. Claude reads `~/agents/finance/balance_sheet.md` if dollar amounts are needed
6. Claude responds *as Caldwell*

The operator sees the response. They can keep chatting; Claude maintains context within the session.

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
  "description": "the operator reaffirmed bonuses route to credit-cards-first per locked priority order",
  "confidence": "high",
  "sources": ["conversation_2026-05-06"],
  "supersedes": null,
  "merge_into": "feedback_debt_priority_order.md",
  "body": "The operator reaffirmed today (re: Q1 bonus question) that bonus checks route to highest-rate credit card first.\n\nThis isn't a new rule — it's the existing locked debt priority being applied to bonus income specifically. The merge_into directive tells the helper to update last_seen and append to sources on the existing note rather than creating a duplicate."
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
Apply capture rules from `~/agents/Atomic Agents/spec/05-capture-rules.md`.
```

This way, spec updates propagate to all skills automatically.

### Use `atomic-agents bundle` to collapse startup loads

A realistic cascade (especially the spec/06 three-layer pattern) reads 15+ files at startup
— each Read is its own model round-trip (~1-5s wall time each). Use
`atomic-agents bundle --if-stale <agent>` in Bash to pre-render the cascade to one file,
then a single Read consumes it. Startup latency drops from 30-90s to 1-3s.

See [`../spec/26-cascade-bundle.md`](../spec/26-cascade-bundle.md) for the bundle command's
full contract, including the `bundle.md` declarative-extras file for skill-specific extras
like operator-identity references that aren't part of the standard cascade.

If `atomic-agents bundle` is unavailable, the fallback is parallel reads — fire all
cascade reads in one tool call. Better than sequential but still N round-trips; the
bundle command is the right path for any cascade with more than ~5 files.

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

## another agent's skill version

another agent already has Telegram as its interactive surface, so a Claude Code skill version isn't necessary. But if the operator ever wants to chat with another agent from a Claude Code session, a `/bishop` skill could be added that reads from another agent's openclaw paths instead of `~/agents/bishop/`.

For all other agents (Caldwell, agent-a, agent-b, Muse), the skill version is the primary interactive surface.

---

## Comparison: cron vs skill

| | Cron | Skill |
|---|---|---|
| **Trigger** | LaunchAgent on schedule | The operator types `/agent` |
| **Code** | Python script in automations repo | SKILL.md in `~/.claude/skills/` |
| **Conversation** | Single shot | Multi-turn |
| **Load order** | Same canonical order | Same canonical order |
| **Capture** | Parsed from response by Python | Parsed by skill harness OR written by Claude directly |
| **Journal** | Written by Python wrapper | Written by Claude at end-of-session |
| **Log** | Written by Python wrapper | Optional (skill harness can log invocation) |
| **Cost tracking** | Per-run in log JSONL | Per-session, harder to track without harness |
| **Failure handling** | `lib.logger.run()` Telegram alerting | Visible to the operator in chat — failures can't hide |

Most agents have BOTH a cron version (autonomous scheduled work) and a skill version (interactive chat). They share the same vault folder; the runtimes are just different doors into the same agent.

---

*See also: [cron-agent](cron-agent.md) for autonomous runtime, [chatgpt-skill-agent](chatgpt-skill-agent.md) for the same skill running on Codex CLI / ChatGPT, [shared-helper](shared-helper.md) for the Python library shape.*
