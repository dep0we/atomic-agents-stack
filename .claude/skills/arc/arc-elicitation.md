# /arc profile — judgment profile elicitation

This document guides the one-shot elicitation interview that drafts the operator's judgment profile. Run once per operator (the global core profile spans all repos). The profile is then composed with a per-repo layer at prediction time.

## When to run

Before the first arc-discovery run on any repo where you want shadow-compare predictions. The `/arc discovery` flow checks for the global core profile and reminds you if absent, but does not block: shadow-compare degrades to low-confidence generic predictions without one.

## What the profile is

A structured JSON file the shadow-compare step reads to predict how the operator will rule on Tier-A forks. It contains:

- The seed rule verbatim (required, character-for-character)
- Core decision principles in the operator's own words
- At least one per-domain layer (code and/or content)

It never leaves the local machine. The profile path: `~/.claude/decision-profile/core.json`

A per-repo layer (optional) lives at `.gstack/arc-judgment-profile.json`. The repo layer wins on conflict. Shadow-compare reads both and composes them at prediction time. This file is **committed project knowledge** (like CLAUDE.md) per the locked committed-vs-gitignored ruling for issue #32 — it ships with the repo so the project's decision context is shareable. Only the raw capture ledger and any `*.local.json` working draft stay gitignored (private). If you want to keep an in-progress draft private, name it `arc-judgment-profile.local.json`.

## How to run the elicitation interview

Ask the AI to lead the conversation. Invoke with: `/arc profile`

The AI will:

1. Explain what the profile is and why it exists.
2. Ask a short set of questions about the operator's decision principles.
3. Draft the profile from the answers.
4. Write it to `~/.claude/decision-profile/core.json` atomically.

The interview takes roughly 5–10 minutes.

## Elicitation interview script (for the AI to follow)

When the operator invokes `/arc profile`, follow this interview:

### Step 1: Explain the context

"I'm going to ask you a few questions to build your judgment profile — a record of how you decide so the arc loop can start measuring how well it can predict your rulings.

This is a one-time setup. The profile stays on your machine and is never sent anywhere. When arc surfaces a Tier-A fork, it will use this profile to make a silent prediction, then I'll ask you to rule as usual. Over time, we'll measure how well the predictions match your rulings.

Ready? This takes about 5 minutes."

### Step 2: Seed rule (required — capture verbatim)

Ask: "I'll start with the foundational rule. The arc loop is already seeded with this principle — confirm it matches your instinct:

**'prefer the recommendation with the best high-quality, long-term outcome; quality over speed/cost'**

Does this reflect how you approach most forks? Any caveats or adjustments?"

Listen. If the operator accepts it verbatim, record it exactly as written above. If they adjust it, record their version VERBATIM (character-for-character) in the `seedRuleVerbatim` field. Do NOT paraphrase.

### Step 3: Core principles

Ask three to five open questions. Use these as a guide — adapt based on earlier answers:

- "When two options both work technically, what tips the balance for you? (Example: simpler implementation vs richer abstraction?)"
- "How do you weigh reversibility? If an option is easier to reverse later vs one that commits you to a direction, how does that factor in?"
- "What kinds of shortcuts do you find most worth calling out — and which do you accept as fine trade-offs?"
- "Is there a past decision you'd make differently now? What principle did that reveal?"
- "On security vs velocity: when do you stop and review vs when do you accept the risk and move on?"

Take notes. Synthesize into 3–7 named principles using the operator's own words.

### Step 4: Per-domain layers

Ask: "The arc loop can serve both a code project (like Atomic Agents) and a content project (like Penny Press). Are there places where your judgment shifts between those contexts — different trade-offs, different quality bars?"

If yes, capture the distinction as a per-domain layer.

### Step 5: Draft and write

Draft the profile JSON and show it to the operator for confirmation before writing. The structure:

```json
{
  "schemaVersion": 1,
  "createdAt": "<ISO-8601 timestamp>",
  "seedRuleVerbatim": "prefer the recommendation with the best high-quality, long-term outcome; quality over speed/cost",
  "corePrinciples": [
    { "id": "quality-over-speed", "statement": "<operator's words>", "caveats": [] },
    ...
  ],
  "domainLayers": {
    "code": {
      "notes": "<any code-specific adjustments>",
      "subtypeWeights": {}
    },
    "content": {
      "notes": "<any content-specific adjustments>",
      "subtypeWeights": {}
    }
  },
  "elicitedAt": "<ISO-8601 timestamp>"
}
```

**Critical**: the `seedRuleVerbatim` field must contain the literal string `prefer the recommendation with the best high-quality, long-term outcome; quality over speed/cost` unless the operator explicitly gave a different verbatim rule.

### Step 6: Write atomically

Write to `~/.claude/decision-profile/core.json` using a temp-file-then-rename pattern:

0. Create the directory if absent: `mkdir -p ~/.claude/decision-profile/` (on a fresh machine this dir does not exist yet, and the temp write would fail otherwise).
1. Write the JSON to `~/.claude/decision-profile/core.json.tmp`
2. Verify it parses as valid JSON
3. Rename it to `~/.claude/decision-profile/core.json` (atomic on POSIX filesystems on the same filesystem)
4. Store a one-slot backup of the previous profile at `~/.claude/decision-profile/core.json.bak` (overwritten on each save) for crash recovery

Confirm: "Your judgment profile is saved. Shadow-compare will use it silently on the next arc-discovery run."

## Per-repo profile layer

If the operator wants project-specific overrides (different quality bar for content vs code, or a project-specific principle), create `.gstack/arc-judgment-profile.json` in the repo with:

```json
{
  "schemaVersion": 1,
  "projectName": "<project>",
  "domain": "code",
  "overrides": [
    { "principleId": "<id from core>", "adjustment": "<what changes in this repo>" }
  ],
  "additionalPrinciples": []
}
```

This file is **committed project knowledge** (like CLAUDE.md) per the locked committed-vs-gitignored ruling for issue #32, so it ships with the repo and is shareable across a team by default. It is read-only during a shadow-compare run — the step reads it but never writes to it. If you want a private working draft instead, name it `arc-judgment-profile.local.json` (gitignored).

## Gitignore notes

The global core profile (`~/.claude/decision-profile/`) lives outside any repo and is never tracked in git. The per-repo profile (`.gstack/arc-judgment-profile.json`) is **committed** (project knowledge, like CLAUDE.md) per the locked committed-vs-gitignored ruling for issue #32 — `install.sh` does NOT gitignore it. Any `*.local.json` working draft IS gitignored (private), as is the ledger (`.gstack/arc-rulings/decisions.jsonl`, covered by the `.gstack/arc-rulings/` pattern `install.sh` adds).
