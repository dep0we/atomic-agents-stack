#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────
# Atomic Agents cron wrapper
#
# Usage from cron:
#     run-atomic-agent.sh <agent-name> "<work item text>"
#     run-atomic-agent.sh eval <agent-name>
#     run-atomic-agent.sh tune <agent-name>
#     run-atomic-agent.sh goal <subcommand> <agent-name> [args...]
#     run-atomic-agent.sh dashboard
#     run-atomic-agent.sh status
#
# Adjust the variables below to your setup, then drop entries from
# crontab.example into your crontab.
# ─────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── Configuration ────────────────────────────────────────────────────────

# Where your agent vault lives.
export ATOMIC_AGENTS_ROOT="${ATOMIC_AGENTS_ROOT:-$HOME/agents}"

# Python interpreter — or a multi-word command like "uv run" or "python3.12 -W ignore".
# Stored as an array so word-splitting is handled safely under set -u.
#   ATOMIC_AGENTS_PYTHON=python3              (default)
#   ATOMIC_AGENTS_PYTHON="uv run"            (uv-managed project)
#   ATOMIC_AGENTS_PYTHON="python3.12 -W ignore"
read -ra PYTHON_CMD <<< "${ATOMIC_AGENTS_PYTHON:-python3}"

# Where logs land (wrapper logs, not the agent's own JSONL log).
LOG_DIR="${ATOMIC_AGENTS_LOG_DIR:-$HOME/.local/state/atomic-agents}"

# Source API keys from a chmod-600 file. Format:
#     export ANTHROPIC_API_KEY=sk-...
#     export OPENAI_API_KEY=sk-...
ENV_FILE="${ATOMIC_AGENTS_ENV_FILE:-$HOME/.config/atomic-agents/env}"

# ── Setup ────────────────────────────────────────────────────────────────

mkdir -p "$LOG_DIR"

if [[ -f "$ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$ENV_FILE"
fi

timestamp() { date +"%Y-%m-%dT%H:%M:%S%z"; }

run_logged() {
    local logfile="$1"; shift
    {
        echo "[$(timestamp)] cmd: $*"
        "$@"
        echo "[$(timestamp)] exit: $?"
    } >> "$LOG_DIR/$logfile" 2>&1
}

# ── Dispatch ─────────────────────────────────────────────────────────────

if [[ $# -lt 1 ]]; then
    cat <<EOF >&2
Usage:
    $0 <agent-name> "<work item text>"     # run an agent
    $0 eval <agent-name>                    # run eval suite
    $0 tune <agent-name>                    # tuning analysis
    $0 goal <subcommand> <agent-name>       # goal manager
    $0 dashboard                            # re-render cost dashboard
    $0 status                               # vault status (schema, snapshots)
EOF
    exit 64
fi

case "$1" in
    eval)
        agent="$2"
        run_logged "${agent}-eval.log" \
            "${PYTHON_CMD[@]}" -m atomic_agents.eval "$agent" --summary-only
        ;;
    tune)
        agent="$2"
        run_logged "${agent}-tune.log" \
            "${PYTHON_CMD[@]}" -m atomic_agents.tuning "$agent"
        ;;
    goal)
        # CLI shape: atomic_agents.goal <subcommand> <agent> [args...]
        # e.g.: run-atomic-agent.sh goal status caldwell
        #        run-atomic-agent.sh goal advance caldwell sg-01 --complete
        subcmd="$2"
        agent="$3"
        shift 3
        run_logged "${agent}-goal.log" \
            "${PYTHON_CMD[@]}" -m atomic_agents.goal "$subcmd" "$agent" "$@"
        ;;
    dashboard)
        run_logged "dashboard.log" \
            "${PYTHON_CMD[@]}" -m atomic_agents.dashboard render
        ;;
    status)
        run_logged "migrate-status.log" \
            "${PYTHON_CMD[@]}" -m atomic_agents.migrate --status
        ;;
    *)
        # First arg is treated as an agent name; second is the work item.
        agent="$1"
        work_item="${2:?work item text required}"
        run_logged "${agent}-run.log" \
            "${PYTHON_CMD[@]}" -m atomic_agents.cli run "$agent" \
                --trigger cron \
                --work-item "$work_item"
        ;;
esac
