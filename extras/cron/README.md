# cron templates

Crontab examples and a portable shell wrapper for running Atomic Agents on a schedule. Linux-first — for macOS see [`../launchd/`](../launchd/).

## Why cron over launchd on Linux

It's just what's there. Linux distros ship cron; launchd is macOS-specific. If you're on systemd, you can use `systemd.timer` instead — same shape, see the systemd note at the bottom.

## Files

| File | What it does |
|---|---|
| [`crontab.example`](crontab.example) | Example crontab entries for run, eval, dashboard, and migration check |
| [`run-atomic-agent.sh`](run-atomic-agent.sh) | Portable shell wrapper that handles env, logging, and exit codes |

## How to use

1. Copy and edit the wrapper script:
   ```bash
   cp run-atomic-agent.sh ~/bin/run-atomic-agent.sh
   chmod +x ~/bin/run-atomic-agent.sh
   $EDITOR ~/bin/run-atomic-agent.sh   # set the variables at the top
   ```

2. Test it manually before scheduling:
   ```bash
   ~/bin/run-atomic-agent.sh <agent-name> "Daily morning briefing"
   ```

3. Add to your crontab:
   ```bash
   crontab -e
   ```

4. Paste entries from `crontab.example`, adjusting paths and times.

## Why a wrapper script and not direct cron entries

Cron's environment is minimal — no `$HOME`, no `$PATH`, no shell profile. The wrapper:
- Sets `PATH` and `ATOMIC_AGENTS_ROOT` explicitly
- Loads the API key from a file (so the key isn't in `crontab -l` output)
- Redirects stdout/stderr to a log file with a timestamp
- Exits with the right status code so cron's `MAILTO=` notifies on failure
- Makes the cron line readable: `~/bin/run-atomic-agent.sh caldwell "Daily brief"` is clearer than the full python invocation

## Logs

The wrapper appends to `<log-dir>/<agent>-<command>.log`. Tail to debug:

```bash
tail -f ~/.local/state/atomic-agents/caldwell-run.log
```

Rotate with `logrotate` if needed; nothing in the wrapper grows unbounded except the log file.

## API key handling

Three options, in order of preference:

1. **Environment variable from a sourced file:** put `ANTHROPIC_API_KEY=sk-...` in `~/.config/atomic-agents/env` (chmod 600), and have the wrapper `source` it. This is what the template wrapper does.
2. **`~/.config/atomic_agents/keys.json`** (chmod 600) — the package reads this at runtime, no env var needed.
3. **Inline in the wrapper script** — works but gets the key into `bash -x` traces. Avoid.

Don't put the key directly in the crontab — `crontab -l` exposes it.

## Failure handling

cron runs every entry independently. If one agent's run fails, the next scheduled agent still runs. The `MAILTO=` directive at the top of the example crontab gets you an email per failure (assuming local mail is set up).

For a more robust setup:
- Have the wrapper push to a notification channel (Pushover, Telegram, ntfy.sh) on non-zero exit
- Aggregate failures by tailing the log

## systemd alternative

If you're on a systemd-based distro and want native scheduling, replace cron with `systemd.timer`. The wrapper script is reusable. Sketch:

```ini
# ~/.config/systemd/user/atomic-agents-run.service
[Service]
Type=oneshot
ExecStart=%h/bin/run-atomic-agent.sh caldwell "Daily morning briefing"

# ~/.config/systemd/user/atomic-agents-run.timer
[Timer]
OnCalendar=*-*-* 07:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

Then `systemctl --user enable --now atomic-agents-run.timer`.
