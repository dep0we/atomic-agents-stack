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
    atomic-agents corpus list --corpus wiki [--agent-root PATH]
    atomic-agents corpus show NAME --corpus wiki [--agent-root PATH]
    atomic-agents corpus query TEXT --corpus wiki [--top-k N] [--agent-root PATH]
    atomic-agents corpus version NAME --corpus wiki [--agent-root PATH]
    atomic-agents corpus restore NAME VERSION_ID --corpus wiki [--agent-root PATH]
    atomic-agents mcp-registry install <name> --command <cmd> [options]
    atomic-agents mcp-registry uninstall <name>
    atomic-agents init <name> [--from-template advisor] [--agents-root PATH]
    atomic-agents init --list-templates

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
    corpus  — Inspect and manage corpus pages (list, show, query, version, restore)
    init    : Scaffold a new agent in under 10 minutes (interactive wizard)
"""

from __future__ import annotations
import argparse
import os
import re
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

    # ── corpus subcommand group ───────────────────────────────────────────
    corpus_cmd = sub.add_parser(
        "corpus",
        help="Inspect and manage corpus pages (list, show, query, version, restore)",
    )
    corpus_sub = corpus_cmd.add_subparsers(dest="corpus_cmd", required=True)

    # corpus list --corpus wiki [--agent-root PATH]
    corpus_list = corpus_sub.add_parser(
        "list", help="List all pages in the given corpus"
    )
    corpus_list.add_argument(
        "--corpus", required=True, choices=["wiki", "raw"], help="corpus to query"
    )
    corpus_list.add_argument(
        "--agent-root",
        default=None,
        help="override ATOMIC_AGENTS_AGENT_ROOT (default: $ATOMIC_AGENTS_AGENT_ROOT or cwd)",
    )

    # corpus show NAME --corpus wiki [--agent-root PATH]
    corpus_show = corpus_sub.add_parser(
        "show", help="Read and display a single corpus page"
    )
    corpus_show.add_argument("name", help="page name stem (e.g. avalanche-vs-snowball)")
    corpus_show.add_argument(
        "--corpus", required=True, choices=["wiki", "raw"], help="corpus to query"
    )
    corpus_show.add_argument(
        "--agent-root",
        default=None,
        help="override ATOMIC_AGENTS_AGENT_ROOT (default: $ATOMIC_AGENTS_AGENT_ROOT or cwd)",
    )

    # corpus query TEXT --corpus wiki [--top-k N] [--agent-root PATH]
    corpus_query = corpus_sub.add_parser(
        "query", help="Search the corpus and list matching pages"
    )
    corpus_query.add_argument("text", help="search text")
    corpus_query.add_argument(
        "--corpus", required=True, choices=["wiki", "raw"], help="corpus to query"
    )
    corpus_query.add_argument(
        "--top-k", type=int, default=10, help="maximum number of results (default: 10)"
    )
    corpus_query.add_argument(
        "--agent-root",
        default=None,
        help="override ATOMIC_AGENTS_AGENT_ROOT (default: $ATOMIC_AGENTS_AGENT_ROOT or cwd)",
    )

    # corpus version NAME --corpus wiki [--agent-root PATH]
    corpus_version = corpus_sub.add_parser(
        "version", help="List all versions of a corpus page"
    )
    corpus_version.add_argument("name", help="page name stem")
    corpus_version.add_argument(
        "--corpus", required=True, choices=["wiki", "raw"], help="corpus to query"
    )
    corpus_version.add_argument(
        "--agent-root",
        default=None,
        help="override ATOMIC_AGENTS_AGENT_ROOT (default: $ATOMIC_AGENTS_AGENT_ROOT or cwd)",
    )

    # corpus restore NAME VERSION_ID --corpus wiki [--agent-root PATH]
    corpus_restore = corpus_sub.add_parser(
        "restore", help="Restore a corpus page to a specific version"
    )
    corpus_restore.add_argument("name", help="page name stem")
    corpus_restore.add_argument("version_id", help="version identifier (backend_id)")
    corpus_restore.add_argument(
        "--corpus", required=True, choices=["wiki", "raw"], help="corpus to query"
    )
    corpus_restore.add_argument(
        "--agent-root",
        default=None,
        help="override ATOMIC_AGENTS_AGENT_ROOT (default: $ATOMIC_AGENTS_AGENT_ROOT or cwd)",
    )

    # ── mcp-registry subcommand group ────────────────────────────────────
    mcp_registry_cmd = sub.add_parser(
        "mcp-registry",
        help="Inspect and manage MCP servers (list, show, validate, install, uninstall, refresh-capabilities)",
        description=(
            "Inspect and manage the MCP server registry for an agent. "
            "Uses the filesystem backend by default (reads <agent-root>/mcp.md). "
            "Override with ATOMIC_AGENTS_MCP_SERVER_REGISTRY_BACKEND env var. "
            "Read-only subcommands: list, show, validate, refresh-capabilities. "
            "Write subcommands (PR 3+): install, uninstall."
        ),
    )
    mcp_registry_sub = mcp_registry_cmd.add_subparsers(
        dest="mcp_registry_cmd", required=True
    )

    # mcp-registry list [--agent-root PATH]
    mcp_registry_list = mcp_registry_sub.add_parser(
        "list",
        help="List all mounted MCP servers (name, description, transport)",
    )
    mcp_registry_list.add_argument(
        "--agent-root",
        default=None,
        help="override ATOMIC_AGENTS_AGENT_ROOT (default: $ATOMIC_AGENTS_AGENT_ROOT or cwd)",
    )

    # mcp-registry show <name> [--agent-root PATH]
    mcp_registry_show = mcp_registry_sub.add_parser(
        "show",
        help="Show full spec for a named MCP server",
    )
    mcp_registry_show.add_argument("name", help="MCP server name")
    mcp_registry_show.add_argument(
        "--agent-root",
        default=None,
        help="override ATOMIC_AGENTS_AGENT_ROOT (default: $ATOMIC_AGENTS_AGENT_ROOT or cwd)",
    )

    # mcp-registry validate <name> [--agent-root PATH]
    mcp_registry_validate = mcp_registry_sub.add_parser(
        "validate",
        help="Static validation of a named MCP server descriptor",
    )
    mcp_registry_validate.add_argument("name", help="MCP server name")
    mcp_registry_validate.add_argument(
        "--agent-root",
        default=None,
        help="override ATOMIC_AGENTS_AGENT_ROOT (default: $ATOMIC_AGENTS_AGENT_ROOT or cwd)",
    )

    # mcp-registry refresh-capabilities [--agent-root PATH]
    mcp_registry_refresh = mcp_registry_sub.add_parser(
        "refresh-capabilities",
        help=(
            "Print current backend capabilities (filesystem backend has no remote "
            "dependency; HTTP backend at PR 4 re-probes the catalog server)"
        ),
    )
    mcp_registry_refresh.add_argument(
        "--agent-root",
        default=None,
        help="override ATOMIC_AGENTS_AGENT_ROOT (default: $ATOMIC_AGENTS_AGENT_ROOT or cwd)",
    )

    # mcp-registry install <name> --command <cmd> [options]
    mcp_registry_install = mcp_registry_sub.add_parser(
        "install",
        help="Install a new MCP server into the registry (write path; PR 3+)",
    )
    mcp_registry_install.add_argument(
        "name", help="MCP server name (charset: [a-zA-Z0-9_.+@-]+)"
    )
    mcp_registry_install.add_argument(
        "--command",
        required=True,
        help="executable for the MCP server (e.g., npx, docker, uv)",
    )
    mcp_registry_install.add_argument(
        "--args",
        default="",
        help=(
            "comma-separated args for the executable. Example: "
            "--args -y,@modelcontextprotocol/server-github. Default: none."
        ),
    )
    mcp_registry_install.add_argument(
        "--env",
        default="",
        help=(
            "comma-separated KEY=$VAR pairs. Use env-var references "
            "(KEY=$VAR_NAME), NOT literal values, to avoid storing secrets in "
            "mcp.md plaintext. Example: GITHUB_PAT=$GITHUB_PAT,LOG_LEVEL=$LOG_LEVEL. "
            "Default: none."
        ),
    )
    mcp_registry_install.add_argument(
        "--description",
        default="",
        help="operator-readable description (single line). Default: empty.",
    )
    mcp_registry_install.add_argument(
        "--transport",
        default="stdio",
        choices=["stdio"],
        help="MCP server transport protocol (only 'stdio' is supported in v1.0; default: stdio)",
    )
    mcp_registry_install.add_argument(
        "--agent-root",
        default=None,
        help="override ATOMIC_AGENTS_AGENT_ROOT (default: $ATOMIC_AGENTS_AGENT_ROOT or cwd)",
    )

    # mcp-registry uninstall <name> [--agent-root PATH]
    mcp_registry_uninstall = mcp_registry_sub.add_parser(
        "uninstall",
        help="Remove an MCP server from the registry (write path; PR 3+). Idempotent.",
    )
    mcp_registry_uninstall.add_argument("name", help="MCP server name to remove")
    mcp_registry_uninstall.add_argument(
        "--agent-root",
        default=None,
        help="override ATOMIC_AGENTS_AGENT_ROOT (default: $ATOMIC_AGENTS_AGENT_ROOT or cwd)",
    )

    # ── init subcommand ───────────────────────────────────────────────────
    init_cmd = sub.add_parser(
        "init",
        help="Scaffold a new agent in under 10 minutes",
        description=(
            "Walk through ~7 questions and produce a working home-user agent. "
            "Use --from-template to skip the interview, or --list-templates "
            "to enumerate available starter templates."
        ),
    )
    init_cmd.add_argument(
        "agent_name",
        nargs="?",
        default=None,
        help="agent name (folder under agents-root); omit when using --list-templates",
    )
    init_cmd.add_argument(
        "--from-template",
        dest="from_template",
        default=None,
        choices=["advisor", "researcher", "writer"],
        help="skip Q&A; scaffold from a starter template (advisor, researcher, or writer)",
    )
    init_cmd.add_argument(
        "--list-templates",
        dest="list_templates",
        action="store_true",
        help="list available starter templates and exit",
    )
    init_cmd.add_argument(
        "--agents-root",
        dest="agents_root",
        default=None,
        help="override ATOMIC_AGENTS_ROOT",
    )

    # ── serve subcommand ──────────────────────────────────────────────────
    serve_cmd = sub.add_parser(
        "serve",
        help="Start the HTTP serve wrapper (requires atomic-agents-stack[serve])",
        description=(
            "Expose agent.call() over HTTP for Cloud Run, GKE, Fly.io, Render, "
            "or any containerized environment. The framework owns the agent loop; "
            "the operator's infrastructure owns auth, rate limiting, and TLS. "
            "See docs/spec/37-serve.md and docs/deployment/ for deployment guides."
        ),
    )
    serve_cmd.add_argument(
        "agent",
        nargs="?",
        default=None,
        help=(
            "agent name to serve (serves a single agent). "
            "Omit with --all to serve all agents in the vault."
        ),
    )
    serve_cmd.add_argument(
        "--all",
        dest="serve_all",
        action="store_true",
        help="serve all agents found in agents-root (mutually exclusive with <agent>)",
    )
    serve_cmd.add_argument(
        "--host",
        default=None,
        help=(
            "bind address (default: 127.0.0.1; override from "
            "ATOMIC_AGENTS_SERVE_HOST env var or serve.md). "
            "Use 0.0.0.0 for Cloud Run (requires --allow-no-auth or serve.md "
            "'## Allow No Auth' when your perimeter is in place)."
        ),
    )
    serve_cmd.add_argument(
        "--port",
        type=int,
        default=None,
        help=(
            "bind port (default: 8000; override from "
            "ATOMIC_AGENTS_SERVE_PORT env var or serve.md)"
        ),
    )
    serve_cmd.add_argument(
        "--allow-no-auth",
        dest="allow_no_auth",
        action="store_true",
        help=(
            "Explicitly allow serving without a perimeter auth layer. "
            "Required when binding to a non-loopback address. "
            "Only use after your perimeter (IAP, ALB, Cloudflare Access, "
            "Tailscale Serve, etc.) is in place. "
            'See docs/spec/37-serve.md §"No-auth default".'
        ),
    )
    serve_cmd.add_argument(
        "--agents-root",
        dest="agents_root",
        default=None,
        help="override ATOMIC_AGENTS_ROOT",
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

    # `corpus` resolves its own agent_root from --agent-root or env var / cwd.
    # It does not use the agents-root / agent-name hierarchy.
    if args.cmd == "corpus":
        return _cmd_corpus(args)

    # `mcp-registry` resolves its own agent_root from --agent-root or env var / cwd.
    if args.cmd == "mcp-registry":
        return _cmd_mcp_registry(args)

    # `init` resolves its own agents_root from --agents-root or env var.
    # It does not use the agents-root / agent-name hierarchy.
    if args.cmd == "init":
        return _cmd_init(args)

    # `serve` resolves its own agents_root from --agents-root or env var.
    # Lazy-imports the serve module so Starlette/uvicorn are not imported
    # on every CLI invocation (spec/37 MUST 1 — progressive disclosure).
    if args.cmd == "serve":
        return _cmd_serve(args)

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
        PersonaError,
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
    except PersonaError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except (OSError, PermissionError) as e:
        print(f"Error: {e}", file=sys.stderr)
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


def _resolve_corpus_agent_root(args) -> Path:
    """Resolve the agent_root for corpus subcommands.

    Resolution order (mirrors persona subcommand's scope_root pattern):
    1. ``--agent-root`` CLI flag (explicit wins).
    2. ``ATOMIC_AGENTS_AGENT_ROOT`` env var.
    3. Current working directory (``Path.cwd()``).
    """
    if args.agent_root is not None:
        return Path(args.agent_root).expanduser().resolve()
    env_val = os.environ.get("ATOMIC_AGENTS_AGENT_ROOT")
    if env_val:
        return Path(env_val).expanduser().resolve()
    return Path.cwd()


def _cmd_corpus(args) -> int:
    """Dispatch corpus subcommands.

    Instantiates the operator-pinned backend via
    ``get_default_corpus_backend(agent_root)``, which reads
    ``ATOMIC_AGENTS_CORPUS_BACKEND`` (default ``"filesystem"``) so the CLI
    surface honours the same env-var override as the runtime.

    Exit codes: 0 on success, 1 on any error (CorpusError, OSError,
    PermissionError, etc.). Errors go to stderr; normal output to stdout.
    Zero LLM calls -- pure local I/O.
    """
    from .corpus import get_default_corpus_backend
    from .exceptions import CorpusError

    agent_root = _resolve_corpus_agent_root(args)
    backend = get_default_corpus_backend(agent_root)
    corpus_cmd = args.corpus_cmd

    try:
        if corpus_cmd == "list":
            return _corpus_list(backend, args.corpus)
        elif corpus_cmd == "show":
            return _corpus_show(backend, args.name, args.corpus)
        elif corpus_cmd == "query":
            return _corpus_query(backend, args.text, args.corpus, args.top_k)
        elif corpus_cmd == "version":
            return _corpus_version(backend, args.name, args.corpus)
        elif corpus_cmd == "restore":
            return _corpus_restore(
                backend, args.name, args.version_id, args.corpus, agent_root
            )
    except CorpusError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except (OSError, PermissionError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


def _corpus_list(backend, corpus: str) -> int:
    """List all pages in the given corpus, one per line."""
    refs = backend.list_pages(corpus)
    if not refs:
        print(f"No pages in {corpus}.")
        return 0
    for ref in refs:
        ts = ref.last_modified.strftime("%Y-%m-%dT%H:%M:%SZ")
        print(f"{ref.name}  ({ref.byte_size} bytes, {ts})")
    return 0


def _corpus_show(backend, name: str, corpus: str) -> int:
    """Read and display a single corpus page (frontmatter header + body)."""
    page = backend.read_page(name, corpus)
    if page is None:
        print(f"Page not found: {name}", file=sys.stderr)
        return 1
    ref = page.ref
    # Print a frontmatter header block above the body
    print(f"--- {name} ({corpus}) ---")
    print(f"title:         {ref.title}")
    if page.description is not None:
        print(f"description:   {page.description}")
    if page.type is not None:
        print(f"type:          {page.type}")
    if page.captured is not None:
        print(f"captured:      {page.captured}")
    if page.tags:
        print(f"tags:          {', '.join(str(t) for t in page.tags)}")
    if page.provenance is not None:
        print(f"provenance:    {page.provenance}")
    if page.confidence is not None:
        print(f"confidence:    {page.confidence}")
    if page.pinned:
        print("pinned:        true")
    ts = ref.last_modified.strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"last_modified: {ts}")
    print(f"byte_size:     {ref.byte_size}")
    print()
    print(page.body)
    return 0


def _corpus_query(backend, text: str, corpus: str, top_k: int) -> int:
    """Run a query against the corpus and print matching pages."""
    refs = backend.query(text, corpus, top_k=top_k)
    if not refs:
        print(f"No matches for {text!r}")
        return 0
    for ref in refs:
        print(f"{ref.name}  ({ref.byte_size} bytes)")
    return 0


def _corpus_version(backend, name: str, corpus: str) -> int:
    """List all versions of a corpus page, newest first."""
    version_refs = backend.list_versions(name, corpus)
    if not version_refs:
        print(f"No versions for {name!r}")
        return 0
    for vref in version_refs:
        # backend_id format: "<corpus>/<stem>/<YYYYMMDDTHHMMSSffffffZ>_<8hex>.md"
        # Parse a human-readable timestamp from the version filename portion
        parts = vref.backend_id.split("/")
        version_filename = parts[-1] if parts else vref.backend_id
        # Strip .md suffix for display; timestamp is the prefix before "_"
        version_stem = (
            version_filename[:-3]
            if version_filename.endswith(".md")
            else version_filename
        )
        timestamp_part = (
            version_stem.split("_")[0] if "_" in version_stem else version_stem
        )
        print(f"{vref.backend_id}  ({timestamp_part})")
    return 0


def _corpus_restore(
    backend,
    name: str,
    version_id: str,
    corpus: str,
    agent_root: Path,
) -> int:
    """Restore a corpus page to a specific version."""
    from .memory.backend import VersionRef

    policy = WritePolicy(write_paths=[agent_root])
    version_ref = VersionRef(backend_id=version_id)
    backend.restore_version(name, corpus, version_ref, policy)
    print(f"Restored {name!r} to version {version_id!r}")
    return 0


def _cmd_init(args) -> int:
    """Dispatch the `atomic-agents init` subcommand.

    Lazy-imports the init wizard module so rich and other wizard-only deps
    do not load for non-init invocations. Matches the lazy-import pattern at
    cli.py:703 (_cmd_doctor) and cli.py:738 (_cmd_persona).
    """
    from .init import run_init

    return run_init(args)


def _resolve_mcp_registry_agent_root(args) -> Path:
    """Resolve agent_root for mcp-registry subcommands.

    Resolution order mirrors the corpus subcommand pattern:
    1. ``--agent-root`` CLI flag (explicit wins).
    2. ``ATOMIC_AGENTS_AGENT_ROOT`` env var.
    3. Current working directory (``Path.cwd()``).
    """
    if args.agent_root is not None:
        return Path(args.agent_root).expanduser().resolve()
    env_val = os.environ.get("ATOMIC_AGENTS_AGENT_ROOT")
    if env_val:
        return Path(env_val).expanduser().resolve()
    return Path.cwd()


def _cmd_mcp_registry(args) -> int:
    """Dispatch mcp-registry subcommands.

    Instantiates the operator-pinned backend via
    ``get_default_mcp_server_registry_backend(agent_root, read_paths)``,
    which reads ``ATOMIC_AGENTS_MCP_SERVER_REGISTRY_BACKEND`` (default
    ``"filesystem"``) so the CLI surface honours the same env-var override
    as the runtime.

    Exit codes: 0 on success, 1 on any error. Errors go to stderr;
    normal output to stdout. Zero LLM calls -- pure local I/O.
    """
    from .mcp_registry import (
        MCPRegistryAuthRequired,
        MCPRegistryDescriptorInvalid,
        MCPRegistryError,
        MCPRegistryUnavailable,
        MCPServerAlreadyInstalled,
        MCPServerNotInRegistry,
        get_default_mcp_server_registry_backend,
    )
    from .exceptions import MCPServerConnectFailed

    agent_root = _resolve_mcp_registry_agent_root(args)

    # Empty read_paths means the CLI skips path-traversal validation. This is
    # consistent with the inspection use case (mcp-registry show/validate/list);
    # install/uninstall write directly to mcp.md without needing read_paths.
    try:
        backend = get_default_mcp_server_registry_backend(agent_root, [])
    except Exception as e:
        print(f"Error: failed to resolve MCP registry backend: {e}", file=sys.stderr)
        return 1

    sub_cmd = args.mcp_registry_cmd

    try:
        if sub_cmd == "list":
            return _mcp_registry_list(backend)
        elif sub_cmd == "show":
            return _mcp_registry_show(backend, args.name)
        elif sub_cmd == "validate":
            return _mcp_registry_validate(backend, args.name)
        elif sub_cmd == "refresh-capabilities":
            return _mcp_registry_refresh_capabilities(backend)
        elif sub_cmd == "install":
            return _mcp_registry_install(
                backend,
                args.name,
                args.command,
                args.args,
                args.env,
                args.description,
                args.transport,
            )
        elif sub_cmd == "uninstall":
            return _mcp_registry_uninstall(backend, args.name)
    except MCPServerConnectFailed as e:
        print(f"Error: env var not resolved: {e}", file=sys.stderr)
        return 1
    except MCPServerAlreadyInstalled as e:
        print(f"Error: MCP server already installed: {e}", file=sys.stderr)
        return 1
    except MCPRegistryDescriptorInvalid as e:
        print(f"Error: MCP server descriptor is invalid: {e}", file=sys.stderr)
        return 1
    except MCPRegistryAuthRequired as e:
        print(
            f"Error: authentication required for MCP registry backend: {e}",
            file=sys.stderr,
        )
        return 1
    except MCPRegistryUnavailable as e:
        print(
            f"Error: MCP registry backend temporarily unavailable: {e}",
            file=sys.stderr,
        )
        return 1
    except MCPServerNotInRegistry as e:
        print(f"Error: MCP server not found: {e}", file=sys.stderr)
        return 1
    except MCPRegistryError as e:
        print(f"Error: MCP registry error: {e}", file=sys.stderr)
        return 1
    except NotImplementedError as e:
        print(
            f"Error: operation not supported by this backend: {e}",
            file=sys.stderr,
        )
        return 1
    except ValueError as e:
        print(f"Error: invalid server name: {e}", file=sys.stderr)
        return 1
    except (OSError, PermissionError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


def _mcp_registry_list(backend) -> int:
    """List all mounted MCP servers as a table: name / description / transport."""
    refs = backend.list_mcp_servers()
    if not refs:
        print("No MCP servers registered.")
        return 0
    name_w = max(len(r.name) for r in refs)
    transport_w = max(len(r.transport) for r in refs)
    header = f"{'NAME':<{name_w}}  {'TRANSPORT':<{transport_w}}  DESCRIPTION"
    print(header)
    print("-" * len(header))
    for ref in refs:
        desc = ref.description or ""
        print(f"{ref.name:<{name_w}}  {ref.transport:<{transport_w}}  {desc}")
    return 0


def _mcp_registry_show(backend, name: str) -> int:
    """Show the full MCPServerSpec for a named server as a formatted table.

    We print the unresolved $VAR refs from mcp.md, NOT the resolved values
    from load_mcp_server, to avoid leaking secrets to stdout.
    """
    # Call load_mcp_server first for validation: name charset check, path-traversal
    # check, and confirmation that the server exists in the catalog. We discard the
    # resolved spec's env (which has real secret values) and re-read raw mcp.md.
    spec = backend.load_mcp_server(name)

    # Re-parse mcp.md with resolve_env=False to get unresolved $VAR refs.
    # This is the safe display path: operators see the wiring ($VAR names)
    # without the resolved values being emitted to stdout.
    unresolved_env: dict[str, str] | None = None
    mcp_md = backend._agent_root / "mcp.md"
    try:
        from .mcp import parse_mcp_md_text

        content = mcp_md.read_text(encoding="utf-8")
        raw_specs = parse_mcp_md_text(content, resolve_env=False, read_paths=None)
        for raw_spec in raw_specs:
            if raw_spec.name == name:
                unresolved_env = raw_spec.env
                break
    except Exception:
        # If re-reading mcp.md fails (file gone between load and re-read),
        # do NOT fall back to printing resolved values. Warn on stderr instead.
        if spec.env:
            print(
                f"<env hidden: {len(spec.env)} entries; re-read of mcp.md failed>",
                file=sys.stderr,
            )

    print(f"name:        {spec.name}")
    print(f"command:     {spec.command}")
    if spec.args:
        print(f"args:        {' '.join(spec.args)}")
    display_env = unresolved_env if unresolved_env is not None else {}
    if display_env:
        print("env:")
        for k, v in display_env.items():
            print(f"  {k}: {v}")
    print(f"transport:   {spec.transport}")
    if spec.description:
        print(f"description: {spec.description}")
    return 0


def _mcp_registry_validate(backend, name: str) -> int:
    """Run a static validation check on a named server descriptor."""
    result = backend.validate(name)
    status = "OK" if result.ok else "FAIL"
    print(f"validate {name!r}: {status}")
    for err in result.errors:
        print(f"  ERROR:   {err}")
    for warn in result.warnings:
        print(f"  WARNING: {warn}")
    return 0 if result.ok else 1


def _mcp_registry_refresh_capabilities(backend) -> int:
    """Re-probe backend capabilities and print the current view."""
    caps = backend.refresh_capabilities()
    print(f"backend_id:                    {backend.backend_id}")
    print(f"supports_install:              {caps.supports_install}")
    print(f"supports_uninstall:            {caps.supports_uninstall}")
    print(f"supports_capability_handshake: {caps.supports_capability_handshake}")
    print(f"supports_audit:                {caps.supports_audit}")
    print(f"durable:                       {caps.durable}")
    return 0


def _parse_env_flag(raw: str) -> dict[str, str]:
    """Parse comma-separated K=V pairs from --env flag.

    Rules per spec/36 + Stream D finding D-F2:
    - Empty input -> {}.
    - Split on ',' for entries; split on FIRST '=' for each entry (values
      may contain '=').
    - Empty key raises ValueError with a clear message.
    - Empty value is valid (results in empty string).
    - An entry with no '=' raises ValueError.

    Note: this parser does NOT support comma-containing values. Operators
    needing such values must edit mcp.md directly or split into multiple
    registrations.
    """
    if not raw:
        return {}
    result: dict[str, str] = {}
    for pair in raw.split(","):
        if "=" not in pair:
            raise ValueError(
                f"--env entry {pair!r} is missing '='. "
                f"Expected format: KEY=$VAR_NAME or KEY=value"
            )
        key, val = pair.split("=", 1)  # split on FIRST '=' only
        if not key:
            raise ValueError(
                f"--env entry {pair!r} has an empty key. Expected format: KEY=$VAR_NAME"
            )
        result[key] = val
    return result


def _parse_args_flag(raw: str) -> list[str]:
    """Parse comma-separated args from --args flag.

    Rules per Stream D finding D-F3:
    - Empty input -> [].
    - Each entry is stripped of leading/trailing whitespace.
    - Empty entries after stripping are silently dropped.
    """
    if not raw:
        return []
    return [a.strip() for a in raw.split(",") if a.strip()]


def _mcp_registry_install(
    backend,
    name: str,
    command: str,
    args_raw: str,
    env_raw: str,
    description: str,
    transport: str,
) -> int:
    """Install a new MCP server. Per Stream D findings D-F1/D-F4/D-F5/D-F9."""
    from .mcp import MCPServerSpec

    # Parse args / env at CLI boundary (raises ValueError on bad shape; caught above).
    args_list = _parse_args_flag(args_raw)
    env_dict = _parse_env_flag(env_raw)

    # Defense-in-depth: refuse newlines in --command, --args, --env at the CLI
    # boundary so operators see a clean, named-flag error before the renderer's
    # ValueError fires. Newlines in these fields would create phantom H2 sections
    # in mcp.md that bypass collision detection and name validation.
    if "\n" in command:
        print(
            "Error: --command must not contain newline characters.",
            file=sys.stderr,
        )
        return 1
    for i, arg in enumerate(args_list):
        if "\n" in arg:
            print(
                f"Error: --args item at index {i} must not contain newline characters.",
                file=sys.stderr,
            )
            return 1
    for env_key, env_val in env_dict.items():
        if "\n" in env_key or "\n" in env_val:
            print(
                f"Error: --env {env_key!r} key or value must not contain newline "
                f"characters.",
                file=sys.stderr,
            )
            return 1

    # Stream D finding D-F9 + Stream B finding B-10 + Security finding:
    # refuse description with H2-looking content (catches both '## ' and '##\t'
    # to match the renderer's re.match(r'^##\s', line) guard).
    if description and any(
        re.match(r"^##\s", line) for line in description.splitlines()
    ):
        print(
            "Error: --description must not contain lines starting with '## ' "
            "(H2 headers delimit server sections in mcp.md).",
            file=sys.stderr,
        )
        return 1

    # Stream D finding D-F1 (decision 3 = WARN + proceed, v1.0 contract):
    # warn on env values that do not look like $VAR references. Literal values
    # land on disk plaintext.
    for env_key, env_val in env_dict.items():
        if not env_val.startswith("$"):
            print(
                f"Warning: --env {env_key}=<value> does not look like an env-var "
                f"reference (expected $VAR_NAME). Literal values are stored "
                f"unredacted in mcp.md. Use --env {env_key}=$SOME_VAR_NAME to "
                f"store the reference instead.",
                file=sys.stderr,
            )

    spec = MCPServerSpec(
        name=name,
        command=command,
        args=args_list,
        env=env_dict,
        transport=transport,
        description=description,
    )

    # Install. backend.install may raise (caught by outer handler).
    ref = backend.install(spec)

    # Stream D finding D-F4: success output prints ONLY the Ref.name.
    # NEVER echo env / command / args / spec repr / load_mcp_server result.
    print(f"Installed MCP server {ref.name!r}.")
    return 0


def _mcp_registry_uninstall(backend, name: str) -> int:
    """Uninstall an MCP server. Idempotent. Per Stream D finding D-F10."""
    backend.uninstall(name)
    # Idempotent: print neutral message that is valid for both present-removed
    # and absent-no-op paths.
    print(f"Uninstalled MCP server {name!r} (or was not present).")
    return 0


def _cmd_serve(args) -> int:
    """Dispatch the `atomic-agents serve` subcommand.

    Lazy-imports the serve module so Starlette + uvicorn are not imported for
    any other CLI invocation. Matches the lazy-import pattern from _cmd_init.
    spec/37 MUST 1 (Starlette is opt-in; base import never loads it).
    """
    from .serve import run_serve  # noqa: PLC0415 -- intentional lazy import

    return run_serve(args)


if __name__ == "__main__":
    sys.exit(main())
