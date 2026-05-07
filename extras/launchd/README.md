# macOS launchd templates

LaunchAgent `.plist` templates for running Atomic Agents on a schedule. macOS only — for Linux see [`../cron/`](../cron/).

These are templates, not ready-to-load files. You substitute placeholders (paths, agent name, schedule) and load them into `~/Library/LaunchAgents/`.

## Why launchd over cron on macOS

- `launchd` survives sleep — if the schedule fires while the Mac is asleep, the job runs when it wakes (cron just misses the slot)
- Logs go to `stdout`/`stderr` paths you specify in the plist
- Can run on a calendar interval (every Monday at 9am) or a time interval (every 6 hours)
- Native macOS — no extra package needed

## Templates

| File | What it does |
|---|---|
| [`com.atomic-agents.run.plist.template`](com.atomic-agents.run.plist.template) | Run one agent against a static work item on a schedule |
| [`com.atomic-agents.eval-daily.plist.template`](com.atomic-agents.eval-daily.plist.template) | Run the eval suite for one agent every day |
| [`com.atomic-agents.dashboard.plist.template`](com.atomic-agents.dashboard.plist.template) | Re-render the cost dashboard hourly |

## How to use a template

1. Copy the template to `~/Library/LaunchAgents/` and rename — drop the `.template` suffix and replace placeholders in the filename if you want multiple agents:
   ```bash
   cp com.atomic-agents.run.plist.template ~/Library/LaunchAgents/com.atomic-agents.run.<your-agent>.plist
   ```

2. Open the file and replace these placeholders (everything in `__KEY__`):

   | Placeholder | What to replace with | Example |
   |---|---|---|
   | `__LABEL__` | A unique label, conventionally reverse-DNS | `com.atomic-agents.run.caldwell` |
   | `__PYTHON_BIN__` | Absolute path to your Python or `uv run` wrapper | `/usr/local/bin/python3` or `/Users/me/.cargo/bin/uv` |
   | `__PROJECT_DIR__` | Absolute path to a directory where commands run from (only relevant for `uv run`) | `/Users/me/Projects/atomic-agents-stack` |
   | `__AGENTS_ROOT__` | Your agent vault root | `/Users/me/agents` |
   | `__AGENT_NAME__` | The agent to invoke | `caldwell` |
   | `__WORK_ITEM__` | The work item text | `Daily morning briefing` |
   | `<integer>7</integer>` / `<integer>0</integer>` | When to run (24-hour) — these are real integers in `<integer>...</integer>` blocks, not `__KEY__` placeholders. Templates default to **7:00 am**; edit the integers directly to change the schedule. |
   | `__API_KEY_ENV_VAR__` | The env var your provider key lives in | `ANTHROPIC_API_KEY` |
   | `__API_KEY_VALUE__` | The actual key | `sk-ant-...` |
   | `__LOG_DIR__` | Where stdout/stderr logs go | `/Users/me/Library/Logs/atomic-agents` |

   You can keep the API key out of the plist by reading it from macOS Keychain — see the "Keychain alternative" section below.

3. Load it:
   ```bash
   launchctl load ~/Library/LaunchAgents/com.atomic-agents.run.<your-agent>.plist
   ```

4. Verify it loaded:
   ```bash
   launchctl list | grep atomic-agents
   ```

5. To run it immediately (don't wait for the schedule):
   ```bash
   launchctl start com.atomic-agents.run.<your-agent>
   ```

## Logs

The templates redirect both stdout and stderr to files under `__LOG_DIR__`. Tail them to debug:

```bash
tail -f /Users/me/Library/Logs/atomic-agents/run-<agent>.out
tail -f /Users/me/Library/Logs/atomic-agents/run-<agent>.err
```

## Unload / disable

```bash
launchctl unload ~/Library/LaunchAgents/com.atomic-agents.run.<your-agent>.plist
```

Then either delete the plist or leave it disabled.

## Keychain alternative (recommended for production)

Don't paste API keys into plists. The Atomic Agents key loader checks macOS Keychain after env vars. Add the key once:

```bash
security add-generic-password \
  -a "$USER" \
  -s "atomic-agents-anthropic" \
  -w "sk-ant-..."
```

Then **delete** the `EnvironmentVariables` block for the API key from your plist. The package will pick it up from Keychain at runtime.

## Verifying the schedule fires

After loading, the next scheduled run lands stdout in your log file. If it doesn't:

1. `launchctl list | grep <label>` — is it listed? Status column should be `0` (last exit code).
2. `tail ~/Library/Logs/atomic-agents/run-<agent>.err` — common: wrong Python path, missing env var.
3. `launchctl start <label>` to trigger immediately and see errors live.
