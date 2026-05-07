# spec/18 — Skills Primitive

> Status: **implemented** (PR feat/skills-primitive)
> Cross-links: spec/17 (custom tools — load_skill is implemented as a built-in tool), spec/01 (agent anatomy), spec/04 (system prompt assembly order)

## Overview

Skills are filesystem-based reusable expertise modules that an agent can load on
demand during inference. A skill packages a focused unit of knowledge (e.g.,
"financial-modeling", "spreadsheet-analysis", "legal-citation") into a folder
under `<agent>/skills/<skill-name>/` with a SKILL.md entry point.

**Why skills?** Today an agent's expertise lives in `persona/IDENTITY.md` +
`SOUL.md` + `tools.md`. There is no way to share a unit of expertise across
multiple agents without copy-paste. Skills are stackable, reusable expertise:
more focused than a persona, more reusable than inline instructions.

**Progressive disclosure.** At init, only metadata (name + description) lands
in the system prompt. The model invokes the `load_skill` built-in tool to pull
a skill's full body into the conversation when relevant. This avoids the
"every skill costs context tokens upfront" problem.

## Storage Layout

```
<agent>/skills/
├── <skill-name>/                    # gerund-form, lowercase-hyphens
│   ├── SKILL.md                     # entry point — frontmatter + body
│   ├── reference.md                 # optional supporting files (one level deep)
│   ├── examples.md
│   └── scripts/                     # optional scripts (operator's responsibility to run)
```

Rules:
- Each skill lives in its own subdirectory.
- The entry point is always named `SKILL.md`.
- Supporting files referenced from SKILL.md must be one level deep — no subdirectories
  (enforced by `load_skill_file`).
- Scripts and other operator assets may live under the skill dir but are not
  loaded by the framework; operators run them directly.

## SKILL.md Frontmatter

```yaml
---
name: financial-modeling            # gerund or noun-phrase, lowercase-hyphens, ≤64 chars
description: >
  Builds and analyzes financial models in Excel and Python. Generates three-statement
  models, DCF valuations, and sensitivity tables. Use when the user asks about
  financial projections, valuations, Excel models, or cash flow analysis.
when_to_use: |
  Trigger this skill when the user:
  - Mentions DCF, IRR, NPV, or any financial model acronym
  - Asks to build or review an Excel model
  - Needs a sensitivity analysis or scenario table
  Optional field. ≤200 words.
---
```

### Frontmatter Fields

| Field | Required | Validation |
|-------|----------|------------|
| `name` | yes | `[a-z0-9][a-z0-9\-]*`, ≤64 chars, no reserved words |
| `description` | yes | third-person, ≤1024 chars |
| `when_to_use` | no | ≤200 words |

## Naming Conventions

**Prefer gerund-form names** (present-participle verb phrases):
- `financial-modeling` (not `finance` or `financial-model`)
- `spreadsheet-analysis` (not `spreadsheets`)
- `legal-citation` (not `citations`)
- `contract-review`
- `data-extraction`

Noun phrases are also acceptable when a gerund doesn't fit naturally:
- `tax-law-us` (domain reference)
- `python-pandas` (technology reference)

**Format rules:**
- Lowercase only
- Hyphens as word separators (no underscores, no spaces)
- Digits allowed (e.g., `iso-27001`)
- ≤64 characters

**Reserved words (blocked):** `anthropic`, `claude`, `atomic_agents`

## Description Writing Guidance

The description is the primary signal the model uses to decide whether to call
`load_skill`. Write it so the model can make a confident routing decision.

**Structure (third person, ≤1024 chars):**
```
<What the skill does.> <When to use it.>
```

**Good:**
> Processes Excel and CSV files, generates pivot tables, and summarizes tabular
> data. Use when the user mentions spreadsheets, .xlsx files, CSV data, or asks
> for tabular analysis.

**Too vague:**
> Helps with data.

**Too long (use `when_to_use` for extended routing guidance):**
> Processes Excel and CSV files, generates pivot tables, and summarizes tabular
> data. Also handles JSON data when it can be represented as a table. Can convert
> between formats. Use when the user mentions spreadsheets, .xlsx files, CSV data,
> pivot tables, data summarization, Excel formulas, VLOOKUP, INDEX/MATCH, or asks
> for tabular analysis. Also useful for data cleaning and normalization tasks...

**What to include in description:**
1. Core capability (what it does, in one sentence)
2. Triggering signals (what user phrases / contexts call for this skill)

**What goes in `when_to_use`:**
Extended routing guidance that would bloat the description — edge cases, domain
jargon lists, anti-patterns.

## Progressive Disclosure Pattern

At agent init:
1. `discover_skills()` scans `<agent>/skills/*/SKILL.md` and parses manifests.
2. The system prompt receives a compact `# Available skills` section with each
   skill's name + description only.
3. `load_skill` and `load_skill_file` are registered as built-in tools in the
   agent's `ToolRegistry`.

At inference time:
1. The model reads the skills menu in the system prompt.
2. When a skill is relevant, the model calls `load_skill(skill_name=...)`.
3. The framework returns the skill's full body (SKILL.md content, frontmatter stripped).
4. The model can then call `load_skill_file(skill_name=..., relative_path=...)` for
   any supporting files referenced in the body.
5. Responses incorporate the loaded skill's guidance.

## The `load_skill` Tool

**Schema:**
```python
{
    "type": "object",
    "properties": {
        "skill_name": {
            "type": "string",
            "description": "Name of the skill to load (as listed in Available skills)."
        }
    },
    "required": ["skill_name"]
}
```

**Behavior:**
- Returns the SKILL.md body (frontmatter stripped) as a string.
- If `skill_name` is not a registered skill, raises `ToolHandlerError` with a
  list of available skill names so the model can recover.

## The `load_skill_file` Tool

**Schema:**
```python
{
    "type": "object",
    "properties": {
        "skill_name": {
            "type": "string",
            "description": "Name of the skill that owns the file."
        },
        "relative_path": {
            "type": "string",
            "description": "Path relative to the skill dir, e.g. 'reference.md'."
        }
    },
    "required": ["skill_name", "relative_path"]
}
```

**Security:** `load_skill_file` refuses `../` traversal and any path that resolves
outside the skill directory. Raises `SkillFileTraversal` (mirrors the capture
path-traversal fix). Only one-level-deep paths are supported.

## Body Length Guidance

Anthropic recommends keeping skill files under 500 lines. This is a **warning**
in atomic-agents (not an error) — operators can fix gradually.

When a body exceeds 500 lines, consider:
1. **Extract reference material** into `reference.md` and link it from SKILL.md.
2. **Split into sub-sections** — separate skills for distinct sub-domains.
3. **Add a table of contents** at the top so the model can scan faster.

If a file must be long, add a table of contents:
```markdown
## Contents
1. [Core algorithm](#core-algorithm)
2. [Common patterns](#common-patterns)
3. [Error handling](#error-handling)
```

## Decision Tree: Skill vs Persona vs tools.md

```
Is this expertise shared across multiple agents?
├── Yes → Skill (copy skill dir to each agent, or symlink)
│         Or: a system-level role with the skill pre-loaded
└── No  → Is it part of the agent's core identity?
          ├── Yes → persona/IDENTITY.md or SOUL.md
          └── No  → Is it a tooling/API permission?
                    ├── Yes → tools.md
                    └── No  → Skill (even for a single agent, skills are cleaner
                               than bloating IDENTITY.md with domain knowledge)
```

**Rule of thumb:**
- **Identity** — who the agent is, how it communicates, its values → persona files
- **Permissions** — what APIs/paths the agent may access → tools.md
- **Domain expertise** — how to analyze spreadsheets, write legal memos, build models → skills

## Authoring Checklist

Before shipping a skill:
- [ ] Name is gerund-form, lowercase-hyphens, ≤64 chars
- [ ] No reserved words (`anthropic`, `claude`, `atomic_agents`) in name
- [ ] Description is third-person, ≤1024 chars, includes what + when
- [ ] Body is ≤500 lines (or has a table of contents if longer)
- [ ] Referenced files are one level deep from SKILL.md
- [ ] Run `atomic-agents skills <agent>` — zero warnings

## Comparison to Anthropic's Agent Skills

| Dimension | Anthropic Skills | Atomic Agents Skills |
|-----------|-----------------|----------------------|
| Storage | API-bound, stored in Anthropic infra | Filesystem-based, vault-native |
| Portability | Anthropic-only | Provider-agnostic |
| Loading | Framework-managed (subset injection) | Progressive via load_skill tool |
| Naming | Gerund, lowercase, hyphens | Same (inspired by Anthropic) |
| Description | Third-person, ≤1024 chars | Same (inspired by Anthropic) |
| Body limit | ≤500 lines recommended | Same (warning, not error) |
| References | One level deep | Same |
| Operator control | API / dashboard | Files on disk |

We adopt Anthropic's authoring conventions (gerund names, third-person descriptions,
progressive disclosure, ≤500-line bodies, one-level-deep references) because they are
well-reasoned and match our vault-native model. The key difference: Anthropic Skills
are Anthropic-platform-bound; atomic-agents skills are plain files that work with
any LLM provider.

## Evaluation-Driven Development

When writing a new skill:
1. Write the description before the body — good routing is the hardest part.
2. Test routing: does the model call `load_skill` when it should, and skip it
   when it shouldn't?
3. Test body quality: once loaded, does the model produce better outputs?
4. Check the line count: `atomic-agents skills <agent>` shows body line counts.

## Validation and Linting

Run `atomic-agents skills <agent>` to audit the skill library:
```
$ atomic-agents skills caldwell
  [ok] financial-modeling
       description: Builds and analyzes financial models in Excel and Python...
       body lines:  87

  [warn] data-extraction
         description: Extracts structured data from unstructured documents...
         body lines:  612 (> 500 — consider splitting)
         WARNING: skill 'data-extraction': body is 612 lines (Anthropic recommends ≤500)...
```

Validation rules:
- **Hard errors** (skill skipped): missing `name`, missing `description`, invalid
  name format, reserved words in name.
- **Warnings** (skill loaded with caution): body > 500 lines, description > 1024
  chars, `when_to_use` > 200 words, references deeper than one level.
