# ChatGPT-skill-version Atomic Agent

How to deploy an Atomic Agent as a ChatGPT or Codex CLI skill — using the same `SKILL.md` format Claude Code uses, because Skills are now an open standard.

This is the third runtime form an Atomic Agent can take. The other two are [cron-agent](cron-agent.md) and [claude-skill-agent](claude-skill-agent.md). All three read/write the same agent folder.

---

## What changed (May 2026)

OpenAI adopted the **Agent Skills open standard** that Anthropic published in October 2025. Skills are now natively supported in multiple runtimes — but with different levels of integration. Be honest about which is verified working today vs. which is documented but unverified.

### Verified-as-of-research (2026-05-06, via Tavily search)

These were confirmed live by direct search at the time of writing:

| Runtime | Status | Source |
|---|---|---|
| **Claude Code** (`~/.claude/skills/`) | Native, stable | Anthropic published the spec October 2025 |
| **Codex CLI** (`~/.codex/skills/`, `--enable skills`) | Native, requires opt-in flag | [OpenAI Codex Skills Guide](https://developers.openai.com/codex/skills) [^1] |
| **ChatGPT (web/desktop)** | Beta, Business+/Enterprise/Edu/Teachers/Healthcare plans only — not on free or Plus | [Skills in ChatGPT — OpenAI Help Center](https://help.openai.com/en/articles/20001066-skills-in-chatgpt) [^2] |
| **OpenAI API** | `POST /v1/skills` documented | [OpenAI Cookbook: Skills in OpenAI API](https://developers.openai.com/cookbook/examples/skills_in_api) [^3] |
| **OpenClaw** | Uses SKILL.md as its core plugin format | Documented in OpenClaw's plugin docs |

[^1]: OpenAI Codex Skills Guide — accessed 2026-05-06 via Tavily search. Confirms `~/.codex/skills/` location, `$skill-name` invocation, and Markdown frontmatter format.
[^2]: OpenAI Help Center — confirms ChatGPT Skills are in beta on Business/Enterprise/Edu/Teachers/Healthcare plans only as of May 2026; Plus and free users do not have access.
[^3]: OpenAI Cookbook — describes the API skills endpoint, including the bundle/zip upload flow.

### Documented but NOT independently verified

- **Conformance to the open standard across all five runtimes** — the spec claim that "the same SKILL.md works in all five" is *mostly* true based on the docs, but real cross-platform testing has not been done by the maintainer or anyone working on this spec. Treat as a future verification target, not a current guarantee. If you're betting infrastructure on it, write a small test agent and deploy it to each runtime first.
- **`POST /v1/skills` writeback semantics** — the API supports uploading skills, but how (or whether) the API-skill model can persist captures back to the operator's vault has not been verified end-to-end. Bundle-snapshot mode is the safe assumption.
- **OpenClaw's MCP-bridge to ChatGPT** — referenced as "a path forward" but no working bridge exists today.

### Implication for the spec

The spec **does not** assume cross-runtime equivalence works perfectly today. The conformance checklist in [../spec/04-runtime-assembly#runtime-conformance-checklist](../spec/04-runtime-assembly.md#runtime-conformance-checklist) explicitly marks ChatGPT web as `❌ limited` and Claude Code skill as `⚠️ partial`. Wave 1 of any deployment should target one runtime and prove it works before assuming the others will.

This was a free *opportunity* (Anthropic published the spec open; OpenAI adopted the format), not a free *win*. Treat it accordingly.

---

## When to use which target

Three places you can deploy a ChatGPT-side Atomic Agent skill:

### 1. Codex CLI (recommended for Atomic Agents)

✅ **Best fit.** Runs locally, has filesystem access, can read `~/agents/caldwell/` directly the same way Claude Code skills can.

✅ Same loading pattern as the Claude version. The agent reads persona, INDEX, atomic notes from disk on demand.

✅ Same capture pattern. Agent can write atomic notes back to the vault.

❌ Requires Codex CLI installed locally. Currently Mac-only for the desktop app; CLI is broader.

### 2. ChatGPT (web/desktop app)

⚠️ **Limited.** ChatGPT web can't read local files. The Atomic Agent's vault lives on your-server (or MacBook); ChatGPT can't reach it.

Options to bridge the gap:
- **Bundle persona + INDEX + key notes** into the skill itself (uploaded as `resources/`). Loses the "vault is source of truth, runtime just reads it" property — the bundle becomes a frozen snapshot.
- **MCP server** to your-server. ChatGPT Connectors can talk to MCP. You'd run a small MCP server on your-server exposing read access to `~/agents/{name}/`. Then ChatGPT web has live vault access. (This is the path forward for serious ChatGPT-web Atomic Agents — it's not free work but it's real.)
- **Accept staleness.** Re-upload the bundle weekly. Atomic notes captured during a ChatGPT-web session can't write back to the vault — they'd have to be transcribed by the operator after the session.

❌ Not recommended for primary use. Codex CLI does what ChatGPT-web tries to do, but with vault access.

✅ Useful as a **mobile/away-from-laptop fallback**. If the operator needs to chat with Caldwell from their phone via ChatGPT app, a stripped-down skill works — bundle the persona + INDEX, accept that they won't capture new memories from this surface.

### 3. OpenAI API (programmatic)

For automation: same shape as the cron version, just calling OpenAI's API instead of Anthropic's. The skill is uploaded via `POST /v1/skills` and attached to chat completions. This is interchangeable with the cron pattern in [cron-agent](cron-agent.md) — just swap which API the Python client points at.

Useful when the operator wants:
- Cost optimization on certain workloads (GPT models for tasks where they're cheaper)
- Multi-model routing (Caldwell uses Claude, but a workflow inside Caldwell could call GPT for a specific subtask)
- Resilience against single-vendor outage

---

## SKILL.md format (cross-platform)

The format is identical across Claude Code, Codex CLI, and ChatGPT:

```yaml
---
name: caldwell
description: Caldwell — the operator's personal financial advisor. Loads the agent's vault folder
  and provides direct, calm financial counsel on debt strategy, income planning, and
  spending decisions.
---

# Caldwell — financial advisor skill

[skill instructions, identical to Claude version]
```

**Constraints from the agentskills.io spec:**

| Field | Constraint |
|---|---|
| `name` | lowercase letters, numbers, and hyphens only; max 64 chars; can't start/end with hyphen |
| `description` | max 1024 chars; must describe what the skill does AND when to use it |
| Filename | exactly `SKILL.md` (case-sensitive) |
| Frontmatter | YAML; avoid XML angle brackets (can inject into system prompt) |

The body is markdown, free-form. Same content rules as the Claude version.

---

## Codex CLI deployment (the main path)

### Install layout

```
~/.codex/skills/
├── caldwell/
│   └── SKILL.md
├── agent-a/
│   └── SKILL.md
├── agent-b/
│   └── SKILL.md
└── ...
```

If a skill needs supporting files (templates, scripts, examples), they go in the same folder:

```
~/.codex/skills/caldwell/
├── SKILL.md
└── resources/
    ├── balance_sheet_template.md
    └── ...
```

### Same SKILL.md as Claude — minor tweaks

The Caldwell SKILL.md from [claude-skill-agent](claude-skill-agent.md) works with Codex CLI almost as-is. Two differences:

**1. Tool name conventions**
- Claude Code: `Read` tool (capitalized)
- Codex CLI: `shell` tool that calls `cat`, `ls`, etc.

**2. Invocation phrasing**
- Claude: `/caldwell <question>`
- Codex CLI: `$caldwell <question>` (explicit) or implicit via description matching

The agent's *behavior* is identical. Only the loader differs. The Atomic Agents shared helper handles this — it abstracts over which tool reads files.

### Sample SKILL.md adapted for Codex CLI

```yaml
---
name: caldwell
description: Caldwell — the operator' personal financial advisor. Loads the agent's vault
  folder and provides direct, calm financial counsel on debt strategy, income planning,
  spending decisions, and money tradeoffs. Use when the operator asks money questions, wants
  to think through a financial decision, or needs help allocating cash.
---

# Caldwell — financial advisor skill

You are Caldwell. Read the agent's persona and memory files from the vault before responding.

## Setup (every invocation)

Use the shell tool to read these files in parallel:

- ~/agents/caldwell/persona/IDENTITY.md
- ~/agents/caldwell/persona/SOUL.md
- ~/agents/caldwell/persona/USER.md
- ~/agents/caldwell/tools.md
- ~/agents/caldwell/memory/INDEX.md
- ~/agents/caldwell/wiki/INDEX.md

After reading, you embody Caldwell.

## Recall pattern

When the operator asks a question:
1. Identify relevant atomic notes from memory/INDEX.md
2. Read those specific files
3. Read relevant wiki pages from wiki/INDEX.md (if any)
4. If specific dollar amounts needed, read ~/agents/finance/balance_sheet.md and relevant accounts/
5. Reason and respond per Caldwell's persona

## Capture pattern

If the operator says something durable, emit a capture marker in your response:

<atomic_capture>
type: feedback
name: <title>
description: <one-line hook>
confidence: <high|medium|low>
sources: [conversation_<date>]
body: |
  <body content>
</atomic_capture>

After the response, write the file to ~/agents/caldwell/memory/{type}_{topic}.md
and update memory/INDEX.md.

Apply capture rules from ~/agents/Atomic Agents/spec/05-capture-rules.md.

## End of session

Append to ~/agents/caldwell/journal/YYYY-MM/YYYY-MM-DD.md:
- What was discussed
- Captures made
- Open questions

## Hard rules

- Never write outside ~/agents/caldwell/ (see tools.md)
- Never recommend specific securities by ticker
- Never take external actions
- Bottom line first
- Match the operator's communication preferences (per persona/USER.md)
```

### Running it

```bash
# First time: enable skills
codex --enable skills

# Then invoke explicitly
$caldwell Should I prepay the mortgage with the Q1 bonus?

# Or implicitly — Codex matches by description
"I want to think through the bonus check allocation"
```

### Updating

Codex detects skill changes automatically. If updates don't appear:
```bash
codex restart
```

---

## ChatGPT web deployment (the limited path)

### Bundle approach

Create a folder that ChatGPT can ingest as a skill:

```
caldwell/
├── SKILL.md
└── resources/
    ├── IDENTITY.md           ← snapshot copied from vault
    ├── SOUL.md
    ├── USER.md
    ├── tools.md
    ├── memory_INDEX.md
    └── recent_atomic_notes.md  ← concatenated 5 most recent notes
```

Upload via:
1. Click profile icon → Skills
2. Click "New skill" → "Upload from your computer"
3. Select the folder

Or zip and upload via the API:

```bash
zip -r caldwell.zip caldwell/
curl -X POST https://api.openai.com/v1/skills \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -F "file=@caldwell.zip"
```

### SKILL.md for ChatGPT-web (modified)

```yaml
---
name: caldwell
description: Caldwell — the operator' personal financial advisor. Calm, direct, no judgment.
  Bundled persona and memory snapshot. Note: this version has limited vault access; use
  Codex CLI for full Atomic Agents experience.
---

# Caldwell — financial advisor skill (ChatGPT-web variant)

You are Caldwell. Your full persona and recent memory are bundled with this skill in
the resources/ folder.

## Setup

Read these resource files in order:

- resources/IDENTITY.md
- resources/SOUL.md
- resources/USER.md
- resources/memory_INDEX.md
- resources/recent_atomic_notes.md

These are snapshots from the operator's vault as of the upload date. They may be stale — note
this if the operator asks about anything time-sensitive.

## Limitations on this surface

- I cannot read live balance sheet data. If a recommendation requires current numbers,
  ask the operator to share the relevant figures in chat.
- I cannot write atomic notes back to the vault. If something durable comes up, ask
  the operator to capture it manually after this session: "This is worth saving — when you're
  back at your laptop, run /caldwell again and tell me to remember [X]."
- I cannot append to the journal. End-of-session summaries are display-only here.

## Behavior

Otherwise, behave as Caldwell. All persona rules apply. Bottom line first. Calm posture.
Never leave the operator stuck without a path forward.
```

### Refresh cadence for ChatGPT-web

Re-upload the bundle when:
- Persona files change in the vault
- Recent atomic notes have shifted significantly
- The operator notices the agent referencing stale info

Practical cadence: **weekly** if the operator uses ChatGPT-web Caldwell regularly; **on-demand** if it's a fallback.

### A small Python helper for bundle generation

```python
# automations/lib/atomic_agents_chatgpt_bundle.py
def build_chatgpt_bundle(agent_name: str, output_dir: Path) -> Path:
    """Generate a ChatGPT skill bundle from an Atomic Agent's vault folder."""
    vault = Path.home() / "docs" / "agents" / agent_name
    bundle = output_dir / agent_name
    resources = bundle / "resources"
    resources.mkdir(parents=True, exist_ok=True)

    # Copy persona files
    for f in ["IDENTITY.md", "SOUL.md", "USER.md"]:
        (resources / f).write_text((vault / "persona" / f).read_text())

    # Copy tools and INDEX
    (resources / "tools.md").write_text((vault / "tools.md").read_text())
    (resources / "memory_INDEX.md").write_text((vault / "memory" / "INDEX.md").read_text())

    # Concatenate 5 most recent atomic notes
    notes = sorted((vault / "memory").glob("*.md"))
    recent = notes[-5:]
    recent_content = "\n\n---\n\n".join(n.read_text() for n in recent)
    (resources / "recent_atomic_notes.md").write_text(recent_content)

    # Write SKILL.md (template for ChatGPT-web variant)
    (bundle / "SKILL.md").write_text(SKILL_MD_TEMPLATE.format(name=agent_name))

    # Zip
    zip_path = output_dir / f"{agent_name}.zip"
    shutil.make_archive(str(zip_path.with_suffix("")), "zip", str(bundle))
    return zip_path
```

Run: `python -m automations.lib.atomic_agents_chatgpt_bundle caldwell --upload`

---

## OpenAI API deployment (programmatic)

For automation flows that want OpenAI models, the cron pattern from [cron-agent](cron-agent.md) applies almost unchanged — just swap the API client:

```python
from openai import OpenAI

client = OpenAI()

# Upload the skill once
with open("caldwell.zip", "rb") as f:
    skill = client.skills.create(file=f)

# Then call with skills attached
response = client.chat.completions.create(
    model="gpt-5.5",
    messages=[
        {"role": "system", "content": ""},  # skill loads here
        {"role": "user", "content": work_item},
    ],
    skills=[skill.id],
)
```

The skill itself runs in OpenAI's hosted shell (`environment.type="container_auto"`), which means it has filesystem access *inside* the container. To give it access to the vault, you'd need to copy the relevant files into the upload, OR run a sidecar that proxies vault reads.

---

## Cross-platform `model.md` updates

If an agent will deploy to multiple platforms, `model.md` should reflect that:

```markdown
# MODEL — Caldwell

## Default models per runtime

| Runtime | Default | Fallback |
|---|---|---|
| Cron (Anthropic API) | claude-opus-4-7-20260101 | claude-sonnet-4-6-20260101 |
| Cron (OpenAI API) | gpt-5.5 | gpt-5.5-instant |
| Claude Code skill | claude-opus-4-7 (inherits from CC) | sonnet-4-6 |
| Codex CLI skill | gpt-5.5 (Codex default) | gpt-5.5-instant |
| ChatGPT web skill | (whatever the user has selected — Plus/Pro/Enterprise) | — |

## Token budget (cross-platform)

Same budgets apply regardless of platform. The runtime translates them into the
right concept (max_tokens, completion_max_tokens, etc.).
```

---

## The portability principle

The whole point: **one Atomic Agent, multiple deployment targets.**

```
~/agents/caldwell/        ← single source of truth (vault)
        │
        ├──→ Cron Python script  → reads vault directly, calls Anthropic or OpenAI API
        ├──→ Claude Code skill    → ~/.claude/skills/caldwell/SKILL.md, reads vault
        ├──→ Codex CLI skill      → ~/.codex/skills/caldwell/SKILL.md, reads vault
        ├──→ ChatGPT web skill    → uploaded bundle (snapshot), no live vault
        └──→ OpenAI API skill     → uploaded bundle, optional sidecar for vault access
```

Five deployment paths. One agent. The vault is the constant.

This portability is what makes Atomic Agents future-proof: when a new platform emerges (Gemini Skills? Claude Routines? something else?), the agent goes there too without rewriting the agent itself. We just write a new loader/skill-file pointing at the same vault.

---

## What to actually deploy where (recommendation)

For Caldwell / agent-a / agent-b / Muse:

| Deployment | Build it? | Why |
|---|---|---|
| **Cron Python (Anthropic)** | YES | Autonomous scheduled work. The primary autonomous path. |
| **Claude Code skill** | YES | The operator's primary interactive surface — `/caldwell` from their Mac. |
| **Codex CLI skill** | OPTIONAL | If the operator wants OpenAI-side interactive parity, or to A/B test models. |
| **ChatGPT web skill** | NO (yet) | Limited utility without MCP-to-your-server bridge. Revisit when MCP bridge is built. |
| **OpenAI API skill** | OPTIONAL | If a specific automation cost-benefits from GPT pricing. |

Default: cron + Claude Code skill. The Codex CLI deployment is one extra file (the SKILL.md) and gives the operator optionality. ChatGPT-web is on the "later, when MCP bridge exists" pile.

---

## When to revisit ChatGPT-web

Two triggers:

1. **MCP-to-your-server bridge is built.** This is a small Python server exposing vault read access via MCP. Once it exists, ChatGPT-web with a Connector can read live vault content, and ChatGPT-web becomes a real Atomic Agents surface.

2. **The operator finds themselves wanting Caldwell on their phone away from their laptop.** ChatGPT mobile app + a stripped-down Caldwell skill (snapshot bundle, weekly refresh) is workable for casual chat. Real captures wait until they're back at the laptop.

Either trigger justifies revisiting. Until one fires, focus on Claude Code + cron + (optionally) Codex CLI.

---

## Sample: Caldwell deployed to Codex CLI

Two-line install once the SKILL.md is written:

```bash
mkdir -p ~/.codex/skills/caldwell
cp /Users/user/ObsidianVault/Atomic\ Agents/samples/caldwell/skills/codex_SKILL.md ~/.codex/skills/caldwell/SKILL.md
```

(That sample SKILL.md doesn't exist yet — it would live alongside the existing samples/caldwell/ folder if the operator wants to use it. The Claude version of the SKILL.md is the same content with minor tool-name tweaks; the helper can generate either from one source.)

---

*See also: [claude-skill-agent](claude-skill-agent.md) for the Claude Code variant, [cron-agent](cron-agent.md) for autonomous runtime, [shared-helper](shared-helper.md) for the Python library.*
