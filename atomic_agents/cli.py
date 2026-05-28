"""CLI for atomic_agents — the `atomic-agents` console script.

Usage:
    atomic-agents run <agent> [options]
    atomic-agents info <agent> [options]
    atomic-agents skills <agent> [options]
    atomic-agents version <agent> <note-filename>
    atomic-agents restore <agent> <note-filename> <version-name>
    atomic-agents bundle <agent> [--if-stale | --refresh] [options]
    atomic-agents doctor [--agent <name>] [--json] [--no-mcp]
    atomic-agents review --backend <kimi> [options]
    atomic-agents persona list
    atomic-agents persona show <persona_id>
    atomic-agents persona snapshot <persona_id> [--label "..."]
    atomic-agents persona list-snapshots <persona_id>
    atomic-agents persona restore <persona_id> <snapshot_id>
    atomic-agents persona clone <source_id> <target_id>

Subcommands:
    run     — Run an agent against a work item
    info    — Show config for an agent without running it
    skills  — List all skills for an agent with metadata and warnings
    version — List memory note version snapshots
    restore — Restore a memory note from a snapshot
    bundle  — Pre-render the cascade into one file for skill-mode loads (spec/26)
    doctor  — Preflight checks before scheduling an agent run
    review  — Cross-family adversarial code review (CLAUDE.md rule #11)
    persona — Manage persona records (list, show, snapshot, restore, clone)
"""

from __future__ import annotations
import argparse
import sys
from pathlib import Path

from .agent import AtomicAgent
from ._platform import get_agents_root
from .memory.filesystem import FilesystemBackend
from .memory.backend import WritePolicy
from .exceptions import AtomicAgentsError, VersionNotFound
from .skills import validate_skill_manifest

# Persona exceptions -- imported lazily inside handlers to avoid
# slowing down the hot path for agents that don't use persona subcommands.
# PersonaNotFound, PersonaExists, PersonaSnapshotNotFound re-exported
# from atomic_agents.persona so CLI handlers can catch them by name.


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="atomic-agents", description="Atomic Agents CLI"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="Run an agent against a work item")
    run.add_argument("agent", help="agent name (folder under agents-root)")
    run.add_argument("--work-item", required=True, help="user message / work item text")
    run.add_argument(
        "--trigger", default="manual", choices=["cron", "skill", "manual", "api"]
    )
    run.add_argument("--model", default=None, help="override default model")
    run.add_argument("--critical", action="store_true", help="bypass cost guardrails")
    run.add_argument(
        "--no-write-captures",
        action="store_true",
        help="extract captures but don't persist (dry-run)",
    )
    run.add_argument("--agents-root", default=None, help="override ATOMIC_AGENTS_ROOT")

    info = sub.add_parser("info", help="Show config for an agent without running it")
    info.add_argument("agent")
    info.add_argument("--agents-root", default=None)

    skills_cmd = sub.add_parser(
        "skills",
        help="List all skills for an agent (name, description, body line count, warnings)",
    )
    skills_cmd.add_argument("agent", help="agent name (folder under agents-root)")
    skills_cmd.add_argument(
        "--agents-root", default=None, help="override ATOMIC_AGENTS_ROOT"
    )

    version_cmd = sub.add_parser("version", help="List versions for a memory note")
    version_cmd.add_argument("agent", help="agent name (folder under agents-root)")
    version_cmd.add_argument(
        "note_filename", help="bare filename, e.g. feedback_comm_style.md"
    )
    version_cmd.add_argument("--agents-root", default=None)

    restore_cmd = sub.add_parser(
        "restore", help="Restore a memory note from a snapshot"
    )
    restore_cmd.add_argument("agent", help="agent name (folder under agents-root)")
    restore_cmd.add_argument(
        "note_filename", help="bare filename, e.g. feedback_comm_style.md"
    )
    restore_cmd.add_argument("version_name", help="version filename to restore from")
    restore_cmd.add_argument("--agents-root", default=None)

    bundle_cmd = sub.add_parser(
        "bundle",
        help="Pre-render the cascade into a single file for skill-mode loads (spec/26)",
        description=(
            "Concatenate every cascade file (persona, tools, model, project layer, "
            "memory INDEX + pinned/recent atomic notes, wiki INDEX, recent journal) "
            "into one markdown file the skill can load via a single Read. Issue #231."
        ),
    )
    bundle_cmd.add_argument("agent", help="agent name (folder under agents-root)")
    bundle_cmd.add_argument(
        "--agents-root",
        default=None,
        help="override ATOMIC_AGENTS_ROOT",
    )
    bundle_cmd.add_argument(
        "--cache-dir",
        default=None,
        help=(
            "override the bundle cache directory "
            "(default: $ATOMIC_AGENTS_CACHE_DIR or ~/.cache/atomic-agents/bundles)"
        ),
    )
    bundle_cmd.add_argument(
        "--extra-file",
        action="append",
        default=[],
        metavar="PATH",
        help=(
            "include an extra file in the bundle (repeatable). For declarative "
            "extras, create <agent>/bundle.md listing one path per line."
        ),
    )
    staleness_group = bundle_cmd.add_mutually_exclusive_group()
    staleness_group.add_argument(
        "--if-stale",
        action="store_true",
        help=(
            "skip regeneration when the bundle's mtime is at least as new as "
            "every source file's mtime (the skill-mode invocation path)"
        ),
    )
    staleness_group.add_argument(
        "--refresh",
        action="store_true",
        help="force regeneration even when the bundle is fresh",
    )
    bundle_cmd.add_argument(
        "--to-stdout",
        action="store_true",
        help="print the bundle contents to stdout instead of writing to disk",
    )
    bundle_cmd.add_argument(
        "--print-path",
        action="store_true",
        help="print the bundle path without (re)generating",
    )

    doctor_cmd = sub.add_parser(
        "doctor",
        help="Run preflight checks (env, vault, keys, model, mcp, locks, write-paths)",
    )
    doctor_cmd.add_argument(
        "--agent",
        default=None,
        help="check this agent specifically (omit to run host-only checks)",
    )
    doctor_cmd.add_argument(
        "--agents-root",
        default=None,
        help="override ATOMIC_AGENTS_ROOT for this run",
    )
    doctor_cmd.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON instead of the human report",
    )
    doctor_cmd.add_argument(
        "--no-mcp",
        action="store_true",
        help="skip MCP server handshake (faster; useful when servers are remote)",
    )

    review_cmd = sub.add_parser(
        "review",
        help="Run a cross-family adversarial code review against a target file",
        description=(
            "Cross-family adversarial review via a non-author model. Per "
            "CLAUDE.md rule #11 (review in rounds) + rule #12 (verify before "
            "claim). Output goes to stdout (markdown); cost summary to stderr."
        ),
    )
    review_cmd.add_argument(
        "--backend",
        required=True,
        choices=["kimi"],
        help="reviewer model family (kimi = Moonshot Kimi 2.6)",
    )
    review_prompt_group = review_cmd.add_mutually_exclusive_group(required=True)
    review_prompt_group.add_argument(
        "--prompt",
        help="adversarial review prompt as inline text",
    )
    review_prompt_group.add_argument(
        "--prompt-file",
        help="path to a markdown file containing the review prompt",
    )
    review_cmd.add_argument(
        "--target",
        default=None,
        help="primary file under review (read first by the reviewer)",
    )
    review_cmd.add_argument(
        "--read-files",
        default="",
        help=(
            "comma-separated list of files for grounding context "
            "(e.g. 'CLAUDE.md,docs/spec/28-judge-layer.md')"
        ),
    )
    review_cmd.add_argument(
        "--working-dir",
        default=None,
        help="working directory for resolving --target and --read-files (default: cwd)",
    )
    review_cmd.add_argument(
        "--model",
        default=None,
        help="override the default model id for the chosen backend",
    )
    review_cmd.add_argument(
        "--max-tokens",
        type=int,
        default=16000,
        help=(
            "max output tokens for the reviewer (default: 16000 — reasoning "
            "models like Kimi K2.6 use a large slice of this for internal "
            "reasoning_content before producing the visible review)"
        ),
    )

    # ── persona subcommand group ──────────────────────────────────────────
    persona_cmd = sub.add_parser(
        "persona",
        help="Manage persona records (list, show, snapshot, restore, clone)",
    )
    persona_sub = persona_cmd.add_subparsers(dest="persona_cmd", required=True)

    # persona list
    persona_sub.add_parser("list", help="List all persona_ids")

    # persona show <persona_id>
    persona_show = persona_sub.add_parser(
        "show", help="Print IDENTITY, SOUL, and USER bodies plus metadata"
    )
    persona_show.add_argument("persona_id", help="persona identifier")

    # persona snapshot <persona_id> [--label "..."]
    persona_snapshot = persona_sub.add_parser(
        "snapshot", help="Create a snapshot of a persona and print the snapshot_id"
    )
    persona_snapshot.add_argument("persona_id", help="persona identifier")
    persona_snapshot.add_argument(
        "--label", default=None, help="optional human-readable label for this snapshot"
    )

    # persona list-snapshots <persona_id>
    persona_list_snaps = persona_sub.add_parser(
        "list-snapshots", help="List snapshots for a persona in chronological order"
    )
    persona_list_snaps.add_argument("persona_id", help="persona identifier")

    # persona restore <persona_id> <snapshot_id>
    persona_restore = persona_sub.add_parser(
        "restore", help="Restore a persona to a previously captured snapshot"
    )
    persona_restore.add_argument("persona_id", help="persona identifier")
    persona_restore.add_argument("snapshot_id", help="snapshot identifier to restore")

    # persona clone <source_id> <target_id>
    persona_clone = persona_sub.add_parser(
        "clone", help="Copy a persona record to a new persona_id"
    )
    persona_clone.add_argument("source_id", help="persona to clone from")
    persona_clone.add_argument(
        "target_id", help="destination persona_id (must not exist)"
    )

    args = parser.parse_args(argv)

    # `review` is a host-only subcommand — no agents-root needed (operates on
    # arbitrary files, not agent folders). All other subcommands resolve
    # agents_root either from --agents-root or the ATOMIC_AGENTS_ROOT env var.
    if args.cmd == "review":
        try:
            return _cmd_review(args)
        except AtomicAgentsError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

    # `persona` is a host-only subcommand that resolves its own scope_root
    # from the cwd. It does not need agents-root.
    if args.cmd == "persona":
        return _cmd_persona(args)

    agents_root = (
        Path(args.agents_root).expanduser().resolve()
        if args.agents_root
        else get_agents_root()
    )

    # Doctor has its own exit-code semantics (0/1/2) and must never raise to the user.
    if args.cmd == "doctor":
        return _cmd_doctor(args)

    try:
        if args.cmd == "run":
            return _cmd_run(args, agents_root)
        elif args.cmd == "info":
            return _cmd_info(args, agents_root)
        elif args.cmd == "skills":
            return _cmd_skills(args, agents_root)
        elif args.cmd == "version":
            return _cmd_version(args, agents_root)
        elif args.cmd == "restore":
            return _cmd_restore(args, agents_root)
        elif args.cmd == "bundle":
            return _cmd_bundle(args, agents_root)
    except AtomicAgentsError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


def _cmd_review(args) -> int:
    """Run a cross-family adversarial review and stream the output to stdout.

    Reads --prompt or --prompt-file, optionally pulls in --target +
    --read-files for grounding, dispatches to the chosen backend's LLM,
    prints the review to stdout, and prints a cost summary to stderr.
    """
    from . import review as review_mod

    if args.prompt is not None:
        prompt_text = args.prompt
    else:
        prompt_path = Path(args.prompt_file).expanduser()
        if not prompt_path.exists():
            print(f"Error: --prompt-file not found: {prompt_path}", file=sys.stderr)
            return 1
        prompt_text = prompt_path.read_text(encoding="utf-8")

    # Refuse empty / whitespace-only prompts. The LLM call would still bill
    # for the system-prompt input tokens, return empty output, and exit 0 —
    # a silent no-op that wastes spend and hides operator misconfiguration.
    if not prompt_text.strip():
        print("Error: prompt is empty", file=sys.stderr)
        return 1

    working_dir = (
        Path(args.working_dir).expanduser().resolve()
        if args.working_dir
        else Path.cwd()
    )

    read_files: list[Path] = []
    if args.read_files:
        for entry in args.read_files.split(","):
            entry = entry.strip()
            if entry:
                read_files.append(Path(entry))

    target = Path(args.target) if args.target else None

    request = review_mod.ReviewRequest(
        backend=args.backend,
        prompt=prompt_text,
        read_files=read_files,
        target=target,
        working_dir=working_dir,
        model=args.model,
        max_tokens=args.max_tokens,
    )
    result = review_mod.run_review(request)
    print(result.text)
    review_mod.print_cost_summary(result)
    return 0


def _cmd_run(args, agents_root: Path) -> int:
    agent = AtomicAgent(
        name=args.agent,
        trigger=args.trigger,
        agents_root=agents_root,
    )
    response = agent.call(
        work_item=args.work_item,
        model_override=args.model,
        critical=args.critical,
        write_captures=not args.no_write_captures,
    )
    if response.skipped:
        print(f"[SKIPPED] {response.skip_reason}", file=sys.stderr)
        return 2
    print(response.text)
    print("", file=sys.stderr)
    print(
        f"--- Stats: model={response.model} "
        f"in={response.input_tokens} out={response.output_tokens} "
        f"cost=${response.cost_usd:.4f} "
        f"latency={response.latency_ms}ms "
        f"captures={len(response.captures)}",
        file=sys.stderr,
    )
    return 0


def _cmd_info(args, agents_root: Path) -> int:
    agent = AtomicAgent(
        name=args.agent,
        trigger="manual",
        agents_root=agents_root,
    )
    cfg = agent.config
    print(f"Agent: {agent.name}")
    print(f"Root:  {agent.agent_root}")
    print(f"Default model: {cfg.default_model}")
    print(f"Fallback:      {cfg.fallback_model}")
    print(f"Cost guardrails enabled: {cfg.cost_guardrails_enabled}")
    if cfg.cost_guardrails_enabled:
        print(f"  Daily cap:   ${cfg.daily_cap_usd}  → action: {cfg.daily_cap_action}")
        print(
            f"  Monthly cap: ${cfg.monthly_cap_usd} → action: {cfg.monthly_cap_action}"
        )
        print(f"  Warning thresholds: {cfg.warning_thresholds}")
    print(f"Read paths:       {len(cfg.read_paths)}")
    for p in cfg.read_paths:
        print(f"  • {p}")
    print(f"Write paths:      {len(cfg.write_paths)}")
    for p in cfg.write_paths:
        print(f"  • {p}")
    print(f"Read-only paths:  {len(cfg.read_only_paths)}")
    for p in cfg.read_only_paths:
        print(f"  • {p}")
    print(f"External APIs: {cfg.external_apis}")
    print(f"Hard NOs:      {len(cfg.hard_nos)} entries")
    return 0


def _cmd_skills(args, agents_root: Path) -> int:
    """List all skills for an agent — name, description, body lines, warnings."""
    agent_root = agents_root / args.agent
    if not agent_root.exists():
        print(f"Error: agent folder not found: {agent_root}", file=sys.stderr)
        return 1

    skills_dir = agent_root / "skills"
    if not skills_dir.is_dir():
        print(f"No skills/ directory found at {agent_root}")
        print("To add skills, create: <agent>/skills/<skill-name>/SKILL.md")
        return 0

    # Re-validate each skill dir to surface per-skill warnings for the operator
    found_any = False
    has_warnings = False
    for skill_subdir in sorted(skills_dir.iterdir()):
        if not skill_subdir.is_dir():
            continue
        skill_md = skill_subdir / "SKILL.md"
        if not skill_md.is_file():
            print(f"  [skip] {skill_subdir.name}/ — no SKILL.md")
            continue
        manifest, warnings = validate_skill_manifest(skill_subdir)
        found_any = True
        if manifest is None:
            print(
                f"  [ERROR] {skill_subdir.name}/: {warnings[0] if warnings else 'unknown error'}"
            )
            has_warnings = True
            continue
        status = "ok" if not warnings else "warn"
        desc_preview = (
            manifest.description[:80] + "..."
            if len(manifest.description) > 80
            else manifest.description
        )
        print(f"  [{status}] {manifest.name}")
        print(f"         description: {desc_preview}")
        print(
            f"         body lines:  {manifest.body_lines}"
            + (" (> 500 — consider splitting)" if manifest.body_lines > 500 else "")
        )
        if manifest.when_to_use:
            wtu_preview = (
                manifest.when_to_use[:60] + "..."
                if len(manifest.when_to_use) > 60
                else manifest.when_to_use
            )
            print(f"         when_to_use: {wtu_preview}")
        for w in warnings:
            print(f"         WARNING: {w}")
            has_warnings = True
        print()

    if not found_any:
        print(f"No skills with SKILL.md found under {skills_dir}")
        print("To add skills, create: <agent>/skills/<skill-name>/SKILL.md")

    return 1 if has_warnings else 0


def _cmd_version(args, agents_root: Path) -> int:
    """List all snapshot versions for a memory note, newest first."""
    agent_root = agents_root / args.agent
    memory_dir = agent_root / "memory"
    if not memory_dir.exists():
        print(f"No memory/ directory found at {memory_dir}", file=sys.stderr)
        return 1
    backend = FilesystemBackend(agent_root, "memory")
    version_refs = backend.list_versions(args.note_filename)
    if not version_refs:
        print(f"No versions found for {args.note_filename}")
        return 0
    for vref in version_refs:
        print(str(vref))
    return 0


def _cmd_restore(args, agents_root: Path) -> int:
    """Restore a memory note from a named snapshot."""
    agent_root = agents_root / args.agent
    memory_dir = agent_root / "memory"
    if not memory_dir.exists():
        print(f"No memory/ directory found at {memory_dir}", file=sys.stderr)
        return 1
    backend = FilesystemBackend(agent_root, "memory")
    try:
        vref = backend.resolve_version_token(args.note_filename, args.version_name)
    except VersionNotFound as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    # Build a permissive policy for CLI restore (whole agent root is writable)
    policy = WritePolicy(write_paths=[agent_root])
    ref = backend.restore_version(args.note_filename, vref, policy)
    print(f"Restored {args.note_filename} from {args.version_name}")
    print(f"  live note: {memory_dir / ref.name}")
    return 0


def _cmd_bundle(args, agents_root: Path) -> int:
    """Pre-render the cascade into a single file for skill-mode loads.

    See spec/26 and issue #231. The skill template's load instructions
    become "run `atomic-agents bundle --if-stale <agent>`, then Read the
    cache file" — turning 18+ sequential Reads into 1 cheap Bash + 1 Read.
    """
    from . import bundle as bundle_mod

    agent_root = agents_root / args.agent
    if not agent_root.exists():
        print(f"Error: agent folder not found: {agent_root}", file=sys.stderr)
        return 1

    cache_dir = (
        Path(args.cache_dir).expanduser().resolve()
        if args.cache_dir
        else bundle_mod.default_cache_dir()
    )

    if args.print_path:
        slug = bundle_mod.slug_for(agent_root, agents_root)
        print(cache_dir / f"{slug}.md")
        return 0

    extra_files = [Path(p) for p in args.extra_file]

    try:
        result = bundle_mod.render_bundle(
            agent_root,
            agents_root=agents_root,
            cache_dir=cache_dir,
            extra_files=extra_files,
            if_stale=args.if_stale,
        )
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if args.to_stdout:
        sys.stdout.write(result.path.read_text(encoding="utf-8"))
        return 0

    status = "regenerated" if result.regenerated else "fresh (skipped)"
    print(f"Bundle {status}: {result.path}")
    print(
        f"  {result.section_count if result.section_count >= 0 else '?'} sections, "
        f"{result.source_count} source files, "
        f"{result.total_bytes / 1024:.1f}KB",
        file=sys.stderr,
    )
    return 0


def _cmd_doctor(args) -> int:
    """Run preflight checks and report to stdout.

    Exit codes:
        0 — every check passed (skips ok)
        1 — one or more failed
        2 — doctor itself crashed (unexpected exception in our own code)
    """
    from . import doctor as doctor_module

    agents_root_override = (
        Path(args.agents_root).expanduser().resolve() if args.agents_root else None
    )
    try:
        results = doctor_module.run_doctor(
            agent_name=args.agent,
            agents_root=agents_root_override,
            skip_mcp=args.no_mcp,
        )
    except Exception as e:  # noqa: BLE001 — exit-2 catch-all by design
        print(f"doctor crashed: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    if args.json:
        sys.stdout.write(doctor_module.render_json(results))
    else:
        sys.stdout.write(doctor_module.render_human(results))

    return doctor_module.overall_exit_code(results)


def _cmd_persona(args) -> int:
    """Dispatch persona subcommands.

    All persona subcommands resolve the PersonaBackend via
    ``get_default_persona_backend(scope_root)`` where ``scope_root`` is cwd.
    The ``ATOMIC_AGENTS_PERSONA_BACKEND`` env var is already wired inside
    ``get_default_persona_backend``; no CLI-side handling is needed.

    Exit codes: 0 on success, 1 on any error (PersonaNotFound,
    PersonaExists, PersonaSnapshotNotFound, NotImplementedError, etc.).
    Errors go to stderr; normal output goes to stdout.
    """
    from .persona.backend import get_default_persona_backend
    from .exceptions import (
        PersonaExists,
        PersonaNotFound,
        PersonaSnapshotNotFound,
    )

    scope_root = Path.cwd()
    try:
        backend = get_default_persona_backend(scope_root)
    except Exception as e:  # noqa: BLE001
        print(f"Error: failed to resolve persona backend: {e}", file=sys.stderr)
        return 1

    persona_cmd = args.persona_cmd

    try:
        if persona_cmd == "list":
            return _persona_list(backend)
        elif persona_cmd == "show":
            return _persona_show(backend, args.persona_id)
        elif persona_cmd == "snapshot":
            return _persona_snapshot(backend, args.persona_id, args.label)
        elif persona_cmd == "list-snapshots":
            return _persona_list_snapshots(backend, args.persona_id)
        elif persona_cmd == "restore":
            return _persona_restore(backend, args.persona_id, args.snapshot_id)
        elif persona_cmd == "clone":
            return _persona_clone(backend, args.source_id, args.target_id)
    except PersonaNotFound as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except PersonaExists as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except PersonaSnapshotNotFound as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except NotImplementedError as e:
        print(f"Error: operation not supported by this backend: {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


def _persona_list(backend) -> int:
    """List all persona_ids known to the backend."""
    ids = backend.list_personas()
    if not ids:
        print("No personas found.")
        return 0
    for pid in ids:
        print(pid)
    return 0


def _persona_show(backend, persona_id: str) -> int:
    """Print IDENTITY, SOUL, and USER bodies plus metadata for a persona."""
    persona = backend.load_persona(persona_id)
    print(f"persona_id: {persona_id}")
    print(f"version:    {persona.version}")
    print(f"created_at: {persona.created_at}")
    if persona.label is not None:
        print(f"label:      {persona.label}")
    print()
    print("--- IDENTITY ---")
    print(persona.identity)
    print()
    print("--- SOUL ---")
    print(persona.soul)
    print()
    print("--- USER ---")
    print(persona.user)
    return 0


def _persona_snapshot(backend, persona_id: str, label: str | None) -> int:
    """Create a snapshot and print the snapshot_id."""
    snapshot_id = backend.snapshot(persona_id, label=label)
    print(snapshot_id)
    return 0


def _persona_list_snapshots(backend, persona_id: str) -> int:
    """List snapshots in chronological order (oldest first)."""
    snapshots = backend.list_snapshots(persona_id)
    if not snapshots:
        print(f"No snapshots found for persona {persona_id!r}.")
        return 0
    for snap in snapshots:
        label_part = f"  label={snap.label!r}" if snap.label is not None else ""
        print(f"{snap.snapshot_id}  {snap.created_at}{label_part}")
    return 0


def _persona_restore(backend, persona_id: str, snapshot_id: str) -> int:
    """Restore a persona to a previously captured snapshot."""
    backend.restore(persona_id, snapshot_id)
    print(f"Restored persona {persona_id!r} from snapshot {snapshot_id!r}.")
    return 0


def _persona_clone(backend, source_id: str, target_id: str) -> int:
    """Clone a persona record to a new persona_id."""
    backend.clone(source_id, target_id)
    print(f"Cloned persona {source_id!r} -> {target_id!r}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
