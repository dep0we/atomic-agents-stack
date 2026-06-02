"""atomic-agents init wizard. Scaffolds a working home-user agent in under 10 minutes.

See docs/spec/35-init-wizard.md for the 14 normative MUSTs this module satisfies.
"""

from __future__ import annotations

# Standard library imports only at module-top. rich is lazy-imported inside run_init
# per CLAUDE.md aesthetic and adversarial review discipline.
import os
import re
import string
import sys
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import _io, _llm, _platform
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
        console.print("\nCanceled. No files were written.")
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

    # Collision check.
    if existing:
        overwrite = _check_collision(agent_dir, console, Confirm)
        if not overwrite:
            return 0

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
    if existing:
        overwrite = _check_collision(agent_dir, console, Confirm)
        if not overwrite:
            return 0

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

    Returns (policies_dict, preset_label). Renders a rich Table (max_width=78).
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
            max_width=78,
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
                import warnings

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

    for line in lines[start:]:
        stripped = line.strip()

        # Toggle code-fence state on ``` or ~~~ delimiters.
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue

        # Track HTML comment state (single-line or multi-line).
        if "<!--" in stripped:
            in_comment = True
        if "-->" in stripped:
            in_comment = False
            continue

        if in_fence or in_comment:
            continue

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
        per_file_headers[relpath] = found

        # Superset check: all required headers must appear in the found set.
        found_set = set(found)
        if not set(required_headers).issubset(found_set):
            failed_files.append(relpath)

    success = len(failed_files) == 0
    return success, per_file_headers, failed_files


# ---------------------------------------------------------------------------
# Add-to-it: diff preview
# ---------------------------------------------------------------------------


def _render_diff_preview(
    agent_dir: Path,
    staging_dir: Path,
    console: Any,
    missing_files: list[str] | None = None,
) -> tuple[int, int, int]:
    """Render a unified diff preview between existing files and staged files.

    Reads existing files with utf-8-sig + CRLF normalization (A9).
    Staged files are always LF (written by _render_files).
    Missing-file backfills are labeled [new file] and show full new content.

    Returns (files_changed, total_insertions, total_deletions) for the summary
    line.
    """
    import difflib

    is_dumb = getattr(console, "is_dumb_terminal", False)
    missing_set = set(missing_files or [])

    files_changed = 0
    total_ins = 0
    total_del = 0

    # Walk the staged directory to find all files.
    for staged_path in sorted(staging_dir.rglob("*")):
        if not staged_path.is_file():
            continue

        try:
            rel = staged_path.relative_to(staging_dir)
        except ValueError:
            continue

        relpath_str = str(rel)
        existing_path = agent_dir / rel

        staged_text = staged_path.read_text(encoding="utf-8")
        staged_lines = staged_text.splitlines(keepends=True)

        if relpath_str in missing_set or not existing_path.exists():
            # New file backfill: show full content with [new file] label.
            console.print(f"[bold]--- [new file] {relpath_str} ---[/bold]")
            diff_text = "".join(f"+{line}" for line in staged_lines)
            if is_dumb:
                console.print(diff_text)
            else:
                from rich.syntax import Syntax

                console.print(Syntax(diff_text, language="diff"))
            files_changed += 1
            total_ins += len(staged_lines)
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
                staged_lines,
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
            from rich.syntax import Syntax

            console.print(Syntax(diff_text, language="diff"))

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
    return files_changed, total_ins, total_del


# ---------------------------------------------------------------------------
# Add-to-it: staging-dir commit (A5 reversal pattern)
# ---------------------------------------------------------------------------


def _commit_add_to_it(agent_dir: Path, staging_dir: Path, console: Any) -> None:
    """Commit the staged scaffold into agent_dir using the A5 reversal pattern.

    Steps:
    1. Compute a backup path: <agent_dir>.bak.<UTC-ISO-microsecond>
    2. Atomically rename agent_dir to bak_path (backup)
    3. Atomically rename staging_dir to agent_dir (commit)
    4. On success: rmtree the backup
    5. On any failure between steps 2 and 3: rename bak back, leave staging,
       print error, re-raise

    Wrapped in try/except BaseException so KeyboardInterrupt during the
    rename window is handled: if KI lands after step 2 but before step 3,
    the outer KI handler will find agent_dir absent + bak present + staging
    present and can complete restoration.
    """
    import shutil

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    bak_path = agent_dir.parent / f"{agent_dir.name}.bak.{ts}"

    # Step 1+2: backup.
    agent_dir.rename(bak_path)
    try:
        # Step 3: commit.
        staging_dir.rename(agent_dir)
    except BaseException as e:
        # Restore backup; leave staging in place for diagnostics.
        try:
            bak_path.rename(agent_dir)
        except OSError:
            pass
        if isinstance(e, KeyboardInterrupt):
            raise
        raise OSError(
            f"Failed to rename staging dir to {agent_dir}; backup restored from {bak_path}. "
            f"Staging dir left at {staging_dir} for manual inspection."
        ) from e
    else:
        # Success: remove backup.
        shutil.rmtree(bak_path, ignore_errors=True)


# ---------------------------------------------------------------------------
# Add-to-it: stale staging dir recovery (M9)
# ---------------------------------------------------------------------------


def _check_stale_staging_dirs(
    agent_dir: Path,
    console: Any,
    Confirm: Any,
) -> bool:
    """Scan for leftover staging directories from a previous run.

    If any <agent_dir>.new.* siblings are found, offer to delete them.
    Returns True if safe to proceed (none found, or operator approved deletion).
    Returns False if operator declined (exit cleanly).
    """
    import shutil

    stale = sorted(agent_dir.parent.glob(f"{agent_dir.name}.new.*"))
    if not stale:
        return True

    for stale_path in stale:
        msg = C.MSG_STAGING_DIR_EXISTS.format(path=stale_path)
        console.print(f"\n[yellow]{msg}[/yellow]")
        do_delete = Confirm.ask(
            "Delete it and continue?",
            console=console,
            default=True,
        )
        if not do_delete:
            return False
        shutil.rmtree(stale_path, ignore_errors=True)

    return True


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
    Table: Any,
    existing_headers: dict[str, list[str]] | None,
) -> int:
    """Add-to-it merge flow: detect sections, render staged scaffold, diff, commit.

    Runs section detection if not already provided via existing_headers.
    On detection failure, offers Overwrite or Cancel only (fail-closed).
    On success, renders the new scaffold to a staging dir, shows a diff,
    and asks the operator to confirm before committing.

    Returns an exit code (0 success, 1 error).
    """
    import shutil

    # Stale staging dir recovery before creating a new one.
    if not _check_stale_staging_dirs(agent_dir, console, Confirm):
        return 0

    # Run section detection.
    success, per_file_headers, failed_files = _detect_sections(agent_dir, template_name)

    if not success:
        failed_list = ", ".join(failed_files)
        console.print(
            f"\n[yellow]{C.MSG_SECTION_DETECTION_FAILED.format(template=template_name, files=failed_list)}[/yellow]"
        )
        do_overwrite = Confirm.ask(
            "Overwrite the existing folder instead?",
            console=console,
            default=False,
        )
        if not do_overwrite:
            return 0
        # Fall through to a standard overwrite via _write_scaffold.
        return None  # type: ignore[return-value]  # caller interprets None as "do overwrite"

    # Identify files missing entirely (will be backfilled from template).
    schema = C.TEMPLATE_SECTION_SCHEMA.get(template_name, {})
    missing_files = [
        relpath for relpath in schema if not (agent_dir / relpath).exists()
    ]
    if missing_files:
        missing_list = ", ".join(missing_files)
        console.print(
            f"\n[dim]{C.MSG_MISSING_FILE_BACKFILL.format(files=missing_list)}[/dim]"
        )

    # Build template vars from defaults (no Q&A re-run for add-to-it).
    default_vars = _default_template_vars(agent_dir.name, template_name)

    # Create staging directory.
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    staging_dir = agent_dir.parent / f"{agent_dir.name}.new.{ts}"

    try:
        _render_files(staging_dir, template_name, default_vars)
        _create_empty_dirs(staging_dir)
    except BaseException as e:
        shutil.rmtree(staging_dir, ignore_errors=True)
        if isinstance(e, KeyboardInterrupt):
            raise
        raise

    # Show diff preview.
    console.print("\n[bold]Preview of changes:[/bold]\n")
    _render_diff_preview(agent_dir, staging_dir, console, missing_files=missing_files)

    # Confirm before committing.
    do_commit = Confirm.ask(
        "Apply these changes?",
        console=console,
        default=True,
    )
    if not do_commit:
        shutil.rmtree(staging_dir, ignore_errors=True)
        console.print("No changes made.")
        return 0

    try:
        _commit_add_to_it(agent_dir, staging_dir, console)
    except (OSError, PathTraversalError) as e:
        console.print(f"[red]{e}[/red]")
        return 1

    console.print(
        f"\n[green]Agent '{agent_dir.name}' updated at {agent_dir}.[/green]\n"
    )
    return 0


# ---------------------------------------------------------------------------
# Collision detection and backup+restore
# ---------------------------------------------------------------------------


def _check_collision(agent_dir: Path, console: Any, Confirm: Any) -> bool:
    """Detect existing scaffold. Offer Overwrite/Cancel (default Cancel).

    Returns True if the operator chose to overwrite.
    """
    console.print(
        f"\n[yellow]A folder named '{agent_dir.name}' already exists at "
        f"{agent_dir}.[/yellow]"
    )
    return bool(
        Confirm.ask(
            "Overwrite it?",
            console=console,
            default=False,
        )
    )


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
    """Create journal/ and log/ directories (MUST 3: OSError caught by caller).

    The framework populates these on first run. We only mkdir here.
    """
    for subdir in ("journal", "log"):
        (agent_dir / subdir).mkdir(parents=True, exist_ok=True)


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
    """Write the seven template files + create journal/ and log/ directories.

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
        "  journal/ (empty), log/ (empty)\n"
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
        exit_code = -1  # unknown

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
