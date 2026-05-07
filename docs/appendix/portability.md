# Portability — using Atomic Agents without Obsidian, your-server, or Sam's setup

The spec is **platform-agnostic**. Every runtime, vault, and sync mechanism in the docs is one example among many. This page explains what's actually required vs. what's just Sam's particular setup.

---

## What's required (the actual primitives)

To run Atomic Agents you need:

1. **A folder** anywhere on disk. Call it `<agents_root>`. That's it. Each agent gets a subfolder under it.
2. **Markdown files** organized per [../spec/01-anatomy](../spec/01-anatomy.md). Plain text. No special editor.
3. **A way to call an LLM** — Anthropic API, OpenAI API, local model via Ollama, or a runtime like Claude Code, Codex CLI, ChatGPT, or openclaw.
4. **Optionally**, a Python environment if you want the shared helper, the cron runtime, or the dashboard.

Nothing else is mandatory.

---

## What's Sam-specific (and how to substitute)

The docs use Sam's setup as a worked example. Here's what to substitute when you're not Sam:

| Docs say | Substitute with |
|---|---|
| `~/agents/` | `<your_agents_root>` — anywhere you want. `~/agents/`, `~/Documents/agents/`, `D:\agents\`, `/srv/atomic-agents/`, all valid. |
| `your-server` (always-on home server hostname) | Your hostname, or just "the machine where the cron runs." |
| another agent, Caldwell, Harper, Paul, Muse | Your own agent names. Pick whatever makes sense. |
| Obsidian Sync | Any sync mechanism (git, Dropbox, iCloud, Syncthing, none). |
| Telegram bot for alerts | Any channel — email, Slack, Discord, log file, none. |
| `~/agents/finance/` (Caldwell example) | Whatever folder holds your operational data. |

---

## Sync mechanisms — pick one or none

The vault is a folder of markdown. How (or whether) you sync it across devices is your choice:

| Sync option | Pros | Cons |
|---|---|---|
| **Obsidian Sync** | E2E encrypted, built into Obsidian, fast. | Costs money. Obsidian-only. |
| **Git** | Free, version-controlled, works on every device with a CLI. | Manual commit/push; not real-time. |
| **iCloud Drive** | Free on macOS/iOS, automatic. | Apple ecosystem only; sometimes slow. |
| **Dropbox / Google Drive / OneDrive** | Free tiers, cross-platform, real-time. | Third-party storage; potential conflict files. |
| **Syncthing** | Self-hosted, P2P, free. | Setup is non-trivial. |
| **None** | Simplest. | Single-machine only. |

Atomic Agents doesn't care. The spec works with any of the above.

**One note on conflict files**: if your sync mechanism creates conflict copies (Dropbox's `~conflict~`, Syncthing's `~conflict-`, etc.), the lint pass detects them as orphans and surfaces them. Resolve manually.

---

## Editor — Obsidian or anything

The spec uses `wikilinks` in some places — that's an Obsidian convenience. They render as plain text in any other markdown editor (VS Code, Marked, Typora, Vim with markdown plugin, GitHub's renderer). They're not load-bearing; the system works without Obsidian.

If you don't use Obsidian:
- Wikilinks display as raw text — that's fine, they don't break anything
- The dashboard HTML renders in any browser — Obsidian not required
- The agent files are plain markdown — readable in any text editor

If you do use Obsidian:
- Wikilinks click through correctly
- Backlinks panel works
- Graph view shows the relationships between atomic notes

Either way, the **spec is the same**.

---

## Runtimes — pick one or many

The spec describes five runtime forms an Atomic Agent can take. You don't need all five.

| Runtime | What it is | When to use |
|---|---|---|
| **Cron job (Python)** | Scheduled autonomous run via launchd/cron | Daily briefs, periodic reviews, queue processing |
| **Claude Code skill** | `/agent-name` slash command in Claude Code | Interactive chat from Anthropic's CLI |
| **Codex CLI skill** | `$agent-name` in OpenAI's Codex CLI | Interactive chat from OpenAI's CLI |
| **ChatGPT web skill** | Uploaded skill in ChatGPT (Business+) | Chat from the ChatGPT app, including mobile |
| **OpenAI API skill** | Programmatic via `POST /v1/skills` | Embedding agents in your own apps |
| **OpenClaw plugin** | OpenClaw runtime (Anthropic-built harness) | another agent's case; you'd only use this if you specifically want OpenClaw |

Pick whichever match your tools. The vault folder is the same; only the loader differs.

**Minimum viable Atomic Agent**: one cron job (~50 lines of Python) reading the agent folder and calling the API. Everything else is opt-in.

---

## Operating systems — works anywhere markdown does

Tested patterns:

- **macOS** (primary, Sam's setup) — Keychain for secrets, launchd for cron, Obsidian for the editor
- **Linux** — systemd timers for cron, gnome-keyring or pass for secrets, any markdown editor
- **Windows** — Task Scheduler for cron, Windows Credential Manager or 1Password CLI for secrets, VS Code for editing

The shared helper handles platform differences in `lib/atomic_agents/_platform.py`.

---

## Handoff checklist — running Atomic Agents on a fresh machine

If someone hands you this spec and you want to set it up from scratch:

1. **Pick `<agents_root>`** — any folder. `mkdir ~/agents`.
2. **Pick a runtime** — start with cron Python if you want autonomous; Claude Code or Codex CLI if you want interactive.
3. **Install the helper** — `pip install atomic-agents` (when published; today: clone the repo).
4. **Create your first agent** — copy `samples/caldwell/` into `<agents_root>/myagent/`. Strip Sam-specific content from `persona/USER.md`. Replace with your own.
5. **Add an API key** — Keychain, `.env`, or `~/.config/atomic_agents/keys.json` (chmod 600). See [../implementation/cron-agent#secrets-handling](../implementation/cron-agent.md#secrets-handling).
6. **Run it** — `python -m atomic_agents.run myagent` or invoke the skill.
7. **Verify** — check the journal entry got written, check the log JSONL, check the INDEX wasn't corrupted.
8. **Iterate** — edit `persona/IDENTITY.md` and `persona/SOUL.md`. Re-run. Watch the agent change.

---

## What the spec does NOT depend on

To be explicit, none of these are required:

- ❌ Obsidian (the app)
- ❌ Obsidian Sync (the service)
- ❌ always-on home server (your-server)
- ❌ macOS specifically (cross-platform)
- ❌ Tailscale (Sam's network setup)
- ❌ Telegram (just one notification channel)
- ❌ Anthropic specifically (works with OpenAI, local models, anything)
- ❌ Claude Code specifically (one of several runtimes)
- ❌ Anything in `~/projects/automations/` (Sam's specific repo)

If you read something in the spec that *seems* to depend on these and isn't called out as Sam-specific, it's a documentation bug. File it.

---

## Sam's actual deployment (for reference)

If you're curious what Sam's running:

- **Vault location**: `/path/to/your-server/docs/agents/` on his always-on home server (also synced to MacBook + iOS via Obsidian Sync)
- **another agent**: runs inside openclaw on your-server (the special case — see [../spec/06-multi-agent-projects](../spec/06-multi-agent-projects.md) for how openclaw maps to the spec)
- **Caldwell, Harper, Paul, Muse**: cron Python jobs on your-server + Claude Code skills on his MacBook
- **Sync**: Obsidian Sync (paid)
- **Editor**: Obsidian primarily; VS Code for the implementation code in the automations repo
- **Secrets**: macOS Keychain on your-server
- **Notifications**: Telegram bot for cron failures

This is one valid configuration. Yours can be entirely different and still work.
