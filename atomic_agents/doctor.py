"""Preflight checks for an atomic-agents installation.

`atomic-agents doctor` runs every check below and reports the results so an
operator can verify the install before scheduling a run. Each check is
independent: one failure does not abort the others. The CLI surface is in
``cli.py``; this module is the pure-logic core (so callers can also import it,
e.g. as a Cloud Run liveness probe).

Checks (one CheckResult per check unless noted):

    env             ATOMIC_AGENTS_ROOT (or default ~/docs/agents) resolves
                    to a directory that exists.
    python          sys.version_info satisfies pyproject's requires-python.
    vault           Required files exist under <agents_root>/<agent>/
                    (persona/IDENTITY.md, tools.md, model.md, memory/INDEX.md).
                    Skipped when no agent name is given.
    provider-keys   For each provider referenced by model.md (default + fallback),
                    a key resolves via env / Keychain / ~/.config keys.json.
                    One CheckResult per provider.
    model           model.md's default_model is in the pricing table; if cost
                    guardrails are enabled, daily/monthly caps are non-zero.
    mcp             Each server in mcp.md responds to a stdio handshake.
                    Skipped with --no-mcp or when mcp.md is absent.
                    One CheckResult per declared server.
    locks           Agent's .lock file is not currently held by another
                    process. (flock releases on death; lingering files are
                    normal — only an actively-held lock is suspicious.)
    memory-backend  FilesystemBackend resolves and stats() returns.
    write-paths     Each tools.md write_paths entry exists and is writable.

Exit code mapping (set by the CLI, not this module):

    0  every check passed (skips ok)
    1  one or more failed
    2  doctor itself crashed

Doctor never raises for a "check failed" condition — it returns a fail
CheckResult. It only crashes when the doctor logic itself is broken (a
genuine exit-2 condition).
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import _cascade, _model, _tools
from ._costs import PRICING
from ._platform import get_agents_root


PASS = "pass"
FAIL = "fail"
SKIP = "skip"
# WARN = "operator-config asks for a backend doctor can't fully probe
# from this host (e.g., Redis URL set but unreachable from the dev
# machine running doctor). Matches the ``check_provider_keys`` pattern
# — doctor never crashes on missing/unreachable optional infrastructure."
WARN = "warn"

# Minimum supported Python version. Mirror pyproject.toml's requires-python.
MIN_PYTHON = (3, 11)

# Provider name → (keychain entry, env var list, config-file key, sdk_module).
# sdk_module is the import name doctor must verify is installed; None means
# the provider's SDK is a hard dependency and import never fails.
_PROVIDER_KEYS = {
    "anthropic": (
        "atomic-agents-anthropic",
        ["ATOMIC_AGENTS_ANTHROPIC_KEY", "ANTHROPIC_API_KEY"],
        "anthropic",
        None,  # anthropic is a hard dep in pyproject.toml
    ),
    "openai": (
        "atomic-agents-openai",
        ["ATOMIC_AGENTS_OPENAI_KEY", "OPENAI_API_KEY"],
        "openai",
        "openai",  # optional extra; needed for both openai and moonshot
    ),
    "moonshot": (
        "atomic-agents-moonshot",
        ["ATOMIC_AGENTS_MOONSHOT_KEY", "MOONSHOT_API_KEY"],
        "moonshot",
        "openai",  # _llm._call_moonshot reuses the openai SDK with a base_url
    ),
}


@dataclass
class CheckResult:
    """One check's outcome.

    name      identifier matching the docstring above (e.g. "env", "python")
    status    PASS | FAIL | SKIP
    message   one-line summary suitable for the terminal
    fix_hint  what the operator should run/edit to fix it (empty on PASS/SKIP)
    detail    optional extra info; surfaces in --json
    """

    name: str
    status: str
    message: str
    fix_hint: str = ""
    detail: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status == PASS

    @property
    def failed(self) -> bool:
        return self.status == FAIL


# ──────────────────────────────────────────────────────────────────
# Entry point


def run_doctor(
    agent_name: str | None = None,
    agents_root: Path | None = None,
    *,
    skip_mcp: bool = False,
) -> list[CheckResult]:
    """Run every check and return the list of CheckResult.

    agent_name=None runs only the host-level checks (env, python). The vault,
    model, provider-keys, mcp, locks, memory-backend, and write-paths checks
    require a target agent and are emitted as SKIP results when no agent is
    given.
    """
    results: list[CheckResult] = []

    results.append(check_env(agents_root))
    results.append(check_python())
    results.append(check_bundle_cache_writable())

    # Resolve agents_root for downstream checks. We tolerate a missing path
    # because check_env will already have flagged it; downstream checks
    # then short-circuit to SKIP.
    resolved_root: Path = (
        Path(agents_root).expanduser().resolve()
        if agents_root is not None
        else get_agents_root()
    )

    if agent_name is None:
        # Order matches run_doctor()'s actual execution sequence below
        # (lock-backend → log-backend → profile-backend →
        # tool-registry-backend → policy-backend → memory-backend) so
        # contributors adding a scope-level backend check see the SKIP
        # enumeration mirror reality.
        for n in (
            "vault",
            "provider-keys",
            "model",
            "mcp",
            "locks",
            "profile-backend",
            "tool-registry-backend",
            "policy-backend",
            "memory-backend",
            "write-paths",
        ):
            results.append(
                CheckResult(
                    name=n,
                    status=SKIP,
                    message="no --agent supplied; skipped",
                )
            )
        return results

    agent_root = resolved_root / agent_name

    # Detect cascade so vault/model/tools resolution mirrors the runtime.
    # detect_cascade returns None for single-agent layouts.
    cascade = _cascade.detect_cascade(agent_root)

    results.append(check_vault(agent_root, cascade=cascade))

    # Parse model.md / tools.md once for the downstream checks. We resolve
    # via cascade when applicable so doctor sees the same config the runtime
    # would. Parse errors (malformed YAML, bad caps) are reported as FAIL
    # results, not crashes — operators should not see "doctor crashed" for
    # a config mistake.
    model_data, model_parse_fail = _safe_parse_model(agent_root, cascade)
    tools_data, tools_parse_fail = _safe_parse_tools(agent_root, cascade)

    if model_parse_fail is not None:
        results.append(model_parse_fail)
    if tools_parse_fail is not None:
        results.append(tools_parse_fail)

    results.extend(check_provider_keys(model_data))
    results.append(check_model(model_data))

    if skip_mcp:
        results.append(
            CheckResult(
                name="mcp",
                status=SKIP,
                message="--no-mcp specified; skipped",
            )
        )
    else:
        results.extend(
            check_mcp(agent_root, read_paths=tools_data.get("read_paths", []))
        )

    results.append(check_lock_backend(agent_root))
    results.append(check_locks(agent_root))
    results.append(check_log_backend(agent_root))
    results.append(check_agent_profile_backend(resolved_root))
    results.append(check_tool_registry_backend(agent_root))
    results.append(check_policy_backend(resolved_root))
    results.append(check_memory_backend(agent_root))
    results.append(check_write_paths(tools_data, agent_root=agent_root))

    return results


def _safe_parse_model(agent_root: Path, cascade) -> tuple[dict, CheckResult | None]:
    """Parse model.md, returning (data, parse_failure_check_or_None).

    On parse failure, returns the parser defaults plus a FAIL CheckResult so
    downstream checks still run with sane defaults instead of crashing the
    whole doctor run.

    Two failure paths are detected:

    1. The parser itself raises (e.g. ``float("not-a-number")`` on a bad cap
       value). The exception is caught here.
    2. The parser silently swallows malformed YAML — ``parse_model_md_text``
       catches ``yaml.YAMLError`` and falls back to defaults. We re-parse the
       embedded YAML fences directly so this case still surfaces as a FAIL.
    """
    try:
        if cascade is not None:
            model_path = _cascade.resolve_model_md(cascade)
            data = _model.parse_model_md(model_path)
            text = model_path.read_text(encoding="utf-8") if model_path else ""
        else:
            mp = agent_root / "model.md"
            data = _model.parse_model_md(mp if mp.exists() else None)
            text = mp.read_text(encoding="utf-8") if mp.exists() else ""
    except Exception as e:  # noqa: BLE001 — operator config issue, not a doctor bug
        return _model.parse_model_md_text(""), CheckResult(
            name="config-parse[model.md]",
            status=FAIL,
            message=f"could not parse model.md: {type(e).__name__}: {e}",
            fix_hint=(
                "Check model.md syntax — see docs/spec/04-runtime-assembly.md "
                "and docs/samples/caldwell/model.md for a reference. "
                "Common causes: malformed YAML in cost_guardrails block, "
                "non-numeric cap values."
            ),
            detail={"error_type": type(e).__name__},
        )

    # Belt-and-braces: surface YAML errors that parse_model_md_text swallows.
    yaml_failure = _detect_yaml_fence_errors(text)
    if yaml_failure is not None:
        return data, CheckResult(
            name="config-parse[model.md]",
            status=FAIL,
            message=f"model.md contains invalid YAML: {yaml_failure}",
            fix_hint=(
                "Check the ```yaml fenced block(s) in model.md. Run "
                "`python -c \"import yaml; yaml.safe_load(open('model.md').read())\"` "
                "for a more precise error location, or see "
                "docs/samples/caldwell/model.md for a reference."
            ),
        )
    return data, None


def _detect_yaml_fence_errors(text: str) -> str | None:
    """Return a one-line description of the first YAML-fence error, or None.

    parse_model_md_text catches yaml.YAMLError and silently falls back to
    defaults — perfectly fine for runtime, but doctor's job is to surface
    operator config errors. Re-parse here directly.
    """
    if not text:
        return None
    import re
    import yaml  # type: ignore[import-untyped]

    blocks = re.findall(r"```yaml\s*\n(.*?)```", text, re.DOTALL)
    for block in blocks:
        try:
            yaml.safe_load(block)
        except yaml.YAMLError as e:
            # The exception's str() includes line/column when available.
            return str(e).splitlines()[0]
    return None


def _safe_parse_tools(agent_root: Path, cascade) -> tuple[dict, CheckResult | None]:
    """Parse tools.md, returning (data, parse_failure_check_or_None)."""
    try:
        if cascade is not None:
            _, tools_text = _cascade.resolve_tools_md(cascade)
            data = _tools.parse_tools_md_text(tools_text)
        else:
            tp = agent_root / "tools.md"
            data = _tools.parse_tools_md(tp) if tp.exists() else {}
        return data, None
    except Exception as e:  # noqa: BLE001 — operator config issue, not a doctor bug
        return {}, CheckResult(
            name="config-parse[tools.md]",
            status=FAIL,
            message=f"could not parse tools.md: {type(e).__name__}: {e}",
            fix_hint=(
                "Check tools.md syntax — see docs/spec/01-anatomy.md for the "
                "section format. Common cause: stray content under a path "
                "section that isn't a bullet."
            ),
            detail={"error_type": type(e).__name__},
        )


# ──────────────────────────────────────────────────────────────────
# Individual checks


def check_env(agents_root_override: Path | None) -> CheckResult:
    """ATOMIC_AGENTS_ROOT or default resolves to an existing directory."""
    if agents_root_override is not None:
        path = Path(agents_root_override).expanduser().resolve()
        source = f"--agents-root {agents_root_override}"
    else:
        env_val = os.environ.get("ATOMIC_AGENTS_ROOT")
        if env_val:
            path = Path(env_val).expanduser().resolve()
            source = f"ATOMIC_AGENTS_ROOT={env_val}"
        else:
            path = get_agents_root()
            source = "default ~/docs/agents (ATOMIC_AGENTS_ROOT unset)"

    if not path.exists():
        return CheckResult(
            name="env",
            status=FAIL,
            message=f"agents-root does not exist: {path} ({source})",
            fix_hint=(
                f"Create it: mkdir -p {path}\n"
                f"  Or set ATOMIC_AGENTS_ROOT to an existing directory."
            ),
            detail={"path": str(path), "source": source},
        )
    if not path.is_dir():
        return CheckResult(
            name="env",
            status=FAIL,
            message=f"agents-root is not a directory: {path}",
            fix_hint=f"Remove the file at {path} and recreate it as a directory.",
            detail={"path": str(path), "source": source},
        )
    return CheckResult(
        name="env",
        status=PASS,
        message=f"agents-root resolves to {path} ({source})",
        detail={"path": str(path), "source": source},
    )


def check_python() -> CheckResult:
    """Interpreter satisfies the minimum supported version."""
    cur = sys.version_info[:2]
    cur_str = f"{cur[0]}.{cur[1]}.{sys.version_info.micro}"
    if cur >= MIN_PYTHON:
        return CheckResult(
            name="python",
            status=PASS,
            message=f"Python {cur_str} (>= {MIN_PYTHON[0]}.{MIN_PYTHON[1]} required)",
            detail={"version": cur_str},
        )
    return CheckResult(
        name="python",
        status=FAIL,
        message=f"Python {cur_str} is too old; need >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]}",
        fix_hint=(
            f"Install Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ and re-run via that "
            "interpreter (e.g. `uv run --python 3.12 atomic-agents doctor`)."
        ),
        detail={"version": cur_str},
    )


def check_vault(agent_root: Path, *, cascade=None) -> CheckResult:
    """Required files exist for this agent layout.

    Single-agent layout: persona/IDENTITY.md, tools.md, model.md, memory/INDEX.md
    must all exist under <agent_root> per spec/04.

    Cascaded layout (spec/06): persona/IDENTITY.md and memory/INDEX.md are
    instance-only. tools.md and model.md may live at the role layer; either
    role-level or instance-level satisfies the requirement.

    Pass ``cascade=_cascade.detect_cascade(agent_root)`` to opt into the
    cascade-aware variant; the default behaviour (cascade=None) is the
    single-agent rule.
    """
    if not agent_root.exists():
        return CheckResult(
            name="vault",
            status=FAIL,
            message=f"agent folder does not exist: {agent_root}",
            fix_hint=(
                f"Create it (or copy a sample): "
                f"cp -r docs/samples/caldwell {agent_root}"
            ),
            detail={"agent_root": str(agent_root)},
        )

    # Always-required, instance-level files.
    instance_required = [
        "persona/IDENTITY.md",
        "memory/INDEX.md",
    ]
    missing = [r for r in instance_required if not (agent_root / r).exists()]

    # tools.md / model.md: either instance-level (always for single-agent) or
    # role-level (cascade-only fallback).
    for filename in ("tools.md", "model.md"):
        instance_path = agent_root / filename
        if instance_path.exists():
            continue
        if cascade is not None:
            role_path = cascade.role_root / filename
            if role_path.exists():
                continue
        missing.append(filename)

    if missing:
        return CheckResult(
            name="vault",
            status=FAIL,
            message=f"required files missing: {', '.join(missing)}",
            fix_hint=(
                "Create the missing files. See docs/spec/01-anatomy.md for the "
                "canonical layout (or docs/spec/06-multi-agent-projects.md "
                "for cascade layouts). docs/samples/caldwell/ is a working "
                "example."
            ),
            detail={
                "missing": missing,
                "agent_root": str(agent_root),
                "cascaded": cascade is not None,
            },
        )
    return CheckResult(
        name="vault",
        status=PASS,
        message=(
            f"all required files present under {agent_root}"
            + (" (cascaded)" if cascade is not None else "")
        ),
        detail={"agent_root": str(agent_root), "cascaded": cascade is not None},
    )


def _provider_for_model(model_id: str) -> str | None:
    """Map a model id to its provider name.

    Convention follows _costs.PRICING keys:
        claude-*       → anthropic
        gpt-*          → openai
        moonshot/*     → moonshot

    Returns None for ids that don't match any known provider prefix.
    """
    if not model_id:
        return None
    if model_id.startswith("claude-"):
        return "anthropic"
    if model_id.startswith("gpt-"):
        return "openai"
    if model_id.startswith("moonshot/"):
        return "moonshot"
    return None


def check_provider_keys(model_data: dict) -> list[CheckResult]:
    """For each provider used by default + fallback, verify a key resolves."""
    providers: list[str] = []
    seen: set[str] = set()
    for key in ("default_model", "fallback_model"):
        mid = model_data.get(key)
        if not mid:
            continue
        prov = _provider_for_model(mid)
        if prov and prov not in seen:
            providers.append(prov)
            seen.add(prov)

    if not providers:
        return [
            CheckResult(
                name="provider-keys",
                status=SKIP,
                message="no recognised provider in model.md (default/fallback)",
            )
        ]

    return [_check_one_provider_key(p) for p in providers]


def _check_one_provider_key(provider: str) -> CheckResult:
    """Resolve a key for a single provider via the production lookup chain.

    Also verifies that the optional SDK module the runtime would import is
    available — without this, the key check passes but the first agent run
    fails with an ImportError after spending nothing on tokens but plenty on
    operator confusion.
    """
    if provider not in _PROVIDER_KEYS:
        return CheckResult(
            name=f"provider-keys[{provider}]",
            status=SKIP,
            message=f"no key-resolution chain registered for {provider!r}",
        )
    keychain_name, env_vars, config_key, sdk_module = _PROVIDER_KEYS[provider]

    # Optional-SDK check first — a missing import is more fundamental than a
    # missing key and the fix is different (extras vs creds).
    if sdk_module is not None:
        try:
            __import__(sdk_module)
        except ImportError as e:
            extra = "openai" if sdk_module == "openai" else sdk_module
            return CheckResult(
                name=f"provider-keys[{provider}]",
                status=FAIL,
                message=f"{provider} requires the {sdk_module!r} package, which is not installed",
                fix_hint=(
                    f"Install the optional extra: uv add 'atomic-agents-stack[{extra}]'\n"
                    f"  Or: pip install {sdk_module}"
                ),
                detail={
                    "provider": provider,
                    "missing_sdk": sdk_module,
                    "underlying_error": str(e),
                },
            )

    # Reuse the production resolver so the doctor verdict and runtime
    # behaviour can never disagree on key resolution.
    from ._llm import _get_key
    from .exceptions import AtomicAgentsError

    try:
        _get_key(env_vars=env_vars, keychain_name=keychain_name, config_key=config_key)
    except AtomicAgentsError as e:
        return CheckResult(
            name=f"provider-keys[{provider}]",
            status=FAIL,
            message=f"{provider} API key not found",
            fix_hint=(
                f"Choose one:\n"
                f"  - export {env_vars[0]}='<key>'\n"
                f"  - security add-generic-password -a $USER -s {keychain_name} -w '<key>' "
                f"(macOS Keychain)\n"
                f'  - add {{"{config_key}": "<key>"}} to ~/.config/atomic_agents/keys.json'
            ),
            detail={
                "provider": provider,
                "keychain": keychain_name,
                "env_vars": env_vars,
                "underlying_error": str(e),
            },
        )
    return CheckResult(
        name=f"provider-keys[{provider}]",
        status=PASS,
        message=f"{provider} API key resolves",
        detail={"provider": provider},
    )


def check_model(model_data: dict) -> CheckResult:
    """default_model is in PRICING; if guardrails enabled, caps are non-zero."""
    default_model = model_data.get("default_model", "")
    if not default_model:
        return CheckResult(
            name="model",
            status=FAIL,
            message="model.md has no default_model",
            fix_hint=(
                "Add a `## Default model` section to model.md with the model id "
                "on the next line. See docs/samples/caldwell/model.md."
            ),
        )
    if default_model not in PRICING:
        return CheckResult(
            name="model",
            status=FAIL,
            message=f"default_model {default_model!r} is not in the pricing table",
            fix_hint=(
                f"Use one of {sorted(PRICING.keys())}, or add {default_model!r} "
                "to atomic_agents/_costs.py PRICING with current rates."
            ),
            detail={"default_model": default_model},
        )

    # If guardrails enabled, caps must be non-zero (zero = unlimited; the issue
    # treats that as a misconfiguration since it disables the feature silently).
    if model_data.get("cost_guardrails_enabled"):
        daily = float(model_data.get("daily_cap_usd", 0.0))
        monthly = float(model_data.get("monthly_cap_usd", 0.0))
        if daily <= 0 or monthly <= 0:
            return CheckResult(
                name="model",
                status=FAIL,
                message=(
                    "cost_guardrails enabled but daily_cap_usd or monthly_cap_usd "
                    f"is 0 (daily={daily}, monthly={monthly})"
                ),
                fix_hint=(
                    "Set both daily_cap_usd and monthly_cap_usd to non-zero "
                    "values in model.md's cost_guardrails block, or set "
                    "enabled: false to opt out."
                ),
                detail={"daily_cap_usd": daily, "monthly_cap_usd": monthly},
            )

    return CheckResult(
        name="model",
        status=PASS,
        message=f"default_model {default_model!r} priced; guardrails ok",
        detail={
            "default_model": default_model,
            "cost_guardrails_enabled": model_data.get("cost_guardrails_enabled", False),
        },
    )


def check_mcp(agent_root: Path, *, read_paths: list | None = None) -> list[CheckResult]:
    """Each server in mcp.md responds to a stdio handshake.

    Returns one CheckResult per declared server. Returns a single SKIP result
    when mcp.md is absent (the common case for agents that don't use MCP).

    ``read_paths`` should be the parsed tools.md read_paths so doctor enforces
    the same path-traversal protection the runtime does. When omitted, doctor
    accepts any args — but the agent itself will reject the same config at
    runtime, so this argument should always be supplied by ``run_doctor``.
    """
    mcp_path = agent_root / "mcp.md"
    if not mcp_path.exists():
        return [
            CheckResult(
                name="mcp",
                status=SKIP,
                message="no mcp.md (agent does not use MCP)",
            )
        ]

    # Lazy import — keeps the doctor command's startup cost low for agents
    # that don't use MCP (which is the majority).
    from . import mcp as mcp_module

    try:
        specs = mcp_module.parse_mcp_md(mcp_path, read_paths=read_paths)
    except Exception as e:
        return [
            CheckResult(
                name="mcp",
                status=FAIL,
                message=f"could not parse mcp.md: {type(e).__name__}: {e}",
                fix_hint=(
                    "Check mcp.md syntax — see docs/spec/19-mcp.md for the format. "
                    "Common causes: unresolved env-var reference, malformed YAML, "
                    "or path-shaped server args that fall outside tools.md "
                    "read_paths (PathTraversalError)."
                ),
            )
        ]

    if not specs:
        return [
            CheckResult(
                name="mcp",
                status=SKIP,
                message="mcp.md present but declares no servers",
            )
        ]

    results: list[CheckResult] = []
    for spec in specs:
        cr = _check_one_mcp_server(spec, mcp_module)
        results.append(cr)
    return results


DEFAULT_MCP_HANDSHAKE_TIMEOUT_SECONDS = 10.0


def _check_one_mcp_server(
    spec, mcp_module, timeout_seconds: float = DEFAULT_MCP_HANDSHAKE_TIMEOUT_SECONDS
) -> CheckResult:
    """Run one server's stdio handshake with a bounded timeout.

    The MCP SDK's ClientSession has no default read timeout. Without a bound
    here, a server that starts but never responds to ``initialize`` /
    ``list_tools`` would hang doctor forever — fatal for the documented
    --json liveness-probe use case.
    """
    from .exceptions import MCPServerConnectFailed

    if spec.transport != "stdio":
        return CheckResult(
            name=f"mcp[{spec.name}]",
            status=SKIP,
            message=f"transport {spec.transport!r} not supported in v1 (stdio only)",
        )
    try:
        conn = _connect_sync_with_timeout(spec, mcp_module, timeout_seconds)
    except TimeoutError:
        return CheckResult(
            name=f"mcp[{spec.name}]",
            status=FAIL,
            message=(
                f"server {spec.name!r} did not respond within {timeout_seconds:.0f}s "
                f"(stdio handshake)"
            ),
            fix_hint=(
                f"Run the command manually and confirm it prints MCP traffic: "
                f"{spec.command} {' '.join(spec.args)}\n"
                f"  If it hangs, the server has a startup/auth/network bug. "
                f"If it works manually, raise the timeout via doctor's "
                f"DEFAULT_MCP_HANDSHAKE_TIMEOUT_SECONDS or use --no-mcp."
            ),
            detail={"server": spec.name, "timeout_seconds": timeout_seconds},
        )
    except MCPServerConnectFailed as e:
        return CheckResult(
            name=f"mcp[{spec.name}]",
            status=FAIL,
            message=f"server {spec.name!r} failed to connect",
            fix_hint=(
                f"Verify the command is on PATH and the server is reachable: "
                f"{spec.command} {' '.join(spec.args)}\n"
                f"  Underlying error: {e}"
            ),
            detail={"server": spec.name, "command": spec.command, "args": spec.args},
        )
    except Exception as e:
        return CheckResult(
            name=f"mcp[{spec.name}]",
            status=FAIL,
            message=f"server {spec.name!r} unexpected error: {type(e).__name__}: {e}",
            fix_hint=f"Manually run: {spec.command} {' '.join(spec.args)}",
        )
    return CheckResult(
        name=f"mcp[{spec.name}]",
        status=PASS,
        message=f"server {spec.name!r} responded ({len(conn.tools)} tools)",
        detail={"server": spec.name, "tool_count": len(conn.tools)},
    )


def _connect_sync_with_timeout(spec, mcp_module, timeout_seconds: float):
    """Run mcp's async handshake with a hard wall-clock bound.

    Wraps `_async_connect_and_list` (used by mcp._connect_sync) in
    asyncio.wait_for so an unresponsive server fails the check after
    timeout_seconds instead of blocking the whole CLI.
    """
    import asyncio

    async def bounded():
        return await asyncio.wait_for(
            mcp_module._async_connect_and_list(spec),
            timeout=timeout_seconds,
        )

    try:
        tools = asyncio.run(bounded())
    except asyncio.TimeoutError as e:
        raise TimeoutError(str(e)) from e
    except Exception as e:
        # Re-raise into the MCPServerConnectFailed shape that the production
        # _connect_sync would have produced, so the caller's exception
        # taxonomy stays the same.
        from .exceptions import MCPServerConnectFailed

        raise MCPServerConnectFailed(
            f"MCP server '{spec.name}' failed to connect: {type(e).__name__}: {e}"
        ) from e

    return mcp_module._ServerConnection(spec=spec, tools=tools)


def check_locks(agent_root: Path, *, stale_seconds: float = 300.0) -> CheckResult:
    """Agent's lock is not currently held by another process.

    Routes through the operator-pinned ``LockBackend.is_held("")``
    (defaults to filesystem; reads ``ATOMIC_AGENTS_LOCK_BACKEND`` /
    ``ATOMIC_AGENTS_LOCK_BACKEND_URL`` for the override path —
    #60 PR 3 + spec/21 §"Operator override surface"). For the
    filesystem default, the on-disk artifact at ``<agent_root>/.lock``
    is unchanged and doctor still reads it for the PID/staleness
    diagnostic message.

    For a Redis-backed deployment whose URL is unreachable from the
    host running doctor (e.g., a developer running ``atomic-agents
    doctor`` from a coffee-shop wifi), the check returns ``WARN``
    rather than ``FAIL`` — matching the ``check_provider_keys`` pattern:
    doctor never crashes on missing/unreachable optional infrastructure.

    A .lock file lingering on disk is normal — POSIX flock() releases on
    process death and Python doesn't unlink the file on exit. The only
    problematic state is "file currently held". If the file is held AND
    its mtime is older than ``stale_seconds``, the holder is likely
    stuck.
    """
    import time

    from .locks import get_default_lock_backend

    backend_id = (
        os.environ.get("ATOMIC_AGENTS_LOCK_BACKEND", "filesystem").strip().lower()
    )

    try:
        backend = get_default_lock_backend(agent_root)
    except ImportError as exc:
        # Operator pinned a backend whose extra isn't installed.
        # FAIL with the specific install command.
        return CheckResult(
            name="locks",
            status=FAIL,
            message=(
                f"ATOMIC_AGENTS_LOCK_BACKEND={backend_id!r} requires "
                f"an extra that isn't installed: {exc}"
            ),
            fix_hint=f"pip install 'atomic-agents-stack[{backend_id}]'",
        )
    except Exception as exc:
        # Misconfigured backend (e.g., redis URL malformed, unknown
        # backend_id). Use FAIL — operator-config drift is doctor's
        # whole job to surface.
        return CheckResult(
            name="locks",
            status=FAIL,
            message=f"lock backend construction failed: {exc}",
            fix_hint=(
                "Check ATOMIC_AGENTS_LOCK_BACKEND + "
                "ATOMIC_AGENTS_LOCK_BACKEND_URL env vars; unset to use "
                "the filesystem default."
            ),
        )

    # Filesystem-specific .lock file check (PID diagnostic + staleness)
    # is still applicable for the filesystem default.
    lock_path = agent_root / ".lock"
    if backend_id == "filesystem" and not lock_path.exists():
        return CheckResult(
            name="locks",
            status=PASS,
            message="no lock file (agent has not run yet, or last run released cleanly)",
        )

    try:
        held = backend.is_held("")
    except Exception as exc:
        # Backend reachability failure (e.g., Redis URL unreachable).
        # WARN — operator-config is set but doctor can't probe.
        return CheckResult(
            name="locks",
            status=WARN,
            message=(
                f"operator-pinned lock backend {backend_id!r} is not "
                f"reachable from this host; doctor cannot probe lock "
                f"state ({exc})"
            ),
            fix_hint=(
                "Verify ATOMIC_AGENTS_LOCK_BACKEND_URL is correct and "
                "the backend is reachable from this host. doctor will "
                "warn but not fail — the framework runtime will fail at "
                "first lock acquire if the backend is truly down."
            ),
        )

    if held:
        # Lock is held by some process. Diagnostic detail is
        # backend-specific: filesystem reads ``<agent_root>/.lock`` for
        # the PID + staleness; distributed backends just report which
        # backend reported held. The Protocol's ``is_held`` returns
        # bool only, so this detail lives in doctor's domain.
        if backend_id == "filesystem" and lock_path.exists():
            try:
                contents = lock_path.read_text(
                    encoding="utf-8", errors="replace"
                ).strip()
            except OSError:
                contents = ""
            mtime = lock_path.stat().st_mtime
            age = time.time() - mtime
            stale = age > stale_seconds
            return CheckResult(
                name="locks",
                status=FAIL,
                message=(
                    f"agent lock at {lock_path} is held"
                    + (
                        f" (stale: age {age:.0f}s > {stale_seconds:.0f}s threshold)"
                        if stale
                        else ""
                    )
                    + (f"; recorded {contents}" if contents else "")
                ),
                fix_hint=(
                    "If the holder process is alive, wait for it to finish or kill it. "
                    f"If the holder is dead, remove the file: rm {lock_path}"
                ),
                detail={
                    "path": str(lock_path),
                    "age_seconds": age,
                    "contents": contents,
                    "stale": stale,
                },
            )
        # Non-filesystem backend reported held; no PID/staleness signal
        # from the Protocol. Surface what we know.
        return CheckResult(
            name="locks",
            status=FAIL,
            message=(
                f"agent lock is held according to backend "
                f"{backend_id!r} (is_held returned True)"
            ),
            fix_hint=(
                "Inspect the backend directly for holder details "
                "(e.g., redis-cli KEYS 'atomic_agents:lock:*'). The "
                "framework will block on the next acquire until the "
                "holder releases or the lease expires."
            ),
            detail={"backend_id": backend_id},
        )

    return CheckResult(
        name="locks",
        status=PASS,
        message="lock file present but not held (clean)",
    )


def check_lock_backend(agent_root: Path) -> CheckResult:
    """Operator-config coherence check for the lock backend (#60 PR 3).

    Validates that ``ATOMIC_AGENTS_LOCK_BACKEND`` (plus
    ``ATOMIC_AGENTS_LOCK_BACKEND_URL`` when non-filesystem) is
    correctly configured:

    * unset / ``filesystem`` → PASS (today's deployment shape — no
      extras needed, no URL needed)
    * ``redis`` with extra installed AND URL reachable → PASS
    * ``redis`` with extra NOT installed → FAIL with the install command
    * ``redis`` with extra installed but URL unreachable → WARN
      (matches ``check_provider_keys`` pattern — doctor doesn't crash
      on missing optional infrastructure)
    * unknown backend_id (typo) → FAIL with the registered backend list

    This is the operator-coherence layer; ``check_locks`` then runs the
    actual held-check through the configured backend. Both checks reuse
    ``get_default_lock_backend`` internally so doctor's verdict and the
    runtime's behavior cannot diverge.
    """
    from .locks import get_default_lock_backend, list_lock_backends

    backend_id = (
        os.environ.get("ATOMIC_AGENTS_LOCK_BACKEND", "filesystem").strip().lower()
    )

    if backend_id == "filesystem":
        return CheckResult(
            name="lock-backend",
            status=PASS,
            message="filesystem backend (default; no extra needed)",
            detail={"backend_id": "filesystem"},
        )

    # ``redis`` is lazy-resolved in ``get_default_lock_backend`` rather
    # than eagerly registered at framework import (avoids pulling the
    # ``redis`` optional dependency into every startup). Treat it as a
    # known id alongside the eagerly-registered backends.
    known_ids = set(list_lock_backends()) | {"redis"}
    if backend_id not in known_ids:
        return CheckResult(
            name="lock-backend",
            status=FAIL,
            message=(
                f"ATOMIC_AGENTS_LOCK_BACKEND={backend_id!r} is not "
                f"a known backend. Known: {sorted(known_ids)}"
            ),
            fix_hint=(
                "Set ATOMIC_AGENTS_LOCK_BACKEND to one of the known "
                "ids, or unset to use the filesystem default."
            ),
        )

    # Non-filesystem backend selected. URL must be set, extra must be
    # importable, backend must be reachable.
    url = os.environ.get("ATOMIC_AGENTS_LOCK_BACKEND_URL")
    if not url:
        return CheckResult(
            name="lock-backend",
            status=FAIL,
            message=(
                f"ATOMIC_AGENTS_LOCK_BACKEND={backend_id!r} requires "
                "ATOMIC_AGENTS_LOCK_BACKEND_URL to be set"
            ),
            fix_hint=(
                "export ATOMIC_AGENTS_LOCK_BACKEND_URL=<url> (e.g., "
                "redis://localhost:6379/0 for the redis backend)."
            ),
        )

    try:
        backend = get_default_lock_backend(agent_root)
    except ImportError as exc:
        return CheckResult(
            name="lock-backend",
            status=FAIL,
            message=(
                f"ATOMIC_AGENTS_LOCK_BACKEND={backend_id!r} requires "
                f"an extra that isn't installed: {exc}"
            ),
            fix_hint=f"pip install 'atomic-agents-stack[{backend_id}]'",
        )
    except Exception as exc:
        return CheckResult(
            name="lock-backend",
            status=FAIL,
            message=f"lock backend construction failed: {exc}",
            fix_hint=(
                "Check ATOMIC_AGENTS_LOCK_BACKEND and "
                "ATOMIC_AGENTS_LOCK_BACKEND_URL for typos."
            ),
        )

    # Probe with a cheap is_held call. Failure is reachability, not held
    # state — WARN, not FAIL (same rationale as check_provider_keys).
    try:
        backend.is_held("")
    except Exception as exc:
        return CheckResult(
            name="lock-backend",
            status=WARN,
            message=(
                f"operator-pinned backend {backend_id!r} configured "
                f"but not reachable from this host: {exc}"
            ),
            fix_hint=(
                f"Verify {url!r} is correct and the backend is up. "
                "Doctor warns instead of failing — the framework "
                "runtime will fail at first acquire if the backend "
                "is truly down."
            ),
        )

    # Redact credentials from the URL before echoing in the message /
    # detail dict — Step 9.1 security specialist Finding 9 (#60 PR 3).
    # ``redis://user:password@host:port/db`` URLs leak the credential
    # to anything that captures doctor output (CI logs, telemetry).
    # urlparse + _replace strips the userinfo segment.
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.password:
        netloc = parsed.hostname or ""
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        safe_url = parsed._replace(netloc=netloc).geturl()
    else:
        safe_url = url

    return CheckResult(
        name="lock-backend",
        status=PASS,
        message=f"{backend_id} backend reachable at {safe_url}",
        detail={"backend_id": backend_id, "url": safe_url},
    )


def check_log_backend(agent_root: Path) -> CheckResult:
    """Operator-config coherence check for the log backend (#61 PR 2).

    Validates that ``ATOMIC_AGENTS_LOG_BACKEND`` (plus
    ``ATOMIC_AGENTS_LOG_BACKEND_URL`` when non-filesystem) is correctly
    configured:

    * unset / ``filesystem`` → PASS (today's deployment shape — writes
      JSONL to ``<agent>/log/YYYY-MM/YYYY-MM-DD.jsonl`` as the
      pre-#61 code did)
    * ``sqlite`` (forward-pointer for #61 PR 3) → FAIL with the
      installation hint until PR 3 ships
    * unknown backend_id (typo) → FAIL with the known-id list, which
      includes the lazy ``sqlite`` forward-pointer so operator typos
      see the same hint they'd get from
      ``get_default_log_backend`` itself

    Mirrors ``check_lock_backend`` shape — both checks reuse the
    framework's ``get_default_log_backend`` factory internally so
    doctor's verdict and the runtime's behavior cannot diverge.
    """
    from .exceptions import BackendNotRegistered
    from .logs import get_default_log_backend, list_log_backends

    backend_id = (
        os.environ.get("ATOMIC_AGENTS_LOG_BACKEND", "filesystem").strip().lower()
    )

    if backend_id == "filesystem":
        try:
            backend = get_default_log_backend(agent_root)
            stats = backend.stats()
        except Exception as exc:
            return CheckResult(
                name="log-backend",
                status=FAIL,
                message=f"filesystem log backend stats() raised {type(exc).__name__}: {exc}",
                fix_hint=(
                    f"Check that {agent_root}/log is readable. The default "
                    "FilesystemLogBackend constructs itself at agent_root "
                    "and reads <agent>/log/YYYY-MM/YYYY-MM-DD.jsonl."
                ),
            )
        return CheckResult(
            name="log-backend",
            status=PASS,
            message=(
                f"filesystem backend ok ({stats.total_records} records, "
                f"{stats.records_this_month} this month)"
            ),
            detail={
                "backend_id": "filesystem",
                "total_records": stats.total_records,
                "records_today": stats.records_today,
                "records_this_month": stats.records_this_month,
                "size_bytes": stats.size_bytes,
            },
        )

    # ``sqlite`` is the forward-pointer name — PR 3 will register it.
    # Treat it as a known id alongside the eagerly-registered backends
    # so doctor's known-id list matches ``get_default_log_backend``'s
    # error message (same Step-11-adversarial-P0-3 mitigation).
    # ``sqlite`` is eagerly registered as of #61 PR 3; the registry's
    # own list is authoritative.
    known_ids = set(list_log_backends())
    if backend_id not in known_ids:
        return CheckResult(
            name="log-backend",
            status=FAIL,
            message=(
                f"ATOMIC_AGENTS_LOG_BACKEND={backend_id!r} is not "
                f"a known backend. Known: {sorted(known_ids)}"
            ),
            fix_hint=(
                "Set ATOMIC_AGENTS_LOG_BACKEND to one of the known "
                "ids, or unset to use the filesystem default."
            ),
        )

    # Non-filesystem id selected — construct via the factory and run a
    # lightweight stats() probe to verify the backend is reachable +
    # schema-healthy. For SQLite, this confirms the file is writable
    # and the schema is at the expected version.
    #
    # Credential safety: any exception from ``get_default_log_backend``
    # may include a URL with embedded credentials. We redact via the
    # same urlparse-based pattern ``check_lock_backend`` uses (security
    # parity — Step 9.1 security CRITICAL #3 on PR 2).
    url = os.environ.get("ATOMIC_AGENTS_LOG_BACKEND_URL")
    safe_url: str | None = None
    if url:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        if parsed.password:
            netloc = parsed.hostname or ""
            if parsed.port:
                netloc = f"{netloc}:{parsed.port}"
            safe_url = parsed._replace(netloc=netloc).geturl()
        else:
            safe_url = url

    try:
        backend = get_default_log_backend(agent_root)
    except BackendNotRegistered:
        # PR 3 registered "sqlite"; any remaining BackendNotRegistered
        # path here means the operator typed an id whose lazy resolver
        # raised. Surface the known-id list (already done above at the
        # eager-registry check) — fall through to the failure path.
        return CheckResult(
            name="log-backend",
            status=FAIL,
            message=f"log backend {backend_id!r} not registered",
            fix_hint=(
                f"The {backend_id!r} backend is reserved but its lazy "
                "resolver failed. Unset ATOMIC_AGENTS_LOG_BACKEND to "
                "use the filesystem default."
            ),
        )
    except Exception:
        # Sanitize the exception message — connection errors from
        # backend constructors commonly embed the full URL including
        # credentials. Drop the exception class name and rely on
        # fix_hint to guide the operator. The full exception is
        # available in the LOG level above DEBUG (not echoed via
        # CheckResult) for the operator who has logging access.
        return CheckResult(
            name="log-backend",
            status=FAIL,
            message=f"log backend {backend_id!r} construction failed",
            fix_hint=(
                "Check ATOMIC_AGENTS_LOG_BACKEND and "
                "ATOMIC_AGENTS_LOG_BACKEND_URL for typos. Run with "
                "DEBUG logging to see the full exception."
            ),
        )

    # Probe with stats() — verifies schema health + reachability.
    # Match check_lock_backend's WARN-on-unreachable pattern: doctor
    # never crashes on missing/unreachable optional infrastructure.
    try:
        stats = backend.stats()
    except Exception:
        # Same credential-redaction rule applies to the probe error
        # path — drop the verbatim exception message.
        return CheckResult(
            name="log-backend",
            status=WARN,
            message=(
                f"operator-pinned backend {backend_id!r} configured "
                "but stats() probe failed"
            ),
            fix_hint=(
                "Verify ATOMIC_AGENTS_LOG_BACKEND_URL is correct + "
                "the backend is reachable from this host. Doctor "
                "warns instead of failing — the framework runtime "
                "will fail at first append if the backend is "
                "truly down."
            ),
        )

    detail: dict[str, Any] = {
        "backend_id": backend_id,
        "total_records": stats.total_records,
        "records_today": stats.records_today,
        "records_this_month": stats.records_this_month,
    }
    if safe_url is not None:
        detail["url"] = safe_url
    if stats.size_bytes is not None:
        detail["size_bytes"] = stats.size_bytes
    return CheckResult(
        name="log-backend",
        status=PASS,
        message=(
            f"{backend_id} backend ok ({stats.total_records} records, "
            f"{stats.records_this_month} this month)"
        ),
        detail=detail,
    )


def check_agent_profile_backend(agents_root: Path) -> CheckResult:
    """Operator-config coherence check for the agent-profile backend (#63 PR 2).

    Validates that ``ATOMIC_AGENTS_PROFILE_BACKEND`` (plus
    ``ATOMIC_AGENTS_PROFILE_BACKEND_URL`` when non-filesystem) is
    correctly configured. Scoped at ``agents_root`` (not ``agent_root``)
    because the profile backend is the scope-flat layer that holds ALL
    agents — `list_agents()` enumerates siblings under the root.

    PASS / WARN / FAIL ladder mirrors ``check_log_backend`` (#61 PR 2):

    * unset / ``filesystem`` → PASS with capability snapshot +
      enumerated agent count
    * unknown backend_id (typo) → FAIL with the known-id list
      pulled from ``list_profile_backends()`` so doctor's verdict
      cannot diverge from the registry's actual contents
    * non-filesystem id reachable + ``capabilities()`` probe ok → PASS
      with redacted URL in detail
    * non-filesystem id construction failure → FAIL (credentials
      dropped from exception text to prevent leak in error-tracking
      services); ``capabilities()`` probe failure → WARN

    URL credential redaction follows the same urlparse + ``_replace``
    pattern as ``check_log_backend`` / ``check_lock_backend`` — strips
    password from netloc.
    """
    from .exceptions import BackendNotRegistered
    from .profile import get_default_profile_backend, list_profile_backends

    backend_id = (
        os.environ.get("ATOMIC_AGENTS_PROFILE_BACKEND", "filesystem").strip().lower()
    )

    if backend_id == "filesystem":
        try:
            backend = get_default_profile_backend(agents_root)
            caps = backend.capabilities()
            agent_count = len(backend.list_agents())
        except Exception as exc:
            return CheckResult(
                name="profile-backend",
                status=FAIL,
                message=(
                    f"filesystem profile backend probe raised "
                    f"{type(exc).__name__}: {exc}"
                ),
                fix_hint=(
                    f"Check that {agents_root} is readable. The default "
                    "FilesystemAgentProfileBackend enumerates subdirectories "
                    "under agents_root that contain persona/IDENTITY.md."
                ),
            )
        return CheckResult(
            name="profile-backend",
            status=PASS,
            message=(
                f"filesystem backend ok ({agent_count} agent"
                f"{'' if agent_count == 1 else 's'} discovered)"
            ),
            detail={
                "backend_id": "filesystem",
                "supports_save": caps.supports_save,
                "supports_clone": caps.supports_clone,
                "supports_snapshot": caps.supports_snapshot,
                "supports_subscribe": caps.supports_subscribe,
                "supports_skills": caps.supports_skills,
                "durable": caps.durable,
                "agent_count": agent_count,
            },
        )

    # Non-filesystem id selected — verify it's known to the registry
    # BEFORE invoking the factory (lazy registrations of future
    # backends — Database / Git / S3 — slot in via list_profile_backends).
    known_ids = set(list_profile_backends())
    if backend_id not in known_ids:
        return CheckResult(
            name="profile-backend",
            status=FAIL,
            message=(
                f"ATOMIC_AGENTS_PROFILE_BACKEND={backend_id!r} is not "
                f"a known backend. Known: {sorted(known_ids)}"
            ),
            fix_hint=(
                "Set ATOMIC_AGENTS_PROFILE_BACKEND to one of the known "
                "ids, or unset to use the filesystem default."
            ),
        )

    # URL credential redaction — same urlparse + _replace pattern as
    # check_log_backend / check_lock_backend. PR 1 also has a textual
    # redaction in get_default_profile_backend's BackendNotRegistered
    # message; this is the structured form for the PASS-path detail dict.
    url = os.environ.get("ATOMIC_AGENTS_PROFILE_BACKEND_URL")
    safe_url: str | None = None
    if url:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        # Redact when EITHER password OR username is present. The
        # password-only check inherited from check_lock_backend /
        # check_log_backend misses token-as-username URLs common with
        # managed services (Upstash ``redis://ghp_TOKEN@host``,
        # PlanetScale ``mysql://API_KEY@host``, Heroku-style URLs).
        # Step 9.1 security specialist finding F-S1; the same gap
        # exists in the sister checks and is tracked as a separate
        # follow-up — fixing all three together would expand PR 2's
        # scope into pre-existing code paths, so PR 2 fixes only the
        # new site (check_agent_profile_backend) and notes the
        # sister-check gap inline. Future cleanup: lift the redaction
        # helper into a shared utility and have all three checks
        # consume it.
        if parsed.password or parsed.username:
            netloc = parsed.hostname or ""
            if parsed.port:
                netloc = f"{netloc}:{parsed.port}"
            safe_url = parsed._replace(netloc=netloc).geturl()
        else:
            safe_url = url

    try:
        backend = get_default_profile_backend(agents_root)
    except BackendNotRegistered:
        return CheckResult(
            name="profile-backend",
            status=FAIL,
            message=f"profile backend {backend_id!r} not registered",
            fix_hint=(
                f"The {backend_id!r} backend is reserved but its lazy "
                "resolver failed. Unset ATOMIC_AGENTS_PROFILE_BACKEND to "
                "use the filesystem default."
            ),
        )
    except Exception:
        # Drop the verbatim exception message — connection errors from
        # backend constructors commonly embed the full URL with credentials.
        return CheckResult(
            name="profile-backend",
            status=FAIL,
            message=f"profile backend {backend_id!r} construction failed",
            fix_hint=(
                "Check ATOMIC_AGENTS_PROFILE_BACKEND and "
                "ATOMIC_AGENTS_PROFILE_BACKEND_URL for typos. Run with "
                "DEBUG logging to see the full exception."
            ),
        )

    # Probe via capabilities() + list_agents() — both lightweight,
    # verify the backend is reachable + schema-healthy. Match the
    # WARN-on-unreachable-probe pattern from check_log_backend.
    try:
        caps = backend.capabilities()
        agent_count = len(backend.list_agents())
    except Exception:
        return CheckResult(
            name="profile-backend",
            status=WARN,
            message=(
                f"operator-pinned backend {backend_id!r} configured "
                "but capabilities() / list_agents() probe failed"
            ),
            fix_hint=(
                "Verify ATOMIC_AGENTS_PROFILE_BACKEND_URL is correct + "
                "the backend is reachable from this host. Doctor warns "
                "instead of failing — the framework runtime will fail "
                "at first load_profile if the backend is truly down."
            ),
        )

    detail: dict[str, Any] = {
        "backend_id": backend_id,
        "supports_save": caps.supports_save,
        "supports_clone": caps.supports_clone,
        "supports_snapshot": caps.supports_snapshot,
        "supports_subscribe": caps.supports_subscribe,
        "supports_skills": caps.supports_skills,
        "durable": caps.durable,
        "agent_count": agent_count,
    }
    if safe_url is not None:
        detail["url"] = safe_url
    return CheckResult(
        name="profile-backend",
        status=PASS,
        message=(
            f"{backend_id} backend ok ({agent_count} agent"
            f"{'' if agent_count == 1 else 's'} discovered)"
        ),
        detail=detail,
    )


def check_tool_registry_backend(agent_root: Path) -> CheckResult:
    """Operator-config coherence check for the tool-registry backend (#64 PR 2).

    Validates that ``ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND`` is correctly
    configured. Scoped at ``agent_root`` (not ``agents_root``) — the
    filesystem reference is per-agent-rooted (``<agent>/tools/<name>.md``
    belongs to ONE agent), distinct from ``check_agent_profile_backend``
    which sits at the scope-flat ``agents_root`` layer.

    PASS / WARN / FAIL ladder mirrors ``check_agent_profile_backend``:

    * unset / ``filesystem`` → PASS with capability snapshot + tool
      count (0 when no ``tools/`` dir, which is the typical case for
      most agents — that's intentional, not a failure mode)
    * unknown backend_id (typo) → FAIL with the known-id list pulled
      from ``list_tool_registry_backends()`` so doctor's verdict cannot
      diverge from the registry's actual contents
    * non-filesystem id reachable + ``capabilities()`` probe ok → PASS
      with redacted URL in detail
    * non-filesystem id construction failure → FAIL (credentials
      dropped from exception text to prevent leak in error-tracking
      services); ``capabilities()`` probe failure → WARN

    URL credential redaction follows the same urlparse + ``_replace``
    pattern as ``check_agent_profile_backend`` / ``check_log_backend``
    / ``check_lock_backend`` (PR 2 inherits the same defense-in-depth
    + sister-check redaction gap noted in spec/22 — fixing all four
    together is tracked as a separate follow-up).
    """
    from .exceptions import BackendNotRegistered
    from .registry import (
        get_default_tool_registry_backend,
        list_tool_registry_backends,
    )

    backend_id = (
        os.environ.get("ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND", "filesystem")
        .strip()
        .lower()
    )

    if backend_id == "filesystem":
        try:
            backend = get_default_tool_registry_backend(agent_root)
            caps = backend.capabilities()
            tool_count = len(backend.list_tools())
        except Exception as exc:
            return CheckResult(
                name="tool-registry-backend",
                status=FAIL,
                message=(
                    f"filesystem tool registry backend probe raised "
                    f"{type(exc).__name__}: {exc}"
                ),
                fix_hint=(
                    f"Check that {agent_root}/tools/ is readable when present. "
                    "The default FilesystemToolRegistryBackend tolerates a "
                    "missing tools/ directory (returns empty list) — a probe "
                    "failure usually means agent_root itself is unreadable."
                ),
            )
        return CheckResult(
            name="tool-registry-backend",
            status=PASS,
            message=(
                f"filesystem backend ok ({tool_count} tool"
                f"{'' if tool_count == 1 else 's'} discovered)"
            ),
            detail={
                "backend_id": "filesystem",
                "supports_install": caps.supports_install,
                "supports_uninstall": caps.supports_uninstall,
                "supports_versioning": caps.supports_versioning,
                "supports_sandbox_validate": caps.supports_sandbox_validate,
                "supports_skills_catalog": caps.supports_skills_catalog,
                "durable": caps.durable,
                "tool_count": tool_count,
            },
        )

    # Non-filesystem id selected — verify it's known to the registry
    # BEFORE invoking the factory (lazy registrations of future
    # backends — SQLite (PR 3), PyPI, git, HTTP — slot in via
    # list_tool_registry_backends).
    known_ids = set(list_tool_registry_backends())
    if backend_id not in known_ids:
        # Redact the echoed value: if an operator accidentally pastes a
        # URL into the BACKEND env var (instead of ..._URL), it carries
        # credentials. Strip anything past ``://`` and truncate at 32
        # chars — same shape as ``registry/__init__.py:_redact_for_error_message``.
        # Sister checks (check_agent_profile_backend / check_log_backend
        # / check_lock_backend) have the same gap; fixing all four
        # together is tracked as a separate follow-up.
        safe_id: str
        if "://" in backend_id:
            safe_id = backend_id.split("://", 1)[0] + "://..."
        elif len(backend_id) > 32:
            safe_id = backend_id[:32] + "..."
        else:
            safe_id = backend_id
        return CheckResult(
            name="tool-registry-backend",
            status=FAIL,
            message=(
                f"ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND={safe_id!r} is "
                f"not a known backend. Known: {sorted(known_ids)}"
            ),
            fix_hint=(
                "Set ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND to one of the "
                "known ids, or unset to use the filesystem default."
            ),
        )

    # URL credential redaction — same urlparse + _replace pattern as
    # sister checks. PR 1 also has a textual redaction in
    # get_default_tool_registry_backend's BackendNotRegistered message;
    # this is the structured form for the PASS-path detail dict.
    url = os.environ.get("ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND_URL")
    safe_url: str | None = None
    if url:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        # Redact when EITHER password OR username is present — covers
        # token-as-username URLs common with managed services. Same
        # shape as check_agent_profile_backend's redaction.
        if parsed.password or parsed.username:
            netloc = parsed.hostname or ""
            if parsed.port:
                netloc = f"{netloc}:{parsed.port}"
            safe_url = parsed._replace(netloc=netloc).geturl()
        else:
            safe_url = url

    try:
        backend = get_default_tool_registry_backend(agent_root)
    except BackendNotRegistered:
        return CheckResult(
            name="tool-registry-backend",
            status=FAIL,
            message=f"tool registry backend {backend_id!r} not registered",
            fix_hint=(
                f"The {backend_id!r} backend is reserved but its lazy "
                "resolver failed. Unset ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND "
                "to use the filesystem default."
            ),
        )
    except Exception:
        # Drop the verbatim exception message — connection errors from
        # backend constructors commonly embed the full URL with credentials.
        return CheckResult(
            name="tool-registry-backend",
            status=FAIL,
            message=f"tool registry backend {backend_id!r} construction failed",
            fix_hint=(
                "Check ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND and "
                "ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND_URL for typos. Run with "
                "DEBUG logging to see the full exception."
            ),
        )

    # Probe via capabilities() + list_tools() — both lightweight,
    # verify the backend is reachable + schema-healthy. Match the
    # WARN-on-unreachable-probe pattern from sister checks.
    try:
        caps = backend.capabilities()
        tool_count = len(backend.list_tools())
    except Exception:
        return CheckResult(
            name="tool-registry-backend",
            status=WARN,
            message=(
                f"operator-pinned backend {backend_id!r} configured "
                "but capabilities() / list_tools() probe failed"
            ),
            fix_hint=(
                "Verify ATOMIC_AGENTS_TOOL_REGISTRY_BACKEND_URL is correct "
                "+ the backend is reachable from this host. Doctor warns "
                "instead of failing — the framework runtime will fail at "
                "first load_tool if the backend is truly down."
            ),
        )

    detail: dict[str, Any] = {
        "backend_id": backend_id,
        "supports_install": caps.supports_install,
        "supports_uninstall": caps.supports_uninstall,
        "supports_versioning": caps.supports_versioning,
        "supports_sandbox_validate": caps.supports_sandbox_validate,
        "supports_skills_catalog": caps.supports_skills_catalog,
        "durable": caps.durable,
        "tool_count": tool_count,
    }
    if safe_url is not None:
        detail["url"] = safe_url
    return CheckResult(
        name="tool-registry-backend",
        status=PASS,
        message=(
            f"{backend_id} backend ok ({tool_count} tool"
            f"{'' if tool_count == 1 else 's'} discovered)"
        ),
        detail=detail,
    )


def check_policy_backend(scope_root: Path) -> CheckResult:
    """Operator-config coherence check for the policy backend (#89 PR 2).

    Validates that ``ATOMIC_AGENTS_POLICY_BACKEND`` is correctly configured.
    Scoped at ``scope_root`` (not ``agent_root``) because the policy backend
    is fleet-scoped — ``<scope_root>/policy.md`` declares fleet-default caps,
    tool allowlists, MCP server allowlists, and model selection that apply
    across all agents under the root.

    PASS / WARN / FAIL ladder mirrors ``check_agent_profile_backend`` /
    ``check_tool_registry_backend``:

    * unset / ``filesystem`` → PASS with capability snapshot (``cache_ttl_s``
      + ``durable``) + ``policy_md_exists`` indicator.  When ``policy.md`` is
      absent, emits WARN — every agent operates in no-opinion mode; this is
      informational so the operator knows they haven't authored fleet policy yet.
    * unknown backend_id (typo in ``ATOMIC_AGENTS_POLICY_BACKEND``) → FAIL
    * non-filesystem id reachable + ``capabilities()`` probe ok → PASS
      with capability snapshot; WARN if ``policy.md`` is absent (same
      no-opinion informational)
    * non-filesystem id construction failure → FAIL (credentials dropped
      from exception text to prevent leak in error-tracking services);
      ``capabilities()`` probe failure → WARN

    URL credential redaction follows the same urlparse + ``_replace`` pattern
    as ``check_agent_profile_backend`` / ``check_tool_registry_backend`` /
    ``check_log_backend`` / ``check_lock_backend`` — strips password AND
    username from netloc (covers token-as-username URLs common with managed
    services).  Although ``ATOMIC_AGENTS_POLICY_BACKEND_URL`` is not yet
    wired in PR 1 (only ``ATOMIC_AGENTS_POLICY_BACKEND`` env var exists
    today), the redaction code path is included here for symmetry with sister
    checks and to avoid a sister-check gap follow-up being filed.
    """
    from .exceptions import BackendNotRegistered
    from .policy import PolicyError, get_default_policy_backend

    backend_id = (
        os.environ.get("ATOMIC_AGENTS_POLICY_BACKEND", "filesystem").strip().lower()
    )

    policy_md_path = scope_root / "policy.md"

    if backend_id == "filesystem":
        try:
            backend = get_default_policy_backend(scope_root)
            caps = backend.capabilities()
        except PolicyError as exc:
            return CheckResult(
                name="policy-backend",
                status=FAIL,
                message=(
                    f"filesystem policy backend probe raised "
                    f"{type(exc).__name__}: {exc}"
                ),
                fix_hint=(
                    f"Check that {scope_root} is readable. The default "
                    "FilesystemPolicyBackend resolves policy.md under scope_root."
                ),
            )
        except Exception as exc:
            return CheckResult(
                name="policy-backend",
                status=FAIL,
                message=(
                    f"filesystem policy backend probe raised "
                    f"{type(exc).__name__}: {exc}"
                ),
                fix_hint=(
                    f"Check that {scope_root} is readable. The default "
                    "FilesystemPolicyBackend resolves policy.md under scope_root."
                ),
            )
        policy_md_exists = policy_md_path.exists()
        detail: dict[str, Any] = {
            "backend_id": "filesystem",
            "cache_ttl_s": caps.cache_ttl_s,
            "durable": caps.durable,
            "policy_md_exists": policy_md_exists,
            "resolved_path": str(policy_md_path),
        }
        if not policy_md_exists:
            return CheckResult(
                name="policy-backend",
                status=WARN,
                message=(
                    "filesystem backend ok but policy.md absent — "
                    "all agents operate in no-opinion mode"
                ),
                fix_hint=(
                    f"Create {policy_md_path} to declare fleet-default cost "
                    "caps, tool allowlists, and model overrides. "
                    "See docs/spec/32-policy-backend.md."
                ),
                detail=detail,
            )
        return CheckResult(
            name="policy-backend",
            status=PASS,
            message="filesystem backend ok (policy.md found)",
            detail=detail,
        )

    # Non-filesystem id selected — verify it's known to the registry
    # BEFORE invoking the factory (lazy registrations of future
    # backends — Postgres, SaaS, git — slot in via list_policy_backends).
    from .policy import list_policy_backends

    known_ids = set(list_policy_backends())
    if backend_id not in known_ids:
        return CheckResult(
            name="policy-backend",
            status=FAIL,
            message=(
                f"ATOMIC_AGENTS_POLICY_BACKEND={backend_id!r} is not "
                f"a known backend. Known: {sorted(known_ids)}"
            ),
            fix_hint=(
                "Set ATOMIC_AGENTS_POLICY_BACKEND to one of the known "
                "ids, or unset to use the filesystem default."
            ),
        )

    # URL credential redaction — same urlparse + _replace pattern as
    # check_agent_profile_backend / check_tool_registry_backend /
    # check_log_backend / check_lock_backend. Redacts when EITHER password
    # OR username is present — covers token-as-username URLs common with
    # managed services (Upstash ``redis://ghp_TOKEN@host``, PlanetScale
    # ``mysql://API_KEY@host``, Heroku-style URLs).
    # ATOMIC_AGENTS_POLICY_BACKEND_URL is not yet wired in PR 1 (only
    # ATOMIC_AGENTS_POLICY_BACKEND exists today) — the redaction code
    # path is included for symmetry with sister checks so a future URL
    # env var already has its credential-safety handled from day 1.
    url = os.environ.get("ATOMIC_AGENTS_POLICY_BACKEND_URL")
    safe_url: str | None = None
    if url:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        # Redact when EITHER password OR username is present. The
        # password-only check inherited from check_lock_backend /
        # check_log_backend misses token-as-username URLs common with
        # managed services (Upstash ``redis://ghp_TOKEN@host``,
        # PlanetScale ``mysql://API_KEY@host``, Heroku-style URLs).
        # The sister-check gap (sister checks share this same pattern
        # but the fix was applied per-check starting from
        # check_agent_profile_backend F-S1) is tracked as a separate
        # follow-up for a shared-utility lift.
        if parsed.password or parsed.username:
            netloc = parsed.hostname or ""
            if parsed.port:
                netloc = f"{netloc}:{parsed.port}"
            safe_url = parsed._replace(netloc=netloc).geturl()
        else:
            safe_url = url

    try:
        backend = get_default_policy_backend(scope_root)
    except BackendNotRegistered:
        return CheckResult(
            name="policy-backend",
            status=FAIL,
            message=f"policy backend {backend_id!r} not registered",
            fix_hint=(
                f"The {backend_id!r} backend is reserved but its lazy "
                "resolver failed. Unset ATOMIC_AGENTS_POLICY_BACKEND to "
                "use the filesystem default."
            ),
        )
    except Exception:
        # Drop the verbatim exception message — connection errors from
        # backend constructors commonly embed the full URL with credentials.
        return CheckResult(
            name="policy-backend",
            status=FAIL,
            message=f"policy backend {backend_id!r} construction failed",
            fix_hint=(
                "Check ATOMIC_AGENTS_POLICY_BACKEND and "
                "ATOMIC_AGENTS_POLICY_BACKEND_URL for typos. Run with "
                "DEBUG logging to see the full exception."
            ),
        )

    # Probe via capabilities() — lightweight, verifies the backend is
    # reachable + schema-healthy. PolicyBackend has no list_X method, so
    # we probe only capabilities(). Match the WARN-on-unreachable-probe
    # pattern from sister checks.
    try:
        caps = backend.capabilities()
    except Exception:
        return CheckResult(
            name="policy-backend",
            status=WARN,
            message=(
                f"operator-pinned backend {backend_id!r} configured "
                "but capabilities() probe failed"
            ),
            fix_hint=(
                "Verify ATOMIC_AGENTS_POLICY_BACKEND_URL is correct + "
                "the backend is reachable from this host. Doctor warns "
                "instead of failing — the framework runtime will fail "
                "at first policy resolution if the backend is truly down."
            ),
        )

    policy_md_exists = policy_md_path.exists()
    detail = {
        "backend_id": backend_id,
        "cache_ttl_s": caps.cache_ttl_s,
        "durable": caps.durable,
        "policy_md_exists": policy_md_exists,
        "resolved_path": str(policy_md_path),
    }
    if safe_url is not None:
        detail["url"] = safe_url

    if not policy_md_exists:
        return CheckResult(
            name="policy-backend",
            status=WARN,
            message=(
                f"{backend_id} backend ok but policy.md absent — "
                "all agents operate in no-opinion mode"
            ),
            fix_hint=(
                f"Create {policy_md_path} to declare fleet-default cost "
                "caps, tool allowlists, and model overrides. "
                "See docs/spec/32-policy-backend.md."
            ),
            detail=detail,
        )
    return CheckResult(
        name="policy-backend",
        status=PASS,
        message=f"{backend_id} backend ok (policy.md found)",
        detail=detail,
    )


def check_memory_backend(agent_root: Path) -> CheckResult:
    """FilesystemBackend resolves and stats() returns successfully."""
    memory_dir = agent_root / "memory"
    if not memory_dir.exists():
        return CheckResult(
            name="memory-backend",
            status=FAIL,
            message=f"memory/ directory missing at {memory_dir}",
            fix_hint=f"Create it: mkdir -p {memory_dir} && touch {memory_dir}/INDEX.md",
        )
    try:
        from .memory.filesystem import FilesystemBackend

        backend = FilesystemBackend(agent_root, "memory")
        stats = backend.stats()
    except Exception as e:
        return CheckResult(
            name="memory-backend",
            status=FAIL,
            message=f"backend stats() raised {type(e).__name__}: {e}",
            fix_hint=(
                "Check that memory/ is readable and INDEX.md is well-formed. "
                "See docs/spec/02-atomic-memory.md."
            ),
        )
    return CheckResult(
        name="memory-backend",
        status=PASS,
        message=f"FilesystemBackend ok ({stats.total_notes} notes)",
        detail={
            "total_notes": stats.total_notes,
            "by_type": stats.by_type,
            "live_bytes": stats.live_bytes,
        },
    )


def check_bundle_cache_writable() -> CheckResult:
    """The bundle cache directory exists / can be created and is writable.

    Per spec/26: the ``atomic-agents bundle`` command writes pre-rendered
    cascades to ``$ATOMIC_AGENTS_CACHE_DIR`` (default
    ``~/.cache/atomic-agents/bundles``). Skill-mode invocations need this dir
    to be writable; this check probes that without touching agent state.
    """
    from . import bundle as bundle_mod

    cache_dir = bundle_mod.default_cache_dir()
    source = (
        f"ATOMIC_AGENTS_CACHE_DIR={os.environ['ATOMIC_AGENTS_CACHE_DIR']}"
        if os.environ.get("ATOMIC_AGENTS_CACHE_DIR")
        else "default ~/.cache/atomic-agents/bundles"
    )

    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return CheckResult(
            name="bundle-cache",
            status=FAIL,
            message=f"cannot create bundle cache dir {cache_dir}: {type(e).__name__}: {e}",
            fix_hint=(
                f"Create it manually: mkdir -p {cache_dir}\n"
                f"  Or set ATOMIC_AGENTS_CACHE_DIR to a writable directory."
            ),
            detail={"path": str(cache_dir), "source": source},
        )

    # Probe write access via a temp file in the cache dir itself.
    probe = cache_dir / ".doctor-probe.tmp"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        return CheckResult(
            name="bundle-cache",
            status=FAIL,
            message=f"bundle cache dir not writable: {cache_dir} ({e})",
            fix_hint=(
                f"Fix permissions: chmod u+w {cache_dir}\n"
                f"  Or set ATOMIC_AGENTS_CACHE_DIR to a writable directory."
            ),
            detail={"path": str(cache_dir), "source": source},
        )

    return CheckResult(
        name="bundle-cache",
        status=PASS,
        message=f"bundle cache dir ok at {cache_dir} ({source})",
        detail={"path": str(cache_dir), "source": source},
    )


def check_write_paths(
    tools_data: dict, *, agent_root: Path | None = None
) -> CheckResult:
    """Each tools.md write_paths entry exists and is writable.

    Also verifies that the agent's memory directory falls inside at least
    one write_path and not inside any read_only_path. FilesystemBackend
    enforces both at write_note() time, so a captures write would otherwise
    fail at runtime after the agent has already spent tokens on the response.

    A non-existent or unwritable write_path will fail the first time the
    agent tries to write a capture there. Better to fail at install time.
    """
    paths = tools_data.get("write_paths", [])
    read_only = tools_data.get("read_only_paths", [])
    if not paths:
        # No write_paths is a hard fail for an agent-scoped check —
        # FilesystemBackend.write_note() raises WritePathViolation on every
        # capture write when the policy's write_paths list is empty. For
        # tools-only callers (no agent_root), empty == "n/a" and we skip.
        if agent_root is None:
            return CheckResult(
                name="write-paths",
                status=SKIP,
                message="tools.md declares no write_paths",
            )
        return CheckResult(
            name="write-paths",
            status=FAIL,
            message="tools.md declares no write_paths; every capture write would be rejected",
            fix_hint=(
                "Add a `## Write paths` section to tools.md listing at least "
                "the agent's memory/ directory. See docs/spec/01-anatomy.md "
                "for the format."
            ),
        )

    failures: list[tuple[str, str]] = []
    for raw in paths:
        p = Path(str(raw)).expanduser()
        if not p.exists():
            failures.append((str(p), "does not exist"))
            continue
        if not os.access(p, os.W_OK):
            failures.append((str(p), "not writable"))

    # Memory-target check: agent_root/memory must (1) be inside a write_path,
    # (2) not be inside a read_only_path, AND (3) actually be writable on
    # disk. The third condition catches the case where write_paths contains
    # a broad parent that's writable but memory/ itself is chmodded read-only.
    # We compare resolved paths so .. and symlinks don't sneak past.
    if agent_root is not None:
        memory_dir = (agent_root / "memory").resolve()
        write_resolved = [Path(str(p)).expanduser().resolve() for p in paths]
        readonly_resolved = [Path(str(p)).expanduser().resolve() for p in read_only]

        if not any(_is_relative_to(memory_dir, w) for w in write_resolved):
            failures.append(
                (
                    str(memory_dir),
                    "agent memory dir is not inside any write_path "
                    "(captures would fail at runtime)",
                )
            )
        elif any(_is_relative_to(memory_dir, ro) for ro in readonly_resolved):
            failures.append(
                (
                    str(memory_dir),
                    "agent memory dir is inside a read_only_path "
                    "(captures would be rejected)",
                )
            )
        elif memory_dir.exists() and not os.access(memory_dir, os.W_OK):
            failures.append(
                (
                    str(memory_dir),
                    "agent memory dir exists but is not writable "
                    "(captures would fail with PermissionError)",
                )
            )

    if failures:
        rendered = "; ".join(f"{p} ({why})" for p, why in failures)
        return CheckResult(
            name="write-paths",
            status=FAIL,
            message=f"write_paths invalid: {rendered}",
            fix_hint=(
                "Create the missing directories or fix permissions. "
                "Every path in tools.md write_paths must exist and be "
                "writable, and the agent's memory/ directory must be "
                "covered by a write_path and not by a read_only_path."
            ),
            detail={"failures": [{"path": p, "reason": w} for p, w in failures]},
        )
    return CheckResult(
        name="write-paths",
        status=PASS,
        message=f"all {len(paths)} write_paths exist and are writable",
        detail={"count": len(paths)},
    )


def _is_relative_to(child: Path, parent: Path) -> bool:
    """Path.is_relative_to landed in 3.9; Python 3.11+ has it. Wrapper for clarity."""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


# ──────────────────────────────────────────────────────────────────
# Output rendering


def render_human(results: list[CheckResult]) -> str:
    """Aligned text output suitable for terminal display."""
    if not results:
        return "(no checks run)\n"

    name_width = max(len(r.name) for r in results)
    badge = {PASS: "[ OK ]", FAIL: "[FAIL]", SKIP: "[skip]", WARN: "[warn]"}

    lines: list[str] = []
    for r in results:
        lines.append(f"{badge[r.status]}  {r.name.ljust(name_width)}  {r.message}")
        if r.fix_hint and r.status == FAIL:
            for hint_line in r.fix_hint.splitlines():
                lines.append(f"           {hint_line}")

    summary = _summarise(results)
    lines.append("")
    lines.append(summary)
    return "\n".join(lines) + "\n"


def render_json(results: list[CheckResult]) -> str:
    """JSON output for scripting / liveness probes."""
    payload = {
        "results": [asdict(r) for r in results],
        "summary": {
            "passed": sum(1 for r in results if r.status == PASS),
            "failed": sum(1 for r in results if r.status == FAIL),
            "skipped": sum(1 for r in results if r.status == SKIP),
            "all_ok": not any(r.failed for r in results),
        },
    }
    return json.dumps(payload, indent=2, default=str) + "\n"


def _summarise(results: list[CheckResult]) -> str:
    passed = sum(1 for r in results if r.status == PASS)
    failed = sum(1 for r in results if r.status == FAIL)
    skipped = sum(1 for r in results if r.status == SKIP)
    if failed:
        return f"FAIL — {failed} failed, {passed} passed, {skipped} skipped"
    return f"OK — {passed} passed, {skipped} skipped"


def overall_exit_code(results: list[CheckResult]) -> int:
    """0 if no failures, 1 otherwise."""
    return 1 if any(r.failed for r in results) else 0
