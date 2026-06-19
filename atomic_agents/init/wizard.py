"""atomic-agents init wizard. Scaffolds a working home-user agent in under 10 minutes.

See docs/spec/35-init-wizard.md for the 15 normative MUSTs this module satisfies.
"""

from __future__ import annotations

# Standard library imports only at module-top. rich is lazy-imported inside run_init
# per CLAUDE.md aesthetic and adversarial review discipline.
import os
import re
import string
import sys
import warnings
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import _io, _llm, _platform, _tools
from ..exceptions import PathTraversalError
from . import constants as C


# ---------------------------------------------------------------------------
# _types helper: lazy-import a tuple of exception types for isinstance dispatch
# ---------------------------------------------------------------------------


def _types(mod: Any, *names: str) -> tuple:
    """Return a tuple of types from ``mod`` suitable for ``isinstance`` dispatch.

    Looks up each name on ``mod`` via ``getattr``. Names that resolve to ``None``
    or that are absent on the module are replaced with ``()`` so the tuple stays
    valid as the second argument to ``isinstance``. This makes exception dispatch
    safe when an optional SDK is unavailable. Also tolerates ``mod=None`` gracefully.

    Example:
        isinstance(e, _types(anthropic_mod, "RateLimitError", "AuthenticationError"))
    """
    result = []
    for name in names:
        t = getattr(mod, name, None)
        if t is not None:
            result.append(t)
    return tuple(result)


# ---------------------------------------------------------------------------
# Public entry point (called from cli.py)
# ---------------------------------------------------------------------------


def run_init(args: Any) -> int:
    """Run the wizard. Returns process exit status (0 success, 1 hard error, 2 misuse).

    args has: agent_name (Optional[str]), from_template (Optional[str]),
    list_templates (bool), agents_root (Optional[str]).
    """
    # MUST 11: --from-template requires an agent name (non-interactive path).
    # Check before any rich import so error is cheap and clear.
    if args.from_template and not args.agent_name:
        print(
            "--from-template requires an agent name. "
            "Run `atomic-agents init <name> --from-template advisor`.",
            file=sys.stderr,
        )
        return 2

    # MUST 2: non-TTY guard for the interactive Q&A path.
    # --from-template and --list-templates do not require an interactive terminal
    # and are carved out here before any rich import.
    if not args.from_template and not args.list_templates and not sys.stdin.isatty():
        print(C.MSG_NO_TTY, file=sys.stderr)
        return 2

    # MUST 14: lazy import rich here, after the non-TTY + arg-validation gates.
    from rich.console import Console
    from rich.prompt import Confirm, Prompt
    from rich.table import Table

    console = Console()

    try:
        # --list-templates writes nothing, so the persona-backend guard is skipped
        # per spec/35: the guard applies when files would be written.
        if args.list_templates:
            return _cmd_list_templates(console)

        # Resolve agents_root once at entry (MUST H6 / M9).
        agents_root = _resolve_agents_root(args)

        # MUST 6: persona-backend warning before any mkdir or file write.
        if not _persona_backend_check(console, Confirm):
            return 0  # decline = clean exit, zero files written

        if args.from_template:
            return _from_template(
                args.from_template,
                args.agent_name,
                agents_root,
                console,
                Prompt,
                Confirm,
            )

        # MUST 7: API key pre-flight via _get_key (env vars + Keychain + keys.json).
        # Carved out of --from-template and --list-templates per spec/35 MUST 7 amendment
        # (P3 lock): those paths write file content only and do not make LLM calls.
        if not _api_key_preflight():
            print(C.MSG_NO_PROVIDER_KEY, file=sys.stderr)
            return 1

        return _interactive(
            args.agent_name, agents_root, console, Prompt, Confirm, Table
        )

    except KeyboardInterrupt:
        console.print("\nCanceled.")
        return 130  # 128 + SIGINT


# ---------------------------------------------------------------------------
# Support: agents_root resolution and pre-flight
# ---------------------------------------------------------------------------


def _resolve_agents_root(args: Any) -> Path:
    """Resolve agents_root once at entry. Expands ~ and resolves symlinks."""
    if args.agents_root:
        return Path(args.agents_root).expanduser().resolve()
    return _platform.get_agents_root()


def _api_key_preflight() -> bool:
    """Return True when an Anthropic API key is available, False otherwise.

    Uses _llm._get_key so all three resolution sources are checked:
    environment variable, macOS Keychain, and ~/.config/atomic_agents/keys.json.
    """
    from ..exceptions import AtomicAgentsError

    try:
        _llm._get_key(
            env_vars=list(C.ANTHROPIC_ENV_VARS),
            keychain_name=C.ANTHROPIC_KEYCHAIN_NAME,
            config_key=C.ANTHROPIC_CONFIG_KEY,
        )
        return True
    except AtomicAgentsError:  # noqa: BLE001
        return False


def _persona_backend_check(console: Any, Confirm: Any) -> bool:
    """Warn when ATOMIC_AGENTS_PERSONA_BACKEND_URL is set non-empty (MUST 6).

    Returns True if safe to proceed, False if the operator declined.
    --list-templates callers skip this because no files are written.
    """
    raw_value = os.environ.get("ATOMIC_AGENTS_PERSONA_BACKEND_URL", "").strip()
    if not raw_value:
        return True  # No custom backend set; safe to proceed.

    redacted = C.redact_url_credentials(raw_value)
    console.print(
        f"\n[yellow]{C.MSG_PERSONA_BACKEND_WARNING} "
        f"(configured backend: {redacted})[/yellow]\n"
    )
    proceed = Confirm.ask(
        "Continue anyway and write per-agent persona files?",
        console=console,
        default=False,
    )
    return bool(proceed)


# ---------------------------------------------------------------------------
# --list-templates path
# ---------------------------------------------------------------------------


def _cmd_list_templates(console: Any) -> int:
    """List the starter templates available via --from-template."""
    console.print("[bold]Available starter templates:[/bold]")
    console.print(
        "  advisor    Caldwell-shaped general-purpose agent (Cautious autonomy)"
    )
    console.print(
        "  researcher Curiosity-first investigator (Cautious autonomy; source-grounded)"
    )
    console.print(
        "  writer     Voice-first content agent (Cautious autonomy; Sonnet default)"
    )
    return 0


# ---------------------------------------------------------------------------
# --from-template path (non-interactive scaffold)
# ---------------------------------------------------------------------------


def _from_template(
    template_name: str,
    agent_name: str | None,
    agents_root: Path,
    console: Any,
    Prompt: Any,
    Confirm: Any,
) -> int:
    """Non-interactive scaffold: validate name, check collision, write defaults."""
    known_templates = {"advisor", "researcher", "writer"}
    if template_name not in known_templates:
        console.print(
            f"[red]Unknown template '{template_name}'.[/red] "
            f"Run `atomic-agents init --list-templates` to see available options."
        )
        return 1

    # Q1 name validation still required (MUST 1).
    if agent_name:
        name = agent_name.strip()
        # Symmetric length check before regex (R2-L1).
        if len(name) > C.AGENT_NAME_MAX_LEN:
            print(C.MSG_INVALID_NAME_TOO_LONG, file=sys.stderr)
            return 2
        if name in C.RESERVED_AGENT_NAMES:
            console.print(C.MSG_INVALID_NAME_RESERVED)
            return 2
        if not C.AGENT_NAME_REGEX.match(name):
            console.print(C.MSG_INVALID_NAME_CHARSET)
            return 2
    else:
        name = _ask_q1_name(console, Prompt)

    agent_dir = agents_root / name

    # H6: capture existing ONCE to eliminate the TOCTOU window between
    # collision check and _write_scaffold's overwrite branch.
    existing = agent_dir.exists()

    # Collision check: three-option when template is known (can offer Add-to-it).
    if existing:
        branch, existing_headers = _check_collision(
            agent_dir, console, Prompt, Confirm, template_name=template_name
        )
        if branch == "cancel":
            return 0
        if branch == "add_to_it":
            return _add_to_it(
                agent_dir=agent_dir,
                agents_root=agents_root,
                template_name=template_name,
                console=console,
                Prompt=Prompt,
                Confirm=Confirm,
                existing_headers=existing_headers,
            )
        # branch == "overwrite": fall through to _write_scaffold below.

    # Build a minimal set of template vars using safe defaults for this template.
    default_vars = _default_template_vars(name, template_name)

    return _write_scaffold(
        agent_dir=agent_dir,
        template_name=template_name,
        vars=default_vars,
        agent_name=name,
        agents_root=agents_root,
        console=console,
        Confirm=Confirm,
        existing=existing,
    )


def _default_template_vars(name: str, template_name: str) -> dict[str, str]:
    """Minimal defaults for --from-template (no Q&A).

    Looks up the preset for the given template from TEMPLATE_PRESET_DEFAULTS,
    then applies the matching AUTONOMY_PRESETS entry to populate the four
    autonomy_* variables (A2 lock).
    """
    preset_label = C.TEMPLATE_PRESET_DEFAULTS.get(template_name, C.PRESET_CAUTIOUS)
    preset = C.AUTONOMY_PRESETS[preset_label]
    return {
        C.TEMPLATE_VAR_AGENT_NAME: name,
        C.TEMPLATE_VAR_MISSION: "(Configure in persona/IDENTITY.md after setup.)",
        C.TEMPLATE_VAR_SCOPE_IN: "- (Add in-scope work items here.)",
        C.TEMPLATE_VAR_SCOPE_OUT: "- (Add out-of-scope refusals here.)",
        C.TEMPLATE_VAR_AUTONOMY_PRESET_LABEL: preset_label,
        C.TEMPLATE_VAR_AUTONOMY_READ_ONLY: preset[C.ACTION_CLASS_READ_ONLY],
        C.TEMPLATE_VAR_AUTONOMY_REVERSIBLE_WRITE: preset[
            C.ACTION_CLASS_REVERSIBLE_WRITE
        ],
        C.TEMPLATE_VAR_AUTONOMY_EXTERNAL_SIDE_EFFECT: preset[
            C.ACTION_CLASS_EXTERNAL_SIDE_EFFECT
        ],
        C.TEMPLATE_VAR_AUTONOMY_HIGH_RISK: preset[C.ACTION_CLASS_HIGH_RISK],
        C.TEMPLATE_VAR_VOICE: "clear, direct, helpful",
        C.TEMPLATE_VAR_COMM_PREFS: "- (Add communication preferences here.)",
        C.TEMPLATE_VAR_HARD_REFUSALS: "(None configured at setup.)",
    }


# ---------------------------------------------------------------------------
# Interactive Q&A path
# ---------------------------------------------------------------------------


def _interactive(
    agent_name: str | None,
    agents_root: Path,
    console: Any,
    Prompt: Any,
    Confirm: Any,
    Table: Any,
) -> int:
    """Full Q&A wizard flow. Collects Q1-Q7, writes scaffold, runs doctor."""
    console.print(
        "\n[bold]Welcome to atomic-agents init.[/bold]\n"
        "Answer seven questions and you will have a working agent in a few minutes.\n"
    )

    # Q1: agent name
    name = _ask_q1_name(console, Prompt, default=agent_name)

    agent_dir = agents_root / name

    # H6: capture existing ONCE to eliminate the TOCTOU window between
    # collision check and _write_scaffold's overwrite branch.
    existing = agent_dir.exists()

    # Collision check before any further prompts.
    # Interactive path has no template_name so only Overwrite/Cancel are offered.
    if existing:
        branch, _headers = _check_collision(
            agent_dir, console, Prompt, Confirm, template_name=None
        )
        if branch == "cancel":
            return 0
        # branch == "overwrite": fall through.

    # Q2: mission
    mission = _ask_q2_mission(console, Prompt)

    # Q3a + Q3b: scope (two prompts, one question slot)
    scope_in = _ask_q3a_scope_in(console, Prompt)
    scope_out = _ask_q3b_scope_out(console, Prompt)

    # Q4: autonomy presets
    autonomy_policies, preset_label = _ask_q4_autonomy(console, Prompt, Table)

    # Q5: voice
    voice = _ask_q5_voice(console, Prompt)

    # Q6: communication preferences
    comm_prefs = _ask_q6_comm_prefs(console, Prompt)

    # Q7: hard refusals (renders to both USER.md and tools.md via P2 lock)
    hard_refusals = _ask_q7_hard_refusals(console, Prompt)

    # Build substitution variables from Q&A answers.
    answers = {
        "name": name,
        "mission": mission,
        "scope_in": scope_in,
        "scope_out": scope_out,
        "autonomy_policies": autonomy_policies,
        "preset_label": preset_label,
        "voice": voice,
        "comm_prefs": comm_prefs,
        "hard_refusals": hard_refusals,
    }
    vars_map = _build_template_vars(answers)

    return _write_scaffold(
        agent_dir=agent_dir,
        template_name="advisor",
        vars=vars_map,
        agent_name=name,
        agents_root=agents_root,
        console=console,
        Confirm=Confirm,
        existing=existing,
    )


# ---------------------------------------------------------------------------
# Q1-Q7 prompt functions
# ---------------------------------------------------------------------------


def _ask_q1_name(console: Any, Prompt: Any, default: str | None = None) -> str:
    """Q1: agent_name with regex + reserved-name validation (MUST 1).

    Loops until a valid, non-reserved name is entered. No filesystem side
    effect occurs until this function returns.
    """
    while True:
        raw = Prompt.ask(
            "Q1. What should I call this agent? "
            "(Letters, numbers, and dashes only. This becomes a folder name.)",
            console=console,
            default=default or "",
        )
        name = (raw or "").strip()
        if not name:
            console.print("[yellow]Please enter a name.[/yellow]")
            continue
        if len(name) > C.AGENT_NAME_MAX_LEN:
            console.print(f"[red]{C.MSG_INVALID_NAME_TOO_LONG}[/red]")
            continue
        if name in C.RESERVED_AGENT_NAMES:
            console.print(f"[red]{C.MSG_INVALID_NAME_RESERVED}[/red]")
            continue
        if not C.AGENT_NAME_REGEX.match(name):
            console.print(f"[red]{C.MSG_INVALID_NAME_CHARSET}[/red]")
            continue
        return name


def _ask_q2_mission(console: Any, Prompt: Any) -> str:
    """Q2: mission statement. Free text; empty re-prompts."""
    while True:
        raw = Prompt.ask(
            "Q2. What is this agent for? "
            "(One or two sentences. What is its job, what does it produce.)",
            console=console,
        )
        text = (raw or "").strip()
        if text:
            return text
        console.print("[yellow]Please enter a mission statement.[/yellow]")


def _ask_q3a_scope_in(console: Any, Prompt: Any) -> str:
    """Q3a: in-scope work. Free text; empty re-prompts."""
    while True:
        raw = Prompt.ask(
            "Q3a. What is in scope? "
            "(A few bullets. What work should this agent accept.)",
            console=console,
        )
        text = (raw or "").strip()
        if text:
            return text
        console.print("[yellow]Please describe what is in scope.[/yellow]")


def _ask_q3b_scope_out(console: Any, Prompt: Any) -> str:
    """Q3b: out-of-scope refusals. Free text; empty re-prompts."""
    while True:
        raw = Prompt.ask(
            "Q3b. What is out of scope? "
            "(A few bullets. What should this agent refuse.)",
            console=console,
        )
        text = (raw or "").strip()
        if text:
            return text
        console.print("[yellow]Please describe what is out of scope.[/yellow]")


def _ask_q4_autonomy(
    console: Any, Prompt: Any, Table: Any
) -> tuple[dict[str, str], str]:
    """Q4: autonomy preset table + customize sub-flow.

    Returns (policies_dict, preset_label). Renders a rich Table (width=78).
    Falls back to plain text when Console.is_dumb_terminal is True (M8).
    """
    is_dumb = getattr(console, "is_dumb_terminal", False)

    if is_dumb:
        # Plain text fallback for dumb terminals.
        console.print("\nQ4. How much should this agent act on its own?")
        console.print(
            f"  1. {C.PRESET_CAUTIOUS}: read=bypass, write=allow_with_audit, "
            f"external=escalate, high_risk=escalate"
        )
        console.print(
            f"  2. {C.PRESET_BALANCED}: read=bypass, write=allow_with_audit, "
            f"external=judge_required, high_risk=escalate"
        )
        console.print(
            f"  3. {C.PRESET_AUTONOMOUS}: read=bypass, write=allow_with_audit, "
            f"external=judge_required, high_risk=judge_required"
        )
        console.print("  4. Customize: set each action class yourself")
    else:
        table = Table(
            title="Q4. How much should this agent act on its own?",
            show_header=True,
            width=78,
        )
        table.add_column("Choice", style="bold")
        table.add_column("read_only")
        table.add_column("reversible_write")
        table.add_column("external_side_effect")
        table.add_column("high_risk")

        for i, label in enumerate(
            [C.PRESET_CAUTIOUS, C.PRESET_BALANCED, C.PRESET_AUTONOMOUS], start=1
        ):
            p = C.AUTONOMY_PRESETS[label]
            table.add_row(
                f"{i}. {label}",
                p[C.ACTION_CLASS_READ_ONLY],
                p[C.ACTION_CLASS_REVERSIBLE_WRITE],
                p[C.ACTION_CLASS_EXTERNAL_SIDE_EFFECT],
                p[C.ACTION_CLASS_HIGH_RISK],
            )
        table.add_row("4. Customize", "(pick per class)", "", "", "")
        console.print(table)

    choice = Prompt.ask(
        "Pick 1-3 or 4 to set each class yourself",
        choices=["1", "2", "3", "4"],
        default="1",
        console=console,
    )

    if choice == "1":
        return dict(C.AUTONOMY_PRESETS[C.PRESET_CAUTIOUS]), C.PRESET_CAUTIOUS
    if choice == "2":
        return dict(C.AUTONOMY_PRESETS[C.PRESET_BALANCED]), C.PRESET_BALANCED
    if choice == "3":
        return dict(C.AUTONOMY_PRESETS[C.PRESET_AUTONOMOUS]), C.PRESET_AUTONOMOUS
    # choice == "4"
    return _customize_autonomy(console, Prompt), C.PRESET_CUSTOMIZE


def _customize_autonomy(console: Any, Prompt: Any) -> dict[str, str]:
    """Q4 customize sub-flow. Per-class picks with plain-English glosses."""
    console.print(
        "\nYou will pick a policy for each of the four action classes. "
        "The policies are:\n"
    )
    for i, p in enumerate(C.POLICIES, start=1):
        console.print(f"  {i}. [bold]{p}[/bold] -- {C.POLICY_LABELS[p]}")
    console.print()

    policy_list = list(C.POLICIES)
    valid_choices = [str(i) for i in range(1, len(policy_list) + 1)]
    policies: dict[str, str] = {}

    for action_class in C.ACTION_CLASSES:
        gloss = C.ACTION_CLASS_GLOSSES[action_class]
        console.print(f"[bold]{action_class}[/bold]: {gloss}")
        choice = Prompt.ask(
            f"  Policy for {action_class}",
            choices=valid_choices,
            default=str(len(policy_list)),  # default to escalate
            console=console,
        )
        policies[action_class] = policy_list[int(choice) - 1]
        console.print()

    return policies


def _ask_q5_voice(console: Any, Prompt: Any) -> str:
    """Q5: voice adjectives. Soft validation; re-prompts once if outside 1-5 (M5)."""
    raw = Prompt.ask(
        "Q5. How should this agent talk? "
        "(Two or three adjectives separated by commas. Examples: calm, direct, witty.)",
        console=console,
    )
    text = (raw or "").strip() or "clear, direct, helpful"

    # Soft validation: if comma-split count is outside 1-5, re-prompt once.
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if not (1 <= len(parts) <= 5):
        console.print(
            "[yellow]I expected 2-3 adjectives separated by commas. "
            "Press Enter to keep your answer as-is.[/yellow]"
        )
        retry = Prompt.ask(
            "Voice adjectives",
            console=console,
            default=text,
        )
        text = (retry or "").strip() or text

    return text


def _ask_q6_comm_prefs(console: Any, Prompt: Any) -> str:
    """Q6: communication preferences. Free text."""
    raw = Prompt.ask(
        "Q6. How do you prefer to communicate with it? "
        "(A few bullets. For example: answer first then explain, or context then answer; "
        "numbers vs prose; short vs detailed.)",
        console=console,
    )
    text = (raw or "").strip()
    return text or "- (No preferences specified at setup. Add details here.)"


def _ask_q7_hard_refusals(console: Any, Prompt: Any) -> str:
    """Q7: hard refusals. Rendered to BOTH USER.md and tools.md (P2 lock)."""
    raw = Prompt.ask(
        "Q7. Anything this agent should never do? "
        "(Hard refusals. Examples: never send email; never write outside its own folder; "
        "never make medical recommendations.)",
        console=console,
    )
    text = (raw or "").strip()
    return text or "(None configured at setup.)"


# ---------------------------------------------------------------------------
# Template variable builder
# ---------------------------------------------------------------------------


def _build_template_vars(answers: dict[str, Any]) -> dict[str, str]:
    """Map Q&A answers to the 12 locked template variable names (H2 lock).

    All 12 variables from spec/35 are included so safe_substitute finds
    every ${...} in all seven template files.
    """
    autonomy_policies: dict[str, str] = answers["autonomy_policies"]
    return {
        C.TEMPLATE_VAR_AGENT_NAME: answers["name"],
        C.TEMPLATE_VAR_MISSION: answers["mission"],
        C.TEMPLATE_VAR_SCOPE_IN: answers["scope_in"],
        C.TEMPLATE_VAR_SCOPE_OUT: answers["scope_out"],
        C.TEMPLATE_VAR_AUTONOMY_PRESET_LABEL: answers["preset_label"],
        C.TEMPLATE_VAR_AUTONOMY_READ_ONLY: autonomy_policies[C.ACTION_CLASS_READ_ONLY],
        C.TEMPLATE_VAR_AUTONOMY_REVERSIBLE_WRITE: autonomy_policies[
            C.ACTION_CLASS_REVERSIBLE_WRITE
        ],
        C.TEMPLATE_VAR_AUTONOMY_EXTERNAL_SIDE_EFFECT: autonomy_policies[
            C.ACTION_CLASS_EXTERNAL_SIDE_EFFECT
        ],
        C.TEMPLATE_VAR_AUTONOMY_HIGH_RISK: autonomy_policies[C.ACTION_CLASS_HIGH_RISK],
        C.TEMPLATE_VAR_VOICE: answers["voice"],
        C.TEMPLATE_VAR_COMM_PREFS: answers["comm_prefs"],
        C.TEMPLATE_VAR_HARD_REFUSALS: answers["hard_refusals"],
    }


# ---------------------------------------------------------------------------
# File rendering
# ---------------------------------------------------------------------------


def _render_files(
    agent_dir: Path,
    template_name: str,
    vars: dict[str, str],
) -> list[Path]:
    """Render all template files via string.Template.safe_substitute (MUST 13).

    Walks the template tree using importlib.resources.files(), renders each
    file, writes through _io.atomic_write (MUST 4).

    Returns list of files written.
    """
    from importlib import resources as _resources

    template_pkg_path = (
        _resources.files("atomic_agents.init") / "templates" / template_name
    )

    written: list[Path] = []

    # Walk the template tree. importlib.resources Traversable objects support
    # iterdir() recursively. We walk depth-first via deque (L1 depth-limited).
    for source_file, rel_parts in _walk_traversable(template_pkg_path, []):
        # Determine the target path under agent_dir.
        rel_path = str(Path(*rel_parts))

        # MUST 4 (defense-in-depth): validate the resolved target stays inside
        # agent_dir even though importlib.resources is trusted today.
        # safe_resolve_under raises PathTraversalError on any escape attempt.
        target = _io.safe_resolve_under(rel_path, agent_dir)

        # Read raw template content.
        raw = source_file.read_text(encoding="utf-8")

        # MUST 13: safe_substitute ONLY; never .substitute().
        rendered = string.Template(raw).safe_substitute(vars)

        # MUST 4: every write goes through atomic_write.
        # MUST 3: OSError is caught and translated by the caller (_write_scaffold).
        _io.atomic_write(target, rendered)
        written.append(target)

    return written


def _walk_traversable(
    root: Any,
    root_parts: list[str],
) -> list[tuple[Any, list[str]]]:
    """Walk a Traversable depth-first using an explicit deque (L1 depth cap).

    Uses a deque instead of recursion to avoid deep call stacks on unusually
    nested template trees. Logs a warning and stops if the walk exceeds
    C.MAX_TEMPLATE_DEPTH levels.
    """
    results = []
    # Stack entries: (node, parts, depth)
    stack: deque[tuple[Any, list[str], int]] = deque()
    stack.append((root, root_parts, 0))

    while stack:
        node, parts, depth = stack.popleft()
        if _traversable_is_dir(node):
            if depth > C.MAX_TEMPLATE_DEPTH:
                warnings.warn(
                    f"Template tree exceeds MAX_TEMPLATE_DEPTH={C.MAX_TEMPLATE_DEPTH}; "
                    f"stopping walk at {parts}. Review the template for deep nesting.",
                    stacklevel=2,
                )
                continue
            for child in node.iterdir():
                stack.append((child, parts + [child.name], depth + 1))
        else:
            if parts:  # skip the root node itself if it happens to be a file
                results.append((node, parts))

    return results


def _traversable_is_dir(node: Any) -> bool:
    """Return True when a Traversable node is a directory."""
    try:
        # importlib.resources.abc.Traversable exposes is_dir() in Python 3.9+.
        return node.is_dir()
    except AttributeError:
        # Fallback: try iterdir; a file raises an error.
        try:
            list(node.iterdir())
            return True
        except (NotADirectoryError, OSError):
            return False


# ---------------------------------------------------------------------------
# Section detection for Add-to-it merge contract (spec/35 MUST 15)
# ---------------------------------------------------------------------------


def _extract_h2_headers(text: str) -> list[str]:
    """Extract ATX-style h2 header strings from markdown text.

    Parser state machine handles:
    - YAML frontmatter: skipped when the first line is ``---`` (up to the next ``---``)
    - Code fences: lines inside `` ``` `` or ``~~~`` delimiters are not scanned
    - HTML comments: lines inside ``<!--`` ... ``-->`` are not scanned
    - Trailing closing hashes on ATX headers are stripped

    Setext-style h2 headers (underline with ``------``) are NOT supported per
    spec/35 MUST 15. Operators with setext files must convert to ATX before
    using Add-to-it.

    Returns a list of stripped header strings in document order.
    """
    headers: list[str] = []
    lines = text.splitlines()

    # Skip YAML frontmatter: if the first non-empty line is ``---``, skip until
    # the next ``---`` closing delimiter.
    start = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                start = i + 1
                break

    in_fence = False
    in_comment = False
    prev_stripped = ""

    for line in lines[start:]:
        stripped = line.strip()

        # Toggle code-fence state on ``` or ~~~ delimiters.
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            prev_stripped = stripped
            continue

        # Track HTML comment state (single-line or multi-line).
        # Only toggle on lines whose stripped form starts with ``<!--``
        # (tightening: avoids false positives from inline-code documentation of
        # HTML comment syntax, e.g. ``See <!-- note --> for details``).
        if stripped.startswith("<!--"):
            in_comment = True
        # Only toggle off when the stripped line ends with ``-->``
        # (symmetric tightening to avoid false-negatives from inline ``-->``).
        if stripped.endswith("-->"):
            in_comment = False
            prev_stripped = stripped
            continue

        if in_fence or in_comment:
            prev_stripped = stripped
            continue

        # Setext-style h2 detection: a line of only ``-`` chars (2+) immediately
        # after a non-empty content line is a setext h2 heading. Fail closed per
        # spec/35 MUST 15 -- the file will be added to failed_files by the caller.
        # A bare ``---`` thematic break is distinguished from a setext underline by
        # requiring the preceding line to be non-empty and not itself a heading-like
        # delimiter (``---`` after blank or ``---`` line is a thematic break, not
        # setext). Using 2+ chars and a non-empty prev line is the CommonMark rule.
        if (
            re.match(r"^-{2,}$", stripped)
            and prev_stripped
            and not prev_stripped.startswith("#")
            and not re.match(r"^[-=]{3,}$", prev_stripped)
        ):
            # Return a sentinel that the caller (_detect_sections) checks for.
            headers.append("__SETEXT_HEADING_DETECTED__")
            return headers

        prev_stripped = stripped

        # ATX h2: ``## Header text`` with optional trailing ``##``.
        m = re.match(r"^##\s+(.+?)(?:\s+#+)?\s*$", line)
        if m:
            headers.append(m.group(1).strip())

    return headers


def _detect_sections(
    agent_dir: Path,
    template_name: str,
) -> tuple[bool, dict[str, list[str]] | None, list[str]]:
    """Detect whether existing files match the template's section schema.

    Reads each file listed in C.TEMPLATE_SECTION_SCHEMA[template_name], extracts
    h2 headers using the state-machine parser, and checks that each file's headers
    are a SUPERSET of the schema's expected headers (operator-added orphan sections
    are allowed; schema-required headers must be present).

    Returns:
        (success, per_file_headers, failed_files)

        success=True when all schema files pass the superset check.
        per_file_headers maps file relpath to extracted h2 header list (or None
          for a missing file that will be backfilled).
        failed_files lists relpaths whose headers did not satisfy the schema.
    """
    schema = C.TEMPLATE_SECTION_SCHEMA.get(template_name, {})
    per_file_headers: dict[str, list[str]] = {}
    failed_files: list[str] = []

    for relpath, required_headers in schema.items():
        target = agent_dir / relpath
        if not target.exists():
            # Missing file: treat as backfill path (MUST 15); record as None.
            per_file_headers[relpath] = []
            # Missing files do NOT count as failures; they will be backfilled.
            continue

        try:
            raw = target.read_text(encoding="utf-8-sig")
        except OSError:
            failed_files.append(relpath)
            per_file_headers[relpath] = []
            continue

        # Normalize CRLF/CR to LF (A9).
        normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
        found = _extract_h2_headers(normalized)

        # M2: Setext-style heading sentinel means fail closed.
        if "__SETEXT_HEADING_DETECTED__" in found:
            failed_files.append(relpath)
            per_file_headers[relpath] = []
            continue

        # H1: Duplicate schema h2 headers in a single file -- fail closed.
        # Operator copy-paste errors produce duplicates that the merge algorithm
        # cannot resolve safely; route to overwrite/cancel so the operator decides.
        seen_in_file: set[str] = set()
        has_duplicate = False
        for h in found:
            if h in seen_in_file:
                has_duplicate = True
                break
            seen_in_file.add(h)
        if has_duplicate:
            failed_files.append(relpath)
            per_file_headers[relpath] = []
            continue

        per_file_headers[relpath] = found

        # Superset check: all required headers must appear in the found set.
        found_set = set(found)
        if not set(required_headers).issubset(found_set):
            failed_files.append(relpath)

    success = len(failed_files) == 0
    return success, per_file_headers, failed_files


# ---------------------------------------------------------------------------
# Add-to-it: section-level merge helpers
# ---------------------------------------------------------------------------


def _split_sections(content: str) -> list[tuple[str | None, str]]:
    """Split markdown content into a list of (header, body) blocks.

    The first block uses None as the header (content before the first h2).
    Subsequent blocks use the h2 header string as the key and include the
    header line itself at the start of the body string.

    Only ATX-style h2 headers split blocks. h3+ headers inside a block are
    kept verbatim as part of that block's body.

    Code fences, HTML comments, and YAML frontmatter are tracked via the same
    state machine used in _extract_h2_headers so we do not split on
    header-shaped lines inside those regions.
    """
    lines = content.splitlines(keepends=True)
    blocks: list[tuple[str | None, str]] = []
    current_header: str | None = None
    current_lines: list[str] = []

    # Skip YAML frontmatter.
    start = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                start = i + 1
                break

    in_fence = False
    in_comment = False

    for line in lines[start:]:
        stripped = line.strip()

        # Toggle code-fence state.
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            current_lines.append(line)
            continue

        # Track HTML comment state (line-start match only -- same tightening as
        # _extract_h2_headers so both state machines are consistent).
        if stripped.startswith("<!--"):
            in_comment = True
        if stripped.endswith("-->"):
            in_comment = False
            current_lines.append(line)
            continue

        if in_fence or in_comment:
            current_lines.append(line)
            continue

        # ATX h2: new section boundary.
        m = re.match(r"^##\s+(.+?)(?:\s+#+)?\s*$", line)
        if m:
            # Flush the current block.
            blocks.append((current_header, "".join(current_lines)))
            current_header = m.group(1).strip()
            current_lines = [line]
        else:
            current_lines.append(line)

    # Flush the final block.
    blocks.append((current_header, "".join(current_lines)))
    return blocks


def _join_sections(blocks: list[tuple[str | None, str]]) -> str:
    """Reassemble section blocks into a single string."""
    return "".join(body for _, body in blocks)


def _split_h3_subsections(body: str) -> tuple[str, list[tuple[str, str]]]:
    """Split the body text of a schema h2 block into a preamble and h3 sub-blocks.

    ``body`` is the full text of one h2 block as returned by ``_split_sections``,
    INCLUDING the opening ``## Header`` line at the start.

    Returns ``(preamble, [(h3_header_line, h3_body), ...])``.

    - ``preamble`` is everything from the start of ``body`` up to (but not
      including) the first ``### `` line.  It includes the ``## Header`` line
      itself plus any content between the h2 and the first h3.
    - Each h3 tuple is ``(full_header_line_including_newline, body_text)``.

    Uses the same state machine as ``_split_sections`` but matching ``^### ``
    instead of ``^## `` so code fences, HTML comments, and YAML frontmatter are
    respected.
    """
    lines = body.splitlines(keepends=True)
    preamble_lines: list[str] = []
    h3_blocks: list[tuple[str, str]] = []
    current_h3_header: str | None = None
    current_h3_lines: list[str] = []

    in_fence = False
    in_comment = False

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            if current_h3_header is None:
                preamble_lines.append(line)
            else:
                current_h3_lines.append(line)
            continue

        if stripped.startswith("<!--"):
            in_comment = True
        if stripped.endswith("-->"):
            in_comment = False
            if current_h3_header is None:
                preamble_lines.append(line)
            else:
                current_h3_lines.append(line)
            continue

        if in_fence or in_comment:
            if current_h3_header is None:
                preamble_lines.append(line)
            else:
                current_h3_lines.append(line)
            continue

        m = re.match(r"^###\s+(.+?)(?:\s+#+)?\s*$", line)
        if m:
            if current_h3_header is None:
                # First h3: flush preamble, nothing to push yet.
                pass
            else:
                # Flush previous h3 block.
                h3_blocks.append((current_h3_header, "".join(current_h3_lines)))
            current_h3_header = m.group(1).strip()
            current_h3_lines = [line]
        else:
            if current_h3_header is None:
                preamble_lines.append(line)
            else:
                current_h3_lines.append(line)

    # Flush the final h3 block if any.
    if current_h3_header is not None:
        h3_blocks.append((current_h3_header, "".join(current_h3_lines)))

    return "".join(preamble_lines), h3_blocks


def _join_h3_subsections(preamble: str, h3_blocks: list[tuple[str, str]]) -> str:
    """Reassemble a preamble and h3 sub-blocks into a single body string.

    Inverse of ``_split_h3_subsections``.  The preamble already contains the
    opening ``## Header`` line; h3 bodies already contain their ``### Header``
    lines.  This is a simple concatenation.
    """
    parts = [preamble]
    parts.extend(body for _, body in h3_blocks)
    return "".join(parts)


def _render_file_to_string(
    template_name: str, rel_parts: list[str], vars: dict[str, str]
) -> str:
    """Render a single template file to a string (does NOT write to disk).

    Uses the same importlib.resources path as _render_files but returns the
    rendered string rather than writing it.
    """
    from importlib import resources as _resources

    node: Any = _resources.files("atomic_agents.init") / "templates" / template_name
    for part in rel_parts:
        node = node / part
    raw = node.read_text(encoding="utf-8")
    return string.Template(raw).safe_substitute(vars)


def _compute_merged_content(
    agent_dir: Path,
    template_name: str,
    fresh_vars: dict[str, str],
) -> dict[str, str]:
    """Compute merged file content for every schema-owned file.

    For each file in TEMPLATE_SECTION_SCHEMA[template_name]:
    - If the file is missing from agent_dir: return fresh-rendered content
      (backfill).
    - Otherwise: split existing and fresh content into section blocks; merge
      using an ADDITIVE strategy for existing schema h2 blocks.

    ADDITIVE merge for existing schema h2 blocks (spec/35 MUST 15):
    - The existing preamble (text between ## Header line and first ###) is
      preserved verbatim -- the operator already filled this in.
    - h3+ subsections present in existing are preserved verbatim in original
      order.
    - h3+ subsections in the fresh template that are NOT in existing are
      appended at the end of the block (template added a new h3 the operator
      has not seen yet).
    - Schema-owned h2 blocks MISSING from existing are backfilled entirely
      from the fresh template (operator never had this section).

    Preamble before first h2: preserved verbatim.
    Orphan h2 sections (operator-authored, not in schema): preserved verbatim
      including all h3+ subsections.

    Operator data directories (memory/, journal/, log/, raw/) are never
    touched. This function only operates on files explicitly listed in the
    schema.

    Returns a dict mapping file relpath (str) -> merged content string.
    """
    schema = C.TEMPLATE_SECTION_SCHEMA.get(template_name, {})
    merged: dict[str, str] = {}

    for relpath, schema_headers in schema.items():
        target = agent_dir / relpath
        schema_header_set = set(schema_headers)

        # Compute relative path parts for the template walk.
        rel_parts = relpath.replace("\\", "/").split("/")

        # Render fresh content from template.
        fresh_text = _render_file_to_string(template_name, rel_parts, fresh_vars)

        if not target.exists():
            # Missing file: backfill entirely from template.
            merged[relpath] = fresh_text
            continue

        # Read and normalize existing file.
        try:
            existing_raw = target.read_text(encoding="utf-8-sig")
        except OSError:
            # If we cannot read the existing file, fall back to fresh content.
            merged[relpath] = fresh_text
            continue
        existing_text = existing_raw.replace("\r\n", "\n").replace("\r", "\n")

        # Split both into h2 section blocks.
        existing_blocks = _split_sections(existing_text)
        fresh_blocks = _split_sections(fresh_text)

        # Build lookup map: header -> body string (from fresh render).
        fresh_body_map: dict[str | None, str] = {h: body for h, body in fresh_blocks}

        # Assemble merged blocks preserving original order from existing.
        # Append any schema-owned blocks present only in fresh (new template
        # sections added since the agent was created).
        merged_blocks: list[tuple[str | None, str]] = []
        existing_headers_seen: set[str | None] = set()

        for header, existing_body in existing_blocks:
            existing_headers_seen.add(header)
            if header is None:
                # Preamble before first h2: always keep existing.
                merged_blocks.append((None, existing_body))
            elif header in schema_header_set:
                # Schema-owned section: ADDITIVE h3-aware merge.
                # Operator preamble wins; existing h3s preserved in order;
                # new template h3s appended at end.
                fresh_body = fresh_body_map.get(header)
                if fresh_body is not None:
                    existing_preamble, existing_h3s = _split_h3_subsections(
                        existing_body
                    )
                    _fresh_preamble, fresh_h3s = _split_h3_subsections(fresh_body)

                    # Operator preamble wins for existing schema h2 blocks.
                    merged_preamble = existing_preamble

                    # Preserve all existing h3 blocks in original order.
                    merged_h3s: list[tuple[str, str]] = list(existing_h3s)
                    existing_h3_names = {name for name, _ in existing_h3s}

                    # Append fresh h3 blocks not yet seen by operator.
                    for h3_name, h3_body in fresh_h3s:
                        if h3_name not in existing_h3_names:
                            merged_h3s.append((h3_name, h3_body))

                    merged_block_body = _join_h3_subsections(
                        merged_preamble, merged_h3s
                    )
                    merged_blocks.append((header, merged_block_body))
                else:
                    # Template removed this section; preserve existing.
                    merged_blocks.append((header, existing_body))
            else:
                # Orphan section (operator-authored): preserve verbatim
                # including any h3+ subsections.
                merged_blocks.append((header, existing_body))

        # Backfill: schema-owned sections in fresh that do not exist in
        # existing (new template sections). Use fresh content entirely.
        for header, fresh_body in fresh_blocks:
            if (
                header is not None
                and header in schema_header_set
                and header not in existing_headers_seen
            ):
                merged_blocks.append((header, fresh_body))

        merged[relpath] = _join_sections(merged_blocks)

    return merged


# ---------------------------------------------------------------------------
# Add-to-it: diff preview (file-level merge; no staging dir)
# ---------------------------------------------------------------------------


def _render_diff_preview(
    agent_dir: Path,
    merged_content: dict[str, str],
    console: Any,
) -> int:
    """Render a unified diff preview between existing files and merged content.

    merged_content maps file relpath (str) -> new merged content string.
    Files absent from the existing agent_dir are labeled [new file] and show
    full new content.

    Reads existing files with utf-8-sig + CRLF normalization (A9).
    Merged content is always LF (produced by section reassembly).

    Returns files_changed count (int). Returns -1 on any exception (sentinel:
    "preview failed, caller should proceed to Confirm regardless"; satisfies
    MUST 3 -- no stack traces propagate to operator).
    """
    try:
        return _render_diff_preview_inner(agent_dir, merged_content, console)
    except Exception as e:  # noqa: BLE001
        etype = type(e).__name__
        console.print(
            f"[yellow]Preview rendering failed: {etype}. "
            "Falling back to file list.[/yellow]"
        )
        for relpath_str in sorted(merged_content.keys()):
            console.print(f"  {relpath_str}")
        return -1


def _render_diff_preview_inner(
    agent_dir: Path,
    merged_content: dict[str, str],
    console: Any,
) -> int:
    """Inner implementation of _render_diff_preview (called via exception-guarded wrapper).

    Do NOT call this directly -- use _render_diff_preview so MUST 3 exception
    handling is always in place.
    """
    import difflib

    is_dumb = getattr(console, "is_dumb_terminal", False)
    if not is_dumb:
        from rich.syntax import Syntax  # noqa: PLC0415

    files_changed = 0
    total_ins = 0
    total_del = 0

    for relpath_str in sorted(merged_content.keys()):
        merged_text = merged_content[relpath_str]
        merged_lines = merged_text.splitlines(keepends=True)
        existing_path = agent_dir / relpath_str

        if not existing_path.exists():
            # New file backfill: show full content with [new file] label.
            console.print(f"[bold]--- [new file] {relpath_str} ---[/bold]")
            diff_text = "".join(f"+{line}" for line in merged_lines)
            if is_dumb:
                console.print(diff_text)
            else:
                console.print(Syntax(diff_text, language="diff"))  # noqa: F821
            files_changed += 1
            total_ins += len(merged_lines)
            continue

        try:
            existing_raw = existing_path.read_text(encoding="utf-8-sig")
        except OSError:
            existing_raw = ""
        existing_text = existing_raw.replace("\r\n", "\n").replace("\r", "\n")
        existing_lines = existing_text.splitlines(keepends=True)

        diff = list(
            difflib.unified_diff(
                existing_lines,
                merged_lines,
                fromfile=f"a/{relpath_str}",
                tofile=f"b/{relpath_str}",
            )
        )
        if not diff:
            continue

        console.print(f"[bold]--- {relpath_str} ---[/bold]")
        diff_text = "".join(diff)
        if is_dumb:
            console.print(diff_text)
        else:
            console.print(Syntax(diff_text, language="diff"))  # noqa: F821

        files_changed += 1
        total_ins += sum(
            1 for line in diff if line.startswith("+") and not line.startswith("+++")
        )
        total_del += sum(
            1 for line in diff if line.startswith("-") and not line.startswith("---")
        )

    console.print(
        f"\n{files_changed} files changed, "
        f"{total_ins} insertion(s)(+), "
        f"{total_del} deletion(s)(-)\n"
    )
    return files_changed


# ---------------------------------------------------------------------------
# Add-to-it: per-file atomic commit (no staging dir)
# ---------------------------------------------------------------------------


def _commit_merges(
    agent_dir: Path,
    merged_content: dict[str, str],
    console: Any,
) -> tuple[list[str], list[str]]:
    """Commit merged content by writing each file via atomic_write.

    Iterates through merged_content in sorted order and calls
    _io.atomic_write for each file. Returns (committed, failed) lists of
    relpaths.

    _io.atomic_write (tmp + fsync + rename) provides per-file atomicity: a
    crash mid-write leaves either the old file or the new file intact, never
    a half-written file. There is no transactional all-or-nothing guarantee
    across multiple files; each file commits independently.
    """
    committed: list[str] = []
    failed: list[str] = []
    pending: list[str] = sorted(merged_content.keys())

    for relpath_str in pending:
        content = merged_content[relpath_str]
        target = agent_dir / relpath_str
        try:
            _io.atomic_write(target, content)
            committed.append(relpath_str)
        except KeyboardInterrupt:
            # Print a summary of what landed before raising so the outer handler
            # can stay generic ("Canceled.").
            remaining = [r for r in pending if r not in committed and r != relpath_str]
            console.print(
                f"\n[yellow]Canceled mid-commit. "
                f"Wrote {len(committed)} of {len(pending)} file(s) before cancel. "
                f"Written: {', '.join(committed) if committed else 'none'}. "
                f"Pending (not written): "
                f"{', '.join([relpath_str] + remaining) if remaining or relpath_str else 'none'}."
                f"[/yellow]"
            )
            raise
        except (OSError, PathTraversalError) as e:
            console.print(f"[red]Failed to write {relpath_str}: {e}[/red]")
            failed.append(relpath_str)

    return committed, failed


# ---------------------------------------------------------------------------
# Add-to-it: main flow
# ---------------------------------------------------------------------------


def _add_to_it(
    agent_dir: Path,
    agents_root: Path,
    template_name: str,
    console: Any,
    Prompt: Any,
    Confirm: Any,
    existing_headers: dict[str, list[str]] | None,
) -> int:
    """Add-to-it merge flow: compute section-level merges, diff, commit per-file.

    existing_headers is the per_file_headers map from _check_collision's
    pre-flight _detect_sections call; it is informational only here.

    Operator-authored memory notes (under memory/ except INDEX.md), journal
    entries (journal/*.md), log files (log/), and raw documents (raw/) are
    never touched: _compute_merged_content only operates on files listed in
    TEMPLATE_SECTION_SCHEMA[template_name]. Schema-owned scaffolding files
    (memory/INDEX.md, wiki/INDEX.md) ARE rewritten through the normal merge
    pattern because they are template-owned routing/structure files.

    Returns an exit code (0 success, 1 error).
    """
    # Identify missing schema files for the backfill notice.
    schema = C.TEMPLATE_SECTION_SCHEMA.get(template_name, {})
    missing_files = [
        relpath for relpath in schema if not (agent_dir / relpath).exists()
    ]
    if missing_files:
        missing_list = ", ".join(missing_files)
        console.print(
            f"\n[dim]{C.MSG_MISSING_FILE_BACKFILL.format(files=missing_list)}[/dim]"
        )

    # Build fresh template vars for the merge.
    fresh_vars = _default_template_vars(agent_dir.name, template_name)

    # Compute section-level merged content for each schema-owned file.
    try:
        merged_content = _compute_merged_content(agent_dir, template_name, fresh_vars)
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]Failed to compute merged content: {e}[/red]")
        return 1

    # Show diff preview.
    console.print("\n[bold]Preview of changes:[/bold]\n")
    files_changed = _render_diff_preview(agent_dir, merged_content, console)

    # If the preview computed successfully and showed zero changes, skip Confirm.
    if files_changed == 0:
        # Zero TEXT diff does not mean the agent is doctor-clean: a declared
        # write-path dir (e.g. drafts/) may have been deleted out from under an
        # otherwise up-to-date agent. Reconcile declared write-paths the same
        # way the non-zero merge path and _write_scaffold do (#541 lockstep),
        # so Add-to-it never reports "up to date" while a declared dir is
        # missing. Idempotent: exist_ok=True, so this is a no-op when every
        # dir already exists.
        try:
            _create_empty_dirs(agent_dir)
        except OSError as e:
            console.print(
                f"[yellow]No text changes to apply, but a declared write-path "
                f"directory could not be created: {_translate_oserror(e, agent_dir)} "
                f"Run `atomic-agents doctor --agent {agent_dir.name}` and create "
                "any missing directories by hand.[/yellow]"
            )
            return 1
        console.print(
            "[green]No changes to apply. Existing scaffold is up to date.[/green]"
        )
        return 0

    # Confirm before committing.
    do_commit = Confirm.ask(
        "Apply these changes?",
        console=console,
        default=True,
    )
    if not do_commit:
        console.print("No changes made.")
        return 0

    committed, failed = _commit_merges(agent_dir, merged_content, console)

    if failed:
        console.print(
            f"[yellow]Partial update: {len(committed)} file(s) written, "
            f"{len(failed)} file(s) failed. "
            f"Written: {', '.join(committed)}. "
            f"Failed: {', '.join(failed)}.[/yellow]"
        )
        return 1

    # Add-to-it may backfill a tools.md that declares write_paths the existing
    # agent never had a directory for (e.g. switching to the writer template
    # introduces drafts/ + revisions/). Create any newly-declared write-path
    # dirs so the merged agent stays doctor-clean — same #541 lockstep the
    # fresh/overwrite scaffold relies on. Idempotent: exist_ok=True.
    try:
        _create_empty_dirs(agent_dir)
    except OSError as e:
        # A declared write-path dir could not be created: the merged agent is
        # NOT doctor-clean, so return non-zero — matching _write_scaffold's
        # contract (don't report success when the agent will fail doctor).
        # Suppress the green "updated" line so the message and exit code agree.
        console.print(
            f"[yellow]Files merged, but a declared write-path directory could "
            f"not be created: {_translate_oserror(e, agent_dir)} Run "
            f"`atomic-agents doctor --agent {agent_dir.name}` and create any "
            "missing directories by hand.[/yellow]"
        )
        return 1

    console.print(
        f"\n[green]Agent '{agent_dir.name}' updated at {agent_dir}.[/green]\n"
    )
    return 0


# ---------------------------------------------------------------------------
# Collision detection and backup+restore
# ---------------------------------------------------------------------------


def _check_collision(
    agent_dir: Path,
    console: Any,
    Prompt: Any,
    Confirm: Any,
    template_name: str | None = None,
) -> tuple[str, dict[str, list[str]] | None]:
    """Detect existing scaffold and ask what to do.

    Returns (branch, existing_headers) where branch is one of:
      "fresh"      -- agent_dir does not exist; no prompt shown.
      "overwrite"  -- operator chose to replace everything.
      "add_to_it"  -- operator chose section-level merge (only when template_name given).
      "cancel"     -- operator chose to leave the folder untouched.

    existing_headers is populated only on the add_to_it branch; it is the
    per_file_headers map from _detect_sections, so the caller can pass it
    directly to _add_to_it.

    When template_name is None (interactive Q&A path), add_to_it is not offered
    because there is no section schema to merge against.
    """
    if not agent_dir.exists():
        return ("fresh", None)

    console.print(
        f"\n[yellow]A folder named [bold]{agent_dir.name}[/bold] already exists at "
        f"[bold]{agent_dir}[/bold].[/yellow]"
    )

    if template_name is None:
        # No template available; only Overwrite or Cancel.
        choice = Prompt.ask(
            "What do you want to do?",
            choices=["overwrite", "cancel"],
            default="cancel",
            console=console,
        )
        return (choice, None)

    choice = Prompt.ask(
        "What do you want to do? "
        "[overwrite]=replace everything fresh, "
        "[add_to_it]=merge new answers into existing files (preserves your memory, journal, and operator-authored sections), "
        "[cancel]=leave the existing folder untouched",
        choices=["overwrite", "add_to_it", "cancel"],
        default="cancel",
        console=console,
    )

    if choice == "add_to_it":
        # Pre-detect sections so the caller knows if Add-to-it is viable.
        ok, headers, failed = _detect_sections(agent_dir, template_name)
        if not ok:
            failed_list = ", ".join(failed)
            console.print(
                f"[yellow]{C.MSG_SECTION_DETECTION_FAILED.format(template=template_name, files=failed_list)}[/yellow]"
            )
            fallback = Prompt.ask(
                "What do you want to do?",
                choices=["overwrite", "cancel"],
                default="cancel",
                console=console,
            )
            return (fallback, None)
        return ("add_to_it", headers)

    return (choice, None)


def _collision_overwrite_backup_restore(
    agent_dir: Path,
    write_func: Any,
) -> None:
    """Atomically rename existing agent_dir to .bak.<UTC-ISO-microsecond>, write, cleanup (MUST 5).

    Steps:
    1. Rename agent_dir to .bak.<timestamp> (atomic mv on POSIX).
    2. Call write_func() which writes to a fresh agent_dir.
    3. On success: rmtree the .bak dir.
    4. On any write failure: rename .bak back; re-raise so the caller can
       surface a plain-English error.
    """
    import shutil

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    backup_path = agent_dir.parent / f"{agent_dir.name}.bak.{ts}"

    agent_dir.rename(backup_path)  # POSIX atomic mv
    try:
        write_func()
    except BaseException as e:
        # Restore on failure: remove any partial new dir, rename backup back.
        if agent_dir.exists():
            shutil.rmtree(agent_dir, ignore_errors=True)
        backup_path.rename(agent_dir)
        if isinstance(e, KeyboardInterrupt):
            raise
        raise
    else:
        # Success: remove backup.
        shutil.rmtree(backup_path, ignore_errors=True)


# ---------------------------------------------------------------------------
# Empty directory creation
# ---------------------------------------------------------------------------


def _create_empty_dirs(agent_dir: Path) -> None:
    """Create every directory the scaffolded tools.md declares as a write path.

    These directories are required by the doctor's write-paths check (every
    declared write_path must exist on disk). This iterates EVERY parsed
    write_path and mkdirs each with exist_ok=True. memory/ and wiki/ are
    typically already present (atomic_write(memory/INDEX.md) and
    atomic_write(wiki/INDEX.md) created them on render), so their mkdir here is
    an idempotent no-op; the remaining declared subdirectories (journal/, log/,
    output/, drafts/, revisions/, ...) are created by this same parse-driven
    loop.

    Rather than hardcode a fixed subdir set (which silently broke the writer
    template's drafts/ + revisions/ when those bullets were added, #541), this
    parses the just-written ``tools.md`` and creates each declared write_path.
    A future template author who adds a write-path bullet gets the directory
    created automatically — the scaffold and the doctor's write-paths check
    stay in lockstep with a single source of truth (the rendered tools.md).

    Security (path traversal): the parser is given ``agent_root=agent_dir`` so
    bare-relative tokens resolve under the agent folder. Every resolved path is
    re-validated with ``_io.safe_resolve_under`` against agent_dir before any
    mkdir. A write_path bullet that resolves OUTSIDE the agent folder (an
    absolute path, a ``~`` path, or a ``../escape``) is REFUSED — never created
    — and a warning is emitted. The wizard only ever creates directories inside
    the agent it is scaffolding; it does not provision arbitrary filesystem
    locations a tools.md might name.

    read_paths and read_only_paths (e.g. researcher's raw/, writer's sources/)
    are intentionally NOT created here — only write_paths are.

    MUST 3: OSError from mkdir propagates and is caught/translated by the
    caller (_write_scaffold).
    """
    tools_path = agent_dir / "tools.md"
    parsed = _tools.parse_tools_md(tools_path, agent_root=agent_dir)
    write_paths = parsed.get("write_paths", [])

    for resolved in write_paths:
        # parse_tools_md already resolved each bullet (absolute Path) using
        # agent_root=agent_dir for bare-relative tokens. Re-validate containment
        # under agent_dir before creating anything: a bullet that resolves
        # outside the agent folder is refused, not created. safe_resolve_under
        # accepts an absolute child directly (root / abs == abs), so pass the
        # already-resolved path straight through — no relpath round-trip (which
        # would crash on Windows with a cross-drive ValueError that the OSError
        # caller does not catch).
        try:
            safe = _io.safe_resolve_under(resolved, agent_dir)
        except PathTraversalError:
            warnings.warn(
                f"init: tools.md write_path {str(resolved)!r} resolves outside "
                f"the agent folder {agent_dir}; refusing to create it. The "
                "wizard only provisions directories inside the agent it "
                "scaffolds. Remove or correct this write_path bullet.",
                stacklevel=2,
            )
            continue
        safe.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# OSError translation (T-EX1)
# ---------------------------------------------------------------------------


def _translate_oserror(e: OSError, path: Path) -> str:
    """Format a plain-English error string from an OSError (T-EX1)."""
    import errno as _errno

    if e.errno == _errno.ENOENT:
        return (
            f"The folder at {path} disappeared between collision check and overwrite. "
            "Re-run the wizard with a fresh state."
        )
    header = C.MSG_OSERROR_HEADER.format(path=path, reason=e.strerror or str(e))
    return f"{header}\n{C.MSG_OSERROR_FIX}"


# ---------------------------------------------------------------------------
# Scaffold writer (shared by interactive + from-template paths)
# ---------------------------------------------------------------------------


def _write_scaffold(
    agent_dir: Path,
    template_name: str,
    vars: dict[str, str],
    agent_name: str,
    agents_root: Path,
    console: Any,
    Confirm: Any,
    existing: bool,
) -> int:
    """Write the template files + create every write-path directory tools.md declares.

    Write-path directories are derived from the just-rendered tools.md via
    _create_empty_dirs (single source of truth; #541), so any template's
    declared write_paths exist on disk for the doctor's write-paths check.

    Handles the backup+restore pattern on overwrite. Every OSError is caught
    and translated to plain English (MUST 3). Returns 0 on success, 1 on error.
    """

    import shutil

    def _do_write() -> None:
        # MUST 3 / MUST 4: render_files uses atomic_write; OSError propagates up.
        _render_files(agent_dir, template_name, vars)
        _create_empty_dirs(agent_dir)

    try:
        if existing:
            # MUST 5: backup+restore on overwrite.
            _collision_overwrite_backup_restore(agent_dir, _do_write)
        else:
            try:
                _do_write()
            except BaseException as e:
                # H4: clean up any partial directory on fresh-write failure
                # so the operator is not left with a broken half-written scaffold.
                shutil.rmtree(agent_dir, ignore_errors=True)
                if isinstance(e, KeyboardInterrupt):
                    raise
                raise
    except PathTraversalError as e:
        # R2-H1: C1's safe_resolve_under raises PathTraversalError, which is NOT
        # an OSError. Defense-in-depth: this only fires if a template ever ships
        # a malicious relative path; templates are static today so this branch
        # is for future-proofing the contract per MUST 4.
        console.print(
            f"[red]Internal error: path validation refused a template file "
            f"({e}). This is a wizard bug; please file an issue.[/red]"
        )
        return 1
    except OSError as e:
        console.print(f"[red]{_translate_oserror(e, agent_dir)}[/red]")
        return 1

    console.print(
        f"\n[green]Agent '{agent_name}' created at {agent_dir}.[/green]\n"
        "Files written:\n"
        "  persona/IDENTITY.md, persona/SOUL.md, persona/USER.md\n"
        "  tools.md, model.md, memory/INDEX.md, wiki/INDEX.md\n"
        "  plus the empty write-path directories declared in tools.md "
        "(e.g. journal/, log/, output/)\n"
    )

    # MUST 8: doctor handoff.
    offer_test_call = _doctor_handoff(agent_name, agents_root, console)
    if not offer_test_call:
        console.print(
            f"Doctor found problems with the new agent. "
            f"Review the output above and fix before running. "
            f"Your files are at {agent_dir}."
        )
        return 1

    # MUST 9: opt-in test call gated on doctor passing.
    _maybe_test_call(agent_name, agents_root, agent_dir, console, Confirm)
    return 0


# ---------------------------------------------------------------------------
# Doctor handoff (R2-M2 split try/except per M11 enhancement)
# ---------------------------------------------------------------------------


def _doctor_handoff(agent_name: str, agents_root: Path, console: Any) -> bool:
    """Run doctor on the new agent and print results.

    Returns True if overall_exit_code is 0 (test-call prompt is safe to offer),
    False if any check FAILed (test-call prompt is suppressed, per MUST 8).

    If doctor itself fails unexpectedly, returns True and advises the operator
    to run doctor manually. The scaffold is ready; doctor is a diagnostic aid.

    Split try/except structure (R2-M2 / M11): run_doctor, overall_exit_code, and
    render_human each have independent exception handlers so a failure in one
    does not suppress output from the others.
    """
    from .. import doctor

    console.print("\n[bold]Running doctor to verify the new agent...[/bold]\n")

    try:
        results = doctor.run_doctor(
            agent_name=agent_name,
            agents_root=agents_root,
            skip_mcp=False,
        )
    except Exception:  # noqa: BLE001
        agent_dir = agents_root / agent_name
        console.print(
            "[yellow]Doctor inconclusive. Your agent is scaffolded at [bold]"
            f"{agent_dir}[/bold]. Run "
            f"[bold]atomic-agents doctor --agent {agent_name}[/bold] "
            "to verify when convenient.[/yellow]"
        )
        return True  # allow test-call prompt; doctor was inconclusive, not failed

    try:
        exit_code = doctor.overall_exit_code(results)
    except Exception:  # noqa: BLE001
        console.print(
            "[yellow]Doctor verdict unclear (overall_exit_code raised). Run "
            f"[bold]atomic-agents doctor --agent {agent_name}[/bold] to verify.[/yellow]"
        )
        return True  # allow test-call prompt; doctor was inconclusive, not failed

    try:
        console.print(doctor.render_human(results))
    except Exception:  # noqa: BLE001
        console.print(
            f"[yellow]Doctor ran but I could not render the report; exit_code={exit_code}. "
            f"Run [bold]atomic-agents doctor --agent {agent_name}[/bold] to see the details.[/yellow]"
        )

    if exit_code == 0:
        # Print preamble if any SKIP results (H5 lock).
        if any(r.status == "skip" for r in results):
            console.print(
                "[dim]Skipped checks are normal for a new agent "
                "(MCP, logs, write-paths configured later).[/dim]"
            )

    return exit_code == 0


# ---------------------------------------------------------------------------
# Opt-in test call
# ---------------------------------------------------------------------------


def _maybe_test_call(
    agent_name: str,
    agents_root: Path,
    agent_dir: Path,
    console: Any,
    Confirm: Any,
) -> None:
    """Offer the opt-in test call and run it if the operator accepts."""
    do_test = Confirm.ask(
        "Want to try a test call now?",
        console=console,
        default=True,
    )
    if do_test:
        _test_call(agent_name, agents_root, agent_dir, console)


def _test_call(
    agent_name: str,
    agents_root: Path,
    agent_dir: Path,
    console: Any,
) -> int:
    """Opt-in test call with full exception catalog (MUST 9). Always exits 0.

    Uses _types() helper for isinstance dispatch so the dispatch is correct
    even when the SDK is vendored or pinned to an unusual version.
    """
    from ..agent import AtomicAgent
    from ..exceptions import AtomicAgentsError

    # Lazy SDK imports for isinstance dispatch via _types(). Fall back to None
    # when unavailable so _types() returns () and isinstance never crashes.
    try:
        import anthropic as _anthropic_mod
    except ImportError:
        _anthropic_mod = None
    try:
        import httpx as _httpx_mod
    except ImportError:
        _httpx_mod = None

    console.print(
        f"\n[bold]Sending a test message to '{agent_name}'...[/bold]\n"
        f'Work item: "{C.TEST_CALL_WORK_ITEM}"\n'
    )

    try:
        agent = AtomicAgent(
            name=agent_name,
            trigger="manual",
            agents_root=agents_root,
        )
        response = agent.call(work_item=C.TEST_CALL_WORK_ITEM)
        if getattr(response, "skipped", False):
            skip_reason = getattr(response, "skip_reason", "unknown reason")
            console.print(f"[yellow]Skipped: {skip_reason}[/yellow]")
        else:
            text = getattr(response, "text", str(response))
            console.print(text)
    except Exception as e:  # noqa: BLE001
        if _anthropic_mod and isinstance(e, _types(_anthropic_mod, "RateLimitError")):
            console.print(
                f"[yellow]{C.MSG_TEST_CALL_RATE_LIMIT.format(agent_name=agent_name)}[/yellow]"
            )
        elif _anthropic_mod and isinstance(
            e, _types(_anthropic_mod, "AuthenticationError")
        ):
            console.print(f"[yellow]{C.MSG_TEST_CALL_AUTH_ERROR}[/yellow]")
        elif (
            _anthropic_mod
            and isinstance(e, _types(_anthropic_mod, "APIConnectionError"))
        ) or (
            _httpx_mod
            and isinstance(e, _types(_httpx_mod, "ConnectError", "TimeoutException"))
        ):
            console.print(f"[yellow]{C.MSG_TEST_CALL_NETWORK}[/yellow]")
        elif isinstance(e, AtomicAgentsError):
            console.print(f"[yellow]Atomic Agents error: {e}[/yellow]")
        else:
            console.print(
                f"[yellow]{C.MSG_TEST_CALL_GENERIC_FALLBACK.format(error_type=type(e).__name__, error_msg=str(e), path=str(agent_dir))}[/yellow]"
            )

    return 0  # always 0 on the opt-in path; scaffold already succeeded
