# 05 — Capture Rules

When to write a memory. When to promote it. When to lint. When to archive.

The hardest part of the spec. Get this wrong and memory becomes either a noise-heavy junk drawer or a dead corpus that never grows.

---

## When to capture an Atomic Note

The agent decides during a session whether something is worth saving as a durable memory.

### Capture when

✅ **User explicitly asks**: "remember this", "save that", "don't forget"

✅ **User corrects the agent**: "no, not like that — do it this way"
- Save as `feedback` type. The correction is the rule. Body should explain the *why* if user gave one.

✅ **User confirms a non-obvious approach worked**: "yes, exactly", "perfect, keep doing that"
- Save as `feedback` type. Validated approaches are as important as corrections.

✅ **User locks a decision after weighing alternatives**: "ok, let's go with X over Y because Z"
- Save as `decision` type. Capture all three: what won, what lost, why.

✅ **User reveals something durable about themselves**: role, preferences, constraints
- Save as `user` type. Slice should be relevant to this agent's job.

✅ **User mentions a tool, system, person, or location the agent should know exists**
- Save as `reference` type. Pointer + when to use it.

✅ **Surprising / non-obvious info that future-you would re-derive painfully**
- Save with appropriate type. Ask: "if I forgot this, how much pain to recover?"

### Do NOT capture

❌ **Information already in persona files** — duplication corrupts the trust hierarchy

❌ **Information derivable from current code/file state** — files are the source of truth, not memory

❌ **Routine task outputs** — "I generated a report on Tuesday" is journal content, not memory

❌ **Anything in git history or a tool's audit log** — those are authoritative; memory is for context

❌ **Ephemeral conversation context** — one-off task details die with the session

❌ **Things that change frequently** — if it'll be wrong in a week, don't write a memory; write a journal entry

### The acid test

Before writing a memory, ask: **"would this surprise a fresh-start agent?"**

If reading the code, running git log, or skimming the persona files would surface it — don't write a memory. The memory layer is for things that *aren't* otherwise discoverable.

---

## How to capture

When the agent decides to capture, the steps are:

1. **Pick the type**: user / feedback / project / decision / reference
2. **Pick a filename**: `{type}_{topic}.md` — lowercase, snake_case, specific
3. **Check for duplicates**: search INDEX for similar entries; if one exists, update it instead
4. **Write the file** with full frontmatter (see [03-file-formats](03-file-formats.md))
5. **Update INDEX.md** with a one-line entry under the right type section
6. **Optionally write a journal note** linking to the new memory

Steps 4-6 are atomic — an orphan memory file (no INDEX entry) is a bug. The agent should validate after writing.

### Capturing factual claims (cite the source)

When a memory captures a factual claim about the user's data or external sources, the capture **must cite the source file**. Per [13-research-integrity](13-research-integrity.md), this is the floor for capture quality on facts.

In the `body` of the captured note, include the citation inline:

```markdown
The user's highest-rate credit card APR is 24.99% as of 2026-05-04 [per ~/agents/finance/balance_sheet.md].
```

In the `sources` frontmatter field, include the file path AND the conversation reference:

```yaml
sources:
  - ~/agents/finance/balance_sheet.md
  - conversation_2026-05-08
```

When a fact has no source file (general knowledge, agent reasoning), explicitly mark it:

```markdown
Avalanche method on credit cards is mathematically optimal for typical APR spreads.
[General knowledge — no source file. Confirmed via web research 2026-05-08.]
```

Captures that include factual claims without source citations should be rejected by the helper's validation (per spec/03 validation rules) when the agent's `research_integrity.layer_1_citations` is enabled in `model.md`.

### Body structure for each type

**`feedback`** — lead with the rule, then `**Why:**`, then `**How to apply:**`

```markdown
The user prefers the bottom line before supporting math.

**Why:** They have pattern-matched on consultants who bury the lede. Specific enough that
a one-paragraph executive summary up front is ALWAYS expected.

**How to apply:** Open every analysis with a 1-3 sentence "what to do" before any
working. Math, charts, comparisons go below the fold.
```

**`decision`** — what won, what lost, why, when, by whom

```markdown
**Decided 2026-04-15:** Avalanche method on credit cards before any mortgage prepayment.

**Considered:** Snowball (psychology), mortgage prepay (math says equal-ish at current rates).

**Won because:** the user's specific anxiety pattern weights "balance reaching zero" heavily.
Mortgage prepay only wins by basis points and the user accepts that tradeoff.

**Confirmed by:** the user, in conversation 2026-04-15.
```

**`user`** — concise factual content about the operator

```markdown
The user's risk tolerance is moderate, debt-averse. They have explicitly said "I do not want to
optimize for gains; I want to extinguish liabilities." Reference frame: 2026 financial reset.
```

**`project`** — current state, who's doing what, when, blockers

```markdown
**Household side venture launch** — status: planning phase, no clients yet.

**Goal:** Add $X/mo gross revenue from the side venture by Q3 2026.

**Blockers:** No website yet, pricing undecided, target client profile not locked.

**Last update:** 2026-05-01 — household alignment conversation, decided to pursue
B2B service offerings rather than e-commerce.
```

**`reference`** — pointer + when to use it

```markdown
**Financial vault:** ~/agents/finance/ on your-server

Contains current balance sheets, income statements, all account snapshots. Updated weekly
by the user. Reference this BEFORE recommending any specific dollar amount or strategy —
recommendations should be grounded in current numbers, not assumed.
```

---

## Promotion: Atomic Notes → Persona

Some atomic notes mature. They get confirmed enough times, applied across enough contexts, that they should *always* be in scope, not just selectively loaded.

### Promotion triggers

A memory is **promotion-eligible** when ANY of:

1. **Reference count**: referenced in 5+ sessions over 30+ days without contradiction
2. **Confirmation count**: confirmed at 3+ distinct moments by the user
3. **Foundational nature**: type=decision with confidence=high and impact spans multiple agent functions
4. **User declares**: "this should always be in mind" or equivalent

### Promotion process

1. **Agent flags the candidate** at end-of-session: "this memory is promotion-eligible — recommend moving to persona/USER.md section X"
2. **The operator reviews** the proposed promotion. May edit the wording.
3. **Promote** — the content moves into the appropriate persona file (IDENTITY / SOUL / USER) under the right section
4. **Mark original** — set `superseded_by: persona/USER.md#section_anchor` on the source atomic note. Don't delete; the supersession chain is the audit trail.
5. **Update INDEX** — add an entry under `## Recently Promoted to Persona` for visibility

### Where to promote

| Source memory type | Promotes to |
|---|---|
| `user` (about the operator) | `USER.md` |
| `feedback` (how to behave) | `SOUL.md` (if about voice/posture) or `IDENTITY.md` (if about doctrine) |
| `decision` (locked choice with broad scope) | `IDENTITY.md` operating doctrine section |
| `reference` (foundational tool/system) | Usually NOT promoted — references stay as references |
| `project` (transient work state) | NEVER promoted — projects are inherently temporary |

### Don't auto-promote

Promotion is high-stakes. Auto-promoting without review risks corrupting the persona files. The agent flags candidates; the operator approves. Keep the trust gradient.

---

## Demotion / archiving

The reverse loop: persona content that no longer holds gets demoted, and atomic memories that go stale get archived.

### Demotion (rare)

If an IDENTITY/SOUL/USER section becomes wrong (e.g., the user's role changes, scope shifts):

1. **Don't delete** — move the section into a `feedback` atomic note with `superseded_by` pointing at the new content
2. **Edit the persona file** with the new truth
3. **Add a new atomic note** capturing the change as a `decision` type

### Archiving stale atomic memory

Lint pass identifies stale candidates:

- `last_seen` more than 90 days old AND not pinned
- `expires_at` passed
- `superseded_by` set (the chain has moved on)

Archived memory:
- Stays in `memory/` (not deleted)
- Gets `archived: true` added to frontmatter
- Removed from active INDEX, moved to `## Archive` section of INDEX
- Loader skips archived files at runtime unless explicitly requested

Why not delete? **Audit trail**. Knowing what was true once is sometimes critical (e.g., "we used to think X — when did that change and why?").

---

## Lint pass

Run periodically (daily for active agents, weekly otherwise) to surface memory quality issues.

### Lint checks

1. **Duplicate detection**: notes with similar `name` or `description` strings (Levenshtein distance ≤ ~5)
2. **Contradiction detection**: notes with same `type` and overlapping `sources` but conflicting body content
3. **Staleness**: notes with `last_seen` > 90 days, not pinned, not promoted
4. **Expired**: notes whose `expires_at` has passed
5. **Orphans**: atomic note files not referenced in INDEX, OR INDEX entries pointing at missing files
6. **Schema drift**: notes without all required frontmatter fields, or with invalid enum values
7. **Provenance gaps**: notes with `confidence: high` but `sources` empty or only `observation`

### Lint outputs

A markdown report at `~/agents/{name}/log/lint_YYYY-MM-DD.md`:

```markdown
# Lint report — Caldwell — 2026-05-06

## Possible duplicates (3)
- feedback_communication_style.md ↔ feedback_communication_preferences.md
- ...

## Possible contradictions (1)
- decision_2026-q3-income-target.md vs decision_2026-q3-target-revised.md
  - Both confidence: high, both type: decision
  - Resolve: which is current?

## Stale (4)
- project_side_venture_launch.md (last_seen 2026-02-10, 86 days)
- ...

## Schema drift (0)

## Orphans (0)
```

The operator reviews. Fixes by editing files (or asking the agent to fix specific items). Lint is a *signal*, not an automatic action.

---

## Conflict resolution

When two memories disagree:

### Default rule: newer wins, both stay

- The older note gets `superseded_by: <newer.md>`
- The newer note gets `supersedes: <older.md>`
- Both files persist. The chain is the history.
- Loader respects `superseded_by` and skips outdated entries by default.

### Surface to the operator when

- Older has `confidence: high` AND newer has `confidence: low` (suspicious)
- Newer's source is `observation` only, older's source is direct user statement
- Conflict spans more than 2 entries (something deeper is wrong)

In these cases, the lint pass flags the conflict. Don't auto-supersede.

### Never auto-delete

Supersession is non-destructive. Deletion is final. Always supersede.

---

## Capture cadence per runtime

| Runtime | Capture frequency | Where it triggers |
|---|---|---|
| **Claude skill** (interactive) | Per session, end-of-conversation prompt | Skill instructions ask agent to flag captures before exiting |
| **Cron job** (autonomous) | Per run, if anything novel happened | Cron Python wrapper inspects model output for capture markers |
| **openclaw** | Per turn, automatic | memory-core handles via tools (`memory_append`, `wiki_apply`) |

For cron and skill versions, the agent must be explicit about captures — output a structured marker in its response that the runner parses. The shared Python helper handles writing the file + updating INDEX.

---

## Capture markers — the robust format

Codex review (finding #14) flagged that the original capture marker format (YAML inside XML-style tags) was fragile: YAML edge cases on colons and indentation, the closing `</atomic_capture>` tag potentially appearing in body text, malformed arrays, model formatting drift. Wave 1 partially addressed this by switching to JSON. Wave 5 specifies the full robustness rules.

### Two capture pathways (in order of preference)

#### Path 1 (preferred): Tool calls

When the runtime supports tool use (Claude API, OpenAI API, Codex CLI with skills, OpenClaw plugins), the agent emits a **structured tool call** instead of a text marker:

```python
# Tool definition the agent sees
{
    "name": "atomic_capture",
    "description": "Capture a durable observation as an atomic memory note. "
                   "Use only when the observation is genuinely durable per "
                   "spec/05 capture rules. When in doubt, capture less.",
    "input_schema": {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["user", "feedback", "project", "decision", "reference"]},
            "name": {"type": "string", "maxLength": 80},
            "description": {"type": "string", "maxLength": 200},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "sources": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "supersedes": {"type": ["string", "null"]},
            "merge_into": {"type": ["string", "null"]},
            "body": {"type": "string"}
        },
        "required": ["type", "name", "description", "confidence", "sources", "body"]
    }
}
```

The model invokes the tool with structured arguments. The runtime extracts them via the API's tool-call mechanism — no parsing fragility because the model never emits free text for the capture; the SDK delivers a typed object.

Validation is automatic at the SDK level (schema-validated tool inputs). If the model produces a malformed tool call, the SDK rejects it before the helper sees it.

This is the **v1 default for runtimes that support tool calls.**

#### Path 2 (fallback): Fenced-code-block markers

For runtimes that don't support tool calls (some streaming flows, ad-hoc API integrations, edge cases), the agent emits a **fenced markdown code block** with a specific language tag:

````markdown
```atomic_capture
{
  "type": "feedback",
  "name": "Bottom-line-first communication preference",
  "description": "The user wants the recommendation before the supporting math",
  "confidence": "high",
  "sources": ["conversation_2026-05-06"],
  "supersedes": null,
  "merge_into": null,
  "body": "The user prefers the bottom line before supporting math.\n\n**Why:** Pattern-matched on consultants who bury the lede.\n\n**How to apply:** Open every analysis with 1-3 sentences of 'what to do' before working."
}
```
````

**Why fenced code blocks instead of XML-style tags:**

- Triple-backtick fenced blocks are unambiguous in markdown — every parser knows the rule
- Models are very good at producing them correctly
- The closing fence (` ``` `) only collides if the body contains literal ``` — handled below
- Language tag (`atomic_capture`) makes the block machine-discoverable without conflicting with normal code rendering

### Strict parsing rules (Path 2)

The runner extracts captures using these rules, in order:

1. **Find blocks** — scan the response for ``` ```atomic_capture ``` followed by content followed by closing ``` ``` ``` on its own line.
2. **Parse content as JSON** — strict mode. Trailing commas, comments, single quotes — all rejected.
3. **Validate against schema** — same schema as the tool-call version (above).
4. **If validation fails** — log the failure with the bad content. Do NOT write the file. Surface to operator.

#### Handling triple backticks in body content

If the model legitimately needs to put triple backticks inside the JSON body string (rare for memory captures but possible — e.g., Caldwell capturing a code-snippet observation), use **quadruple-backtick fences** instead:

`````markdown
````atomic_capture
{
  "body": "The user asked me to remember this code pattern:\n\n```python\nlambda x: x.lower()\n```\n\nIt's the avalanche-classifier pattern from the work session."
}
````
`````

The runner handles both `` ``` `` (3) and `` ```` `` (4) fences. Models default to 3; bump to 4 only when the body genuinely contains 3-backtick fences.

### Multiple captures per response

A single agent response may emit multiple capture markers — each one a separate atomic note write. The runner extracts ALL fenced `atomic_capture` blocks (or all tool calls) and processes them in order.

```python
def extract_captures(response_text: str) -> list[Capture]:
    captures = []
    # Match both 3- and 4-backtick fences
    pattern = r"^(`{3,4})atomic_capture\n(.*?)\n\1$"
    for match in re.finditer(pattern, response_text, re.MULTILINE | re.DOTALL):
        json_content = match.group(2)
        try:
            data = json.loads(json_content)
            captures.append(Capture(**data))
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            log_capture_parse_failure(json_content, str(e))
            # Don't add to captures; helper continues with other valid ones
    return captures
```

### Validation before write

Every capture (Path 1 or Path 2) goes through the same validation pipeline before the helper writes it:

1. ✅ All required fields present (`type`, `name`, `description`, `confidence`, `sources`, `body`)
2. ✅ `type` in the locked taxonomy
3. ✅ `confidence` in `{high, medium, low}`
4. ✅ `name` ≤ 80 chars; `description` ≤ 200 chars
5. ✅ `sources` is a non-empty array of strings
6. ✅ If `supersedes` set, the referenced file exists
7. ✅ If `merge_into` set, the referenced file exists
8. ✅ Filename derived from name (per spec/03 naming convention) doesn't collide with an existing file unless `merge_into` is set

Any validation failure → capture rejected, surfaced to operator. The agent's response is still delivered (the user gets their answer); just the side-effect write is blocked.

### When the model emits `</atomic_capture>` in body text

Specifically the failure mode Codex flagged. With the new fenced-code-block format, this is no longer an issue — the closing fence is ``` ``` ``` (or ` ```` `), not the angle-bracketed tag. If a model ever emits literal `</atomic_capture>` in body content (e.g., explaining the format itself in a memory), it's just a string within JSON — it doesn't terminate the block.

The original XML-style format is **deprecated** and removed from the spec as of Wave 5. Any agent still emitting it is using an old prompt; update the agent's persona/SOUL or the skill SKILL.md to use the new format.

### When the JSON itself is malformed

Models occasionally emit JSON with subtle issues (smart quotes, missing commas, unescaped newlines in strings). Strategies, in order:

1. **Strict parse** first — use `json.loads()` with no leniency. If it works, done.
2. **Targeted repair** — if it fails, try fixing common issues automatically:
   - Replace smart quotes with straight quotes
   - Strip trailing commas
   - Repair string newlines (real newlines → `\n` escapes)
3. **If repair fails** — log the raw content, surface to operator, do NOT write.

The repair pass is opt-in (`--repair-malformed-captures` flag on the helper). Default behavior is strict — better to lose a capture than to corrupt the schema.

### Tool calls are the right answer when available

The fenced-code-block format works. But tool calls are strictly better when the runtime supports them:

| Concern | Tool calls | Fenced blocks |
|---|---|---|
| Schema validation | At SDK level, before content is generated | After parsing, may have already produced bad output |
| Robustness to model formatting drift | Strong — typed argument inputs | Weaker — depends on model adherence to fence format |
| Multi-capture per response | Native — multiple tool calls | Native — multiple blocks |
| Streaming | Tool calls block streaming until complete; predictable | Streaming can produce partial blocks; runner needs to handle EOF |
| Cross-runtime | Limited to runtimes with tool-call support | Universal — any text response can have fenced blocks |

**Default to tool calls.** Use fenced blocks as the explicit fallback for runtime contexts that don't support tools.

### Idempotency

The helper deduplicates within a single run: if the same response emits two identical captures (same name, same body), only one write happens. This protects against models that emit the same capture twice in one response by accident.

Dedup key: (`type`, `name`, body hash). If two captures match, keep the first; log the dup as a warning.

---

---

## Anti-patterns

❌ **Capturing every conversation in full** → memory becomes noise

❌ **Capturing things derivable from code** → memory rots when code changes

❌ **Auto-promoting after N references without review** → persona corruption

❌ **Deleting memory that disagrees with new info** → losing audit trail

❌ **Pinning more than ~3-5 memories** → tax on every call

❌ **Refusing to capture without user prompt** → memory stays static, agent doesn't learn

The right cadence: capture frequently when something durable happens; promote rarely; archive periodically; never delete.

---

*Next: [06-multi-agent-projects](06-multi-agent-projects.md) — composition pattern when multiple agents share a project.*
