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
from typing import Callable

from .agent import AtomicAgent
from ._cli_registry import CliCommand, discover_commands
from ._platform import get_agents_root
from .memory import get_default_memory_backend
from .memory.backend import WritePolicy
from .exceptions import AtomicAgentsError, VersionNotFound
from .skills import validate_skill_manifest

# Persona exceptions -- imported lazily inside handlers to avoid
# slowing down the hot path for agents that don't use persona subcommands.
# PersonaNotFound, PersonaExists, PersonaSnapshotNotFound re-exported
# from atomic_agents.persona so CLI handlers can catch them by name.


def _register_run(sub: argparse._SubParsersAction) -> None:
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


def _register_info(sub: argparse._SubParsersAction) -> None:
    info = sub.add_parser("info", help="Show config for an agent without running it")
    info.add_argument("agent")
    info.add_argument("--agents-root", default=None)


def _register_skills(sub: argparse._SubParsersAction) -> None:
    skills_cmd = sub.add_parser(
        "skills",
        help="List all skills for an agent (name, description, body line count, warnings)",
    )
    skills_cmd.add_argument("agent", help="agent name (folder under agents-root)")
    skills_cmd.add_argument(
        "--agents-root", default=None, help="override ATOMIC_AGENTS_ROOT"
    )


def _register_version(sub: argparse._SubParsersAction) -> None:
    version_cmd = sub.add_parser("version", help="List versions for a memory note")
    version_cmd.add_argument("agent", help="agent name (folder under agents-root)")
    version_cmd.add_argument(
        "note_filename", help="bare filename, e.g. feedback_comm_style.md"
    )
    version_cmd.add_argument("--agents-root", default=None)


def _register_restore(sub: argparse._SubParsersAction) -> None:
    restore_cmd = sub.add_parser(
        "restore", help="Restore a memory note from a snapshot"
    )
    restore_cmd.add_argument("agent", help="agent name (folder under agents-root)")
    restore_cmd.add_argument(
        "note_filename", help="bare filename, e.g. feedback_comm_style.md"
    )
    restore_cmd.add_argument("version_name", help="version filename to restore from")
    restore_cmd.add_argument("--agents-root", default=None)


def _register_bundle(sub: argparse._SubParsersAction) -> None:
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
    bundle_cmd.add_argument(
        "--validate",
        action="store_true",
        help=(
            "verify the bundle faithfully contains every cascade body the "
            "runtime assembles into the system prompt (content parity, not byte "
            "equality). Renders a FRESH bundle in-memory, builds the runtime "
            "system prompt via AtomicAgent.load(), and reports drift. Exit 0 on "
            "parity (modulo known divergences: the runtime '# Available skills' "
            "section is omitted per issue #593; model.md is bundle-only); exit 1 "
            "and lists missing content on unexpected drift. spec/26."
        ),
    )


def _register_doctor(sub: argparse._SubParsersAction) -> None:
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


def _register_review(sub: argparse._SubParsersAction) -> None:
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


def _register_persona(sub: argparse._SubParsersAction) -> None:
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


def _register_corpus(sub: argparse._SubParsersAction) -> None:
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
    corpus_query.add_argument(
        "--critical",
        action="store_true",
        help=(
            "bypass the embed cost gate (headroom check AND fail-closed-on-degraded "
            "refusal) for this query; still emits reservation/release/cost audit records"
        ),
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


def _register_mcp_registry(sub: argparse._SubParsersAction) -> None:
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


def _register_secrets(sub: argparse._SubParsersAction) -> None:
    # ── secrets subcommand group ─────────────────────────────────────────
    # Secrets are flat per-deployment (NOT per-agent) so no --agent-root arg.
    # Observability only: check presence, show source, validate. Never prints
    # secret values (spec/38 secrecy MUSTs 4-6).
    secrets_cmd = sub.add_parser(
        "secrets",
        help="Inspect credential resolution (check presence, show source, validate)",
        description=(
            "Read-only observability for credential resolution. "
            "Shows where a key resolves from (env var, Keychain, keys.json) "
            "without printing the value. "
            "Uses the registered SecretBackend (default: filesystem). "
            "Override with ATOMIC_AGENTS_SECRET_BACKEND env var."
        ),
    )
    secrets_sub = secrets_cmd.add_subparsers(dest="secrets_cmd", required=True)

    # secrets check <KEY>
    secrets_check = secrets_sub.add_parser(
        "check",
        help="Check whether a key is present (exit 0) or absent (exit 1)",
    )
    secrets_check.add_argument(
        "key",
        help="key name to check (e.g., ANTHROPIC_API_KEY); must match [A-Z0-9_]+",
    )

    # secrets which <KEY>
    secrets_which = secrets_sub.add_parser(
        "which",
        help="Show which source a key resolves from (never prints the value)",
    )
    secrets_which.add_argument(
        "key",
        help="key name to locate (e.g., ANTHROPIC_API_KEY); must match [A-Z0-9_]+",
    )

    # secrets validate
    secrets_sub.add_parser(
        "validate",
        help="Validate the configured secret backend instantiates cleanly",
    )


def _register_init(sub: argparse._SubParsersAction) -> None:
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


def _register_serve(sub: argparse._SubParsersAction) -> None:
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


def _register_deploy(sub: argparse._SubParsersAction) -> None:
    # ── deploy subcommand ─────────────────────────────────────────────────
    # The deployment planner (spec/49). Takes a POSITIONAL agent like
    # init/serve. `deploy <agent>` plans + installs a supervised loopback
    # deployment, verifies it, then GUIDES exposure (never performs it).
    # `deploy status <agent>` / `deploy down <agent>` are nested subcommands.
    deploy_cmd = sub.add_parser(
        "deploy",
        help="Plan, install, and verify a supervised loopback deployment (macOS)",
        description=(
            "Planner that sequences init/doctor/serve to get an agent "
            "running, supervised (user-level launchd, no sudo), and verified "
            "on loopback, then prints tailored network-exposure guidance. It "
            "never performs the exposure step (the operator owns the "
            "perimeter; see docs/spec/48-deploy.md and spec/37)."
        ),
    )
    # `deploy <agent>` is the primary form; `deploy status <agent>` and
    # `deploy down <agent>` are the read/teardown variants. argparse cannot
    # natively express "a bare positional OR a nested subcommand without
    # collision", so we capture the positionals generically (1-2 tokens) and
    # disambiguate in the dispatcher (_cmd_deploy): if the first token is
    # "status"/"down" it is the action and the second is the agent; otherwise
    # the first token is the agent and the action is the implicit full deploy.
    deploy_cmd.add_argument(
        "deploy_args",
        nargs="*",
        metavar="[status|down] <agent>",
        help=("either `<agent>` (full deploy) or `status <agent>` / `down <agent>`"),
    )
    deploy_cmd.add_argument(
        "--plan",
        dest="deploy_plan",
        action="store_true",
        help="print the tagged plan and exit (no side effects, no billed call)",
    )
    deploy_cmd.add_argument(
        "--yes",
        dest="deploy_yes",
        action="store_true",
        help="assume yes for consent steps (non-interactive)",
    )
    deploy_cmd.add_argument(
        "--verify-call",
        dest="deploy_verify_call",
        action="store_true",
        help=(
            "after the free healthz+doctor verify, fire a real POST /call "
            "(bills tokens + writes a capture; opt-in only)"
        ),
    )
    deploy_cmd.add_argument(
        "--port",
        dest="deploy_port",
        type=int,
        default=None,
        help=(
            "bind port (precedence: --port > ATOMIC_AGENTS_SERVE_PORT > "
            "serve.md Bind Port > default 8000)"
        ),
    )
    deploy_cmd.add_argument(
        "--agents-root",
        dest="agents_root",
        default=None,
        help="override ATOMIC_AGENTS_ROOT",
    )


def _register_manage(sub: argparse._SubParsersAction) -> None:
    # ── manage subcommand (spec/55 #624) ──────────────────────────────────────
    # Registration only builds argparse structure — it does NOT import the
    # `.manage` package (which pulls in agent_registry/logs/principal). The
    # heavy import stays lazy inside `_cmd_manage`'s dispatch (principle #6 /
    # spec/55 note), so `atomic-agents run` / `doctor` / any other command
    # never pays for it.
    manage_cmd = sub.add_parser(
        "manage",
        help="Apply validated, audited changes to agent config (spec/55)",
        description=(
            "Write verbs for the agent fleet: validate, preview, confirm, and "
            "atomically apply changes to agent configuration files. Every verb "
            "supports --dry-run (preview only), --yes (non-interactive), and "
            "--json (structured output for copilot drivers). See spec/55 for "
            "the full safety contract."
        ),
    )
    manage_sub = manage_cmd.add_subparsers(dest="manage_verb", required=True)

    # ── manage govern <agent> ─────────────────────────────────────────────────
    govern_cmd = manage_sub.add_parser(
        "govern",
        help="Edit governance.md fields (owner, permission-tier, lifecycle-status, ...)",
        description=(
            "Surgical governance.md frontmatter editor. Validates field names and "
            "enum values, previews the before/after diff, and writes atomically "
            "with a restorable snapshot. Appends an audit event to the per-agent "
            "log stream and to a distinct fleet stream when the log backend keeps "
            "them separate (a shared distributed backend records one central row)."
        ),
    )
    govern_cmd.add_argument(
        "agent",
        help="agent name (folder under agents-root)",
    )
    govern_cmd.add_argument(
        "--set",
        dest="set",
        action="append",
        metavar="field=value",
        default=None,
        help=(
            "Set a governance field. Repeatable. Field names are hyphenated "
            "(e.g. --set permission-tier=writes --set owner=alice@example.com). "
            "Nested/list fields (review.*, risk.*, sources.*, actions.*) are not "
            "yet settable via --set; edit governance.md directly. "
            "Use --set updated-at=null to clear a field."
        ),
    )
    # List-mutation flags (spec/55 CLI-surface grammar). PINNED now so the
    # recognized-vs-unrecognized status of the flag never shifts in a later PR;
    # PR1 recognises them but returns a clean structured "not yet settable via
    # CLI" refusal (never an argparse ``unrecognized arguments`` parser error,
    # which would also bypass the --json contract). PR2 implements the semantics.
    govern_cmd.add_argument(
        "--add",
        dest="add",
        action="append",
        metavar="path=item",
        default=None,
        help=(
            "Append an element to a list field (sources.*, actions.*). Reserved: "
            "not yet settable via CLI in PR1 — edit governance.md directly."
        ),
    )
    govern_cmd.add_argument(
        "--remove",
        dest="remove",
        action="append",
        metavar="path=item",
        default=None,
        help=(
            "Remove an element from a list field (sources.*, actions.*). Reserved: "
            "not yet settable via CLI in PR1 — edit governance.md directly."
        ),
    )
    govern_cmd.add_argument(
        "--set-json",
        dest="set_json",
        action="append",
        metavar="path=json-array",
        default=None,
        help=(
            "Replace a whole list field with a JSON array (sources.*, actions.*). "
            "Reserved: not yet settable via CLI in PR1 — edit governance.md directly."
        ),
    )
    govern_cmd.add_argument(
        "--show",
        dest="show",
        action="store_true",
        default=False,
        help="Print the current resolved governance record and exit (read-only).",
    )
    # ── restore verb (#710) ────────────────────────────────────────────────
    govern_cmd.add_argument(
        "--restore",
        dest="restore",
        metavar="snapshot-id",
        default=None,
        help=(
            "Roll back governance.md to a prior snapshot taken by this verb "
            "(see --list-snapshots). Runs the full validate/preview/confirm/"
            "snapshot+write/audit routine — restore itself takes a "
            "pre-restore snapshot, so a restore is always undoable via a "
            "second --restore. Mutually exclusive with --set."
        ),
    )
    govern_cmd.add_argument(
        "--list-snapshots",
        dest="list_snapshots",
        action="store_true",
        default=False,
        help=(
            "List snapshot ids available for --restore, oldest first "
            "(read-only; symmetric with --show)."
        ),
    )
    govern_cmd.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=False,
        help="Preview the before/after diff without writing. --yes is ignored when set.",
    )
    govern_cmd.add_argument(
        "--yes",
        dest="yes",
        action="store_true",
        default=False,
        help="Apply without the interactive confirmation prompt (required on non-TTY).",
    )
    govern_cmd.add_argument(
        "--json",
        dest="json",
        action="store_true",
        default=False,
        help=(
            "Emit machine-readable JSON output (canonical underscore schema keys). "
            "Refusals emit {ok: false, error_type, reason}; success emits "
            "{ok: true, agent, changes, audit_status, snapshot_path?, dry_run?} "
            "(snapshot_path present only when a prior file was snapshotted; "
            "dry_run present on --dry-run)."
        ),
    )
    govern_cmd.add_argument(
        "--agents-root",
        dest="agents_root",
        default=None,
        help="override ATOMIC_AGENTS_ROOT (fleet-scoped; matches init/registry convention)",
    )


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
    backend = get_default_memory_backend(agent_root)
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
    backend = get_default_memory_backend(agent_root)
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

    if getattr(args, "validate", False):
        return _run_bundle_validation(args, agents_root, agent_root, extra_files)

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


def _run_bundle_validation(
    args, agents_root: Path, agent_root: Path, extra_files
) -> int:
    """Validate that the bundle faithfully contains what the runtime assembles.

    Content parity (spec/26 ``--validate``, issue #593): renders a FRESH bundle
    in-memory (so the check is against current sources, never a stale on-disk
    copy), builds the runtime system prompt via ``AtomicAgent.load()`` +
    ``assemble_system_prompt()``, and compares. The agent construction lives
    here in the CLI layer so ``bundle.py`` never imports ``AtomicAgent``; the
    pure text comparison lives in ``bundle.validate_bundle_parity``.

    Exit 0 + PASS summary when content parity holds (modulo known divergences:
    the runtime ``# Available skills`` section omitted per #593; ``model.md``
    bundle-only). Exit 1 + the missing-content list on unexpected drift.
    """
    from . import bundle as bundle_mod
    from .agent import AtomicAgent

    # Render a fresh bundle to a throwaway cache dir so validation never serves
    # (or clobbers) a stale on-disk bundle, and never honors --if-stale here.
    import tempfile

    # The agent "name" is the path component(s) from agents_root to agent_root
    # (the full relative path for cascaded layouts).
    try:
        rel_name = str(agent_root.relative_to(agents_root))
    except ValueError:
        rel_name = agent_root.name

    try:
        agent = AtomicAgent(name=rel_name, agents_root=agents_root)
        agent.load()
        system_prompt = agent.assemble_system_prompt()
    except Exception as e:  # noqa: BLE001 — surface any load failure as exit 1
        print(
            f"Error: could not build runtime system prompt for validation: "
            f"{type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return 1

    # Guard the validator's one cardinal sin: an empty runtime prompt would make
    # validate_bundle_parity() return a VACUOUS pass (zero sections checked).
    # load() above is what populates the cascade fields; if the assembled prompt
    # is still empty, something is wrong with the agent — fail closed rather than
    # report a meaningless PASS.
    if not system_prompt.strip():
        print(
            "Error: runtime system prompt is empty — cannot validate parity. "
            "The agent assembled no cascade content; check the agent config.",
            file=sys.stderr,
        )
        return 1

    with tempfile.TemporaryDirectory(prefix="atomic-bundle-validate-") as tmp:
        try:
            result = bundle_mod.render_bundle(
                agent_root,
                agents_root=agents_root,
                cache_dir=Path(tmp),
                extra_files=extra_files,
                if_stale=False,
            )
            bundle_text = result.path.read_text(encoding="utf-8")
        except FileNotFoundError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

    report = bundle_mod.validate_bundle_parity(system_prompt, bundle_text)
    print(bundle_mod.format_validation_report(report))
    return 0 if report.ok else 1


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
            return _corpus_query(
                backend,
                args.text,
                args.corpus,
                args.top_k,
                agent_root,
                critical=getattr(args, "critical", False),
            )
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


def _corpus_query(
    backend,
    text: str,
    corpus: str,
    top_k: int,
    agent_root: Path,
    *,
    critical: bool = False,
) -> int:
    """Run a query against the corpus and print matching pages.

    When the corpus backend supports semantic search (``supports_semantic_search=True``)
    the ``query()`` call internally invokes ``embed()`` — a billable provider
    call. This function applies a cost gate mirroring ``dream._check_cap``:

    1. Resolve the embedding model_id from backend capabilities.
    2. Estimate tokens via ``ceil(utf8_bytes / EMBED_BYTES_PER_TOKEN)`` (same
       basis as the batch gate for non-empty text; an empty query clamps to 1
       token, which is conservative).
    3. Read cost history and apply headroom check (unless ``critical=True``).
    4. Emit ``embed_reservation`` JSONL record before the query.
    5. Call ``backend.query()`` inside try/finally.
    6. Emit ``embed_release`` in the finally block.
    7. Emit ``embed_cost`` record conditioned on ``actual_usd > 0``.

    Spec/22 primitive: ``"embed"`` (PRIMITIVE_EMBED) — the records emitted here
    carry ``primitive="embed"``; ``cli_corpus_query`` is the informal name of
    this gate SITE, not a spec/22 taxonomy value.
    Gate site: the only framework-controlled query-embed gate site (spec/46
    §'Gate-site normative MUSTs'). Direct callers of ``corpus.query()`` on a
    pgvector backend outside this CLI path are ungated by design — see spec/46.

    ``--critical`` skips headroom enforcement but still emits all audit records.

    Crash-window parity: ``actual_usd`` is charged only on a non-exception
    ``query()`` return. If the backend's internal ``embed()`` commits real spend
    and the downstream vector search then raises, this site records ``$0`` for a
    call that did bill the provider — the CLI analog of the agent.call()
    crash-window already tracked at #568. Sub-cent (single embed, never a batch);
    the ``status="error"`` ``embed_release`` record keeps the failed call visible
    in the audit trail. This site is in scope for the #568 follow-up rather than
    carrying a second divergent posture here.
    """
    import math
    from datetime import datetime, timezone
    from uuid import uuid4

    from . import _costs, _model
    from .logs import get_default_log_backend
    from .logs.types import PRIMITIVE_EMBED, RunRecord

    # ── Resolve embedding gate parameters ──────────────────────────────────────
    caps = backend.capabilities
    if not caps.supports_semantic_search or caps.embedding_backend_resolved is None:
        # No embedding backend wired — plain FTS/substring query, no gate needed.
        refs = backend.query(text, corpus, top_k=top_k)
        if not refs:
            print(f"No matches for {text!r}")
            return 0
        for ref in refs:
            print(f"{ref.name}  ({ref.byte_size} bytes)")
        return 0

    embed_backend = caps.embedding_backend_resolved
    model_id = embed_backend.model_id

    # Estimate tokens: ceil(utf8_bytes / EMBED_BYTES_PER_TOKEN) — same
    # conservative basis as the batch gate in agent.call() (spec/46
    # §'Token estimate basis'). Constant lives in _costs to avoid drift.
    utf8_bytes = len(text.encode("utf-8"))
    tokens_est = (
        math.ceil(utf8_bytes / _costs.EMBED_BYTES_PER_TOKEN) if utf8_bytes > 0 else 1
    )
    per_call_cost, cost_estimated = _costs.calc_embedding_cost(model_id, tokens_est)

    # ── Mint a standalone run_id (top-level CLI call, no parent) ───────────────
    run_id = f"cli-embed-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')}-{uuid4().hex[:8]}"
    agent_name = agent_root.name

    # ── Cost gate (mirrors dream._check_cap) ───────────────────────────────────
    log_backend = get_default_log_backend(agent_root)
    log_dir = agent_root / "log"
    if not critical:
        _model_md_path = agent_root / "model.md"
        if _model_md_path.exists():
            # Pre-check existence ONCE to eliminate the TOCTOU race in a
            # post-exception re-check (file deleted between raise and check).
            try:
                model_data = _model.parse_model_md(_model_md_path)
            except Exception:
                # PRESENT-but-unreadable: fail-closed (a blind read on an existing
                # file is a degraded signal — silently proceeding would grant
                # unbounded embed spend, the opposite of the gate's posture).
                print(
                    "Error: cost data unreadable — embed query gate fail-closed. "
                    "Use --critical to bypass.",
                    file=sys.stderr,
                )
                return 1
        else:
            # ABSENT: legitimately no caps configured → proceed.
            model_data = {}
        if model_data.get("cost_guardrails_enabled"):
            # Hoist the cap existence check so uncapped agents skip both log
            # reads (mirrors dream._check_cap's early-exit on no effective cap).
            daily_cap = model_data.get("daily_cap_usd", 0.0)
            monthly_cap = model_data.get("monthly_cap_usd", 0.0)
            has_cap = daily_cap > 0 or monthly_cap > 0
            if has_cap:
                today_r = _costs.sum_cost_for_period(
                    log_dir,
                    "today",
                    source="actor",
                    backend=log_backend,
                    agent_name=agent_name,
                )
                month_r = _costs.sum_cost_for_period(
                    log_dir,
                    "this_month",
                    source="actor",
                    backend=log_backend,
                    agent_name=agent_name,
                )
                if today_r.degraded or month_r.degraded:
                    print(
                        "Error: cost data unreadable — embed query gate fail-closed. "
                        "Use --critical to bypass.",
                        file=sys.stderr,
                    )
                    return 1
                today_cost = today_r.total_usd
                month_cost = month_r.total_usd
                daily_rem = (daily_cap - today_cost) if daily_cap > 0 else float("inf")
                monthly_rem = (
                    (monthly_cap - month_cost) if monthly_cap > 0 else float("inf")
                )
                headroom = min(daily_rem, monthly_rem)
                if per_call_cost > headroom:
                    print(
                        f"Error: embed query reservation ${per_call_cost:.6f} exceeds "
                        f"remaining headroom ${headroom:.6f}. Use --critical to bypass.",
                        file=sys.stderr,
                    )
                    return 1

    # ── Emit embed_reservation ─────────────────────────────────────────────────
    def _emit(record: dict) -> None:
        record.setdefault("ts", datetime.now().astimezone().isoformat())
        record.setdefault("run_id", run_id)
        # Stamp the originating agent (mirrors agent.py._log's
        # record.setdefault("agent_name", self.name)). Without this the records
        # persist with agent_name=None, and on a shared SQLite/Postgres log
        # backend the cost-read filter `(agent_name = ? OR agent_name IS NULL)`
        # folds them into EVERY agent's cap baseline — a cross-agent spend leak.
        record.setdefault("agent_name", agent_name)
        rr = RunRecord.from_dict(record)
        try:
            log_backend.append(rr)
        except Exception as _emit_exc:
            # Audit trail is best-effort in the CLI path; never crash on log failure.
            # Print a non-fatal warning so a dropped embed billing record isn't silent.
            print(
                f"warning: failed to write embed audit record ({record.get('trigger')}): {_emit_exc}",
                file=sys.stderr,
            )

    _emit(
        {
            "trigger": "embed_reservation",
            "run_id": run_id,
            "parent_run_id": None,
            "parent_agent": agent_name,
            "model": model_id,
            "input_tokens": 0,
            "output_tokens": 0,
            "reserved_usd": per_call_cost,
            "batch_size": 1,
            "cost_estimated": cost_estimated,
            "cost_source": "actor",
            "critical": critical,
            "status": "ok",
            "summary": (
                f"cli corpus query: reserved ${per_call_cost:.6f} for single embed "
                f"(model={model_id}, corpus={corpus})"
            ),
            "primitive": PRIMITIVE_EMBED,
        }
    )

    # ── Query with try/finally to always emit release ──────────────────────────
    actual_usd = 0.0
    refs = []
    exc_to_reraise = None
    try:
        refs = backend.query(text, corpus, top_k=top_k)
        # Charged on any non-exception query() success even when query() silently
        # fell back to FTS (pg down / embed()->None) with zero billable embed —
        # over-charges, never under-charges; embed-outcome signal deferred to #589.
        actual_usd = per_call_cost  # per-call estimate (no provider token usage signal)
    except Exception as _exc:
        exc_to_reraise = _exc
    finally:
        _emit(
            {
                "trigger": "embed_release",
                "run_id": run_id,
                "parent_run_id": None,
                "parent_agent": agent_name,
                "model": model_id,
                "input_tokens": 0,
                "output_tokens": 0,
                "reserved_usd": per_call_cost,
                "actual_usd": actual_usd,
                "batch_size": 1,
                "cost_estimated": cost_estimated,
                "cost_source": "actor",
                "critical": critical,
                "status": "ok" if exc_to_reraise is None else "error",
                "summary": (
                    f"cli corpus query: actual ${actual_usd:.6f} vs "
                    f"reserved ${per_call_cost:.6f} (model={model_id})"
                ),
                "primitive": PRIMITIVE_EMBED,
            }
        )
        # Cross-call embed accounting: dedicated embed_cost record so
        # sum_cost_for_period folds this query's spend into the cap baseline
        # on subsequent calls (mirrors _emit_embed_cost_record in agent.py).
        if actual_usd > 0:
            _emit(
                {
                    "trigger": "embed_cost",
                    "run_id": run_id,
                    "parent_run_id": None,
                    "parent_agent": agent_name,
                    "model": model_id,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cost_usd": actual_usd,
                    "cost_source": "actor",
                    "cost_estimated": cost_estimated,
                    "critical": critical,
                    "status": "ok",
                    "summary": (
                        f"cli corpus query embed cost: ${actual_usd:.6f} (model={model_id})"
                    ),
                    "primitive": PRIMITIVE_EMBED,
                }
            )

    if exc_to_reraise is not None:
        raise exc_to_reraise

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


def _cmd_secrets(args) -> int:
    """Dispatch secrets subcommands: check, which, validate.

    Secrets are flat per-deployment (not per-agent) so no agent_root is
    needed or accepted. All subcommands are read-only observability:
    they never print secret values (spec/38 secrecy MUSTs 4-6).

    Exit codes: 0 on success / key present, 1 on failure / key absent.
    """
    from .secret_backend import SecretError, get_default_secret_backend

    sub_cmd = args.secrets_cmd

    try:
        backend = get_default_secret_backend()
    except SecretError as e:
        print(f"Error: failed to instantiate secret backend: {e}", file=sys.stderr)
        return 1

    if sub_cmd == "check":
        key = args.key
        try:
            present = backend.has(key)
        except ValueError as e:
            print(f"Error: invalid key name: {e}", file=sys.stderr)
            return 1
        if present:
            print(f"{key}: present")
            return 0
        else:
            print(f"{key}: absent")
            return 1

    elif sub_cmd == "which":
        key = args.key
        try:
            ref = backend.locate(key)
        except ValueError as e:
            print(f"Error: invalid key name: {e}", file=sys.stderr)
            return 1
        if ref is None:
            print(f"{key}: absent (not found in any source)")
            return 1
        # Print only the source label — NEVER the resolved value (spec/38 MUST 6).
        print(f"{key}: {ref.source}")
        return 0

    elif sub_cmd == "validate":
        caps = backend.capabilities
        print(f"backend_id:             {backend.backend_id}")
        print(f"supports_rotation:      {caps.supports_rotation}")
        print(f"supports_audit_logging: {caps.supports_audit_logging}")
        print(f"persists_plaintext:     {caps.persists_plaintext}")
        return 0

    print(f"Error: unknown secrets subcommand: {sub_cmd}", file=sys.stderr)
    return 1


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


def _cmd_deploy(args) -> int:
    """Dispatch the `atomic-agents deploy` subcommand (spec/48).

    Forms:
      deploy <agent> [--plan] [--yes] [--verify-call] [--port N] [--agents-root]
      deploy status <agent> [--agents-root]
      deploy down <agent> [--agents-root]

    Lazy-imports the deploy module so launchd/socket/urllib machinery is not
    imported on every CLI invocation. Matches the lazy-import pattern from
    _cmd_serve / _cmd_init.
    """
    from . import deploy as deploy_mod  # noqa: PLC0415 -- intentional lazy import

    positionals = list(args.deploy_args or [])

    _USAGE = (
        "Usage: atomic-agents deploy <agent> [--plan] [--yes] [--verify-call] "
        "[--port N]\n"
        "       atomic-agents deploy status <agent>\n"
        "       atomic-agents deploy down <agent>"
    )

    # status / down variants: first token is the action, second is the agent.
    if positionals and positionals[0] in ("status", "down"):
        action = positionals[0]
        if len(positionals) != 2:
            # Do not echo the user-supplied action back into the message — the
            # two valid actions are named statically here, which also avoids a
            # CodeQL clear-text-logging false positive on the argv-derived value.
            print(
                "Error: `deploy status` and `deploy down` each require exactly "
                f"one agent name.\n{_USAGE}",
                file=sys.stderr,
            )
            return 1
        agent = positionals[1]
        if action == "status":
            return deploy_mod.deploy_status(agent)
        return deploy_mod.deploy_down(agent)

    # Implicit full-deploy form: `deploy <agent>`.
    if len(positionals) != 1:
        print(
            "Error: deploy requires exactly one agent name "
            f"(got {len(positionals)} positional arguments).\n{_USAGE}",
            file=sys.stderr,
        )
        return 1
    agent = positionals[0]

    try:
        return deploy_mod.deploy(
            agent,
            agents_root=args.agents_root,
            cli_port=args.deploy_port,
            plan_only=args.deploy_plan,
            assume_yes=args.deploy_yes,
            verify_call=args.deploy_verify_call,
        )
    except deploy_mod.DeployError as e:
        print(f"Error: {e}", file=sys.stderr)
        return e.exit_code


def _cmd_manage(args) -> int:
    """Dispatch the ``atomic-agents manage`` subcommand group (spec/55 #624).

    Lazy-imports the manage module so agent_registry, logs, and principal are
    not imported on every ``atomic-agents run`` / ``doctor`` invocation.
    Matches the lazy-import pattern from _cmd_serve / _cmd_deploy / _cmd_init.
    """
    from .manage import run_manage  # noqa: PLC0415 -- intentional lazy import

    agents_root = (
        Path(args.agents_root).expanduser().resolve()
        if getattr(args, "agents_root", None)
        else get_agents_root()
    )
    return run_manage(args, agents_root)


# ---------------------------------------------------------------------------
# Command table -- wires each _register_* / _cmd_* pair above into a
# CliCommand (see atomic_agents/_cli_registry.py). This replaces the old
# hardcoded `if args.cmd == "...": ...` dispatch chain: main() below builds
# its command table by iterating _BUILTIN_COMMANDS (extended with any
# out-of-tree entry-point commands) instead.
# ---------------------------------------------------------------------------


def _dispatch_agent_scoped(
    handler: Callable[[argparse.Namespace, Path], int],
) -> Callable[[argparse.Namespace], int]:
    """Wrap a handler that needs ``agents_root`` resolved + errors caught.

    Reproduces exactly what ``main()`` used to do centrally for run / info /
    skills / version / restore / bundle before this refactor: resolve
    ``agents_root`` from ``--agents-root`` or ``ATOMIC_AGENTS_ROOT``, call the
    handler with it, and turn an ``AtomicAgentsError`` into an ``Error: ...``
    stderr line + exit 1 instead of an uncaught traceback.
    """

    def wrapped(args: argparse.Namespace) -> int:
        agents_root = (
            Path(args.agents_root).expanduser().resolve()
            if args.agents_root
            else get_agents_root()
        )
        try:
            return handler(args, agents_root)
        except AtomicAgentsError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

    return wrapped


def _dispatch_with_error_wrap(
    handler: Callable[[argparse.Namespace], int],
) -> Callable[[argparse.Namespace], int]:
    """Wrap a handler that needs no ``agents_root`` but still catches
    ``AtomicAgentsError`` the way ``main()`` used to for ``review``."""

    def wrapped(args: argparse.Namespace) -> int:
        try:
            return handler(args)
        except AtomicAgentsError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

    return wrapped


def _builtin_commands() -> list[CliCommand]:
    """Build the built-in command table.

    Deliberately built fresh on every call (from ``main()``) rather than
    once as a module-level constant: each ``CliCommand`` entry below
    resolves ``_cmd_run`` / ``_cmd_corpus`` / etc. by name at the moment
    this function runs, so ``unittest.mock.patch("atomic_agents.cli._cmd_X",
    ...)`` -- the pattern several existing tests already use -- is honored
    the same way it was when ``main()`` dispatched via a bare
    ``_cmd_x(args)`` call. A module-level constant would instead freeze in
    the ORIGINAL function objects at import time, silently defeating that
    patching pattern.
    """
    return [
        CliCommand("run", _register_run, _dispatch_agent_scoped(_cmd_run)),
        CliCommand("info", _register_info, _dispatch_agent_scoped(_cmd_info)),
        CliCommand("skills", _register_skills, _dispatch_agent_scoped(_cmd_skills)),
        CliCommand("version", _register_version, _dispatch_agent_scoped(_cmd_version)),
        CliCommand("restore", _register_restore, _dispatch_agent_scoped(_cmd_restore)),
        CliCommand("bundle", _register_bundle, _dispatch_agent_scoped(_cmd_bundle)),
        # doctor has its own 0/1/2 exit-code semantics and must never raise
        # to the user; it resolves its own agents_root internally, so it
        # dispatches directly.
        CliCommand("doctor", _register_doctor, _cmd_doctor),
        CliCommand("review", _register_review, _dispatch_with_error_wrap(_cmd_review)),
        # persona / corpus / mcp-registry / secrets resolve their own scope
        # root (persona/corpus/mcp-registry: --*-root flag / env var / cwd;
        # secrets: deployment-scoped, no root at all) and handle their own
        # exceptions internally -- see each _cmd_* docstring above.
        CliCommand("persona", _register_persona, _cmd_persona),
        CliCommand("corpus", _register_corpus, _cmd_corpus),
        CliCommand("mcp-registry", _register_mcp_registry, _cmd_mcp_registry),
        CliCommand("secrets", _register_secrets, _cmd_secrets),
        # init / serve / deploy / manage lazy-import their implementation
        # modules inside their own _cmd_* dispatch function (progressive
        # disclosure, principle #6) -- registration above never imports them.
        CliCommand("init", _register_init, _cmd_init),
        CliCommand("serve", _register_serve, _cmd_serve),
        CliCommand("deploy", _register_deploy, _cmd_deploy),
        CliCommand("manage", _register_manage, _cmd_manage),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="atomic-agents", description="Atomic Agents CLI"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # Build the command table: built-ins plus any out-of-tree extension
    # commands discovered via the `atomic_agents.cli_commands` entry-point
    # group (see _cli_registry.discover_commands). Each command registers
    # its own subparser; main() has no per-command special-casing left.
    builtins = _builtin_commands()
    builtin_names = {c.name for c in builtins}
    commands = discover_commands(builtins)
    command_by_name: dict[str, CliCommand] = {}
    for command in commands:
        try:
            command.register(sub)
        except Exception as e:  # noqa: BLE001 -- a broken plugin must not brick the CLI
            # A built-in's register() raising is OUR bug — re-raise it loud
            # rather than swallow it (a silently-missing `init`/`doctor` would
            # be far more confusing to debug than a traceback). A plugin's
            # register() raising (its own code, or an argparse
            # duplicate/collision error) runs on EVERY invocation before
            # parse_args, so it must be isolated: skip that one command with a
            # stderr warning and keep every other command working.
            if command.name in builtin_names:
                raise
            print(
                f"warning: CLI command plugin {command.name!r} failed to "
                f"register its subparser and was skipped: {e}",
                file=sys.stderr,
            )
            continue
        command_by_name[command.name] = command

    args = parser.parse_args(argv)
    return command_by_name[args.cmd].dispatch(args)


if __name__ == "__main__":
    sys.exit(main())
