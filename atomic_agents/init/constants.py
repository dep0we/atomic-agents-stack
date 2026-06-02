"""Locked constants for the init wizard. Single source of truth.

Spec/35 documents these by name. Any drift between this module and spec/35 is a
CLAUDE.md rule 13 violation. The Round 1 adversarial reviewer checks parity.
"""

from __future__ import annotations
import re
from typing import Final

# ---------------------------------------------------------------------------
# Action class vocabulary (spec/28 verbatim, line 101 + line 855)
# ---------------------------------------------------------------------------

ACTION_CLASS_READ_ONLY: Final = "read_only"
ACTION_CLASS_REVERSIBLE_WRITE: Final = "reversible_write"
ACTION_CLASS_EXTERNAL_SIDE_EFFECT: Final = "external_side_effect"
ACTION_CLASS_HIGH_RISK: Final = "high_risk"
ACTION_CLASSES: Final = (
    ACTION_CLASS_READ_ONLY,
    ACTION_CLASS_REVERSIBLE_WRITE,
    ACTION_CLASS_EXTERNAL_SIDE_EFFECT,
    ACTION_CLASS_HIGH_RISK,
)

# Plain-English gloss shown next to each class in the customize sub-flow.
ACTION_CLASS_GLOSSES: Final = {
    ACTION_CLASS_READ_ONLY: "reading files, searching notes, listing directories",
    ACTION_CLASS_REVERSIBLE_WRITE: "writing notes, drafting documents, staging work",
    ACTION_CLASS_EXTERNAL_SIDE_EFFECT: "sending email, posting messages, anything the world sees",
    ACTION_CLASS_HIGH_RISK: "deleting files, force-pushing code, anything irreversible",
}

# Policy values (spec/28 line 855: bypass | allow_with_audit | judge_required | escalate).
POLICY_BYPASS: Final = "bypass"
POLICY_ALLOW_WITH_AUDIT: Final = "allow_with_audit"
POLICY_JUDGE_REQUIRED: Final = "judge_required"
POLICY_ESCALATE: Final = "escalate"
POLICIES: Final = (
    POLICY_BYPASS,
    POLICY_ALLOW_WITH_AUDIT,
    POLICY_JUDGE_REQUIRED,
    POLICY_ESCALATE,
)

# Plain-English label for each policy (operator-facing).
POLICY_LABELS: Final = {
    POLICY_BYPASS: "just do it",
    POLICY_ALLOW_WITH_AUDIT: "do it and log it",
    POLICY_JUDGE_REQUIRED: "ask the judge first",
    POLICY_ESCALATE: "ask me first",
}

# ---------------------------------------------------------------------------
# Q4 autonomy presets (3 quick picks + 1 customize)
# ---------------------------------------------------------------------------

PRESET_CAUTIOUS: Final = "Cautious"
PRESET_BALANCED: Final = "Balanced"
PRESET_AUTONOMOUS: Final = "Autonomous"
PRESET_CUSTOMIZE: Final = "Customize"

AUTONOMY_PRESETS: Final = {
    PRESET_CAUTIOUS: {
        ACTION_CLASS_READ_ONLY: POLICY_BYPASS,
        ACTION_CLASS_REVERSIBLE_WRITE: POLICY_ALLOW_WITH_AUDIT,
        ACTION_CLASS_EXTERNAL_SIDE_EFFECT: POLICY_ESCALATE,
        ACTION_CLASS_HIGH_RISK: POLICY_ESCALATE,
    },
    PRESET_BALANCED: {
        ACTION_CLASS_READ_ONLY: POLICY_BYPASS,
        ACTION_CLASS_REVERSIBLE_WRITE: POLICY_ALLOW_WITH_AUDIT,
        ACTION_CLASS_EXTERNAL_SIDE_EFFECT: POLICY_JUDGE_REQUIRED,
        ACTION_CLASS_HIGH_RISK: POLICY_ESCALATE,
    },
    PRESET_AUTONOMOUS: {
        ACTION_CLASS_READ_ONLY: POLICY_BYPASS,
        ACTION_CLASS_REVERSIBLE_WRITE: POLICY_ALLOW_WITH_AUDIT,
        ACTION_CLASS_EXTERNAL_SIDE_EFFECT: POLICY_JUDGE_REQUIRED,
        ACTION_CLASS_HIGH_RISK: POLICY_JUDGE_REQUIRED,
    },
}

# ---------------------------------------------------------------------------
# agent_name validation
# ---------------------------------------------------------------------------

# Alphanumeric and hyphen; no leading or trailing hyphen; max 64 chars.
# The alternation handles the single-character case (no internal hyphen allowed).
AGENT_NAME_REGEX: Final = re.compile(
    r"^[a-zA-Z0-9][a-zA-Z0-9-]{0,62}[a-zA-Z0-9]$|^[a-zA-Z0-9]$"
)
AGENT_NAME_MAX_LEN: Final = 64

# CLI subcommand names that would shadow the wizard-created agent.
# Sourced from cli.py sub.add_parser() calls + "init" (this new subcommand).
RESERVED_AGENT_NAMES: Final = frozenset(
    {
        "init",
        "run",
        "info",
        "skills",
        "version",
        "restore",
        "bundle",
        "doctor",
        "review",
        "persona",
        "corpus",
    }
)

# ---------------------------------------------------------------------------
# Template variable names (str.Template safe_substitute keys)
# ---------------------------------------------------------------------------

TEMPLATE_VAR_AGENT_NAME: Final = "agent_name"
TEMPLATE_VAR_MISSION: Final = "mission"
TEMPLATE_VAR_SCOPE_IN: Final = "scope_in"
TEMPLATE_VAR_SCOPE_OUT: Final = "scope_out"
TEMPLATE_VAR_AUTONOMY_PRESET_LABEL: Final = "autonomy_preset_label"
TEMPLATE_VAR_AUTONOMY_READ_ONLY: Final = "autonomy_read_only"
TEMPLATE_VAR_AUTONOMY_REVERSIBLE_WRITE: Final = "autonomy_reversible_write"
TEMPLATE_VAR_AUTONOMY_EXTERNAL_SIDE_EFFECT: Final = "autonomy_external_side_effect"
TEMPLATE_VAR_AUTONOMY_HIGH_RISK: Final = "autonomy_high_risk"
TEMPLATE_VAR_VOICE: Final = "voice"
TEMPLATE_VAR_COMM_PREFS: Final = "comm_prefs"
TEMPLATE_VAR_HARD_REFUSALS: Final = "hard_refusals"

# ---------------------------------------------------------------------------
# Pre-flight provider key resolution
# Mirrors doctor.py:_get_anthropic_key() / _llm._get_anthropic_key() exactly.
# Order: ATOMIC_AGENTS_ANTHROPIC_KEY first, then ANTHROPIC_API_KEY.
# ---------------------------------------------------------------------------

ANTHROPIC_ENV_VARS: Final = ("ATOMIC_AGENTS_ANTHROPIC_KEY", "ANTHROPIC_API_KEY")
ANTHROPIC_KEYCHAIN_NAME: Final = "atomic-agents-anthropic"
ANTHROPIC_CONFIG_KEY: Final = "anthropic"

# ---------------------------------------------------------------------------
# Plain-English error / status messages
# ---------------------------------------------------------------------------

MSG_NO_TTY: Final = (
    "This command needs an interactive terminal. For non-interactive use, run "
    "`atomic-agents init <name> --from-template advisor` to scaffold a Caldwell-shaped "
    "agent. See `atomic-agents init --list-templates` for other options."
)

MSG_NO_API_KEY: Final = (
    "No Anthropic API key found. Try one of:\n"
    "  export ANTHROPIC_API_KEY=sk-ant-...\n"
    "  add to macOS Keychain as 'atomic-agents-anthropic'\n"
    '  add to ~/.config/atomic_agents/keys.json as {"anthropic": "sk-ant-..."}\n'
    "Get a key at console.anthropic.com."
)

MSG_OSERROR_HEADER: Final = "Couldn't write to {path}: {reason}."
MSG_OSERROR_FIX: Final = (
    "Check permissions or pick a different location with `--agents-root`."
)

MSG_INVALID_NAME_CHARSET: Final = (
    "Names use letters, numbers, and dashes only, with no leading or trailing dash. "
    "Maximum 64 characters. Please try again."
)
MSG_INVALID_NAME_RESERVED: Final = (
    "That name is reserved by a built-in command. Please choose a different name."
)

MSG_PERSONA_BACKEND_WARNING: Final = (
    "You have a custom persona backend configured "
    "(ATOMIC_AGENTS_PERSONA_BACKEND_URL is set). This wizard writes per-agent persona "
    "files. The framework reads them via the legacy filesystem walk, which works forever, "
    "but your custom backend will not see them as a shared persona."
)

# Opt-in test call exception messages.
MSG_TEST_CALL_RATE_LIMIT: Final = (
    "The API is busy right now. Wait a minute and try: "
    "`atomic-agents run {agent_name} --work-item 'Hello'`."
)
MSG_TEST_CALL_AUTH_ERROR: Final = (
    "Your API key was rejected. Check that it is active at console.anthropic.com."
)
MSG_TEST_CALL_NETWORK: Final = (
    "Could not reach the Anthropic API. Check your network connection."
)
MSG_TEST_CALL_GENERIC_FALLBACK: Final = (
    "Something went wrong during the test call: {error_type}: {error_msg}. "
    "Your agent scaffold is ready at {path}."
)

# ---------------------------------------------------------------------------
# Default agent values written into model.md
# ---------------------------------------------------------------------------

DEFAULT_MODEL_PRIMARY: Final = "claude-opus-4-7"
DEFAULT_MODEL_FALLBACK: Final = "claude-sonnet-4-6"
DEFAULT_DAILY_CAP_USD: Final = 0.50
DEFAULT_MONTHLY_CAP_USD: Final = 7.00

# ---------------------------------------------------------------------------
# Opt-in smoke test at the end of the wizard
# ---------------------------------------------------------------------------

# Work item sent to the agent during the opt-in test call.
TEST_CALL_WORK_ITEM: Final = "Hello, can you tell me about yourself?"

# Default test call timeout in seconds.
TEST_CALL_TIMEOUT_S: Final = 30
