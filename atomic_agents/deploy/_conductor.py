"""deploy/_conductor.py — the planner + executor (spec/48).

This module ties the deploy sub-modules together. It builds an ordered, tagged
:class:`Plan` (the *planner*), then runs each step (the *executor*), then
verifies on loopback, then prints exposure guidance. It owns no new runtime —
it sequences ``init`` / ``doctor`` / ``serve`` (MUST 1).

Flow (ASCII)::

    deploy(agent)
        │
        ├─ plan_deploy(agent)  ── pure: emit ordered [tag] steps ──┐
        │                                                          │
        │   --plan?  ─► print plan, return 0  (MUST 6: no side     │
        │                effect, no billed call) ◄─────────────────┘
        │
        ▼ execute (only when not --plan)
        1. preflight        [auto]     python/PATH/ROOT resolved
        2. agent-exists     [consent]  init handoff OR fail (--yes)
        3. doctor-gate      [auto]     run_doctor + overall_exit_code==0
        4. provider-key     [manual]   check_provider_keys; pause+recheck
        5. supervise        [consent]  resolve port → probe → render → bootstrap
        6. verify           [auto]     healthz==ok AND doctor exit==0  (loopback)
        │       │
        │       └─ FAIL ─► rollback: bootout + remove plist (MUST 8) ─► return !=0
        ▼
        7. exposure         [manual]   GUIDE not perform: print tailored command
        │
        ▼ return 0

    deploy_status(agent)  ─► read launchd (MUST 12) ─► absent/loaded/running/crashed
    deploy_down(agent)    ─► bootout + remove plist (MUST 12)

Testability: every system interaction is routed through the sub-modules' own
injectable seams (``runner`` for launchctl/tailscale, ``binder`` for the bind
probe, ``http_get``/``http_post`` for verification). The executor itself takes
those seams as keyword arguments and threads them down, so a test can drive a
full deploy without touching the host.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .._model import parse_model_md
from .._platform import get_agents_root
from . import _exposure, _launchd, _ports, _verify
from ._types import DeployState, LaunchdStatus, Plan, Step, StepTag

_LOOPBACK_HOST = "127.0.0.1"


class DeployError(Exception):
    """Raised for a deploy failure deploy must surface to the operator.

    Carries a human-readable message and an exit code (default 1). The CLI
    maps this to ``print(str(e)) + return e.exit_code``.
    """

    def __init__(self, message: str, *, exit_code: int = 1) -> None:
        self.exit_code = exit_code
        super().__init__(message)


# A prompter takes a one-line question and returns True (consent) / False.
# The production default reads from stdin; tests inject a fake.
Prompter = Callable[[str], bool]

# An init_runner takes (agent, agents_root) and runs `atomic-agents init`,
# returning a process exit code (0 = success). The production default invokes
# the existing init wizard entry point; tests inject a fake so the consent
# handoff is exercised without launching the interactive wizard.
InitRunner = Callable[[str, Path], int]


def _default_prompter(question: str) -> bool:
    """Production consent prompt: ask on stdout, read y/N from stdin."""
    try:
        answer = input(f"{question} [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def _default_init_runner(agent: str, agents_root: Path) -> int:
    """Production init handoff: invoke the existing `atomic-agents init` wizard.

    spec/48 MUST 1 — deploy drives ``init`` through its existing entry point; it
    does NOT reimplement scaffolding. Builds the args-shaped object the wizard
    expects (``agent_name`` / ``from_template`` / ``list_templates`` /
    ``agents_root``) and returns its exit code.
    """
    from types import SimpleNamespace

    from ..init import run_init

    return run_init(
        SimpleNamespace(
            agent_name=agent,
            from_template=None,
            list_templates=False,
            agents_root=str(agents_root),
        )
    )


# ──────────────────────────────────────────────────────────────────────────
# Planner — pure, side-effect-free (MUST 6)
# ──────────────────────────────────────────────────────────────────────────


def plan_deploy(agent: str) -> Plan:
    """Build the ordered, tagged deployment plan for ``agent`` (spec/48).

    Pure function: NO filesystem mutation, NO subprocess, NO billed/LLM call
    (MUST 6). The executor consumes the returned :class:`Plan`; ``--plan``
    renders it and exits.

    The step tags encode the predicate from spec/48 §"Execution model":
      - ``auto``    user-space, no shared consequence — runs silently.
      - ``consent`` touches shared/user state — prompts unless ``--yes``.
      - ``manual``  operator-owned — print instructions, pause/finish.
    """
    steps = [
        Step(
            key="preflight",
            tag=StepTag.AUTO,
            title="Preflight: Python + PATH + ATOMIC_AGENTS_ROOT",
            detail="Confirm interpreter version and resolve the agents root.",
        ),
        Step(
            key="agent-exists",
            tag=StepTag.CONSENT,
            title="Ensure the agent folder exists (hand off to init if missing)",
            detail="init is interactive and writes files; never an auto step.",
        ),
        Step(
            key="doctor-gate",
            tag=StepTag.AUTO,
            title="doctor gate — preflight checks must pass",
            detail="Runs doctor --no-mcp; overall_exit_code must be 0.",
        ),
        Step(
            key="provider-key",
            tag=StepTag.MANUAL,
            title="Provider key present (env / Keychain / keys.json)",
            detail="deploy never stores the key; it verifies one resolves.",
        ),
        Step(
            key="supervise",
            tag=StepTag.CONSENT,
            title="Resolve port, probe it, install the launchd serve agent",
            detail="Renders ~/Library/LaunchAgents/<label>.plist and bootstraps it.",
        ),
        Step(
            key="verify",
            tag=StepTag.AUTO,
            title="Verify on loopback (healthz == ok AND doctor exit == 0)",
            detail="Non-mutating, unbilled. On failure: roll back the install.",
        ),
        Step(
            key="exposure",
            tag=StepTag.MANUAL,
            title="Exposure guidance (deploy guides; the operator performs)",
            detail="Prints the exact tailscale command or a perimeter-doc pointer.",
        ),
    ]
    return Plan(agent=agent, steps=steps)


# ──────────────────────────────────────────────────────────────────────────
# Executor helpers
# ──────────────────────────────────────────────────────────────────────────


def _resolve_agents_root(agents_root: str | Path | None) -> Path:
    """Resolve the agents root from an explicit override or the environment."""
    if agents_root is not None:
        return Path(agents_root).expanduser().resolve()
    return get_agents_root()


def _consent(
    tag_question: str,
    *,
    assume_yes: bool,
    prompter: Prompter,
) -> bool:
    """Return True if a consent step may proceed.

    ``--yes`` (assume_yes) auto-approves consent steps (spec/48 §"Execution
    model"). Otherwise the operator is prompted.
    """
    if assume_yes:
        return True
    return prompter(tag_question)


def _step_agent_exists(
    agent: str,
    agent_root: Path,
    agents_root: Path,
    *,
    assume_yes: bool,
    prompter: Prompter,
    init_runner: "InitRunner",
    out,
) -> None:
    """Step 2 — the agent folder must exist (spec/48 step 2).

    ``init`` is interactive and writes files, so it is never an ``auto`` step
    (it is tagged ``consent``). spec/48 step 2:
      - Without ``--yes``: hand off to ``atomic-agents init <agent>`` after the
        operator consents (the wizard itself is interactive). If the handoff
        succeeds and the folder now exists, the step passes.
      - With ``--yes`` (non-interactive): fail-fast with the exact ``init``
        command — deploy will NOT launch the interactive wizard unattended.
    """
    if agent_root.is_dir():
        return

    if assume_yes:
        raise DeployError(
            f"agent {agent!r} not found at {agent_root}.\n"
            f"Run `atomic-agents init {agent}` first (it is interactive and "
            f"writes files, so deploy will not run it for you in --yes mode).",
            exit_code=1,
        )

    # Interactive consent before handing off to the init wizard (consent step).
    if not prompter(
        f"agent {agent!r} not found at {agent_root}. "
        f"Run `atomic-agents init {agent}` now?"
    ):
        raise DeployError(
            f"agent {agent!r} not found and init declined. "
            f"Run `atomic-agents init {agent}` first, then re-run deploy.",
            exit_code=1,
        )

    print(f"      Handing off to `atomic-agents init {agent}`...", file=out)
    rc = init_runner(agent, agents_root)
    if rc != 0 or not agent_root.is_dir():
        raise DeployError(
            f"`atomic-agents init {agent}` did not complete "
            f"(exit {rc}); the agent folder still does not exist at "
            f"{agent_root}. Fix init, then re-run deploy.",
            exit_code=1,
        )


def _step_doctor_gate(
    agent: str,
    agents_root: Path,
) -> None:
    """Step 3 — doctor must pass (spec/48 step 3).

    Runs ``doctor --no-mcp`` through the existing entry point (MUST 1) and
    fails loud when any check fails. No LLM call: doctor is unbilled.
    """
    from .. import doctor as doctor_module

    results = doctor_module.run_doctor(
        agent_name=agent,
        agents_root=agents_root,
        skip_mcp=True,
    )
    if doctor_module.overall_exit_code(results) != 0:
        failed = [r.name for r in results if r.failed]
        raise DeployError(
            "doctor gate failed — fix these before deploying: "
            + ", ".join(failed)
            + f"\nRun `atomic-agents doctor --agent {agent} --no-mcp` for detail.",
            exit_code=1,
        )


def _step_provider_key(
    agent: str,
    agent_root: Path,
) -> None:
    """Step 4 — a provider key must resolve (spec/48 step 4).

    Reuses doctor's ``check_provider_keys`` so deploy's verdict and the
    runtime's key resolution can never disagree. deploy does NOT store the
    key (spec/38 SecretBackend is read-only); it only confirms one is
    reachable and otherwise prints the three setup options.
    """
    from .. import doctor as doctor_module

    model_md = agent_root / "model.md"
    model_data = parse_model_md(model_md if model_md.exists() else None)
    results = doctor_module.check_provider_keys(model_data)

    failed = [r for r in results if r.failed]
    if failed:
        hints = "\n".join(f"  - {r.message}\n{r.fix_hint}" for r in failed)
        raise DeployError(
            "provider key not found. deploy does not store keys — set one of:\n"
            f"{hints}\n"
            "Then re-run `atomic-agents deploy " + agent + "`.",
            exit_code=1,
        )


def _resolve_env_only_provider_key(
    agent_root: Path,
    *,
    environ: dict[str, str] | None = None,
) -> tuple[str, str] | None:
    """Return ``(env_name, value)`` iff a provider key's SOLE source is an env var.

    spec/48 MUST 5 / step list step 4 — the env-var-only operator path. The
    step-4 gate confirms a key resolves in the DEPLOYING shell, but a
    ``gui/$UID`` launchd agent does NOT inherit that shell's env. When the key's
    only source is an env var, serve started by launchd would not find it, so
    deploy MUST inject it into the plist as ``KEY=VALUE`` (with the documented
    cleartext caveat). When the source is Keychain / keys.json, deploy MUST NOT
    inject it — serve's ``_llm._get_key()`` reads those at runtime.

    Detection reuses the production resolvers so deploy's verdict and the
    runtime's key resolution can never disagree:
      - find the winning env var (the highest-priority env alias that is set +
        non-empty) for each provider in model.md;
      - probe the NON-env sources (Keychain, keys.json) directly; if neither
        holds the key, env is the sole source → inject.

    Returns None when no env-only key is found (key absent — already caught by
    step 4 — or key also reachable from a non-env source).
    """
    from .. import doctor as doctor_module
    from ..secret_backend.filesystem import (
        _resolve_from_keychain,
        _resolve_from_keys_json,
    )

    env = environ if environ is not None else dict(os.environ)

    model_md = agent_root / "model.md"
    model_data = parse_model_md(model_md if model_md.exists() else None)

    # The providers deploy must supply a key for (default + fallback), mapped to
    # their (keychain_name, env_vars, config_key) triple via doctor's table.
    providers: list[str] = []
    seen: set[str] = set()
    for key in ("default_model", "fallback_model"):
        mid = model_data.get(key)
        if not mid:
            continue
        prov = doctor_module._provider_for_model(mid)
        if prov and prov not in seen:
            providers.append(prov)
            seen.add(prov)

    for provider in providers:
        spec = doctor_module._PROVIDER_KEYS.get(provider)
        if spec is None:
            continue
        keychain_name, env_vars, config_key, _sdk = spec
        if not env_vars:
            # e.g. vertex-gemini uses ADC, not an env-var key — nothing to inject.
            continue

        # The winning env var: first alias that is set + non-empty (matches the
        # resolver's env-first, alias-order precedence).
        env_name: str | None = None
        env_value: str | None = None
        for candidate in env_vars:
            val = env.get(candidate)
            if val is not None and val.strip():
                env_name, env_value = candidate, val.strip()
                break
        if env_name is None:
            # No env source for this provider — either absent (step 4 catches
            # it) or it lives only in Keychain/keys.json (no injection needed).
            continue

        # Probe the NON-env sources. If EITHER holds the key, env is not the
        # sole source → serve will find it at runtime → do NOT inject.
        if _resolve_from_keychain(keychain_name) is not None:
            continue
        if _resolve_from_keys_json(config_key) is not None:
            continue

        # Env is the sole source for this provider's key — inject it.
        return env_name, env_value

    return None


@dataclass
class _SupervisionResult:
    """Outcome of the supervise step: the resolved port + the plist path."""

    port: int
    plist_path: Path
    wrote_plaintext_key: bool


def _step_supervise(
    agent: str,
    agent_root: Path,
    agents_root: Path,
    *,
    cli_port: int | None,
    environ: dict[str, str] | None,
    launchd_runner: _launchd.Runner,
    binder: _ports.Binder,
    launch_agents_dir: Path | None,
) -> _SupervisionResult:
    """Step 5 — resolve the port, probe it, render + bootstrap the agent.

    spec/48 §"Port resolution" + §"Supervision":
      - resolve port (deploy --port > env > serve.md > default),
      - pre-bootstrap socket-bind probe (MUST 10 — conflict fails loud),
      - render the plist (MUST 4/5), then bootstrap (MUST 7 idempotent).
    """
    # MUST 10 — port validation precedes probing: an out-of-range port (0 or
    # >65535) fails loud here, before the bind probe / launchd ever sees it.
    try:
        port = _ports.resolve_port(agent_root, cli_port=cli_port, environ=environ)
    except _ports.PortRangeError as exc:
        raise DeployError(str(exc), exit_code=1) from exc

    # MUST 10 — pre-bootstrap bind probe. A conflict raises PortConflictError;
    # we surface it loud and never silently rebind.
    try:
        _ports.probe_port_free(_LOOPBACK_HOST, port, binder=binder)
    except _ports.PortConflictError as exc:
        raise DeployError(str(exc), exit_code=1) from exc

    # MUST 5 — env-var-only provider key. A gui/$UID launchd serve does NOT
    # inherit the deploying shell, so if the key's sole source is an env var we
    # inject it into the plist as KEY=VALUE (render_plist prints the cleartext
    # caveat). Keychain/keys.json sources are read by serve at runtime — never
    # injected.
    plaintext_key = _resolve_env_only_provider_key(agent_root, environ=environ)

    rendered = _launchd.render_plist(
        agent,
        port,
        agents_root=agents_root,
        environ=environ,
        plaintext_key=plaintext_key,
    )

    try:
        plist_path = _launchd.install_launchd_agent(
            agent,
            rendered.plist_bytes,
            launch_agents_dir=launch_agents_dir,
            runner=launchd_runner,
        )
    except _launchd.DeployLaunchdError as exc:
        raise DeployError(
            f"failed to install the launchd agent: {exc}", exit_code=1
        ) from exc

    return _SupervisionResult(
        port=port,
        plist_path=plist_path,
        wrote_plaintext_key=rendered.wrote_plaintext_key,
    )


def _rollback_and_report(
    agent: str,
    *,
    supervision: _SupervisionResult,
    launchd_runner: _launchd.Runner,
    launch_agents_dir: Path | None,
    binder: _ports.Binder,
    verify_exc: Exception | None,
    out,
    err,
) -> int:
    """Tear down a just-installed agent after a failed verify (spec/48 MUST 8).

    Boots out the launchd agent and removes the plist deploy wrote — no
    bootstrapped-but-broken service is left behind. Returns the non-zero exit
    code. Called for BOTH a False verify predicate and a verify that raised
    (``verify_exc`` is the raised exception, or None for a clean predicate
    failure).

    MUST 10 (post-bootstrap address-in-use): before rolling back, re-probe the
    port. If it is now bound and deploy does not own a healthy serve there, the
    likely cause is another process holding the port — report
    "address in use :<port>" explicitly so the operator does not chase a
    phantom serve bug. Best-effort: a re-probe that itself errors is ignored.
    """
    # MUST 10 — post-bootstrap address-in-use detection (best-effort).
    addr_in_use = False
    try:
        # binder returns True when the port is FREE; False when bound. If the
        # port is now bound (not free) after our verify failed, something is
        # holding it — surface that as the proximate cause.
        addr_in_use = binder(_LOOPBACK_HOST, supervision.port) is False
    except Exception:  # noqa: BLE001 — re-probe is best-effort, never fatal
        addr_in_use = False

    # MUST 8 — bootout the just-installed agent and remove the plist. If the
    # bootout itself fails (a real launchctl error, not "already absent"),
    # teardown raises and leaves the plist in place — surface that rather than
    # claim a clean rollback we could not perform.
    try:
        _launchd.teardown_launchd_agent(
            agent,
            launch_agents_dir=launch_agents_dir,
            runner=launchd_runner,
            remove_plist=True,
        )
    except _launchd.DeployLaunchdError as exc:
        print(
            f"Error: verification failed AND rollback could not complete: {exc}\n"
            f"The launchd agent {_launchd.label_for(agent)} may still be loaded; "
            f"run `atomic-agents deploy down {agent}` to retry teardown.",
            file=err,
        )
        return 1

    if addr_in_use:
        print(
            f"Error: address in use :{supervision.port} — another process is "
            f"bound to {_LOOPBACK_HOST}:{supervision.port}, so the supervised "
            "serve could not bind it.",
            file=err,
        )

    if verify_exc is not None:
        print(
            "Error: verification raised an unexpected error "
            f"({type(verify_exc).__name__}: {verify_exc}); rolled back the "
            f"launchd agent (booted out + removed {supervision.plist_path}).\n"
            f"Inspect logs at ~/Library/Logs/{_launchd.label_for(agent)}.err.log, "
            f"then re-run `atomic-agents deploy {agent}`.",
            file=err,
        )
    else:
        print(
            "Error: verification failed; rolled back the launchd agent "
            f"(booted out + removed {supervision.plist_path}).\n"
            f"Inspect logs at ~/Library/Logs/{_launchd.label_for(agent)}.err.log, "
            f"then re-run `atomic-agents deploy {agent}`.",
            file=err,
        )
    return 1


# ──────────────────────────────────────────────────────────────────────────
# Entry point: deploy
# ──────────────────────────────────────────────────────────────────────────


def deploy(
    agent: str,
    *,
    agents_root: str | Path | None = None,
    cli_port: int | None = None,
    plan_only: bool = False,
    assume_yes: bool = False,
    verify_call: bool = False,
    environ: dict[str, str] | None = None,
    out=None,
    err=None,
    prompter: Prompter = _default_prompter,
    init_runner: InitRunner = _default_init_runner,
    launchd_runner: _launchd.Runner = _launchd._default_runner,
    binder: _ports.Binder = _ports._socket_binder,
    exposure_runner: _exposure.Runner = _exposure._default_runner,
    http_get: _verify.HttpGet = _verify._default_http_get,
    http_post: _verify.HttpPost = _verify._default_http_post,
    launch_agents_dir: Path | None = None,
    verify_retries: int = 10,
    verify_retry_delay_s: float = 0.5,
) -> int:
    """Plan + execute a loopback deployment, verify, then guide exposure.

    spec/48 — the conductor. Returns a process exit code (0 = success).

    With ``plan_only`` (the ``--plan`` flag) this prints the tagged plan and
    returns 0 with ZERO side effects: no filesystem write, no launchctl call,
    no billed/LLM call (MUST 6). The early return happens BEFORE any executor
    step, the bind probe, or any subprocess.

    All system seams are injectable (``launchd_runner``, ``binder``,
    ``exposure_runner``, ``http_get``/``http_post``, ``prompter``) so the full
    flow is unit-testable without touching the host.
    """
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr

    # ── Planner (always pure) ──────────────────────────────────────────────
    plan = plan_deploy(agent)

    # MUST 6 — --plan prints the plan and exits with no side effects.
    if plan_only:
        print(plan.render(), file=out)
        return 0

    # Validate the agent name early via the label slug rule (reuses init's
    # charset). An invalid name fails before any side effect.
    try:
        _launchd.label_for(agent)
    except ValueError as exc:
        print(f"Error: {exc}", file=err)
        return 1

    resolved_root = _resolve_agents_root(agents_root)
    agent_root = resolved_root / agent

    try:
        # Step 1 — preflight is implicit: agents_root resolution + the doctor
        # gate (step 3) cover python/vault. Nothing shared to consent for here.
        print(f"[1/7] Preflight — agents root: {resolved_root}", file=out)

        # Step 2 — agent folder must exist (consent: init handoff).
        print("[2/7] Checking the agent folder exists...", file=out)
        _step_agent_exists(
            agent,
            agent_root,
            resolved_root,
            assume_yes=assume_yes,
            prompter=prompter,
            init_runner=init_runner,
            out=out,
        )

        # Step 3 — doctor gate.
        print("[3/7] Running the doctor gate (doctor --no-mcp)...", file=out)
        _step_doctor_gate(agent, resolved_root)

        # Step 4 — provider key (manual).
        print("[4/7] Checking a provider key resolves...", file=out)
        _step_provider_key(agent, agent_root)

        # Step 5 — supervise (consent): resolve port, probe, render, bootstrap.
        if not _consent(
            f"Install a user-level launchd agent for {agent!r} (no sudo)?",
            assume_yes=assume_yes,
            prompter=prompter,
        ):
            print("Aborted before installing the launchd agent.", file=err)
            return 1
        print("[5/7] Installing the launchd serve agent...", file=out)
        supervision = _step_supervise(
            agent,
            agent_root,
            resolved_root,
            cli_port=cli_port,
            environ=environ,
            launchd_runner=launchd_runner,
            binder=binder,
            launch_agents_dir=launch_agents_dir,
        )
        if supervision.wrote_plaintext_key:
            print(
                "  Caveat: the provider key's only source was an env var, so it "
                "was written into the plist in cleartext (spec/48 MUST 5).",
                file=err,
            )

    except DeployError as exc:
        print(f"Error: {exc}", file=err)
        return exc.exit_code

    # ── Step 6 — verify on loopback. Rollback on failure (MUST 8). ──────────
    print("[6/7] Verifying on loopback (healthz + doctor)...", file=out)

    # MUST 8 — verify MUST NOT be able to leave the launchd agent installed.
    # ``verify_deployment``'s production http_get already converts transport
    # failures (connection-refused on a not-yet-bound serve) into a clean FAIL
    # sentinel, but a custom http seam or a downstream bug could still raise.
    # Wrap the WHOLE verify so ANY exception — not just a False predicate, not
    # just DeployError — triggers rollback + non-zero exit before we return.
    verify_exc: Exception | None = None
    result: _verify.VerifyResult | None = None
    try:
        result = _verify.verify_deployment(
            agent,
            _LOOPBACK_HOST,
            supervision.port,
            verify_call=verify_call,
            http_get=http_get,
            http_post=http_post,
            retries=verify_retries,
            retry_delay_s=verify_retry_delay_s,
        )
    except Exception as exc:  # noqa: BLE001 — rollback must catch everything
        verify_exc = exc

    if result is not None:
        for name, passed, message in result.checks:
            mark = "ok" if passed else "FAIL"
            print(f"      [{mark}] {name}: {message}", file=out)

    if verify_exc is not None or (result is not None and not result.ok):
        return _rollback_and_report(
            agent,
            supervision=supervision,
            launchd_runner=launchd_runner,
            launch_agents_dir=launch_agents_dir,
            binder=binder,
            verify_exc=verify_exc,
            out=out,
            err=err,
        )

    # ── Step 7 — exposure guidance (GUIDE, NEVER PERFORM). MUST 11. ─────────
    print("[7/7] Exposure guidance:", file=out)
    tailscale_present = _exposure.detect_tailscale(runner=exposure_runner)
    print(
        _exposure.exposure_guidance(
            supervision.port, tailscale_present=tailscale_present
        ),
        file=out,
    )

    print(
        f"\nDeployed {agent!r}: supervised on {_LOOPBACK_HOST}:{supervision.port} "
        "— survives until reboot (reboot-persistence TBD, issue #539).",
        file=out,
    )
    return 0


# ──────────────────────────────────────────────────────────────────────────
# Entry point: status
# ──────────────────────────────────────────────────────────────────────────


def deploy_status(
    agent: str,
    *,
    out=None,
    err=None,
    launchd_runner: _launchd.Runner = _launchd._default_runner,
    launch_agents_dir: Path | None = None,
) -> int:
    """Report the live deployment state, derived from launchd (spec/48 MUST 12).

    State is read at call time from plist existence + ``launchctl print``;
    NEVER from a cached sidecar. Prints one of ``absent`` / ``loaded`` /
    ``running`` / ``crashed`` plus PID and last-exit detail when available.

    Returns 0 when the agent is RUNNING or LOADED, 1 when ABSENT or CRASHED —
    so ``deploy status`` is usable as a shell health predicate.
    """
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr

    try:
        status: LaunchdStatus = _launchd.read_launchd_status(
            agent,
            launch_agents_dir=launch_agents_dir,
            runner=launchd_runner,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=err)
        return 1

    print(f"agent:      {agent}", file=out)
    print(f"label:      {status.label}", file=out)
    print(f"state:      {status.state.value}", file=out)
    print(f"plist:      {status.plist_path}", file=out)
    if status.pid is not None:
        print(f"pid:        {status.pid}", file=out)
    if status.last_exit_status is not None:
        print(f"last exit:  {status.last_exit_status}", file=out)

    # RUNNING / LOADED are healthy-ish (0); ABSENT / CRASHED are not (1).
    if status.state in (DeployState.RUNNING, DeployState.LOADED):
        return 0
    return 1


# ──────────────────────────────────────────────────────────────────────────
# Entry point: down
# ──────────────────────────────────────────────────────────────────────────


def deploy_down(
    agent: str,
    *,
    out=None,
    err=None,
    launchd_runner: _launchd.Runner = _launchd._default_runner,
    launch_agents_dir: Path | None = None,
) -> int:
    """Tear a deployment down: bootout + remove the plist (spec/48 MUST 12).

    Full teardown — the launchd label is booted out of ``gui/$UID`` and the
    plist is removed so no deployment record remains (the plist IS the record,
    MUST 2). Idempotent: tearing down an absent deployment is a clean no-op.
    """
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr

    try:
        label = _launchd.label_for(agent)
        _launchd.teardown_launchd_agent(
            agent,
            launch_agents_dir=launch_agents_dir,
            runner=launchd_runner,
            remove_plist=True,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=err)
        return 1

    print(f"Tore down {agent!r} (booted out {label} + removed its plist).", file=out)
    return 0
