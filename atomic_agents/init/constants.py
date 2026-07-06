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

# Per-template autonomy preset defaults. Used by _default_template_vars in wizard.py
# when --from-template is invoked without going through the interactive Q&A.
# All three templates default to Cautious per the design decisions:
# - advisor: Cautious is the home-user-safe default per PR 1
# - researcher: Cautious because web search APIs are classified read_only
#   (spec/28 amended in PR 2), so the rare outbound action (email a summary)
#   correctly escalates rather than going through judge_required overhead
# - writer: Cautious because publishing is a high-stakes external action that
#   the operator must approve per draft
TEMPLATE_PRESET_DEFAULTS: Final[dict[str, str]] = {
    "advisor": PRESET_CAUTIOUS,
    "researcher": PRESET_CAUTIOUS,
    "writer": PRESET_CAUTIOUS,
}

# Section schema for Add-to-it recovery merge per spec/35 MUST 15.
# Maps template name -> file relpath -> ordered list of exact h2 header strings.
# The wizard's section-detection state machine compares an existing file's
# extracted h2 headers against this schema. When they match, the wizard
# offers Add-to-it. When they don't match, the wizard fails closed and
# offers Overwrite or Cancel only.
TEMPLATE_SECTION_SCHEMA: Final[dict[str, dict[str, list[str]]]] = {
    "advisor": {
        "persona/IDENTITY.md": [
            "Who I am",
            "Mission",
            "Scope",
            "Operating doctrine",
            "Operating mode",
            "Autonomy ladder",
            "What I'm NOT (the bright lines)",
        ],
        "persona/SOUL.md": [
            "Voice",
            "Posture",
            "Evolution discipline",
            "Things I have learned about this operator",
        ],
        "persona/USER.md": [
            "Role and context",
            "Communication preferences",
            "Things to avoid",
            "Supporting professionals (when to recommend outside help)",
        ],
        "tools.md": [
            "Read paths",
            "Write paths (own folder ONLY)",
            "External APIs",
            "Hard NOs (absolute, no exceptions)",
            "Soft NOs (require explicit operator override)",
            "Read budget",
            "Tool failure behavior",
        ],
        "model.md": [
            "Default model",
            "Fallback",
            "Token budget",
            "Prompt caching strategy",
            "Cost guardrail",
            "Research integrity",
        ],
        "memory/INDEX.md": [
            "Critical Feedback",
            "Locked Decisions",
            "User Profile",
            "Active Projects",
            "Reference",
            "Recently Promoted to Persona",
            "Archive (superseded)",
        ],
        "wiki/INDEX.md": [
            "Background and context",
            "Reference material",
            "How wiki pages cite sources",
        ],
        "governance.md": [
            "Forbidden actions",
            "Failure modes",
            "Pause / retire criteria",
        ],
    },
    "researcher": {
        "persona/IDENTITY.md": [
            "Who I am",
            "Mission",
            "Scope",
            "Operating doctrine",
            "Operating mode",
            "Research integrity",
            "Autonomy ladder",
            "What I'm NOT (the bright lines)",
        ],
        "persona/SOUL.md": [
            "Voice",
            "Posture",
            "Evolution discipline",
            "Things I have learned about this operator",
        ],
        "persona/USER.md": [
            "Role and context",
            "Communication preferences",
            "Things to avoid",
            "Supporting professionals (when to recommend outside help)",
        ],
        "tools.md": [
            "Read paths",
            "Write paths (own folder ONLY)",
            "External APIs",
            "Hard NOs (absolute, no exceptions)",
            "Soft NOs (require explicit operator override)",
            "Read budget",
            "Tool failure behavior",
        ],
        "model.md": [
            "Default model",
            "Fallback",
            "Token budget",
            "Prompt caching strategy",
            "Cost guardrail",
            "Research integrity",
        ],
        "memory/INDEX.md": [
            "Critical Feedback",
            "Research Conclusions",
            "User Profile",
            "Active Investigations",
            "Reference",
            "Recently Promoted to Persona",
            "Archive (superseded)",
        ],
        "wiki/INDEX.md": [
            "Source citations",
            "Pending distillation",
            "Background and context",
            "Reference material",
            "How wiki pages cite sources",
        ],
        "governance.md": [
            "Forbidden actions",
            "Failure modes",
            "Pause / retire criteria",
        ],
    },
    "writer": {
        "persona/IDENTITY.md": [
            "Who I am",
            "Mission",
            "Scope",
            "Operating doctrine",
            "Operating mode",
            "Autonomy ladder",
            "What I'm NOT (the bright lines)",
        ],
        "persona/SOUL.md": [
            "Voice",
            "Posture",
            "Evolution discipline",
            "Things I have learned about this operator",
        ],
        "persona/USER.md": [
            "Role and context",
            "Communication preferences",
            "Things to avoid",
            "Revision and consistency preferences",
            "Supporting professionals (when to recommend outside help)",
        ],
        "tools.md": [
            "Read paths",
            "Write paths (own folder ONLY)",
            "External APIs",
            "Hard NOs (absolute, no exceptions)",
            "Soft NOs (require explicit operator override)",
            "Read budget",
            "Tool failure behavior",
        ],
        "model.md": [
            "Default model",
            "Fallback",
            "Token budget",
            "Prompt caching strategy",
            "Cost guardrail",
            "Research integrity",
        ],
        "memory/INDEX.md": [
            "Critical Feedback",
            "Locked Decisions",
            "User Profile",
            "Active Projects",
            "Reference",
            "Recently Promoted to Persona",
            "Archive (superseded)",
        ],
        "wiki/INDEX.md": [
            "Background and context",
            "Reference material",
            "How wiki pages cite sources",
        ],
        "governance.md": [
            "Forbidden actions",
            "Failure modes",
            "Pause / retire criteria",
        ],
    },
}

# Maximum directory depth the template walk will traverse. Defensive cap
# against future template trees that ship deeply nested structures.
# Current advisor template is 3 levels deep (templates/<name>/persona/IDENTITY.md).
MAX_TEMPLATE_DEPTH: Final[int] = 16

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
# "deploy" and its nested actions "status"/"down" are reserved too: an agent
# named "status" or "down" would be unreachable via `deploy <agent>` because
# the deploy dispatcher reads those tokens as the action, not the agent name
# (spec/48 — `deploy status <agent>` / `deploy down <agent>`).
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
        "deploy",
        "status",
        "down",
        # spec/55 #624: ``manage`` is a new CLI subgroup; ``govern`` is the first
        # verb. Both are reserved so agents named "manage" or "govern" cannot
        # produce ambiguous CLI tokens (e.g. ``atomic-agents manage govern manage``).
        "manage",
        "govern",
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
    "`atomic-agents init <name> --from-template <template>` to scaffold from a "
    "starter template. See `atomic-agents init --list-templates` for the "
    "available templates."
)

# NOTE: this constant carries the help message printed when the pre-flight
# resolver finds no Anthropic credential. The name MSG_NO_PROVIDER_KEY (not
# MSG_NO_API_KEY) intentionally avoids CodeQL's clear-text-logging heuristic,
# which pattern-matches variables named *_API_KEY as candidate secrets even
# when the content is a literal help template (no real credential value).
MSG_NO_PROVIDER_KEY: Final = (
    "No Anthropic credential found. Try one of:\n"
    "  export ANTHROPIC_API_KEY=sk-ant-...\n"
    "  add to macOS Keychain as 'atomic-agents-anthropic'\n"
    '  add to ~/.config/atomic_agents/keys.json as {"anthropic": "sk-ant-..."}\n'
    "Get a credential at console.anthropic.com."
)

MSG_OSERROR_HEADER: Final = "Couldn't write to {path}: {reason}."
MSG_OSERROR_FIX: Final = (
    "Check permissions or pick a different location with `--agents-root`."
)

MSG_INVALID_NAME_CHARSET: Final = (
    "Names use letters, numbers, and dashes only, with no leading or trailing dash. "
    "Maximum 64 characters. Please try again."
)
MSG_INVALID_NAME_TOO_LONG: Final = (
    "Names must be 64 characters or shorter. Please try again."
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

MSG_SECTION_DETECTION_FAILED: Final = (
    "Your existing files don't match the {template} template's expected structure. "
    "Add to it can't safely merge without that structure match. Choose Overwrite "
    "to replace everything, or Cancel to leave your files untouched. The files "
    "that did not match: {files}."
)

MSG_MISSING_FILE_BACKFILL: Final = (
    "Some template-owned files were missing and will be backfilled from the template: {files}. "
    "Review the diff preview below to see what will be created."
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

DEFAULT_MODEL_PRIMARY: Final = "claude-opus-4-8"
DEFAULT_MODEL_FALLBACK: Final = "claude-sonnet-4-6"
DEFAULT_DAILY_CAP_USD: Final = 0.50
DEFAULT_MONTHLY_CAP_USD: Final = 7.00

# ---------------------------------------------------------------------------
# Opt-in smoke test at the end of the wizard
# ---------------------------------------------------------------------------

# Work item sent to the agent during the opt-in test call.
TEST_CALL_WORK_ITEM: Final = "Hello, can you tell me about yourself?"


def redact_url_credentials(url: str) -> str:
    """Drop user:password@ from URL netloc; keep scheme + host + path visible.

    Used in the persona-backend warning to show operators which backend is
    configured without leaking credentials. The existing _redact_for_error_message
    pattern in persona/mandate/corpus backends strips everything after ://,
    which hides the host entirely; that defeats the operator-decision goal.

    Examples:
      https://user:pass@example.com/api -> https://example.com/api
      redis://example.com:6379           -> redis://example.com:6379
      file:///local/path                 -> file:///local/path
    """
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(url)
    if "@" in parsed.netloc:
        netloc = parsed.netloc.split("@", 1)[1]
    else:
        netloc = parsed.netloc
    return urlunparse(parsed._replace(netloc=netloc))
