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
    memory-backend-config  ATOMIC_AGENTS_MEMORY_BACKEND coherence check (known
                            id; for non-default ids, also confirms the backend
                            constructs. filesystem needs no extras).
    memory-backend  Operator-configured MemoryBackend resolves and stats() returns.
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
from dataclasses import asdict, dataclass, field, replace
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
    # Vertex AI — ADC auth, not a string API key. check_provider_keys routes
    # vertex-gemini to check_vertex_credentials() rather than _get_key().
    # sdk_module="google.genai" triggers the [vertex] extra import check first.
    "vertex-gemini": (
        None,  # no Keychain entry — ADC, not a key secret
        [],  # no env vars for the key itself (GOOGLE_CLOUD_PROJECT is project config)
        None,  # no keys.json entry
        "google.genai",  # [vertex] optional extra
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
        # (lock-backend → locks → log-backend → profile-backend →
        # tool-registry-backend → mandate-backend → policy-backend →
        # persona-backend → corpus-backend →
        # mcp-server-registry-backend → secret-backend →
        # memory-backend-config → memory-backend) so contributors adding
        # a scope-level backend check see the SKIP enumeration mirror reality.
        #
        # Note: ``secret-backend`` is deployment-scoped (check_secret_backend
        # takes no agent_root), but it is grouped here for output consistency
        # with the other backend checks.  An operator running without --agent
        # sees a uniform "all backend checks require --agent" summary rather
        # than a mix of PASS/FAIL and SKIP entries.  The agent-free way to
        # verify the secret backend is ``atomic-agents secrets validate``; an
        # explicit no-agent/--deployment doctor mode is tracked in #371.
        for n in (
            "vault",
            "provider-keys",
            "model",
            "mcp",
            "lock-backend",
            "locks",
            "log-backend",
            "profile-backend",
            "tool-registry-backend",
            "mandate-backend",
            "policy-backend",
            "persona-backend",
            "corpus-backend",
            "mcp-server-registry-backend",
            "secret-backend",
            "goal-backend",
            "outcome-backend",
            "journal-backend",
            "queue-backend",
            "memory-backend-config",
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
    results.append(check_mandate_backend(resolved_root))
    # Pass cascade so check_policy_backend can warn when the backend is
    # scoped to agents_root instead of cascade.project_root (fix #236).
    results.append(check_policy_backend(resolved_root, cascade=cascade))
    results.append(check_persona_backend(resolved_root))
    results.append(check_corpus_backend(agent_root))
    results.append(check_mcp_server_registry_backend(agent_root))
    results.append(check_secret_backend())
    results.append(check_goal_backend(agent_root))
    results.append(check_outcome_backend(agent_root))
    results.append(check_journal_backend(agent_root))
    results.append(check_queue_backend(agent_root))
    # memory-backend-config (coherence) runs before memory-backend (liveness),
    # mirroring the check_lock_backend → check_locks ordering (#60 PR 3).
    results.append(check_memory_backend_config(agent_root))
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
        claude-*           → anthropic
        gpt-*              → openai
        moonshot/*         → moonshot
        vertex/gemini-*    → vertex-gemini

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
    # vertex/gemini-* → vertex-gemini (scoped to gemini- to avoid collision
    # with future vertex/claude-* which will be a separate backend)
    if model_id.startswith("vertex/gemini-"):
        return "vertex-gemini"
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
            # Map sdk_module import name → pyproject.toml optional-extra name
            _sdk_to_extra = {"openai": "openai", "google.genai": "vertex"}
            # Map sdk_module import name → PyPI distribution name (the import
            # path and the pip name diverge for google.genai → google-genai;
            # copy-pasting the import path into `pip install` 404s).
            _sdk_to_pip = {"openai": "openai", "google.genai": "google-genai"}
            extra = _sdk_to_extra.get(sdk_module, sdk_module)
            pip_name = _sdk_to_pip.get(sdk_module, sdk_module)
            return CheckResult(
                name=f"provider-keys[{provider}]",
                status=FAIL,
                message=f"{provider} requires the {sdk_module!r} package, which is not installed",
                fix_hint=(
                    f"Install the optional extra: uv add 'atomic-agents-stack[{extra}]'\n"
                    f"  Or: pip install {pip_name}"
                ),
                detail={
                    "provider": provider,
                    "missing_sdk": sdk_module,
                    "underlying_error": str(e),
                },
            )

    # Vertex AI uses ADC (Application Default Credentials) rather than an API
    # key string. Route to the dedicated credential check instead of _get_key().
    if provider == "vertex-gemini":
        return check_vertex_credentials()

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


def check_vertex_credentials() -> CheckResult:
    """Verify that Vertex AI Application Default Credentials (ADC) are usable.

    Closes the false-PASS gap (credentials object != usable credentials):
    calls ``credentials.refresh(Request())`` to mint an access token — a
    metadata-server round-trip that proves ADC actually works without making
    a billable LLM generation call.

    This check is intentionally NOT a generate_content() or count_tokens()
    call — those are billable LLM operations and doctor must never trigger
    unguarded LLM cost (CLAUDE.md rule #4: every code path that calls an
    LLM has a cost gate; doctor runs outside agent.call() and has no gate).

    WARN (not FAIL) when GOOGLE_CLOUD_PROJECT is absent: on Cloud Run / GKE
    the project is resolved from the metadata server automatically. The absence
    of the env var does not mean ADC is broken on those platforms.
    """
    # Step 1: SDK import check (google-genai = [vertex] extra)
    try:
        import google.genai  # noqa: F401 — confirms [vertex] extra installed
    except ImportError as e:
        return CheckResult(
            name="provider-keys[vertex-gemini]",
            status=FAIL,
            message="google-genai SDK not installed (required for vertex-gemini)",
            fix_hint=(
                "Install the [vertex] extra:\n"
                "  uv add 'atomic-agents-stack[vertex]'\n"
                "  Or: pip install google-genai"
            ),
            detail={"missing_sdk": "google.genai", "underlying_error": str(e)},
        )

    # Step 2: ADC resolution — does a credentials object exist at all?
    # Import submodules into local names to avoid attribute-lookup issues when
    # sys.modules is patched in tests (google.auth vs google module attribute).
    try:
        import google.auth as _gauth
        import google.auth.exceptions as _gauth_exc
        import google.auth.transport.requests as _gauth_requests
    except ImportError as e:
        return CheckResult(
            name="provider-keys[vertex-gemini]",
            status=FAIL,
            message=f"google-auth not installed: {e}",
            fix_hint=(
                "Install the [vertex] extra:\n"
                "  uv add 'atomic-agents-stack[vertex]'\n"
                "  Or: pip install google-auth google-genai"
            ),
            detail={"underlying_error": str(e)},
        )

    try:
        project = os.environ.get("GOOGLE_CLOUD_PROJECT")
        credentials, detected_project = _gauth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
    except _gauth_exc.DefaultCredentialsError as e:
        return CheckResult(
            name="provider-keys[vertex-gemini]",
            status=FAIL,
            message="Vertex AI ADC credentials not found",
            fix_hint=(
                "Run one of:\n"
                "  gcloud auth application-default login    (local dev)\n"
                "  Set GOOGLE_APPLICATION_CREDENTIALS to a service account key file\n"
                "  Deploy to Cloud Run / GKE with a service account attached\n"
                "Also set: export GOOGLE_CLOUD_PROJECT='<your-gcp-project-id>'"
            ),
            detail={"underlying_error": str(e)},
        )
    except Exception as e:
        return CheckResult(
            name="provider-keys[vertex-gemini]",
            status=FAIL,
            message=f"Vertex AI ADC resolution failed: {type(e).__name__}: {e}",
            fix_hint=(
                "Run: gcloud auth application-default login\n"
                "And set: export GOOGLE_CLOUD_PROJECT='<your-gcp-project-id>'"
            ),
            detail={"underlying_error": str(e)},
        )

    # Step 3: Token mint — proves the credentials object is actually usable,
    # not just that one was constructed. Uses credentials.refresh() which hits
    # the OAuth metadata server (not a Vertex generation endpoint — not billable).
    try:
        request = _gauth_requests.Request()
        credentials.refresh(request)
    except _gauth_exc.TransportError as e:
        return CheckResult(
            name="provider-keys[vertex-gemini]",
            status=FAIL,
            message=f"Vertex AI ADC token refresh failed (network error): {e}",
            fix_hint=(
                "Check network connectivity to Google's OAuth endpoint.\n"
                "On GCE/Cloud Run this is automatic; on dev machines ensure\n"
                "you have run: gcloud auth application-default login"
            ),
            detail={"underlying_error": str(e)},
        )
    except Exception as e:
        return CheckResult(
            name="provider-keys[vertex-gemini]",
            status=FAIL,
            message=f"Vertex AI ADC token refresh failed: {type(e).__name__}: {e}",
            fix_hint=(
                "Re-authenticate: gcloud auth application-default login\n"
                "Or check that your service account key file is valid."
            ),
            detail={"underlying_error": str(e)},
        )

    # Step 4: Project env var check — WARN only (not FAIL) because Cloud Run /
    # GKE auto-resolves the project from the metadata server.
    effective_project = project or detected_project
    detail: dict = {
        "provider": "vertex-gemini",
        "project": effective_project,
        "token_valid": True,
    }
    if not project:
        return CheckResult(
            name="provider-keys[vertex-gemini]",
            status=WARN,
            message=(
                "Vertex AI ADC token minted successfully, but GOOGLE_CLOUD_PROJECT "
                "env var is not set"
            ),
            fix_hint=(
                "On dev machines, set: export GOOGLE_CLOUD_PROJECT='<your-gcp-project-id>'\n"
                "On Cloud Run / GKE, the project is resolved from the metadata server "
                "automatically — this warning can be ignored in those environments."
            ),
            detail=detail,
        )

    return CheckResult(
        name="provider-keys[vertex-gemini]",
        status=PASS,
        message=f"Vertex AI ADC credentials valid; project={effective_project!r}",
        detail=detail,
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
        # Provider-specific fix_hint: vertex/* model ids get ADC instructions,
        # not API-key or PRICING-list instructions.
        if default_model.startswith("vertex/"):
            fix = (
                f"The model {default_model!r} is not in the pricing table. "
                "Add it to atomic_agents/_costs.py PRICING under the 'vertex/' prefix "
                "with current rates from https://cloud.google.com/vertex-ai/pricing. "
                "Known Vertex Gemini entries: "
                + ", ".join(
                    k for k in sorted(PRICING.keys()) if k.startswith("vertex/")
                )
            )
        else:
            fix = (
                f"Use one of {sorted(PRICING.keys())}, or add {default_model!r} "
                "to atomic_agents/_costs.py PRICING with current rates."
            )
        return CheckResult(
            name="model",
            status=FAIL,
            message=f"default_model {default_model!r} is not in the pricing table",
            fix_hint=fix,
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

    # ``postgres`` is lazy-resolved (never eagerly registered) so it won't
    # appear in list_log_backends() at startup. Include it as a forward-
    # pointer so an operator who types ``ATOMIC_AGENTS_LOG_BACKEND=postgre``
    # sees ``postgres`` in doctor's Known list — same Step-11-adversarial-
    # P0-3 mitigation that fixed the locks arc (the known_ids union with the
    # lazy ``redis`` id in ``check_lock_backend``).
    known_ids = set(list_log_backends()) | {"postgres"}
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
        # "sqlite" is eagerly registered; a BackendNotRegistered here means
        # a registered-but-lazy resolver raised after passing the known-id
        # check above (e.g. a third-party backend whose factory raised).
        # The 'postgres' branch in get_default_log_backend never raises
        # BackendNotRegistered — it raises ValueError (missing URL) or
        # returns a backend — so 'postgres' is not an example of this path.
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
    # Always close the backend in a finally block: no-op for filesystem/
    # sqlite (no close() method), releases the psycopg connection for
    # postgres (per postgres.py docstring: operators MUST call close() in
    # teardown to release server-side connections).
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
    finally:
        if hasattr(backend, "close"):
            try:
                backend.close()
            except Exception:
                pass

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


def check_mandate_backend(scope_root: Path) -> CheckResult:
    """Operator-config coherence check for the mandate backend (#124 PR 2).

    Validates that ``ATOMIC_AGENTS_MANDATE_BACKEND`` (plus
    ``ATOMIC_AGENTS_MANDATE_BACKEND_URL`` when non-filesystem) is
    correctly configured. Scoped at ``scope_root`` (not ``agent_root``)
    because the mandate backend is two-tier scope-aware — project-level
    ``<scope_root>/mandates.md`` plus per-agent ``<scope_root>/<agent>/mandates.md``.
    The doctor check verifies the backend constructs and probes cleanly at
    scope_root; the actual two-tier descriptor resolution at runtime is the
    backend's job via the ``scope`` parameter on ``list_mandates``.

    The check passes ``scope="project:doctor"`` as the lightweight probe
    so it inspects project-root mandates only and never recurses into
    per-agent dirs (per ``atomic_agents/mandate/filesystem.py::_mandates_path``
    — project-kind scope discards the name component and resolves to
    ``<scope_root>/mandates.md``).

    PASS / WARN / FAIL ladder mirrors ``check_policy_backend`` /
    ``check_persona_backend``:

    * unset / ``filesystem`` → PASS with capability snapshot
      (``supports_revocation``, ``supports_external_state_change_notification``,
      ``durable``, ``supports_crash_recovery``) + ``mandate_count`` +
      ``mandates_md_exists`` indicator. When ``mandates.md`` is absent at
      ``scope_root``, emits WARN — agents under this scope have no
      operator-granted authorities; this is informational so the operator
      knows they haven't authored mandates yet (mirrors Policy's
      ``policy_md_exists`` no-opinion WARN).
    * unknown backend_id (typo in ``ATOMIC_AGENTS_MANDATE_BACKEND``) → FAIL
      with the echoed env value redacted at ``://`` to prevent credential
      leak if an operator pasted a URL into the id env var by mistake.
    * non-filesystem id reachable + ``capabilities()`` / ``list_mandates()``
      probe ok → PASS with capability snapshot + redacted URL in detail.
    * non-filesystem id construction failure → FAIL (credentials dropped
      from exception text to prevent leak in error-tracking services);
      ``capabilities()`` / ``list_mandates()`` probe failure → WARN.

    URL credential redaction follows the same urlparse + ``_replace``
    pattern as ``check_policy_backend`` / ``check_persona_backend`` —
    strips password AND username from netloc (covers token-as-username
    URLs common with managed services).
    """
    from .exceptions import BackendNotRegistered
    from .mandate import (
        get_default_mandate_backend,
        list_mandate_backends,
    )

    backend_id = (
        os.environ.get("ATOMIC_AGENTS_MANDATE_BACKEND", "filesystem").strip().lower()
    )

    mandates_md_path = scope_root / "mandates.md"
    # Project-root probe scope — name component is informational for
    # project kind; the backend reads <scope_root>/mandates.md regardless
    # of the trailing name (spec/29 + filesystem._mandates_path).
    probe_scope = "project:doctor"

    if backend_id == "filesystem":
        try:
            backend = get_default_mandate_backend(scope_root)
            caps = backend.capabilities()
            mandates = backend.list_mandates(probe_scope)
        except Exception as exc:
            return CheckResult(
                name="mandate-backend",
                status=FAIL,
                message=(
                    f"filesystem mandate backend probe raised "
                    f"{type(exc).__name__}: {exc}"
                ),
                fix_hint=(
                    f"Check that {scope_root} is readable and any "
                    f"existing {mandates_md_path} parses cleanly. "
                    "See docs/spec/29-mandates.md."
                ),
            )
        mandates_md_exists = mandates_md_path.exists()
        mandate_count = len(mandates)
        detail: dict[str, Any] = {
            "backend_id": "filesystem",
            "supports_revocation": caps.supports_revocation,
            "supports_external_state_change_notification": (
                caps.supports_external_state_change_notification
            ),
            "durable": caps.durable,
            "supports_crash_recovery": caps.supports_crash_recovery,
            "mandate_count": mandate_count,
            "mandates_md_exists": mandates_md_exists,
            "resolved_path": str(mandates_md_path),
        }
        if not mandates_md_exists:
            return CheckResult(
                name="mandate-backend",
                status=WARN,
                message=(
                    "filesystem backend ok but mandates.md absent — "
                    "no operator-granted authorities at this scope"
                ),
                fix_hint=(
                    f"Create {mandates_md_path} to declare operator-granted "
                    "scoped authorities (durable, revocable). "
                    "See docs/spec/29-mandates.md."
                ),
                detail=detail,
            )
        return CheckResult(
            name="mandate-backend",
            status=PASS,
            message=(
                f"filesystem backend ok ({mandate_count} mandate"
                f"{'' if mandate_count == 1 else 's'} discovered)"
            ),
            detail=detail,
        )

    # Non-filesystem id selected — verify it's known to the registry
    # BEFORE invoking the factory (lazy registrations of future
    # backends — Postgres, SaaS, Slack-bot — slot in via list_mandate_backends).
    known_ids = set(list_mandate_backends())
    if backend_id not in known_ids:
        # Redact the echoed value: if an operator accidentally pastes a
        # credential-bearing URL into ATOMIC_AGENTS_MANDATE_BACKEND
        # (instead of ATOMIC_AGENTS_MANDATE_BACKEND_URL), it carries a
        # password. Strip anything past "://" and truncate at 32 chars —
        # same shape as the redaction in check_persona_backend /
        # check_tool_registry_backend.
        from .mandate.backend import _redact_for_error_message as _redact_mid

        safe_id = _redact_mid(backend_id)
        return CheckResult(
            name="mandate-backend",
            status=FAIL,
            message=(
                f"ATOMIC_AGENTS_MANDATE_BACKEND={safe_id!r} is not "
                f"a known backend. Known: {sorted(known_ids)}"
            ),
            fix_hint=(
                "Set ATOMIC_AGENTS_MANDATE_BACKEND to one of the known "
                "ids, or unset to use the filesystem default."
            ),
        )

    # URL credential redaction — same urlparse + _replace pattern as
    # check_policy_backend / check_persona_backend / check_log_backend.
    # Redacts when EITHER password OR username is present — covers
    # token-as-username URLs common with managed services.
    url = os.environ.get("ATOMIC_AGENTS_MANDATE_BACKEND_URL")
    safe_url: str | None = None
    if url:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        if parsed.password or parsed.username:
            netloc = parsed.hostname or ""
            if parsed.port:
                netloc = f"{netloc}:{parsed.port}"
            safe_url = parsed._replace(netloc=netloc).geturl()
        else:
            safe_url = url

    try:
        backend = get_default_mandate_backend(scope_root)
    except BackendNotRegistered:
        return CheckResult(
            name="mandate-backend",
            status=FAIL,
            message=f"mandate backend {backend_id!r} not registered",
            fix_hint=(
                f"The {backend_id!r} backend is reserved but its lazy "
                "resolver failed. Unset ATOMIC_AGENTS_MANDATE_BACKEND to "
                "use the filesystem default."
            ),
        )
    except Exception:
        # Drop the verbatim exception message — connection errors from
        # backend constructors commonly embed the full URL with credentials.
        return CheckResult(
            name="mandate-backend",
            status=FAIL,
            message=f"mandate backend {backend_id!r} construction failed",
            fix_hint=(
                "Check ATOMIC_AGENTS_MANDATE_BACKEND and "
                "ATOMIC_AGENTS_MANDATE_BACKEND_URL for typos. Run with "
                "DEBUG logging to see the full exception."
            ),
        )

    # Probe via capabilities() + list_mandates() — both lightweight,
    # verify the backend is reachable + schema-healthy. Match the
    # WARN-on-unreachable-probe pattern from sister checks.
    try:
        caps = backend.capabilities()
        mandates = backend.list_mandates(probe_scope)
    except Exception:
        return CheckResult(
            name="mandate-backend",
            status=WARN,
            message=(
                f"operator-pinned backend {backend_id!r} configured "
                "but capabilities() / list_mandates() probe failed"
            ),
            fix_hint=(
                "Verify ATOMIC_AGENTS_MANDATE_BACKEND_URL is correct + "
                "the backend is reachable from this host. Doctor warns "
                "instead of failing — the framework runtime will fail "
                "at first MandateCheck if the backend is truly down."
            ),
        )

    mandates_md_exists = mandates_md_path.exists()
    mandate_count = len(mandates)
    detail = {
        "backend_id": backend_id,
        "supports_revocation": caps.supports_revocation,
        "supports_external_state_change_notification": (
            caps.supports_external_state_change_notification
        ),
        "durable": caps.durable,
        "supports_crash_recovery": caps.supports_crash_recovery,
        "mandate_count": mandate_count,
        "mandates_md_exists": mandates_md_exists,
        "resolved_path": str(mandates_md_path),
    }
    if safe_url is not None:
        detail["url"] = safe_url

    if not mandates_md_exists:
        return CheckResult(
            name="mandate-backend",
            status=WARN,
            message=(
                f"{backend_id} backend ok but mandates.md absent — "
                "no operator-granted authorities at this scope"
            ),
            fix_hint=(
                f"Create {mandates_md_path} to declare operator-granted "
                "scoped authorities (durable, revocable). "
                "See docs/spec/29-mandates.md."
            ),
            detail=detail,
        )
    return CheckResult(
        name="mandate-backend",
        status=PASS,
        message=(
            f"{backend_id} backend ok ({mandate_count} mandate"
            f"{'' if mandate_count == 1 else 's'} discovered)"
        ),
        detail=detail,
    )


def check_policy_backend(scope_root: Path, *, cascade=None) -> CheckResult:
    """Operator-config coherence check for the policy backend (#89 PR 2 + 3a).

    Validates that ``ATOMIC_AGENTS_POLICY_BACKEND`` is correctly configured.
    Scoped at ``scope_root`` (not ``agent_root``) because the policy backend
    is fleet-scoped — ``<scope_root>/policy.md`` declares fleet-default caps,
    tool allowlists, MCP server allowlists, and model selection that apply
    across all agents under the root.

    When ``cascade`` is supplied (a ``_cascade.CascadePaths`` or any object
    with a ``.project_root`` attribute), the check also warns when a filesystem
    backend is scoped to ``agents_root`` instead of ``cascade.project_root``
    because in cascade layouts, ``policy.md`` lives at the project root and the
    runtime re-resolves the backend there (fix #236 PR 3a).

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
        # Cascade-scope mismatch warning (#236 fix, PR 3a).
        # When the agent is in a cascade layout, policy.md should live at
        # cascade.project_root (the project-level dir), not at agents_root.
        # The runtime auto-corrects this at agent construction time; the doctor
        # surfaces it so operators who passed an explicit FilesystemPolicyBackend
        # scoped to agents_root instead of cascade.project_root see a warning
        # before production traffic hits the wrong scope.
        # Only emits for filesystem backends — SaaS/Postgres backends manage
        # their own scope resolution without a project_root attribute.
        # F7 fix (PR 3a Round 1 P3): use isinstance(FilesystemPolicyBackend)
        # instead of hasattr(_project_root). Third-party PolicyBackend impls
        # may happen to have a _project_root attribute that isn't a Path
        # (e.g., a tenant ID); the hasattr guard alone produces spurious
        # warnings + breaks Protocol abstraction (private-attr access).
        from .policy.filesystem import FilesystemPolicyBackend

        if (
            cascade is not None
            and isinstance(backend, FilesystemPolicyBackend)
            and Path(backend._project_root).resolve()
            != Path(cascade.project_root).resolve()
        ):
            detail["cascade_scope_mismatch"] = True
            detail["cascade_project_root"] = str(cascade.project_root)
            return CheckResult(
                name="policy-backend",
                status=WARN,
                message=(
                    "filesystem policy backend is scoped to agents_root "
                    f"({scope_root}) but this is a cascade layout — "
                    f"policy.md should live at cascade.project_root "
                    f"({cascade.project_root})"
                ),
                fix_hint=(
                    "The runtime auto-corrects this (AtomicAgent re-resolves "
                    "the default backend to cascade.project_root after cascade "
                    "detection). If you passed an explicit policy_backend= kwarg, "
                    f"scope it to {cascade.project_root} instead of {scope_root}."
                ),
                detail=detail,
            )

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


def check_persona_backend(scope_root: Path) -> CheckResult:
    """Operator-config coherence check for the persona backend (#62 PR 2).

    Validates that ``ATOMIC_AGENTS_PERSONA_BACKEND`` (plus
    ``ATOMIC_AGENTS_PERSONA_BACKEND_URL`` when non-filesystem) is
    correctly configured.  Scoped at ``scope_root`` (not ``agent_root``)
    because the persona backend is scope-flat — ``list_personas()``
    enumerates the shared ``.personas/`` directory under the root.

    PASS / WARN / FAIL ladder mirrors ``check_agent_profile_backend`` /
    ``check_policy_backend``:

    * unset / ``filesystem`` → PASS with capability snapshot +
      enumerated persona count
    * unknown backend_id (typo in ``ATOMIC_AGENTS_PERSONA_BACKEND``) → FAIL
      with the known-id list pulled from ``list_persona_backends()``
    * non-filesystem id reachable + ``capabilities()`` probe ok → PASS
      with capability snapshot + redacted URL in detail
    * non-filesystem id construction failure → FAIL (credentials dropped
      from exception text to prevent leak in error-tracking services);
      ``capabilities()`` / ``list_personas()`` probe failure → WARN

    URL credential redaction follows the same urlparse + ``_replace``
    pattern as ``check_agent_profile_backend`` / ``check_policy_backend``
    — strips password AND username from netloc (covers token-as-username
    URLs common with managed services).  ``ATOMIC_AGENTS_PERSONA_BACKEND_URL``
    is read for symmetry with sister checks so a future URL env var already
    has its credential-safety handled from day 1.
    """
    from .exceptions import BackendNotRegistered
    from .persona import get_default_persona_backend, list_persona_backends

    backend_id = (
        os.environ.get("ATOMIC_AGENTS_PERSONA_BACKEND", "filesystem").strip().lower()
    )

    if backend_id == "filesystem":
        try:
            backend = get_default_persona_backend(scope_root)
            caps = backend.capabilities()
            persona_count = len(backend.list_personas())
        except Exception as exc:
            return CheckResult(
                name="persona-backend",
                status=FAIL,
                message=(
                    f"filesystem persona backend probe raised "
                    f"{type(exc).__name__}: {exc}"
                ),
                fix_hint=(
                    f"Check that {scope_root} is readable. The default "
                    "FilesystemPersonaBackend enumerates personas under "
                    f"{scope_root}/.personas/."
                ),
            )
        return CheckResult(
            name="persona-backend",
            status=PASS,
            message=(
                f"filesystem backend ok ({persona_count} persona"
                f"{'' if persona_count == 1 else 's'} discovered)"
            ),
            detail={
                "backend_id": "filesystem",
                "supports_save": caps.supports_save,
                "supports_clone": caps.supports_clone,
                "supports_snapshot": caps.supports_snapshot,
                "supports_subscribe": caps.supports_subscribe,
                "supports_templates": caps.supports_templates,
                "durable": caps.durable,
                "persona_count": persona_count,
            },
        )

    # Non-filesystem id selected — verify it's known to the registry
    # BEFORE invoking the factory (lazy registrations of future
    # backends — Postgres, SaaS, git — slot in via list_persona_backends).
    known_ids = set(list_persona_backends())
    if backend_id not in known_ids:
        # Redact the echoed value: if an operator accidentally pastes a
        # credential-bearing URL into ATOMIC_AGENTS_PERSONA_BACKEND (instead
        # of ATOMIC_AGENTS_PERSONA_BACKEND_URL), it carries a password.
        # Strip anything past "://" and truncate at 32 chars — same shape as
        # persona/backend.py:_redact_for_error_message and the identical
        # redaction in check_tool_registry_backend.
        from .persona.backend import _redact_for_error_message as _redact_pid

        safe_id = _redact_pid(backend_id)
        return CheckResult(
            name="persona-backend",
            status=FAIL,
            message=(
                f"ATOMIC_AGENTS_PERSONA_BACKEND={safe_id!r} is not "
                f"a known backend. Known: {sorted(known_ids)}"
            ),
            fix_hint=(
                "Set ATOMIC_AGENTS_PERSONA_BACKEND to one of the known "
                "ids, or unset to use the filesystem default."
            ),
        )

    # URL credential redaction — same urlparse + _replace pattern as
    # check_agent_profile_backend / check_policy_backend / check_log_backend /
    # check_lock_backend.  Redacts when EITHER password OR username is present
    # — covers token-as-username URLs common with managed services (Upstash
    # ``redis://ghp_TOKEN@host``, PlanetScale ``mysql://API_KEY@host``).
    url = os.environ.get("ATOMIC_AGENTS_PERSONA_BACKEND_URL")
    safe_url: str | None = None
    if url:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        if parsed.password or parsed.username:
            netloc = parsed.hostname or ""
            if parsed.port:
                netloc = f"{netloc}:{parsed.port}"
            safe_url = parsed._replace(netloc=netloc).geturl()
        else:
            safe_url = url

    try:
        backend = get_default_persona_backend(scope_root)
    except BackendNotRegistered:
        return CheckResult(
            name="persona-backend",
            status=FAIL,
            message=f"persona backend {backend_id!r} not registered",
            fix_hint=(
                f"The {backend_id!r} backend is reserved but its lazy "
                "resolver failed. Unset ATOMIC_AGENTS_PERSONA_BACKEND to "
                "use the filesystem default."
            ),
        )
    except Exception:
        # Drop the verbatim exception message — connection errors from
        # backend constructors commonly embed the full URL with credentials.
        return CheckResult(
            name="persona-backend",
            status=FAIL,
            message=f"persona backend {backend_id!r} construction failed",
            fix_hint=(
                "Check ATOMIC_AGENTS_PERSONA_BACKEND and "
                "ATOMIC_AGENTS_PERSONA_BACKEND_URL for typos. Run with "
                "DEBUG logging to see the full exception."
            ),
        )

    # Probe via capabilities() + list_personas() — both lightweight,
    # verify the backend is reachable + schema-healthy.  Match the
    # WARN-on-unreachable-probe pattern from sister checks.
    try:
        caps = backend.capabilities()
        persona_count = len(backend.list_personas())
    except Exception:
        return CheckResult(
            name="persona-backend",
            status=WARN,
            message=(
                f"operator-pinned backend {backend_id!r} configured "
                "but capabilities() / list_personas() probe failed"
            ),
            fix_hint=(
                "Verify ATOMIC_AGENTS_PERSONA_BACKEND_URL is correct + "
                "the backend is reachable from this host. Doctor warns "
                "instead of failing — the framework runtime will fail "
                "at first persona resolution if the backend is truly down."
            ),
        )

    detail: dict[str, Any] = {
        "backend_id": backend_id,
        "supports_save": caps.supports_save,
        "supports_clone": caps.supports_clone,
        "supports_snapshot": caps.supports_snapshot,
        "supports_subscribe": caps.supports_subscribe,
        "supports_templates": caps.supports_templates,
        "durable": caps.durable,
        "persona_count": persona_count,
    }
    if safe_url is not None:
        detail["url"] = safe_url
    return CheckResult(
        name="persona-backend",
        status=PASS,
        message=(
            f"{backend_id} backend ok ({persona_count} persona"
            f"{'' if persona_count == 1 else 's'} discovered)"
        ),
        detail=detail,
    )


def check_corpus_backend(agent_root: Path) -> CheckResult:
    """Operator-config coherence check for the corpus backend (#65 PR 3).

    Validates that ``ATOMIC_AGENTS_CORPUS_BACKEND`` (plus
    ``ATOMIC_AGENTS_CORPUS_BACKEND_URL`` when non-filesystem) is correctly
    configured. Scoped at ``agent_root`` because the corpus backend is
    per-agent -- ``wiki/`` and ``raw/`` live directly under ``agent_root``,
    not under a shared scope root.

    This is the most feature-rich doctor check in the codebase because
    it has a PASS / WARN / FAIL ladder with multiple WARN conditions.

    PASS / WARN / FAIL ladder (spec/34 PR 3 + plan-eng-review finding P1
    for the page-count cliff):

    **FAIL** when ANY of:

    * ``get_default_corpus_backend(agent_root)`` raises (e.g.,
      ``CorpusBackendNotRegistered``, malformed env var, sqlite path
      unwritable).
    * ``stats(corpus="wiki")`` or ``stats(corpus="raw")`` raises
      (capability missing or backend corrupted).

    **WARN** when ANY of:

    * ``supports_full_text_search=False`` AND
      ``stats(corpus="wiki").page_count > 1000`` OR
      ``stats(corpus="raw").page_count > 1000`` (the page-count cliff
      WARN -- filesystem keyword grep at large scale can take seconds per
      query; plan-eng-review 2026-05-29 finding P1).
    * ``ATOMIC_AGENTS_CORPUS_BACKEND_URL`` is set in the environment but
      ``ATOMIC_AGENTS_CORPUS_BACKEND`` is unset. The URL is being interpreted
      with the implicit filesystem default rather than an operator-stated
      backend binding; surfaces the implicit-default state for clarity.

    **PASS** when ALL of the above FAIL / WARN conditions are clear.

    Capability snapshot included in ``detail`` (per Subagent 2
    recommendation -- capability fields are provider names, not
    credentials; no redaction needed):

    * ``backend_id`` -- e.g. ``"filesystem"`` or ``"sqlite"``
    * ``supports_full_text_search``
    * ``supports_semantic_search``
    * ``supports_versioning``
    * ``embedding_provider``
    * ``wiki_page_count`` (when probe succeeds)
    * ``raw_page_count`` (when probe succeeds)

    URL credential redaction follows the same urlparse + ``_replace``
    pattern as sister checks -- strips password AND username from netloc
    (covers token-as-username URLs common with managed services).
    """
    from .corpus import get_default_corpus_backend, list_corpus_backends
    from .exceptions import CorpusBackendNotRegistered

    raw_backend_id = os.environ.get("ATOMIC_AGENTS_CORPUS_BACKEND", "").strip().lower()
    # Empty-string treated as "not set" -- matches get_default_corpus_backend
    # fallback logic in corpus/__init__.py.
    backend_id = raw_backend_id if raw_backend_id else "filesystem"

    # WARN condition: ATOMIC_AGENTS_CORPUS_BACKEND_URL is set but
    # ATOMIC_AGENTS_CORPUS_BACKEND is unset. The URL IS honored by
    # get_default_corpus_backend (routes through the filesystem factory at
    # corpus/__init__.py:239), but the backend binding is implicit; an
    # operator reading the env config cannot tell which backend is active
    # without reading the source. Surfacing the implicit-default state
    # lets operators make their config explicit.
    url_env = os.environ.get("ATOMIC_AGENTS_CORPUS_BACKEND_URL", "").strip()
    url_without_backend = bool(url_env) and not raw_backend_id

    # URL credential redaction for detail dict -- same urlparse + _replace
    # pattern as check_mandate_backend / check_persona_backend /
    # check_policy_backend / check_log_backend. Redacts when EITHER password
    # OR username is present (covers token-as-username URLs common with
    # managed services).
    safe_url: str | None = None
    if url_env:
        from urllib.parse import urlparse

        parsed = urlparse(url_env)
        if parsed.password or parsed.username:
            netloc = parsed.hostname or ""
            if parsed.port:
                netloc = f"{netloc}:{parsed.port}"
            safe_url = parsed._replace(netloc=netloc).geturl()
        else:
            safe_url = url_env

    # Step 1: construct the backend. Any exception here is a hard FAIL --
    # the operator cannot use the corpus at all.
    try:
        backend = get_default_corpus_backend(agent_root)
    except CorpusBackendNotRegistered as exc:
        # Redact the verbatim exception for the same reason as sister checks:
        # connection errors from backend constructors can embed the full URL
        # with credentials in the exception text.
        from .corpus import _redact_for_error_message as _redact_cid

        safe_exc = _redact_cid(str(exc))
        return CheckResult(
            name="corpus-backend",
            status=FAIL,
            message=(
                f"Could not construct CorpusBackend (cause: {safe_exc}). "
                "Check ATOMIC_AGENTS_CORPUS_BACKEND and "
                "ATOMIC_AGENTS_CORPUS_BACKEND_URL environment variables."
            ),
            fix_hint=(
                "Set ATOMIC_AGENTS_CORPUS_BACKEND to one of the known ids "
                f"({sorted(list_corpus_backends())}), or unset to use the "
                "filesystem default."
            ),
        )
    except Exception as exc:
        # Drop the verbatim exception message -- connection errors from
        # backend constructors commonly embed the full URL with credentials.
        return CheckResult(
            name="corpus-backend",
            status=FAIL,
            message=(
                f"Could not construct CorpusBackend (cause: {type(exc).__name__}). "
                "Check ATOMIC_AGENTS_CORPUS_BACKEND and "
                "ATOMIC_AGENTS_CORPUS_BACKEND_URL environment variables."
            ),
            fix_hint=(
                "Check ATOMIC_AGENTS_CORPUS_BACKEND and "
                "ATOMIC_AGENTS_CORPUS_BACKEND_URL for typos. Run with "
                "DEBUG logging to see the full exception."
            ),
        )

    caps = backend.capabilities

    # Step 2: probe both corpora via stats(). Any exception here is a hard
    # FAIL -- the backend is constructed but the core stats interface is
    # broken, indicating corruption or misconfiguration.
    wiki_stats = None
    raw_stats = None
    for corpus_name in ("wiki", "raw"):
        try:
            if corpus_name == "wiki":
                wiki_stats = backend.stats(corpus_name)
            else:
                raw_stats = backend.stats(corpus_name)
        except Exception as exc:
            return CheckResult(
                name="corpus-backend",
                status=FAIL,
                message=(
                    f"CorpusBackend constructed but stats({corpus_name!r}) failed "
                    f"(cause: {type(exc).__name__}). Backend may be corrupted or "
                    "misconfigured."
                ),
                fix_hint=(
                    "Verify the corpus directories (wiki/ and raw/) under "
                    f"{agent_root} are readable. Run with DEBUG logging to see "
                    "the full exception."
                ),
                detail={
                    "backend_id": backend_id,
                    "supports_full_text_search": caps.supports_full_text_search,
                    "supports_semantic_search": caps.supports_semantic_search,
                    "supports_versioning": caps.supports_versioning,
                    "embedding_provider": caps.embedding_provider,
                },
            )

    # Build capability snapshot. wiki_stats and raw_stats are guaranteed
    # non-None here (both probes succeeded). Defensive conditional rather
    # than bare assert: assert is disabled in optimized Python builds and
    # would crash with AssertionError if a future refactor moves the
    # probe loop or adds a conditional-return before this point. Returning
    # CheckResult(FAIL) preserves the always-returns-CheckResult contract.
    if wiki_stats is None or raw_stats is None:
        return CheckResult(
            name="corpus-backend",
            status=FAIL,
            message=(
                "Internal error: stats probe completed without setting both "
                "wiki_stats and raw_stats. This indicates a logic error in "
                "check_corpus_backend. Report this with the stack trace."
            ),
            detail={
                # Capability snapshot from caps is already available at this
                # point; include it so the operator has context to debug
                # without re-running the doctor. Round 2 finding F5.
                "backend_id": backend_id,
                "supports_full_text_search": caps.supports_full_text_search,
                "supports_semantic_search": caps.supports_semantic_search,
                "supports_versioning": caps.supports_versioning,
                "embedding_provider": caps.embedding_provider,
            },
        )

    detail: dict[str, Any] = {
        "backend_id": backend_id,
        "supports_full_text_search": caps.supports_full_text_search,
        "supports_semantic_search": caps.supports_semantic_search,
        "supports_versioning": caps.supports_versioning,
        "embedding_provider": caps.embedding_provider,
        "wiki_page_count": wiki_stats.page_count,
        "raw_page_count": raw_stats.page_count,
    }
    if safe_url is not None:
        detail["url"] = safe_url

    # Step 3: check WARN conditions. Emit the first WARN triggered --
    # URL-without-backend takes precedence because it represents a silent
    # misconfiguration that will affect ALL corpora regardless of scale.
    if url_without_backend:
        return CheckResult(
            name="corpus-backend",
            status=WARN,
            message=(
                "ATOMIC_AGENTS_CORPUS_BACKEND_URL is set but "
                "ATOMIC_AGENTS_CORPUS_BACKEND is not. The URL was used with "
                "the default backend resolution. Set ATOMIC_AGENTS_CORPUS_BACKEND "
                "explicitly to make the binding clear."
            ),
            fix_hint=(
                "Set ATOMIC_AGENTS_CORPUS_BACKEND=<backend_id> (e.g., "
                "filesystem or sqlite) to declare the backend explicitly. "
                "The URL is honored by both backends; this WARN exists to "
                "surface implicit-default operator configuration, not silent "
                "ignore."
            ),
            detail=detail,
        )

    # Page-count cliff WARN (plan-eng-review 2026-05-29 finding P1):
    # when supports_full_text_search=False, filesystem keyword grep at
    # large scale can take seconds per query. Probe BOTH corpora.
    PAGE_COUNT_CLIFF = 1000
    if not caps.supports_full_text_search:
        for corpus_name, page_count in (
            ("wiki", wiki_stats.page_count),
            ("raw", raw_stats.page_count),
        ):
            if page_count > PAGE_COUNT_CLIFF:
                return CheckResult(
                    name="corpus-backend",
                    status=WARN,
                    message=(
                        f"Large corpus detected ({page_count} pages, "
                        f"{corpus_name!r} corpus). Set "
                        "ATOMIC_AGENTS_CORPUS_BACKEND=sqlite for indexed query "
                        "performance. Filesystem keyword grep at this scale can "
                        "take seconds per query."
                    ),
                    fix_hint=(
                        "Set ATOMIC_AGENTS_CORPUS_BACKEND=sqlite to activate "
                        "SQLite FTS5 indexed search. See docs/spec/34-corpus-backend.md."
                    ),
                    detail=detail,
                )

    # All PASS -- backend healthy, no WARN conditions triggered.
    wiki_count = wiki_stats.page_count
    raw_count = raw_stats.page_count
    return CheckResult(
        name="corpus-backend",
        status=PASS,
        message=(
            f"{backend_id} backend ok "
            f"(wiki: {wiki_count} page{'' if wiki_count == 1 else 's'}, "
            f"raw: {raw_count} page{'' if raw_count == 1 else 's'})"
        ),
        detail=detail,
    )


def check_mcp_server_registry_backend(agent_root: Path) -> CheckResult:
    """Operator-config coherence check for the MCP server registry backend (#201 PR 2).

    Validates that ``ATOMIC_AGENTS_MCP_SERVER_REGISTRY_BACKEND`` (plus
    ``ATOMIC_AGENTS_MCP_SERVER_REGISTRY_BACKEND_URL`` when non-filesystem)
    is correctly configured:

    * unset / empty / ``filesystem`` → PASS (today's filesystem-default
      deployment shape -- no extras needed, no URL needed).
    * unknown backend_id (typo or pasted credential) → FAIL with credential-
      redacted echo + list of registered ids. Uses ``_redact_for_error_message``
      from ``mcp_registry/__init__.py`` (handles ``://`` URL heuristic AND
      ``user:pass@host`` DSN heuristic AND length truncation -- distinct from
      the inline truncation in ``check_tool_registry_backend`` which misses
      DSN-style values).
    * transient probe failure → WARN (matches ``check_provider_keys`` pattern;
      doctor does not crash on optional infrastructure).

    Reads ``backend.capabilities`` (property, not method). Detail dict
    includes all 5 capability fields plus ``mcp_server_count`` from
    ``list_mcp_servers()`` (NOT ``load_all_mcp_servers``, which materializes
    resolved env values).

    Mirrors the operator-coherence layer pattern of ``check_lock_backend``
    and ``check_tool_registry_backend``. ``run_doctor`` then exercises the
    backend through the agent's actual construction path.
    """
    from .mcp_registry import (
        MCPRegistryError,
        MCPRegistryUnavailable,
        _redact_for_error_message,
        get_default_mcp_server_registry_backend,
        list_mcp_server_registry_backends,
    )
    from .mcp_registry.backend import MCPRegistryDescriptorInvalid

    raw = (
        os.environ.get("ATOMIC_AGENTS_MCP_SERVER_REGISTRY_BACKEND", "").strip().lower()
    )
    backend_id = raw if raw else "filesystem"
    available = list_mcp_server_registry_backends()

    if backend_id not in available:
        safe_id = _redact_for_error_message(raw)
        return CheckResult(
            name="mcp-server-registry-backend",
            status=FAIL,
            message=(
                f"ATOMIC_AGENTS_MCP_SERVER_REGISTRY_BACKEND={safe_id!r} is "
                f"not a known backend. Available: {available}"
            ),
            fix_hint=(
                f"Set ATOMIC_AGENTS_MCP_SERVER_REGISTRY_BACKEND to one of "
                f"{available}, or unset it to use the filesystem default."
            ),
            detail={"safe_backend_id": safe_id, "available_backends": available},
        )

    try:
        backend = get_default_mcp_server_registry_backend(agent_root, [])
    except MCPRegistryError as exc:
        safe = _redact_for_error_message(str(exc))
        return CheckResult(
            name="mcp-server-registry-backend",
            status=FAIL,
            message=f"failed to construct {backend_id!r} backend",
            fix_hint=f"check the env vars: {safe}",
            detail={"backend_id": backend_id},
        )

    try:
        refs = backend.list_mcp_servers()
    except MCPRegistryUnavailable:
        return CheckResult(
            name="mcp-server-registry-backend",
            status=WARN,
            message=(
                f"operator-pinned backend {backend_id!r} configured but "
                f"list_mcp_servers() probe failed"
            ),
            fix_hint=(
                "Verify the catalog is reachable. Doctor warns instead of "
                "failing; the framework runtime will fail at first list or "
                "load if the backend is truly down."
            ),
            detail={"backend_id": backend_id},
        )
    except Exception as exc:
        return CheckResult(
            name="mcp-server-registry-backend",
            status=FAIL,
            message=(
                f"backend {backend_id!r} probe raised {type(exc).__name__}: {exc}"
            ),
            fix_hint="See logs for the exception details.",
            detail={"backend_id": backend_id},
        )

    # Predict agent-construction success: AtomicAgent.__init__ calls
    # load_all_mcp_servers() at construction (spec/36 framework invariant).
    # list_mcp_servers() above swallows parse errors and returns [], so a
    # malformed mcp.md would PASS doctor but crash construction. Probe
    # load_all_mcp_servers() here to catch descriptor errors that
    # list_mcp_servers() hides. WARN on transient (already caught above);
    # FAIL on permanent descriptor invalidity.
    try:
        backend.load_all_mcp_servers()
    except MCPRegistryDescriptorInvalid as exc:
        return CheckResult(
            name="mcp-server-registry-backend",
            status=FAIL,
            message=(
                f"{backend_id!r} backend has malformed descriptor: "
                f"{_redact_for_error_message(str(exc))}"
            ),
            fix_hint=(
                "Fix the descriptor (mcp.md sections require 'command:'). "
                "Doctor probes this to predict agent construction; without "
                "the fix every AtomicAgent for this agent will fail at "
                "construction with MCPRegistryDescriptorInvalid."
            ),
            detail={"backend_id": backend_id, "mcp_server_count": len(refs)},
        )
    except MCPRegistryUnavailable:
        # Transient failure during materialization; the list path already
        # returned successfully so the backend is reachable but a server
        # spec resolution (env var, validation) failed transiently. Treat
        # as WARN, not FAIL: an operator may resolve by exporting the
        # missing env var.
        return CheckResult(
            name="mcp-server-registry-backend",
            status=WARN,
            message=(
                f"backend {backend_id!r} list ok but load_all_mcp_servers() "
                f"raised transient failure (env var or validation)"
            ),
            fix_hint=(
                "Verify required env vars are set in the doctor process. "
                "Agent construction will fail with the same error until "
                "resolved."
            ),
            detail={"backend_id": backend_id, "mcp_server_count": len(refs)},
        )

    caps = backend.capabilities
    return CheckResult(
        name="mcp-server-registry-backend",
        status=PASS,
        message=(
            f"{backend_id} backend ok "
            f"({len(refs)} MCP server{'s' if len(refs) != 1 else ''} mounted)"
        ),
        detail={
            "backend_id": backend.backend_id,
            "supports_install": caps.supports_install,
            "supports_uninstall": caps.supports_uninstall,
            "supports_capability_handshake": caps.supports_capability_handshake,
            "supports_audit": caps.supports_audit,
            "durable": caps.durable,
            "mcp_server_count": len(refs),
        },
    )


def check_secret_backend() -> CheckResult:
    """SecretBackend resolves and the configured backend instantiates cleanly.

    Probes the registered backend via ``get_default_secret_backend()`` and
    verifies that capability advertisement is structurally valid.  Does NOT
    call ``get()`` for any specific key (that is ``check_provider_keys``'s
    job) -- this check confirms only that the backend machinery is wired up.

    Doctor dual-probe lesson (MEMORY.md): ``has()`` can pass even when
    ``get()`` fails.  This check probes the backend construction and
    capability surface, not key resolution.  Key resolution is validated by
    ``check_provider_keys`` which routes through the same backend via
    ``_get_key()``.

    ATOMIC_AGENTS_SECRET_BACKEND_URL is redacted before echo to avoid
    leaking credentials in doctor output.
    """
    from .secret_backend import SecretError, get_default_secret_backend
    from .secret_backend import _redact_for_error_message

    raw_backend_id = (
        os.environ.get("ATOMIC_AGENTS_SECRET_BACKEND", "").strip().lower()
        or "filesystem"
    )
    raw_url = os.environ.get("ATOMIC_AGENTS_SECRET_BACKEND_URL", "")
    safe_backend_id = _redact_for_error_message(raw_backend_id)
    safe_url = _redact_for_error_message(raw_url) if raw_url else "(not set)"

    try:
        backend = get_default_secret_backend()
    except SecretError as e:
        return CheckResult(
            name="secret-backend",
            status=FAIL,
            message=(
                f"failed to instantiate secret backend "
                f"(ATOMIC_AGENTS_SECRET_BACKEND={safe_backend_id!r}, "
                f"ATOMIC_AGENTS_SECRET_BACKEND_URL={safe_url}): {e}"
            ),
            fix_hint=(
                "Unset ATOMIC_AGENTS_SECRET_BACKEND to use the filesystem default, "
                "or set it to a registered backend id (known: 'filesystem', 'gcp'). "
                "For gcp: set ATOMIC_AGENTS_SECRET_BACKEND_URL=projects/<project_id>/secrets "
                "and install the [gcp] extra: uv add 'atomic-agents-stack[gcp]'."
            ),
            detail={"backend_id": safe_backend_id, "error": str(e)},
        )
    except Exception as e:
        return CheckResult(
            name="secret-backend",
            status=FAIL,
            message=f"secret backend raised unexpected error: {type(e).__name__}: {e}",
            detail={"backend_id": safe_backend_id, "error_type": type(e).__name__},
        )

    caps = backend.capabilities

    # For the gcp backend, also run the ADC liveness probe. Re-stamp the
    # result with the stable slot name "secret-backend" so an operator keying off
    # check names sees one consistent name across all backends. Merge backend_id
    # into the detail dict so EVERY delegated outcome (PASS, WARN, and all FAIL
    # branches) carries it -- the failure paths are exactly where an operator
    # parsing detail["backend_id"] needs it most (Principle #5: audit trail is
    # structural). check_gcp_secret_backend(), when called directly, keeps its
    # own "secret-backend[gcp]" name.
    if backend.backend_id == "gcp":
        gcp_result = check_gcp_secret_backend()
        return replace(
            gcp_result,
            name="secret-backend",
            detail={**(gcp_result.detail or {}), "backend_id": "gcp"},
        )

    return CheckResult(
        name="secret-backend",
        status=PASS,
        message=(
            f"secret backend '{backend.backend_id}' ready "
            f"(supports_rotation={caps.supports_rotation}, "
            f"persists_plaintext={caps.persists_plaintext})"
        ),
        detail={
            "backend_id": backend.backend_id,
            "supports_rotation": caps.supports_rotation,
            "supports_audit_logging": caps.supports_audit_logging,
            "persists_plaintext": caps.persists_plaintext,
        },
    )


def check_gcp_secret_backend() -> CheckResult:
    """Verify GCP Secret Manager ADC credentials are usable (non-billable probe).

    Mirrors ``check_vertex_credentials()`` exactly:
    - Step 1: SDK import check (google-cloud-secret-manager = [gcp] extra)
    - Step 2: ADC resolution via ``google.auth.default()``
    - Step 3: Token mint via ``credentials.refresh(Request())`` -- proves ADC
      actually works without making a billable Secret Manager call.
    - Step 4: WARN (not FAIL) when GOOGLE_CLOUD_PROJECT is absent. On Cloud Run /
      GKE the project is resolved from the metadata server automatically; the env
      var absence does not mean ADC is broken on those platforms.

    This check is intentionally NOT a ``access_secret_version()`` call -- that is
    a billable operation and doctor must never trigger unguarded API cost.
    """
    # Step 1: SDK import check ([gcp] extra)
    try:
        from google.cloud import secretmanager as _sm  # noqa: F401
    except ImportError as e:
        return CheckResult(
            name="secret-backend[gcp]",
            status=FAIL,
            message="google-cloud-secret-manager SDK not installed (required for gcp backend)",
            fix_hint=(
                "Install the [gcp] extra:\n"
                "  uv add 'atomic-agents-stack[gcp]'\n"
                "  Or: pip install google-cloud-secret-manager"
            ),
            detail={
                "missing_sdk": "google.cloud.secretmanager",
                "underlying_error": str(e),
            },
        )

    # Step 2: ADC resolution
    try:
        import google.auth as _gauth
        import google.auth.exceptions as _gauth_exc
        import google.auth.transport.requests as _gauth_requests
    except ImportError as e:
        return CheckResult(
            name="secret-backend[gcp]",
            status=FAIL,
            message=f"google-auth not installed: {e}",
            fix_hint=(
                "Install the [gcp] extra:\n"
                "  uv add 'atomic-agents-stack[gcp]'\n"
                "  Or: pip install google-auth google-cloud-secret-manager"
            ),
            detail={"underlying_error": str(e)},
        )

    try:
        project = os.environ.get("GOOGLE_CLOUD_PROJECT")
        credentials, detected_project = _gauth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
    except _gauth_exc.DefaultCredentialsError as e:
        return CheckResult(
            name="secret-backend[gcp]",
            status=FAIL,
            message="GCP Secret Manager ADC credentials not found",
            fix_hint=(
                "Run one of:\n"
                "  gcloud auth application-default login    (local dev)\n"
                "  Set GOOGLE_APPLICATION_CREDENTIALS to a service account key file\n"
                "  Deploy to Cloud Run / GKE with a service account attached\n"
                "Also set: export GOOGLE_CLOUD_PROJECT='<your-gcp-project-id>'"
            ),
            detail={"underlying_error": str(e)},
        )
    except Exception as e:
        return CheckResult(
            name="secret-backend[gcp]",
            status=FAIL,
            message=f"GCP Secret Manager ADC resolution failed: {type(e).__name__}: {e}",
            fix_hint=(
                "Run: gcloud auth application-default login\n"
                "And set: export GOOGLE_CLOUD_PROJECT='<your-gcp-project-id>'"
            ),
            detail={"underlying_error": str(e)},
        )

    # Step 3: Token mint -- proves the credentials object is actually usable.
    # credentials.refresh(Request()) hits the OAuth metadata server, not a
    # billable Secret Manager endpoint.
    try:
        request = _gauth_requests.Request()
        credentials.refresh(request)
    except _gauth_exc.TransportError as e:
        return CheckResult(
            name="secret-backend[gcp]",
            status=FAIL,
            message=f"GCP Secret Manager ADC token refresh failed (network error): {e}",
            fix_hint=(
                "Check network connectivity to Google's OAuth endpoint.\n"
                "On GCE/Cloud Run this is automatic; on dev machines ensure\n"
                "you have run: gcloud auth application-default login"
            ),
            detail={"underlying_error": str(e)},
        )
    except Exception as e:
        return CheckResult(
            name="secret-backend[gcp]",
            status=FAIL,
            message=f"GCP Secret Manager ADC token refresh failed: {type(e).__name__}: {e}",
            fix_hint=(
                "Re-authenticate: gcloud auth application-default login\n"
                "Or check that your service account key file is valid."
            ),
            detail={"underlying_error": str(e)},
        )

    # Step 4: Project env var check -- WARN only (not FAIL).
    effective_project = project or detected_project
    detail: dict = {
        "backend_id": "gcp",
        "project": effective_project,
        "token_valid": True,
    }
    if not project:
        return CheckResult(
            name="secret-backend[gcp]",
            status=WARN,
            message=(
                "GCP Secret Manager ADC token minted successfully, but "
                "GOOGLE_CLOUD_PROJECT env var is not set"
            ),
            fix_hint=(
                "On dev machines, set: export GOOGLE_CLOUD_PROJECT='<your-gcp-project-id>'\n"
                "On Cloud Run / GKE, the project is resolved from the metadata server "
                "automatically -- this warning can be ignored in those environments."
            ),
            detail=detail,
        )

    return CheckResult(
        name="secret-backend[gcp]",
        status=PASS,
        message=(
            f"GCP Secret Manager ADC credentials valid; project={effective_project!r}"
        ),
        detail=detail,
    )


def check_memory_backend_config(agent_root: Path) -> CheckResult:
    """Operator-config coherence check for the memory backend (#382 PR 1).

    Validates that ``ATOMIC_AGENTS_MEMORY_BACKEND`` is correctly configured:

    * unset / ``filesystem`` → PASS (default; no extras needed)
    * unknown backend_id (typo) → FAIL with the registered backend list

    This is the operator-coherence layer; ``check_memory_backend`` then runs
    the actual liveness probe through the configured backend.  Both checks
    reuse ``get_default_memory_backend`` internally so doctor's verdict and
    the runtime's behavior cannot diverge (doctor-reuses-factory invariant,
    per MEMORY.md feedback_doctor_dual_probe_pattern).

    Mirrors the ``check_lock_backend`` / ``check_locks`` pair shape.
    """
    # Lazy import — the memory package registers defaults at import time;
    # importing inside the function body ensures _register_defaults() has run
    # before any registry query, regardless of module import order.
    from .memory import (
        get_default_memory_backend,
        list_backends,
        _LAZY_BACKEND_IDS,
    )

    backend_id = (
        os.environ.get("ATOMIC_AGENTS_MEMORY_BACKEND", "filesystem").strip().lower()
    )

    if backend_id == "filesystem":
        return CheckResult(
            name="memory-backend-config",
            status=PASS,
            message="filesystem backend (default; no extra needed)",
            detail={"backend_id": "filesystem"},
        )

    # Check whether the backend_id is known.
    known_ids = sorted(set(list_backends()) | _LAZY_BACKEND_IDS)
    if backend_id not in known_ids:
        raw = os.environ.get("ATOMIC_AGENTS_MEMORY_BACKEND", "")
        return CheckResult(
            name="memory-backend-config",
            status=FAIL,
            message=(
                f"ATOMIC_AGENTS_MEMORY_BACKEND={raw!r} is not a known backend. "
                f"Known: {known_ids}"
            ),
            fix_hint=(
                "Set ATOMIC_AGENTS_MEMORY_BACKEND to one of the known ids, "
                "or unset to use the filesystem default."
            ),
        )

    # Registered non-filesystem backend — construct it through the factory
    # (which dispatches via the registry), confirming it is actually
    # constructable, not merely present in list_backends().  Close it
    # immediately: a future connection-backed backend (#258) would open a
    # connection/pool per doctor run that must be released — the MemoryBackend
    # Protocol defines close() for exactly this.
    _config_backend = None
    try:
        _config_backend = get_default_memory_backend(agent_root)
    except Exception as exc:
        return CheckResult(
            name="memory-backend-config",
            status=FAIL,
            message=f"memory backend construction failed: {exc}",
            fix_hint=(
                f"Check ATOMIC_AGENTS_MEMORY_BACKEND={backend_id!r} and any "
                f"required connection env vars."
            ),
        )
    finally:
        if _config_backend is not None and hasattr(_config_backend, "close"):
            try:
                _config_backend.close()
            except Exception:
                pass

    return CheckResult(
        name="memory-backend-config",
        status=PASS,
        message=f"{backend_id!r} backend configured",
        detail={"backend_id": backend_id},
    )


def check_memory_backend(agent_root: Path) -> CheckResult:
    """Operator-configured MemoryBackend resolves and stats() returns successfully.

    Routes through ``get_default_memory_backend`` (the operator-config factory)
    so the liveness probe hits the SAME backend the runtime constructs — not
    always FilesystemBackend regardless of ``ATOMIC_AGENTS_MEMORY_BACKEND``.
    Mirrors the ``check_locks`` / ``get_default_lock_backend`` pattern
    (doctor-reuses-factory invariant, MEMORY.md feedback_doctor_dual_probe_pattern).
    """
    # Filesystem-shaped precheck: the on-disk memory/ dir must exist for the
    # filesystem reference impl. This precheck is ONLY correct for the
    # filesystem default — a future non-filesystem backend (#258 Postgres/
    # pgvector) may legitimately have no local memory/ dir, so we skip the
    # guard and let the factory + stats() probe be authoritative for any
    # non-filesystem selection. (Without this gate the liveness check would
    # spuriously FAIL a healthy non-local backend, contradicting the
    # doctor-reuses-factory invariant.)
    backend_id = (
        os.environ.get("ATOMIC_AGENTS_MEMORY_BACKEND", "filesystem").strip().lower()
    )
    memory_dir = agent_root / "memory"
    if backend_id == "filesystem" and not memory_dir.exists():
        return CheckResult(
            name="memory-backend",
            status=FAIL,
            message=f"memory/ directory missing at {memory_dir}",
            fix_hint=f"Create it: mkdir -p {memory_dir} && touch {memory_dir}/INDEX.md",
        )
    backend = None
    try:
        from .memory import get_default_memory_backend
        from .exceptions import BackendNotRegistered

        backend = get_default_memory_backend(agent_root)
        stats = backend.stats()
    except BackendNotRegistered as e:
        return CheckResult(
            name="memory-backend",
            status=FAIL,
            message=f"memory backend not registered: {e}",
            fix_hint=(
                "Unset ATOMIC_AGENTS_MEMORY_BACKEND or set it to a registered "
                "backend id. See check_memory_backend_config for details."
            ),
        )
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
    finally:
        # Release any connection/pool a future connection-backed backend (#258)
        # opened; harmless for the filesystem default. Protocol defines close().
        if backend is not None and hasattr(backend, "close"):
            try:
                backend.close()
            except Exception:
                pass
    backend_class_name = type(backend).__name__
    return CheckResult(
        name="memory-backend",
        status=PASS,
        message=f"{backend_class_name} ok ({stats.total_notes} notes)",
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


def check_goal_backend(agent_root: Path) -> CheckResult:
    """GoalBackend resolves and the configured backend instantiates cleanly.

    Validates that ATOMIC_AGENTS_GOAL_BACKEND is correctly configured.
    Scoped at agent_root because the goal backend is per-agent — goal.md lives
    directly under agent_root.

    Doctor dual-probe pattern (MEMORY.md feedback_doctor_dual_probe_pattern):
    Probes BOTH:
    1. list_archived() — lightweight list operation
    2. load_goal() — heavy deserialize+validate path (when goal.md is present)

    When goal.md is absent (reactive agent with no goal), returns PASS with a
    note ('goal_md_present': False). A missing goal.md is NOT a failure condition
    — matching check_corpus_backend's pattern for agents without a corpus.

    PASS / FAIL ladder (this check has no WARN path):
    FAIL when:
    * get_default_goal_backend(agent_root) raises (bad env var or not registered).
    * list_archived() raises.
    * goal.md is present AND load_goal() raises (corrupted goal.md).

    PASS otherwise (including when goal.md is absent).
    """
    from .goal import (  # noqa: PLC0415
        get_default_goal_backend,
        list_goal_backends,
        _redact_for_error_message,
    )
    from .exceptions import (  # noqa: PLC0415
        AtomicAgentsError,
        GoalCorrupted,
        SchemaValidationError,
    )

    raw_backend_id = (
        os.environ.get("ATOMIC_AGENTS_GOAL_BACKEND", "").strip().lower() or "filesystem"
    )
    # Credential safety: ATOMIC_AGENTS_GOAL_BACKEND is a single env var that may
    # carry a URL- or DSN-shaped value (postgres://user:pass@host/db). Redact
    # before it reaches the rendered CheckResult message or detail — the factory
    # redacts in its own error, but the doctor recomputes raw_backend_id from
    # os.environ and must redact independently. Same per-backend
    # _redact_for_error_message convention as logs/profile/corpus/mcp_registry/
    # secret_backend (the standing credential-echo-redaction pattern).
    safe_backend_id = _redact_for_error_message(raw_backend_id)

    try:
        backend = get_default_goal_backend(agent_root)
    except Exception as e:
        # Build the known-id list from the registry so the hint stays accurate
        # as operators register additional backends (matches the dynamic id list
        # get_default_goal_backend's own error reports).
        return CheckResult(
            name="goal-backend",
            status=FAIL,
            message=(
                f"failed to instantiate goal backend "
                f"(ATOMIC_AGENTS_GOAL_BACKEND={safe_backend_id!r}): {e}"
            ),
            fix_hint=(
                "Unset ATOMIC_AGENTS_GOAL_BACKEND to use the filesystem default, "
                f"or set it to a registered backend id (known: {list_goal_backends()}). "
            ),
            detail={"backend_id": safe_backend_id, "error": str(e)},
        )

    # Dual-probe step 1: lightweight list
    try:
        archived = backend.list_archived(agent_root.name)
    except Exception as e:
        return CheckResult(
            name="goal-backend",
            status=FAIL,
            message=f"goal backend list_archived() raised: {type(e).__name__}: {e}",
            detail={
                "backend_id": backend.backend_id,
                "error": str(e),
                "error_type": type(e).__name__,
            },
        )

    # Dual-probe step 2: heavy load (only when goal.md is present)
    goal_md_present = (agent_root / "goal.md").is_file()
    if goal_md_present:
        try:
            backend.load_goal(agent_root.name)
        except (GoalCorrupted, SchemaValidationError) as e:
            # NOTE: these are subclasses of AtomicAgentsError, so they MUST be
            # caught before the bare-AtomicAgentsError clause below — corruption
            # is a real FAIL, not a benign vanished-file race.
            return CheckResult(
                name="goal-backend",
                status=FAIL,
                message=f"goal.md present but load_goal() failed: {e}",
                fix_hint=(
                    "Inspect goal.md with 'python -m atomic_agents.goal "
                    "--agents-root <agents_root> status <agent>' to identify the "
                    "corruption (--agents-root precedes the subcommand; it defaults "
                    "to the platform agents root if omitted). The file may have a "
                    "schema_version mismatch or missing required frontmatter fields."
                ),
                detail={
                    "backend_id": backend.backend_id,
                    "goal_md_present": True,
                    "error": str(e),
                },
            )
        except AtomicAgentsError:
            # TOCTOU: goal.md vanished between the is_file() probe and load_goal()
            # (e.g. a concurrent archive_goal unlinked it). That's "no active
            # goal" — benign, not corruption — so fall through to PASS rather
            # than a spurious FAIL for a transient race. (GoalCorrupted /
            # SchemaValidationError subclass AtomicAgentsError but are caught
            # above, so only the bare absent-file raise reaches here.)
            goal_md_present = False
        except Exception as e:
            return CheckResult(
                name="goal-backend",
                status=FAIL,
                message=f"goal backend load_goal() raised unexpected error: {type(e).__name__}: {e}",
                detail={
                    "backend_id": backend.backend_id,
                    "goal_md_present": True,
                    "error_type": type(e).__name__,
                    "error": str(e),
                },
            )

    caps = backend.capabilities()
    return CheckResult(
        name="goal-backend",
        status=PASS,
        message=(
            f"goal backend '{backend.backend_id}' ready"
            + (" (no goal.md for this agent)" if not goal_md_present else "")
        ),
        detail={
            "backend_id": backend.backend_id,
            "goal_md_present": goal_md_present,
            "archived_count": len(archived),
            "supports_canonical_export": caps.supports_canonical_export,
            "supports_archive": caps.supports_archive,
            "supports_history_query": caps.supports_history_query,
        },
    )


def check_outcome_backend(agent_root: Path) -> CheckResult:
    """OutcomeBackend resolves and the configured backend instantiates cleanly.

    Validates that ATOMIC_AGENTS_OUTCOME_BACKEND is correctly configured.
    Scoped at agent_root because the outcome backend is per-agent — result.json
    lives under agent_root/outcomes/runs/<run_id>/.

    Doctor dual-probe pattern (MEMORY.md feedback_doctor_dual_probe_pattern):
    Probes BOTH:
    1. list_runs() — lightweight enumeration (MUST NOT raise even when outcomes/
       is absent; returns [] for agents with no completed runs)
    2. read_result() — heavy JSON-parse + OutcomeResult reconstruction path
       (only when list_runs() returns at least one run_id)

    When no runs exist (new agent or agent that has never run an outcome),
    returns PASS with outcome_runs_present=False, read_result_probed=False,
    run_count=0 in the full detail dict (the same 7-key shape documented in
    spec/27: backend_id, outcome_runs_present, run_count, read_result_probed,
    read_result_vanished, supports_canonical_export, supports_artifact_storage).
    This is NOT a failure — matching check_goal_backend's pattern for agents
    without a goal.md (reactive agents with no active goal pass cleanly).

    Light probe = list_runs(agent_id) MUST NOT raise even when outcomes/ absent.
    Heavy probe = read_result(agent_id, run_id) for the most-recent run_id
    returned by list_runs(), skipped with PASS when list_runs() returns [].

    PASS / FAIL ladder (this check has no WARN path):
    FAIL when:
    * get_default_outcome_backend(agent_root) raises (bad env var or not registered).
    * list_runs() raises.
    * Runs exist AND read_result() raises (corrupted result.json).

    PASS otherwise (including when no runs exist).
    """
    from .outcome import (  # noqa: PLC0415
        get_default_outcome_backend,
        list_outcome_backends,
        _redact_for_error_message,
    )
    from .exceptions import (  # noqa: PLC0415
        AtomicAgentsError,
        OutcomeCorrupted,
        PathTraversalError,
    )

    raw_backend_id = (
        os.environ.get("ATOMIC_AGENTS_OUTCOME_BACKEND", "").strip().lower()
        or "filesystem"
    )
    # Credential safety: ATOMIC_AGENTS_OUTCOME_BACKEND may carry a URL- or
    # DSN-shaped value. Redact before it reaches the rendered CheckResult
    # message or detail. Same per-backend _redact_for_error_message convention
    # as logs/profile/corpus/mcp_registry/secret_backend/goal.
    safe_backend_id = _redact_for_error_message(raw_backend_id)

    try:
        backend = get_default_outcome_backend(agent_root)
    except Exception as e:
        return CheckResult(
            name="outcome-backend",
            status=FAIL,
            message=(
                f"failed to instantiate outcome backend "
                f"(ATOMIC_AGENTS_OUTCOME_BACKEND={safe_backend_id!r}): {e}"
            ),
            fix_hint=(
                "Unset ATOMIC_AGENTS_OUTCOME_BACKEND to use the filesystem default, "
                f"or set it to a registered backend id (known: {list_outcome_backends()}). "
            ),
            detail={"backend_id": safe_backend_id, "error": str(e)},
        )

    # Dual-probe step 1: lightweight list
    try:
        run_ids = backend.list_runs(agent_root.name)
    except Exception as e:
        return CheckResult(
            name="outcome-backend",
            status=FAIL,
            message=f"outcome backend list_runs() raised: {type(e).__name__}: {e}",
            detail={
                "backend_id": backend.backend_id,
                "error": str(e),
                "error_type": type(e).__name__,
            },
        )

    # Dual-probe step 2: heavy read (only when at least one run exists).
    # When no runs exist, the dual-probe depth is limited — documented in the
    # check docstring and detail dict so operators know the probe scope.
    outcome_runs_present = bool(run_ids)
    read_result_probed = False
    read_result_vanished = False
    if outcome_runs_present:
        # Probe the most recent run (last in lexicographic order = most recent
        # for 'outcome-YYYYMMDD-...' run_id naming convention).
        latest_run_id = run_ids[-1]
        try:
            backend.read_result(agent_root.name, latest_run_id)
            read_result_probed = True
        except OutcomeCorrupted as e:
            # NOTE: OutcomeCorrupted is a subclass of AtomicAgentsError, so it
            # MUST be caught before the bare-AtomicAgentsError clause below —
            # corruption is a real FAIL, not a benign vanished-file race.
            return CheckResult(
                name="outcome-backend",
                status=FAIL,
                message=f"result.json present but read_result() failed: {e}",
                fix_hint=(
                    f"Inspect result.json at "
                    f"{agent_root / 'outcomes' / 'runs' / latest_run_id / 'result.json'} "
                    "to identify the corruption (may have missing required fields or "
                    "invalid JSON)."
                ),
                detail={
                    "backend_id": backend.backend_id,
                    "outcome_runs_present": True,
                    "run_id_probed": latest_run_id,
                    "error": str(e),
                },
            )
        except PathTraversalError as e:
            # A traversing / symlinked run (run dir or result.json escaping
            # agent_root) is a containment alarm, NOT a benign vanished-file race.
            # PathTraversalError subclasses AtomicAgentsError, so it MUST be
            # caught before the bare-AtomicAgentsError clause below or it would
            # false-PASS as read_result_vanished.
            return CheckResult(
                name="outcome-backend",
                status=FAIL,
                message=f"result.json path escaped agent_root (path traversal): {e}",
                fix_hint=(
                    "A run directory or result.json under outcomes/runs/ is a "
                    "symlink or escapes the agent vault. Remove the offending "
                    "symlink; legitimate runs are real files written in-vault."
                ),
                detail={
                    "backend_id": backend.backend_id,
                    "outcome_runs_present": True,
                    "run_id_probed": latest_run_id,
                    "error": str(e),
                    "error_type": type(e).__name__,
                },
            )
        except AtomicAgentsError:
            # TOCTOU: result.json vanished between list_runs() and read_result()
            # (e.g. a concurrent cleanup removed the run dir). That's "no result"
            # — benign, not corruption — so fall through to PASS rather than a
            # spurious FAIL for a transient race. (OutcomeCorrupted subclasses
            # AtomicAgentsError but is caught above, so only the bare absent-run
            # raise reaches here.)
            #
            # Do NOT mutate outcome_runs_present here: list_runs() DID return
            # run_ids, so run_count and the "no completed runs" message must stay
            # keyed to len(run_ids) (otherwise the detail dict would report
            # run_count >= 1 alongside a "no runs" message — a self-contradiction).
            # The vanished-run race is recorded distinctly via read_result_probed
            # staying False + the explicit detail note below.
            read_result_vanished = True
        except Exception as e:
            return CheckResult(
                name="outcome-backend",
                status=FAIL,
                message=f"outcome backend read_result() raised unexpected error: {type(e).__name__}: {e}",
                detail={
                    "backend_id": backend.backend_id,
                    "outcome_runs_present": True,
                    "run_id_probed": latest_run_id,
                    "error_type": type(e).__name__,
                    "error": str(e),
                },
            )

    caps = backend.capabilities()
    return CheckResult(
        name="outcome-backend",
        status=PASS,
        message=(
            f"outcome backend '{backend.backend_id}' ready"
            # Keyed to len(run_ids), NOT the read-result outcome, so the message
            # and detail.run_count never disagree (a TOCTOU vanished run leaves
            # run_count >= 1 but read_result_probed False — recorded in detail).
            + (" (no completed runs for this agent)" if not run_ids else "")
        ),
        detail={
            "backend_id": backend.backend_id,
            # outcome_runs_present reflects whether list_runs() found runs on
            # disk (kept truthful even when the latest run vanished mid-check).
            "outcome_runs_present": outcome_runs_present,
            "run_count": len(run_ids),
            "read_result_probed": read_result_probed,
            # True only on the benign TOCTOU path: list_runs() returned a run but
            # read_result() found it gone (concurrent cleanup). PASS is correct.
            "read_result_vanished": read_result_vanished,
            "supports_canonical_export": caps.supports_canonical_export,
            "supports_artifact_storage": caps.supports_artifact_storage,
        },
    )


def check_journal_backend(agent_root: Path) -> CheckResult:
    """JournalBackend resolves and the configured backend instantiates cleanly.

    Validates that ATOMIC_AGENTS_JOURNAL_BACKEND is correctly configured.
    Scoped at agent_root because the journal backend is per-agent — journal
    entries live under agent_root/journal/YYYY-MM/YYYY-MM-DD.md.

    Doctor dual-probe pattern (MEMORY.md feedback_doctor_dual_probe_pattern):
    Probes BOTH:
    1. list_entries(limit=1) — lightweight enumeration (MUST NOT raise even when
       journal/ is absent; returns [] for agents with no journal entries)
    2. entry.path.read_bytes() — heavy read path for the first returned entry
       (only when list_entries() returns at least one entry)

    When no entries exist (new agent or agent that has never written a journal
    entry), returns PASS with journal_entries_found=0, read_bytes_probed=False
    in the detail dict. This is NOT a failure — matching check_goal_backend's
    pattern for agents without a goal.md.

    PASS / FAIL ladder (this check has no WARN path):
    FAIL when:
    * get_default_journal_backend(agent_root) raises (bad env var or not registered).
    * list_entries() raises.
    * A symlinked journal/ DIRECTORY escapes agent_root. NOTE: list_entries()
      does NOT surface this — the filesystem backend's _journal_dir() raises
      PathTraversalError, which list_entries() CATCHES and returns [] (an absent
      journal). That would silently PASS the operator's vault even though every
      runtime read drops the ENTIRE journal. So doctor probes the escape vector
      DIRECTLY: when the backend exposes a _journal_dir() helper, doctor calls it
      and FAILs on PathTraversalError. This is the real misconfiguration class
      doctor exists to catch (a journal/ symlinked outside the vault).
    * Entries exist AND the existing file is unreadable for a NON-benign reason
      (PermissionError, or any unexpected error from read_bytes()).

    Deliberately NOT a FAIL (#427 PR1 ADOPT-NOW byte-identity ruling): an
    individual symlinked .md ENTRY that resolves outside agent_root. The runtime
    (bundle/agent/dream via list_entries) FOLLOWS such an entry exactly as the
    legacy rglob callers did, so doctor must agree with that runtime contract
    rather than FAIL on what the runtime reads
    (feedback_doctor_dual_probe_pattern: doctor's verdict and runtime behavior
    cannot disagree). Only a symlinked DIRECTORY escape is refused.

    PASS otherwise, including:
    * when no entries exist, and
    * benign TOCTOU — the entry vanished between list_entries() and read_bytes()
      (FileNotFoundError), e.g. a concurrent archive/cleanup removed the file.
    """
    from .journal import (  # noqa: PLC0415
        get_default_journal_backend,
        list_journal_backends,
        _redact_for_error_message,
    )
    from .exceptions import PathTraversalError  # noqa: PLC0415

    raw_backend_id = (
        os.environ.get("ATOMIC_AGENTS_JOURNAL_BACKEND", "").strip().lower()
        or "filesystem"
    )
    # Credential safety: ATOMIC_AGENTS_JOURNAL_BACKEND may carry a URL- or
    # DSN-shaped value. Redact before it reaches the rendered CheckResult
    # message or detail. Same per-backend _redact_for_error_message convention
    # as logs/profile/corpus/mcp_registry/secret_backend/goal/outcome.
    safe_backend_id = _redact_for_error_message(raw_backend_id)

    try:
        backend = get_default_journal_backend(agent_root)
    except Exception as e:
        return CheckResult(
            name="journal-backend",
            status=FAIL,
            message=(
                f"failed to instantiate journal backend "
                f"(ATOMIC_AGENTS_JOURNAL_BACKEND={safe_backend_id!r}): {e}"
            ),
            fix_hint=(
                "Unset ATOMIC_AGENTS_JOURNAL_BACKEND to use the filesystem default, "
                f"or set it to a registered backend id (known: {list_journal_backends()}). "
            ),
            detail={"backend_id": safe_backend_id, "error": str(e)},
        )

    # Normalized permissions-class fix_hint, shared between the list_entries and
    # read_bytes probes so a PermissionError surfaces the SAME remediation
    # regardless of which probe trips first (the journal/ DIRECTORY denied → the
    # list probe; an individual ENTRY file denied → the read_bytes probe). The
    # construction probe keeps its own env-var fix_hint because a construction
    # failure is a config error (bad/unregistered backend id), not a permissions
    # error. The verdict stays FAIL in every case; only the fix_hint is unified.
    _PERMS_FIX_HINT = (
        "A path under journal/ exists but cannot be read (permissions). "
        "Fix the file or directory's mode or ownership "
        "(e.g. chmod/chown the journal/ directory and its entry files)."
    )

    # Dual-probe step 1: lightweight list
    try:
        entries = backend.list_entries(limit=1, newest_first=True)
    except PermissionError as e:
        # journal/ directory (or an entry within it) is permission-denied and
        # the backend's list surfaced it as a raise rather than degrading to [].
        # Normalize to the shared permissions fix_hint so this matches the
        # read_bytes probe's remediation wording exactly.
        return CheckResult(
            name="journal-backend",
            status=FAIL,
            message=f"journal backend list_entries() raised: {type(e).__name__}: {e}",
            fix_hint=_PERMS_FIX_HINT,
            detail={
                "backend_id": backend.backend_id,
                "error": str(e),
                "error_type": type(e).__name__,
            },
        )
    except Exception as e:
        return CheckResult(
            name="journal-backend",
            status=FAIL,
            message=f"journal backend list_entries() raised: {type(e).__name__}: {e}",
            detail={
                "backend_id": backend.backend_id,
                "error": str(e),
                "error_type": type(e).__name__,
            },
        )

    # Directory-escape probe: a symlinked journal/ DIRECTORY pointing outside
    # agent_root is the real escape vector — but list_entries() CATCHES the
    # backend's PathTraversalError and returns [] (an absent journal), so the
    # step-1 probe above PASSes it silently. That is a genuine operator
    # misconfiguration where every runtime read drops the ENTIRE journal, which
    # is exactly the class doctor exists to catch. Probe it DIRECTLY via the
    # filesystem backend's _journal_dir() helper (guarded by hasattr so non-fs
    # backends are unaffected). This is distinct from — and does NOT reintroduce
    # — the per-entry symlink re-check the ADOPT-NOW ruling forbids: an
    # individual symlinked .md ENTRY resolving outside agent_root is still a
    # deliberate PASS (followed through for byte-identity); only a symlinked
    # DIRECTORY escape FAILs here.
    journal_dir_probe = getattr(backend, "_journal_dir", None)
    if callable(journal_dir_probe):
        try:
            journal_dir_probe()
        except PathTraversalError as e:
            return CheckResult(
                name="journal-backend",
                status=FAIL,
                message=(
                    f"journal/ directory escapes agent_root "
                    f"(symlink containment violation): {e}"
                ),
                fix_hint=(
                    "The agent's journal/ directory is a symlink resolving "
                    "outside agent_root. Every runtime read (system prompt, "
                    "dream consolidation) silently drops the entire journal. "
                    "Replace the symlink with a real directory under agent_root, "
                    "or move journal content back inside the vault."
                ),
                detail={
                    "backend_id": backend.backend_id,
                    "error": str(e),
                    "error_type": type(e).__name__,
                },
            )

    # Dual-probe step 2: heavy read (only when at least one entry exists).
    # When no entries exist, the dual-probe depth is limited — documented in
    # the check docstring and detail dict so operators know the probe scope.
    journal_entries_found = len(entries)
    read_bytes_probed = False
    if journal_entries_found > 0:
        latest_entry = entries[0]
        # NO per-entry symlink-containment re-check here (#427 PR1 ADOPT-NOW,
        # round-3 ruling). The byte-identity ruling requires list_entries()/
        # query_by_date() to FOLLOW individual symlinked .md entries exactly as
        # the three legacy rglob callers did (bundle/agent _safe_read_text and
        # dream read_text all follow symlinks). The runtime therefore READS a
        # symlinked-out entry through; doctor must agree with that runtime
        # contract rather than FAIL on what the runtime reads (a doctor verdict
        # that contradicts runtime behavior is the very thing
        # feedback_doctor_dual_probe_pattern forbids). The real escape vector —
        # a symlinked journal/ DIRECTORY pointing outside agent_root — is still
        # refused at the backend layer by _journal_dir()'s PathTraversalError
        # guard (surfaced here as a list_entries() FAIL above). An individual
        # symlinked day-file is deliberately tolerated by reads.
        try:
            latest_entry.path.read_bytes()
            read_bytes_probed = True
        except FileNotFoundError:
            # Benign TOCTOU: the entry vanished between list_entries() and
            # read_bytes() (e.g. a concurrent archive/cleanup removed the file).
            # read_bytes() raises FileNotFoundError (an OSError subclass), NOT
            # AtomicAgentsError — so this branch (not the generic Exception one)
            # is what actually catches the documented race. Fall through to PASS.
            pass
        except PermissionError as e:
            # A genuine read failure on an existing file (e.g. mode 000). This is
            # a real FAIL — the entry exists but cannot be read.
            return CheckResult(
                name="journal-backend",
                status=FAIL,
                message=(
                    f"journal entry exists but is unreadable: {type(e).__name__}: {e}"
                ),
                fix_hint=_PERMS_FIX_HINT,
                detail={
                    "backend_id": backend.backend_id,
                    "journal_entries_found": journal_entries_found,
                    "entry_path_probed": str(latest_entry.path),
                    "error": str(e),
                    "error_type": type(e).__name__,
                },
            )
        except Exception as e:
            return CheckResult(
                name="journal-backend",
                status=FAIL,
                message=(
                    f"journal backend entry.path.read_bytes() raised "
                    f"unexpected error: {type(e).__name__}: {e}"
                ),
                detail={
                    "backend_id": backend.backend_id,
                    "journal_entries_found": journal_entries_found,
                    "entry_path_probed": str(latest_entry.path),
                    "error": str(e),
                    "error_type": type(e).__name__,
                },
            )

    caps = backend.capabilities()
    return CheckResult(
        name="journal-backend",
        status=PASS,
        message=(
            f"journal backend '{backend.backend_id}' ready"
            + (
                " (no journal entries for this agent)"
                if not journal_entries_found
                else ""
            )
        ),
        detail={
            "backend_id": backend.backend_id,
            "journal_entries_found": journal_entries_found,
            "read_bytes_probed": read_bytes_probed,
            "supports_canonical_export": caps.supports_canonical_export,
            "supports_date_query": caps.supports_date_query,
        },
    )


def check_queue_backend(agent_root: Path) -> CheckResult:
    """QueueBackend resolves and the configured backend instantiates cleanly.

    Validates that ATOMIC_AGENTS_QUEUE_BACKEND is correctly configured.
    Scoped at agent_root but probes the CASCADE project queue — this check
    detects cascade via detect_cascade(agent_root) and derives project_root
    from the cascade. When no cascade is detected (single-agent layout),
    returns SKIP (no cascade queue to probe).

    Doctor dual-probe pattern (MEMORY.md feedback_doctor_dual_probe_pattern):
    Probes BOTH:
    1. list_claimed() — lightweight enumeration (MUST NOT raise even when
       claimed/ is absent; returns [] for projects with no active claims)
    2. get_default_queue_backend() instantiation — confirms env var is valid

    PASS / WARN / FAIL ladder (this check has a WARN path):
    SKIP when:
    * No cascade is detected for agent_root (single-agent layout, no project queue).

    FAIL when:
    * get_default_queue_backend(project_root) raises (bad env var or not registered).
    * list_claimed() raises.
    * _queue_root() raises PathTraversalError (symlinked queue/ escape).

    WARN when:
    * capabilities().single_host_only=True AND ATOMIC_AGENTS_MULTI_HOST=true
      (or '1'). A filesystem queue in a declared multi-host deployment may cause
      double-claims if workers run on different hosts. Operators should switch
      to a Redis/SQS/DB QueueBackend for multi-host safety.

    PASS otherwise (including when no queue/ exists for this project).

    Multi-host detection: ATOMIC_AGENTS_MULTI_HOST env var (set to 'true' or '1'
    by operators on Cloud Run / Kubernetes deployments). Defined in spec/44 §Doctor
    check. A follow-up issue should harmonize this with LockBackend's
    single_host_only WARN pattern — #TODO file during this PR.
    """
    from ._cascade import detect_cascade  # noqa: PLC0415
    from .queue import (  # noqa: PLC0415
        get_default_queue_backend,
        list_queue_backends,
        _redact_for_error_message,
    )
    from .exceptions import PathTraversalError  # noqa: PLC0415

    # Step 1: detect cascade to get project_root.
    cascade = detect_cascade(agent_root)
    if cascade is None:
        return CheckResult(
            name="queue-backend",
            status=SKIP,
            message="queue backend check skipped: agent is not in a multi-agent cascade project",
        )

    project_root = cascade.project_root

    raw_backend_id = (
        os.environ.get("ATOMIC_AGENTS_QUEUE_BACKEND", "").strip().lower()
        or "filesystem"
    )
    # Credential safety: ATOMIC_AGENTS_QUEUE_BACKEND may carry a URL- or
    # DSN-shaped value. Redact before it reaches the rendered CheckResult.
    safe_backend_id = _redact_for_error_message(raw_backend_id)

    try:
        backend = get_default_queue_backend(project_root)
    except Exception as e:
        return CheckResult(
            name="queue-backend",
            status=FAIL,
            message=(
                f"failed to instantiate queue backend "
                f"(ATOMIC_AGENTS_QUEUE_BACKEND={safe_backend_id!r}): {e}"
            ),
            fix_hint=(
                "Unset ATOMIC_AGENTS_QUEUE_BACKEND to use the filesystem default, "
                f"or set it to a registered backend id (known: {list_queue_backends()}). "
            ),
            detail={"backend_id": safe_backend_id, "error": str(e)},
        )

    # Dual-probe step 1: lightweight list
    try:
        claimed = backend.list_claimed()
    except Exception as e:
        return CheckResult(
            name="queue-backend",
            status=FAIL,
            message=f"queue backend list_claimed() raised: {type(e).__name__}: {e}",
            detail={
                "backend_id": backend.backend_id,
                "error": str(e),
                "error_type": type(e).__name__,
            },
        )

    # Directory-escape probe: a symlinked queue/ DIRECTORY pointing outside
    # project_root is the real escape vector. Probe via _queue_root() helper.
    queue_root_probe = getattr(backend, "_queue_root", None)
    if callable(queue_root_probe):
        try:
            queue_root_probe()
        except PathTraversalError as e:
            return CheckResult(
                name="queue-backend",
                status=FAIL,
                message=(
                    f"queue/ directory escapes project_root "
                    f"(symlink containment violation): {e}"
                ),
                fix_hint=(
                    "The project's queue/ directory is a symlink resolving "
                    "outside project_root. Replace the symlink with a real "
                    "directory under project_root."
                ),
                detail={
                    "backend_id": backend.backend_id,
                    "error": str(e),
                    "error_type": type(e).__name__,
                },
            )

    # Single-host WARN: check if backend claims single_host_only=True in a
    # declared multi-host deployment (ATOMIC_AGENTS_MULTI_HOST=true/1).
    caps = backend.capabilities()
    multi_host_declared = os.environ.get("ATOMIC_AGENTS_MULTI_HOST", "").lower() in (
        "1",
        "true",
    )
    if caps.single_host_only and multi_host_declared:
        return CheckResult(
            name="queue-backend",
            status=WARN,
            message=(
                f"queue backend '{backend.backend_id}' is single-host-only "
                f"but ATOMIC_AGENTS_MULTI_HOST is set. "
                f"Double-claims are possible across hosts."
            ),
            fix_hint=(
                "Switch to a Redis/SQS/DB QueueBackend for multi-host safety. "
                "Filesystem queue-claim atomicity (POSIX rename) does not extend "
                "across hosts — two workers on different hosts may claim the same item."
            ),
            detail={
                "backend_id": backend.backend_id,
                "single_host_only": caps.single_host_only,
                "multi_host_declared": multi_host_declared,
                "active_claims": len(claimed),
            },
        )

    return CheckResult(
        name="queue-backend",
        status=PASS,
        message=f"queue backend '{backend.backend_id}' ready",
        detail={
            "backend_id": backend.backend_id,
            "project_root": str(project_root),
            "single_host_only": caps.single_host_only,
            "supports_canonical_export": caps.supports_canonical_export,
            "active_claims": len(claimed),
        },
    )


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
