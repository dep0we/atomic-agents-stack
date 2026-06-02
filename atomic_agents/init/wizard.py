"""atomic-agents init wizard. Scaffolds a working home-user agent in under 10 minutes.

See docs/spec/35-init-wizard.md for the 14 normative MUSTs this module satisfies.
"""

from __future__ import annotations

# Standard library imports only at module-top. rich is lazy-imported inside run_init
# per CLAUDE.md aesthetic and adversarial review discipline.
import os
import string
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import _io, _llm, _platform
from ..exceptions import PathTraversalError
from . import constants as C


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

    # --list-templates writes nothing, so the persona-backend guard is skipped
    # per spec/35: the guard applies when files would be written.
    if args.list_templates:
        return _cmd_list_templates(console)

    # Resolve agents_root once at entry (MUST H6 / M9).
    agents_root = _resolve_agents_root(args)

    # MUST 7: API key pre-flight via _get_key (env vars + Keychain + keys.json).
    if not _api_key_preflight():
        print(C.MSG_NO_PROVIDER_KEY, file=sys.stderr)
        return 1

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

    return _interactive(args.agent_name, agents_root, console, Prompt, Confirm, Table)


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
    if not os.environ.get("ATOMIC_AGENTS_PERSONA_BACKEND_URL", "").strip():
        return True  # No custom backend set; safe to proceed.

    console.print(f"\n[yellow]{C.MSG_PERSONA_BACKEND_WARNING}[/yellow]\n")
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
    """Print the available template names and a one-line description."""
    console.print("\nAvailable templates:\n")
    console.print(
        "  [bold]advisor[/bold]  -- General-purpose personal advisor. "
        "Good starting point for most home-user agents."
    )
    console.print()
    console.print(
        "Run `atomic-agents init <name> --from-template advisor` to scaffold "
        "without the Q&A wizard.\n"
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
    known_templates = {"advisor"}
    if template_name not in known_templates:
        console.print(
            f"[red]Unknown template '{template_name}'.[/red] "
            f"Run `atomic-agents init --list-templates` to see available options."
        )
        return 1

    # Q1 name validation still required (MUST 1).
    if agent_name:
        name = agent_name.strip()
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

    # Build a minimal set of template vars using safe defaults.
    default_vars = _default_template_vars(name)

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


def _default_template_vars(name: str) -> dict[str, str]:
    """Minimal defaults for --from-template (no Q&A)."""
    preset = C.AUTONOMY_PRESETS[C.PRESET_CAUTIOUS]
    return {
        C.TEMPLATE_VAR_AGENT_NAME: name,
        C.TEMPLATE_VAR_MISSION: "(Configure in persona/IDENTITY.md after setup.)",
        C.TEMPLATE_VAR_SCOPE_IN: "- (Add in-scope work items here.)",
        C.TEMPLATE_VAR_SCOPE_OUT: "- (Add out-of-scope refusals here.)",
        C.TEMPLATE_VAR_AUTONOMY_PRESET_LABEL: C.PRESET_CAUTIOUS,
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
    # iterdir() recursively. We walk depth-first.
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
    node: Any,
    parts: list[str],
) -> list[tuple[Any, list[str]]]:
    """Recursively walk a Traversable, yielding (file_node, [relative, parts])."""
    results = []
    for child in node.iterdir():
        child_parts = parts + [child.name]
        if _traversable_is_dir(child):
            results.extend(_walk_traversable(child, child_parts))
        else:
            results.append((child, child_parts))
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
    """Atomically rename existing agent_dir to .bak.<UTC-ISO>, write, cleanup (MUST 5).

    Steps:
    1. Rename agent_dir to .bak.<timestamp> (atomic mv on POSIX).
    2. Call write_func() which writes to a fresh agent_dir.
    3. On success: rmtree the .bak dir.
    4. On any write failure: rename .bak back; re-raise so the caller can
       surface a plain-English error.
    """
    import shutil

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = agent_dir.parent / f"{agent_dir.name}.bak.{ts}"

    agent_dir.rename(backup_path)  # POSIX atomic mv
    try:
        write_func()
    except Exception:
        # Restore on failure: remove any partial new dir, rename backup back.
        if agent_dir.exists():
            shutil.rmtree(agent_dir, ignore_errors=True)
        backup_path.rename(agent_dir)
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
            except Exception:
                # H4: clean up any partial directory on fresh-write failure
                # so the operator is not left with a broken half-written scaffold.
                shutil.rmtree(agent_dir, ignore_errors=True)
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
# Doctor handoff
# ---------------------------------------------------------------------------


def _doctor_handoff(agent_name: str, agents_root: Path, console: Any) -> bool:
    """Run doctor on the new agent and print results.

    Returns True if overall_exit_code is 0 (test-call prompt is safe to offer),
    False if any check FAILed (test-call prompt is suppressed, per MUST 8).

    If doctor itself fails unexpectedly, returns True and advises the operator
    to run doctor manually. The scaffold is ready; doctor is a diagnostic aid.
    """
    from .. import doctor

    console.print("\n[bold]Running doctor to verify the new agent...[/bold]\n")
    try:
        results = doctor.run_doctor(
            agent_name=agent_name,
            agents_root=agents_root,
            skip_mcp=False,
        )
        console.print(doctor.render_human(results))
    except Exception:  # noqa: BLE001
        agent_dir = agents_root / agent_name
        console.print(
            f"Doctor inconclusive. Your agent is scaffolded at `{agent_dir}`. "
            f"Run `atomic-agents doctor --agent {agent_name}` whenever you want to verify."
        )
        return True

    exit_code = doctor.overall_exit_code(results)

    # If any results are SKIP, surface the preamble (H5 lock).
    skip_status = getattr(doctor, "SKIP", "skip")
    has_skips = any(
        getattr(r, "status", "").lower() == skip_status.lower()
        if isinstance(skip_status, str)
        else getattr(r, "status", None) == skip_status
        for r in results
    )
    if exit_code == 0 and has_skips:
        console.print(
            "[dim]Skipped checks are normal for a new agent "
            "(MCP, logs, and write-paths are configured later).[/dim]\n"
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

    Uses isinstance checks via lazy imports so the dispatch is correct even when
    the SDK is vendored or pinned to an unusual version.
    """
    from ..agent import AtomicAgent
    from ..exceptions import AtomicAgentsError

    # Lazy SDK imports for isinstance dispatch. Fall back to None when unavailable
    # so getattr(mod, "ClassName", ()) returns () and isinstance never crashes.
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
        if _anthropic_mod and isinstance(
            e, getattr(_anthropic_mod, "RateLimitError", ())
        ):
            console.print(
                f"[yellow]{C.MSG_TEST_CALL_RATE_LIMIT.format(agent_name=agent_name)}[/yellow]"
            )
        elif _anthropic_mod and isinstance(
            e, getattr(_anthropic_mod, "AuthenticationError", ())
        ):
            console.print(f"[yellow]{C.MSG_TEST_CALL_AUTH_ERROR}[/yellow]")
        elif (
            _anthropic_mod
            and isinstance(e, getattr(_anthropic_mod, "APIConnectionError", ()))
        ) or (
            _httpx_mod
            and isinstance(
                e,
                (
                    getattr(_httpx_mod, "ConnectError", ()),
                    getattr(_httpx_mod, "TimeoutException", ()),
                ),
            )
        ):
            console.print(f"[yellow]{C.MSG_TEST_CALL_NETWORK}[/yellow]")
        elif isinstance(e, AtomicAgentsError):
            console.print(f"[yellow]Atomic Agents error: {e}[/yellow]")
        else:
            console.print(
                f"[yellow]{C.MSG_TEST_CALL_GENERIC_FALLBACK.format(error_type=type(e).__name__, error_msg=str(e), path=str(agent_dir))}[/yellow]"
            )

    return 0  # always 0 on the opt-in path; scaffold already succeeded
