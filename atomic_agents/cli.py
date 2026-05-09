"""CLI for atomic_agents — the `atomic-agents` console script.

Usage:
    atomic-agents run <agent> [options]
    atomic-agents info <agent> [options]
    atomic-agents skills <agent> [options]
    atomic-agents version <agent> <note-filename>
    atomic-agents restore <agent> <note-filename> <version-name>
    atomic-agents doctor [--agent <name>] [--json] [--no-mcp]

Subcommands:
    run     — Run an agent against a work item
    info    — Show config for an agent without running it
    skills  — List all skills for an agent with metadata and warnings
    version — List memory note version snapshots
    restore — Restore a memory note from a snapshot
    doctor  — Preflight checks before scheduling an agent run
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
from .skills import discover_skills, validate_skill_manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="atomic-agents", description="Atomic Agents CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="Run an agent against a work item")
    run.add_argument("agent", help="agent name (folder under agents-root)")
    run.add_argument("--work-item", required=True, help="user message / work item text")
    run.add_argument("--trigger", default="manual", choices=["cron", "skill", "manual", "api"])
    run.add_argument("--model", default=None, help="override default model")
    run.add_argument("--critical", action="store_true", help="bypass cost guardrails")
    run.add_argument("--no-write-captures", action="store_true",
                      help="extract captures but don't persist (dry-run)")
    run.add_argument("--agents-root", default=None,
                      help="override ATOMIC_AGENTS_ROOT")

    info = sub.add_parser("info", help="Show config for an agent without running it")
    info.add_argument("agent")
    info.add_argument("--agents-root", default=None)

    skills_cmd = sub.add_parser(
        "skills",
        help="List all skills for an agent (name, description, body line count, warnings)",
    )
    skills_cmd.add_argument("agent", help="agent name (folder under agents-root)")
    skills_cmd.add_argument("--agents-root", default=None,
                             help="override ATOMIC_AGENTS_ROOT")

    version_cmd = sub.add_parser("version", help="List versions for a memory note")
    version_cmd.add_argument("agent", help="agent name (folder under agents-root)")
    version_cmd.add_argument("note_filename", help="bare filename, e.g. feedback_comm_style.md")
    version_cmd.add_argument("--agents-root", default=None)

    restore_cmd = sub.add_parser("restore", help="Restore a memory note from a snapshot")
    restore_cmd.add_argument("agent", help="agent name (folder under agents-root)")
    restore_cmd.add_argument("note_filename", help="bare filename, e.g. feedback_comm_style.md")
    restore_cmd.add_argument("version_name", help="version filename to restore from")
    restore_cmd.add_argument("--agents-root", default=None)

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
        "--agents-root", default=None,
        help="override ATOMIC_AGENTS_ROOT for this run",
    )
    doctor_cmd.add_argument(
        "--json", action="store_true",
        help="emit machine-readable JSON instead of the human report",
    )
    doctor_cmd.add_argument(
        "--no-mcp", action="store_true",
        help="skip MCP server handshake (faster; useful when servers are remote)",
    )

    args = parser.parse_args(argv)

    agents_root = Path(args.agents_root).expanduser().resolve() if args.agents_root else get_agents_root()

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
    except AtomicAgentsError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
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
    print(f"--- Stats: model={response.model} "
          f"in={response.input_tokens} out={response.output_tokens} "
          f"cost=${response.cost_usd:.4f} "
          f"latency={response.latency_ms}ms "
          f"captures={len(response.captures)}", file=sys.stderr)
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
        print(f"  Monthly cap: ${cfg.monthly_cap_usd} → action: {cfg.monthly_cap_action}")
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
            print(f"  [ERROR] {skill_subdir.name}/: {warnings[0] if warnings else 'unknown error'}")
            has_warnings = True
            continue
        status = "ok" if not warnings else "warn"
        desc_preview = manifest.description[:80] + "..." if len(manifest.description) > 80 else manifest.description
        print(f"  [{status}] {manifest.name}")
        print(f"         description: {desc_preview}")
        print(f"         body lines:  {manifest.body_lines}"
              + (" (> 500 — consider splitting)" if manifest.body_lines > 500 else ""))
        if manifest.when_to_use:
            wtu_preview = manifest.when_to_use[:60] + "..." if len(manifest.when_to_use) > 60 else manifest.when_to_use
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


if __name__ == "__main__":
    sys.exit(main())
