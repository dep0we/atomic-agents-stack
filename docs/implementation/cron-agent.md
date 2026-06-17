# Cron-version Atomic Agent

How to build the autonomous, scheduled version of an Atomic Agent — Python script triggered by `launchd` or cron, runs against the agent's vault folder, writes outputs to `journal/` and `log/`.

This is one of two runtime forms an Atomic Agent takes. The other is the [claude-skill-agent](claude-skill-agent.md) for interactive chat. Both read/write the same vault folder.

---

## When to use cron

✅ The agent has scheduled, recurring work (daily morning brief, weekly retrospective, quarterly review)

✅ The agent processes a queue (work items written by the operator or another agent)

✅ The agent watches for triggers (file changes, calendar events, external API webhooks)

❌ The agent is purely reactive to the operator's chat — that's the skill version

❌ The agent runs more frequently than every ~5 minutes — at that cadence, run a long-lived service instead

---

## Where the code lives

In an `automations` repo on GitHub (`user/automations`), under:

```
~/projects/automations/                        ← MacBook dev workspace
└── jobs/
    └── agents/
        ├── caldwell_daily_brief.py
        ├── caldwell_weekly_review.py
        └── ...
```

Deployed to your-server at `/path/to/your-server/automations/` via the existing `ai.your-server.automations-deploy` LaunchAgent (auto-pulls every 5 min).

LaunchAgent plists live in:

```
~/projects/automations/launchd/
├── ai.your-server.caldwell-daily-brief.plist
└── ...
```

---

## High-level flow

```
LaunchAgent fires on schedule
       ↓
Python script: jobs/agents/caldwell_daily_brief.py
       ↓
Calls atomic_agents to:
  1. Load agent files (persona, INDEXes, recent notes, recent journal)
  2. Assemble system prompt per spec runtime-assembly order
  3. Build user message (the work item — for cron, usually a structured prompt
     that says "do today's brief")
  4. Call Anthropic API (model from model.md)
  5. Parse response for capture markers
  6. Write capture markers to memory/, update INDEX
  7. Write journal entry
  8. Write log record
       ↓
Outputs persist in ~/agents/caldwell/
       ↓
another agent or the operator reads outputs as needed
```

---

## Skeleton script

```python
#!/usr/bin/env python3
"""
Caldwell daily brief — runs every morning at 06:30 CT.
Reviews yesterday's financial activity, surfaces anything that needs attention,
writes the brief to journal/ and a summary to ~/agents/automations/output/caldwell/.
"""

from pathlib import Path
from automations.lib import logger, telegram
from automations.lib.atomic_agents import AtomicAgent

AGENT_NAME = "caldwell"
VAULT_ROOT = Path.home() / "docs"

def main():
    agent = AtomicAgent(
        name=AGENT_NAME,
        vault_root=VAULT_ROOT,
        trigger="cron",
    )

    # 1. Load
    agent.load_persona()
    agent.load_indexes()
    agent.load_pinned_atomic_units()
    agent.load_recent_atomic_units(n=5)
    agent.load_recent_journal(n=1)

    # 2. Build the work item for cron
    work_item = """
    Daily brief for today.
    Read ~/agents/finance/balance_sheet.md and the last 3 days of activity.
    Surface: anything notable, anything off-track from the Q3 income target,
    anything that needs the operator's attention.
    Format: bottom-line first per the operator's stated preference. Aim for 5-10 sentences.
    """

    # 3. Call the model
    response = agent.call(
        work_item=work_item,
        model_override=None,  # use default from model.md
    )

    # 4. Parse capture markers (if any) and write atomic notes
    captures = agent.extract_captures(response)
    for capture in captures:
        agent.write_atomic_note(capture)

    # 5. Write journal entry
    agent.append_journal({
        "what_happened": "Daily brief — autonomous cron run",
        "response": response.text,
        "captures": [c.filename for c in captures],
    })

    # 6. Write log record
    agent.write_log({
        "trigger": "cron",
        "model": response.model_id,
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
        "cost_usd": response.cost_usd,
        "cache_hit": response.cache_hit,
        "status": "ok",
        "summary": "Daily brief generated",
    })

    # 7. Optional: write a published artifact to the agent's output/ folder
    #    for downstream consumption (another agent, the operator, other agents)
    output_path = VAULT_ROOT / "agents" / AGENT_NAME / "output" / f"{response.date}.md"
    agent.write_output(output_path, response.text)  # helper enforces tools.md write paths


if __name__ == "__main__":
    with logger.run(name=f"{AGENT_NAME}-daily-brief"):
        main()
```

---

## The shared library: `atomic_agents`

(Sketched in detail in [shared-helper](shared-helper.md))

A Python class that handles the boilerplate:

```python
class AtomicAgent:
    def __init__(self, name: str, vault_root: Path, trigger: str): ...

    # Loading (per runtime-assembly spec)
    def load_persona(self) -> None: ...
    def load_indexes(self) -> None: ...
    def load_pinned_atomic_units(self) -> None: ...
    def load_recent_atomic_units(self, n: int = 5) -> None: ...
    def load_recent_journal(self, n: int = 1) -> None: ...

    # Calling
    def call(self, work_item: str, model_override: str | None = None) -> Response: ...

    # Output
    def extract_captures(self, response: Response) -> list[Capture]: ...
    def write_atomic_note(self, capture: Capture) -> None: ...
    def update_index(self, capture: Capture) -> None: ...
    def append_journal(self, content: dict) -> None: ...
    def write_log(self, record: dict) -> None: ...
```

The class reads `tools.md` and `model.md` as runtime config. It enforces the autonomy ladder (e.g., refuses to write outside the agent's own folder).

---

## Capture marker parsing

The agent emits captures in this format inside its response:

```
<atomic_capture>
type: feedback
name: Bottom-line-first communication preference
description: the operator wants the recommendation in 1-3 sentences before any working
confidence: high
sources: [conversation_2026-05-06]
body: |
  Body content here in markdown...
</atomic_capture>
```

The `extract_captures()` method finds these blocks, validates the frontmatter against [../spec/03-file-formats](../spec/03-file-formats.md), and prepares them for writing.

If a capture fails validation:
- Don't write the file
- Log the failure
- Surface to the operator via Telegram alert (the runner's `lib.logger.run()` handles this)

---

## LaunchAgent plist

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>ai.your-server.caldwell-daily-brief</string>

    <key>ProgramArguments</key>
    <array>
        <string>/opt/homebrew/bin/python3.11</string>
        <string>/path/to/your-server/automations/jobs/agents/caldwell_daily_brief.py</string>
    </array>

    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>6</integer>
        <key>Minute</key>
        <integer>30</integer>
    </dict>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <!-- DO NOT put API keys here. plist files are world-readable, get
             backed up, leak in process inspection, and end up in logs.
             Instead, the script reads keys from one of:
               1. macOS Keychain (recommended)
               2. ~/.config/atomic_agents/keys.json (chmod 600, gitignored)
               3. A .env file loaded by the script itself (chmod 600, gitignored)
             See "Secrets handling" section below this plist for patterns. -->
    </dict>

    <key>StandardOutPath</key>
    <string>/path/to/your-server/automations/log/caldwell-daily-brief-stdout.log</string>

    <key>StandardErrorPath</key>
    <string>/path/to/your-server/automations/log/caldwell-daily-brief-stderr.log</string>

    <key>WorkingDirectory</key>
    <string>/path/to/your-server/automations</string>
</dict>
</plist>
```

Bootstrap with: `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.your-server.caldwell-daily-brief.plist`

---

## Secrets handling (don't put API keys in the plist)

API keys belong in one of three places, in order of preference:

### Option 1: macOS Keychain (recommended)

Store the key once:

```bash
security add-generic-password \
  -a "$USER" \
  -s "atomic-agents-anthropic" \
  -w "sk-ant-api03-..."
```

The script reads it at runtime:

```python
import subprocess

def get_anthropic_key() -> str:
    result = subprocess.run(
        ["security", "find-generic-password",
         "-a", os.environ["USER"],
         "-s", "atomic-agents-anthropic",
         "-w"],
        capture_output=True, text=True, check=True
    )
    return result.stdout.strip()
```

The plist passes nothing sensitive. Keys never touch disk in plaintext outside the encrypted Keychain.

### Option 2: `~/.config/atomic_agents/keys.json` (chmod 600)

```bash
mkdir -p ~/.config/atomic_agents
chmod 700 ~/.config/atomic_agents
cat > ~/.config/atomic_agents/keys.json <<EOF
{
  "anthropic": "sk-ant-api03-...",
  "openai": "sk-proj-..."
}
EOF
chmod 600 ~/.config/atomic_agents/keys.json
```

Add `~/.config/atomic_agents/` to your global gitignore. The shared helper reads this file by default. Less secure than Keychain (still on disk) but simpler.

### Option 3: `.env` loaded by the script (chmod 600)

```bash
echo 'ATOMIC_AGENTS_ANTHROPIC_KEY=sk-ant-api03-...' > ~/.env.atomic-agents
chmod 600 ~/.env.atomic-agents
```

Script loads it via `python-dotenv` or similar. Same security profile as Option 2.

### Never

- ❌ Plain key in plist `EnvironmentVariables` (world-readable in `/Library/LaunchAgents/`, leaks via Time Machine backups, visible to `ps env`)
- ❌ Key in the Python script itself
- ❌ Key in a markdown file in the vault
- ❌ Key in any file tracked by git, even `.env.example`

### Per the spec

`tools.md` declares which API keys an agent uses; it does NOT contain the keys themselves. The shared helper resolves "this agent uses Anthropic" → "look up `atomic-agents-anthropic` in Keychain" at runtime.

---

## Failure modes and recovery

| Failure | Detection | Recovery |
|---|---|---|
| API rate limit | `lib.atomic_agents` catches 429, writes `status: error` to log | Retry with exponential backoff up to 3x; on persistent fail, write log + Telegram alert |
| Model unavailable | API returns model error | Fall back to model from `model.md` `Fallback` field; log fallback usage |
| Vault file missing | Loader raises FileNotFoundError | Log error, surface to the operator, don't run this cycle (skip rather than corrupt) |
| Capture validation fails | `extract_captures` raises ValidationError | Don't write the bad capture; log details; continue with other captures |
| Daily token cap hit | `lib.atomic_agents` checks log before call | Skip the run; log skip reason; retry tomorrow |
| Out-of-scope write attempt | Helper enforces `tools.md` write paths | Block the write; log violation; do NOT silently route around it |

---

## Cost monitoring

Each run writes `cost_usd` to the log. A weekly summary cron job aggregates costs:

```python
# jobs/agents/_weekly_cost_summary.py
import json
from pathlib import Path
from datetime import date, timedelta

LOG_DIR = Path.home() / "docs" / "agents" / "caldwell" / "log"

def total_cost_last_7_days():
    today = date.today()
    total = 0.0
    for offset in range(7):
        d = today - timedelta(days=offset)
        log_file = LOG_DIR / f"{d.year}-{d.month:02d}" / f"{d.isoformat()}.jsonl"
        if not log_file.exists():
            continue
        for line in log_file.read_text().splitlines():
            rec = json.loads(line)
            total += rec.get("cost_usd", 0.0)
    return total
```

Surface the weekly cost to the operator via Telegram or as a journal entry.

---

## Deduplication

Cron scripts sometimes fire more than once per intended tick (LaunchAgent retries, manual replays, system wake-from-sleep). Without dedup, each firing runs the LLM and writes a second log entry. `cron_tick_key` produces a stable idempotency key for a schedule tick — two firings within the same window yield the same key, and the second is served from cache with zero LLM spend.

```python
# jobs/agents/caldwell_daily_brief.py (excerpt)
from datetime import datetime, timezone
from atomic_agents.idempotency import cron_tick_key

def main():
    agent = AtomicAgent(name="caldwell")
    key = cron_tick_key(
        agent_name="caldwell",
        schedule_name="daily-brief",
        when=datetime.now(timezone.utc),
        granularity="day",       # same key for all firings on the same UTC day
    )
    response = agent.call(
        work_item="Write today's brief.",
        idempotency_key=key,
    )
    if response.deduped:
        print(f"Already ran today (replayed from {response.replayed_run_id}). Skipping.")
        return
    # normal handling ...
```

`granularity` options: `"minute"` / `"hour"` / `"day"` / `"week"`. Match the granularity to your cron schedule — a daily LaunchAgent → `"day"`, an hourly job → `"hour"`.

For queue-driven cascade agents, use `extract_queue_idempotency_key` instead:

```python
from atomic_agents._cascade import claim_next_queued, extract_queue_idempotency_key
import json

item = claim_next_queued(project_root, role="writer", lease_token="daily-brief-lease")
if item is None:
    sys.exit(0)
payload = json.loads(item.path.read_text())
work_text = payload.get("work_item") or item.original_name
idemp_key = extract_queue_idempotency_key(payload)   # reads payload['idempotency_key']
response = agent.call(work_item=work_text, idempotency_key=idemp_key)
```

`extract_queue_idempotency_key` reads ONLY `payload['idempotency_key']` and
deliberately does NOT fall back to `payload['id']` — queue ids are not
guaranteed unique across distinct work items, so an id fallback could
false-dedup two unrelated runs (silently dropping a real run). Missing key →
`None` → no dedup (the LLM runs).

### Implicit dedup via model.md (no explicit key)

If you can't supply an explicit key, add a `## Dedup Body Hash` section to the
agent's `model.md` (presence enables; default OFF). `agent.call()` then derives
an implicit key from `sha256(work_item + model + max_tokens + temperature)`, so
a bit-identical re-delivery dedups automatically. An explicit `idempotency_key=`
always wins over the derived hash. See spec/45 PR2.

---

## Testing a new cron agent before scheduling

1. Run the script manually: `python3 jobs/agents/caldwell_daily_brief.py`
2. Inspect the journal entry it wrote
3. Inspect any new atomic notes it captured
4. Verify the log entry is well-formed
5. Read the response to make sure it's actually useful (not generic)
6. Iterate on `IDENTITY.md` / `SOUL.md` / model temperature until output quality meets bar
7. Only after that, install the LaunchAgent

Don't schedule a new agent without seeing at least 3 manual runs that produced quality output. Bad cron output silently fills the log; nobody reads it; you've built a expensive noise generator.

---

## another agent is special (preview)

another agent runs inside openclaw, not as a cron Python script. The cron pattern in this doc is for non-openclaw agents (Caldwell, agent-a, agent-b, Muse-roles).

When we adapt the Atomic Agents spec to another agent, we'll write a separate adaptation doc covering:
- How openclaw's memory-core + memory-wiki plugins map to Atomic Notes + Atomic Wiki
- How `~/.openclaw/workspace/` files (IDENTITY/SOUL/USER) map to `~/agents/bishop/persona/`
- The sync layer between vault-source-of-truth and openclaw-runtime-paths

For now, the cron pattern is the canonical implementation reference.

---

*See also: [claude-skill-agent](claude-skill-agent.md) for interactive runtime, [shared-helper](shared-helper.md) for the Python library shape.*
