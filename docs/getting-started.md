# Getting started with Atomic Agents

This guide takes you from a fresh clone to a running agent in about 15 minutes. By the end you'll have:

- The `atomic_agents` package installed
- A vault directory with one agent
- That agent answering a real prompt
- A cost dashboard you can open in a browser

If you want the design rationale before the how-to, read [`README.md`](README.md) and [`architecture.md`](architecture.md) first. If you want spec depth, [`spec/01-anatomy.md`](spec/01-anatomy.md) is the entry point.

## Prerequisites

- Python 3.11 or 3.12
- [`uv`](https://docs.astral.sh/uv/) (recommended) or plain `pip`
- An LLM provider API key — Anthropic for the default, OpenAI or Moonshot if you'd rather use those

## 1. Install

```bash
git clone https://github.com/dep0we/atomic-agents-stack.git
cd atomic-agents-stack
uv sync --extra dev
```

Verify the install:

```bash
uv run pytest -q
```

You should see `4430 passed` (exact count may vary slightly with skips). If anything fails, the install isn't right yet — fix that before continuing.

## 2. Set your API key

The package looks for keys in three places, in order: environment variables → macOS Keychain → `~/.config/atomic_agents/keys.json`. Pick one.

**Option A — environment variable** (simplest for getting started):

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

**Option B — macOS Keychain** (best for production on a Mac):

```bash
security add-generic-password -a "$USER" -s "atomic-agents-anthropic" -w "sk-ant-..."
```

**Option C — config file** (works everywhere; chmod 600 it):

```bash
mkdir -p ~/.config/atomic_agents
cat > ~/.config/atomic_agents/keys.json <<'EOF'
{"anthropic": "sk-ant-..."}
EOF
chmod 600 ~/.config/atomic_agents/keys.json
```

For OpenAI or Moonshot, swap the env-var name (`OPENAI_API_KEY`, `MOONSHOT_API_KEY`) or the JSON key.

## 3. Pick your vault location

The "vault" is just a directory. Each agent lives in its own subfolder.

```bash
mkdir -p ~/agents
export ATOMIC_AGENTS_ROOT=~/agents
```

You can put `ATOMIC_AGENTS_ROOT` in your shell profile so future sessions pick it up automatically. Or pass `--agents-root <path>` to every command.

## 4. Create your first agent

Each agent is a folder of plain markdown files. Minimum viable structure:

```
~/agents/scout/
├── persona/
│   ├── IDENTITY.md   # who the agent is
│   ├── SOUL.md       # voice and posture
│   └── USER.md       # who you are, from the agent's perspective
├── tools.md          # what it can read/write
├── model.md          # which LLM, what budget
├── memory/           # learned observations (start empty)
├── journal/          # narrative log (start empty)
└── log/              # JSONL run history (start empty)
```

You can copy the [Caldwell sample](samples/caldwell/) as a template — it's a complete worked example for a fictional financial-planning use case. Or scaffold a minimal one:

```bash
cd ~/agents
mkdir -p scout/{persona,memory,journal,log}

cat > scout/persona/IDENTITY.md <<'EOF'
# IDENTITY — Scout

I'm Scout, a research assistant. I help you triage news, summarize
long documents, and surface what's worth your attention.

## Scope
- Reading public web content the user paste in
- Summarizing in plain language
- Calling out the one thing that matters most

## Out of scope
- Browsing the web autonomously (I work from what's given)
- Making purchase or financial decisions
EOF

cat > scout/persona/SOUL.md <<'EOF'
# SOUL — Scout

Direct. No throat-clearing. Surface the one most important thing
first, then the supporting context. If something looks like noise,
say so.
EOF

cat > scout/persona/USER.md <<'EOF'
# USER

The user is a busy practitioner who wants signal over noise. They've
seen too many "here are 10 things you might want to know" lists.
Lead with the recommendation, then the math.
EOF

cat > scout/tools.md <<'EOF'
## Read paths
- ~/agents/scout/

## Write paths
- ~/agents/scout/memory/
- ~/agents/scout/journal/
- ~/agents/scout/log/

## Hard NOs
- never make purchase recommendations
- never recommend specific securities
EOF

cat > scout/model.md <<'EOF'
## Default model

claude-haiku-4-5-20251001

Max input prompt tokens: 12,000
Max output tokens: 2,000

```yaml
cost_guardrails:
  enabled: true
  daily_cap_usd: 1.00
  monthly_cap_usd: 15.00
  daily_cap_action: skip
  monthly_cap_action: alert
  warning_thresholds: [0.50, 0.80]
```
EOF
```

That's a complete agent. The empty `memory/` and `journal/` directories are fine — the agent populates them as it runs.

## 5. Inspect the agent

Before running, sanity-check the config:

```bash
uv run atomic-agents info scout
```

You should see the model, token caps, cost guardrails, and the read/write paths printed back. If anything's missing, the markdown didn't parse the way you expected.

## 6. Run it

```bash
uv run atomic-agents run scout --work-item "Summarize the key points of REST vs GraphQL"
```

The CLI prints the agent's response, then a footer with token counts, cost, latency, captures written, and the run ID.

A new line lands in `~/agents/scout/log/2026-05/2026-05-07.jsonl`. If the agent emits any captures (it probably won't on a one-shot like this), they show up in `~/agents/scout/memory/`.

## 7. View costs

```bash
uv run python -m atomic_agents.dashboard render
open ~/agents/_dashboard/index.html  # macOS; use xdg-open on Linux
```

You'll see an HTML dashboard with the global rollup, per-agent breakdown, daily cost chart, and top-10 most expensive runs. Pure JSONL aggregation — no daemon, always fresh.

For a live view with a Refresh button:

```bash
uv run python -m atomic_agents.dashboard serve
```

Stop with Ctrl-C. Don't bind to `0.0.0.0` unless you mean to expose it.

## 8. (Optional) Set up scheduled runs

Two options depending on your OS — both have ready-to-edit templates in [`extras/`](../extras/).

**macOS — launchd.** Templates in [`extras/launchd/`](../extras/launchd/):

```bash
cp extras/launchd/com.atomic-agents.run.plist.template \
   ~/Library/LaunchAgents/com.atomic-agents.run.scout.plist
# Edit the file: replace __KEY__ placeholders with your paths
launchctl load ~/Library/LaunchAgents/com.atomic-agents.run.scout.plist
```

**Linux — cron.** Wrapper script in [`extras/cron/`](../extras/cron/):

```bash
cp extras/cron/run-atomic-agent.sh ~/bin/
chmod +x ~/bin/run-atomic-agent.sh
# Edit variables at top of the script
crontab -e   # add an entry from extras/cron/crontab.example
```

## 9. Verify your install with `doctor`

Before scheduling a recurring run, make sure every preflight check passes:

```bash
atomic-agents doctor --agent <name>
```

That validates env, Python, vault layout, provider keys, model + cost
guardrails, MCP servers, lock state, memory backend, and write paths in one
shot. Each failing check prints the exact command needed to fix it. Add
`--no-mcp` to skip MCP handshakes if your servers are remote, or `--json`
for liveness-probe output. See [`spec/27-doctor.md`](spec/27-doctor.md) for
the full check catalogue.

Make this the last step of every deploy: doctor exits 0 ⇒ ready to schedule.

## 10. (Optional) Wire into Claude Code

[`extras/claude-code-skills/`](../extras/claude-code-skills/) has SKILL.md wrappers for every CLI surface. Copy the ones you want:

```bash
cp -R extras/claude-code-skills/atomic-agents-* ~/.claude/skills/
```

Then in any Claude Code session you can ask things like *"run scout against this work item"* or *"show me costs"* and it'll route to the right CLI invocation.

## What to do next

- **Add memory.** When the agent learns something durable, capture it as an atomic note. See [`spec/05-capture-rules.md`](spec/05-capture-rules.md) for when and how.
- **Add evals.** Once you've shipped a few agent runs, write golden tests so you can score behavior changes. See [`spec/08-evaluation.md`](spec/08-evaluation.md) and [`implementation/eval-runner.md`](implementation/eval-runner.md).
- **Tune over time.** After you've got eval history, the tuning analyzer (`python -m atomic_agents.tuning <agent>`) detects patterns and proposes edits. See [`spec/11-tuning.md`](spec/11-tuning.md).
- **Build a goal-driven agent.** For agents that pursue a multi-step persistent goal across many sessions, see [`spec/12-goals-and-intent.md`](spec/12-goals-and-intent.md).
- **Multi-agent projects.** For Muse-style setups where multiple roles share a project context, see [`spec/06-multi-agent-projects.md`](spec/06-multi-agent-projects.md).
- **For autonomous deployment:** read [GOVERNANCE.md](GOVERNANCE.md) — pre-deploy checklist, naming conventions, audit practices for solo + small-team operators.

## Troubleshooting

- **`No API key found`.** Re-check step 2. The package prints the lookup order on failure — work through it.
- **`AgentLockBusy`.** Another invocation is running, or a stale lock from a crash. Wait, or check `<agent>/.lock` mtime; the lock auto-releases after 5 minutes.
- **`CostGuardrailBlocked`.** You hit the daily or monthly cap in `model.md`. Either pass `--critical` (logged), or raise the caps.
- **`WritePathViolation`.** The agent tried to write outside the paths in `tools.md`. Check the `Write paths` section.
- **Tests pass locally but my agent errors.** Run `atomic-agents info <name>` first — that surfaces config-parse problems before any LLM call.
