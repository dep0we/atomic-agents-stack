"""deploy/_launchd.py — macOS launchd renderer + install/teardown/status.

This module is **macOS-only by design** (spec/48 §"Supervision"). There is NO
host-adapter abstraction: the MVP optimizes the home/Mac path and adding a
Linux/systemd renderer is a later phase (CLAUDE.md: "Don't add abstractions for
hypothetical future needs").

Testability (spec/48 §"TESTABILITY"): every subprocess call (``launchctl``) is
routed through an injectable ``runner`` callable so unit tests can mock the
launchctl interactions without installing a real launchd agent. The plist
renderer is a pure function; the install/teardown functions accept a ``runner``
and an ``installed_root`` so tests point them at a tmp dir.

    plist render (pure)  ─┐
                          ├─► install_launchd_agent ─► runner(launchctl bootstrap)
    keys.json/Keychain ──┘
    runner(launchctl print) ─► read_launchd_status ─► DeployState
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..init import constants as _init_constants
from ._types import DeployState, LaunchdStatus

# A runner is any callable that takes an argv list and returns a CompletedProcess.
# subprocess.run is the production default; tests inject a fake.
Runner = Callable[..., "subprocess.CompletedProcess[str]"]

# launchd label namespace for atomic-agents serve agents. One label per agent.
_LABEL_PREFIX = "ai.atomic-agents.serve"


class DeployLaunchdError(Exception):
    """Raised when a launchctl interaction fails in a way deploy must surface."""


# launchctl interactions are local and fast; a hang means launchd is wedged.
# Bound every call so deploy cannot block indefinitely (spec/48 — no hung
# deploy). A timeout is mapped to the launchd error type by the caller.
_LAUNCHCTL_TIMEOUT_S = 30


def _default_runner(argv: list[str]) -> "subprocess.CompletedProcess[str]":
    """Production runner: run a subprocess, capture text output, never raise.

    We deliberately do NOT pass ``check=True`` — callers inspect ``returncode``
    so they can distinguish "label absent" (a benign non-zero from
    ``launchctl print``) from a real failure.

    A ``timeout`` bounds the call so a wedged launchd cannot hang deploy
    forever. On ``TimeoutExpired`` we return a non-zero CompletedProcess whose
    stderr names the timeout, so the returncode-inspecting callers treat it as a
    REAL failure (``DeployLaunchdError``) — never as the benign "label absent"
    case.
    """
    try:
        return subprocess.run(  # noqa: PLW1510 -- returncode inspected by caller
            argv,
            capture_output=True,
            text=True,
            timeout=_LAUNCHCTL_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            argv,
            returncode=124,  # conventional timeout exit code
            stdout="",
            stderr=f"launchctl timed out after {_LAUNCHCTL_TIMEOUT_S}s: {' '.join(argv)}",
        )


def label_for(agent: str) -> str:
    """Return the launchd label for an agent, validating the slug.

    The label is ``ai.atomic-agents.serve.<slug>`` where ``<slug>`` is the agent
    name run through the SAME charset/validation ``init`` enforces (spec/48
    §"Supervision" — a route/path segment is not guaranteed launchd-label-safe).

    Raises ``ValueError`` if the agent name is empty, too long, reserved, or
    fails the init charset.
    """
    name = (agent or "").strip()
    if not name:
        raise ValueError("agent name must not be empty")
    if len(name) > _init_constants.AGENT_NAME_MAX_LEN:
        raise ValueError(
            f"agent name too long: {len(name)} > {_init_constants.AGENT_NAME_MAX_LEN}"
        )
    if name in _init_constants.RESERVED_AGENT_NAMES:
        raise ValueError(f"agent name is reserved: {name!r}")
    if not _init_constants.AGENT_NAME_REGEX.match(name):
        raise ValueError(
            f"agent name {name!r} is not a valid launchd-label slug "
            "(alphanumeric + internal hyphen only)"
        )
    return f"{_LABEL_PREFIX}.{name}"


def plist_path_for(agent: str, *, launch_agents_dir: Path | None = None) -> Path:
    """Return the plist path for an agent's launchd label.

    Default location: ``~/Library/LaunchAgents/<label>.plist`` (spec/48). Tests
    pass ``launch_agents_dir`` pointed at a tmp dir.
    """
    label = label_for(agent)
    base = launch_agents_dir or (Path.home() / "Library" / "LaunchAgents")
    return base / f"{label}.plist"


def resolve_program_arguments(agent: str, port: int) -> list[str]:
    """Resolve the launchd ``ProgramArguments`` for ``serve``.

    spec/48 MUST 4: a ``gui/$UID`` agent does NOT inherit the interactive PATH,
    so the executable MUST be an ABSOLUTE path, not the bare ``atomic-agents``.
    Resolve via ``shutil.which("atomic-agents")``; fall back to the current
    interpreter running the module entry point.

    ``shutil.which`` can return a RELATIVE path when a relative entry (e.g. ``.``
    or ``./bin``) is on ``PATH``. A relative ProgramArguments[0] would not
    resolve under launchd (whose working directory is not the deploying shell's
    cwd), so we ``resolve()`` the hit and REQUIRE it to be absolute; if it is
    not (or which found nothing), fall back to ``sys.executable -m`` which is
    always absolute.
    """
    host = "127.0.0.1"
    console = shutil.which("atomic-agents")
    # A relative which() hit (a relative PATH entry was matched) is NOT usable
    # under launchd, whose cwd is not the deploying shell's. Only trust an
    # already-absolute hit; otherwise fall through to the sys.executable form.
    if console and Path(console).is_absolute():
        resolved = Path(console).resolve()
        if resolved.is_absolute():  # defensive; resolve() is always absolute
            return [
                str(resolved),
                "serve",
                agent,
                "--host",
                host,
                "--port",
                str(port),
            ]
    # Fallback: the running interpreter + module entry point. sys.executable is
    # always absolute.
    return [
        sys.executable,
        "-m",
        "atomic_agents.cli",
        "serve",
        agent,
        "--host",
        host,
        "--port",
        str(port),
    ]


@dataclass
class PlistRenderResult:
    """The rendered plist plus a flag noting whether a plaintext key was written.

    ``wrote_plaintext_key`` is True only when the provider key's sole source is
    an env var and it was injected as a ``KEY=VALUE`` env var (spec/48 MUST 5).
    Callers print the documented cleartext caveat when this is True.
    """

    plist_bytes: bytes
    program_arguments: list[str]
    environment_variables: dict[str, str]
    wrote_plaintext_key: bool


def render_plist(
    agent: str,
    port: int,
    *,
    agents_root: Path,
    environ: dict[str, str] | None = None,
    plaintext_key: tuple[str, str] | None = None,
) -> PlistRenderResult:
    """Render the launchd plist for an agent (pure function — no I/O).

    spec/48 §"Supervision":
      - Label ``ai.atomic-agents.serve.<slug>``.
      - ABSOLUTE ``ProgramArguments`` (``shutil.which`` or ``sys.executable``).
      - ``RunAtLoad`` + ``KeepAlive`` true.
      - ``EnvironmentVariables`` ALWAYS inject HOME / USER / PATH /
        ATOMIC_AGENTS_ROOT (MUST 5).
      - The provider key is NOT written into the plist by default. It is
        injected as a ``KEY=VALUE`` env var ONLY when ``plaintext_key`` is set
        (its sole source is an env var); the caller decides that and documents
        the cleartext caveat (MUST 5).

    ``environ`` defaults to ``os.environ`` (copied) so tests inject a controlled
    map. ``plaintext_key`` is an optional ``(env_name, value)`` pair.
    """
    env = dict(environ if environ is not None else os.environ)
    label = label_for(agent)
    program_args = resolve_program_arguments(agent, port)

    # MUST 5 — always inject the four base vars. Fall back to sane defaults so a
    # missing var in the deploying shell does not produce an empty/broken plist.
    environment_variables: dict[str, str] = {
        "HOME": env.get("HOME", str(Path.home())),
        "USER": env.get("USER", env.get("LOGNAME", "")),
        "PATH": env.get("PATH", "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"),
        "ATOMIC_AGENTS_ROOT": str(agents_root),
    }

    wrote_plaintext_key = False
    if plaintext_key is not None:
        env_name, env_value = plaintext_key
        environment_variables[env_name] = env_value
        wrote_plaintext_key = True

    plist_dict = {
        "Label": label,
        "ProgramArguments": program_args,
        "RunAtLoad": True,
        "KeepAlive": True,
        "EnvironmentVariables": environment_variables,
        # StandardOut/Error to a per-label log so a crashing serve leaves a
        # recoverable artifact (CLAUDE.md rule 8). Under HOME so it is user-space.
        "StandardOutPath": str(
            Path(environment_variables["HOME"]) / "Library" / "Logs" / f"{label}.log"
        ),
        "StandardErrorPath": str(
            Path(environment_variables["HOME"])
            / "Library"
            / "Logs"
            / f"{label}.err.log"
        ),
    }

    plist_bytes = plistlib.dumps(plist_dict)
    return PlistRenderResult(
        plist_bytes=plist_bytes,
        program_arguments=program_args,
        environment_variables=environment_variables,
        wrote_plaintext_key=wrote_plaintext_key,
    )


def _uid() -> int:
    """The current user's UID (the launchd ``gui/$UID`` domain target)."""
    return os.getuid()


def _is_bootstrapped(agent: str, *, runner: Runner = _default_runner) -> bool:
    """Return True if the label is currently bootstrapped in ``gui/$UID``.

    Uses ``launchctl print gui/$UID/<label>`` — exit 0 means the label is
    known to launchd; non-zero means it is not bootstrapped.
    """
    label = label_for(agent)
    cp = runner(["launchctl", "print", f"gui/{_uid()}/{label}"])
    return cp.returncode == 0


def install_launchd_agent(
    agent: str,
    plist_bytes: bytes,
    *,
    launch_agents_dir: Path | None = None,
    runner: Runner = _default_runner,
) -> Path:
    """Write the plist and bootstrap the launchd agent (spec/48 §"Supervision").

    MUST 7 (idempotent re-deploy): if the label is already bootstrapped, bootout
    it FIRST, then bootstrap the freshly-written plist — a clean restart, never
    a double-bind or error.

    MUST 3: never invokes ``sudo``; the domain is ``gui/$UID``.

    Returns the path the plist was written to. Raises ``DeployLaunchdError`` if
    bootstrap fails.
    """
    label = label_for(agent)
    plist_path = plist_path_for(agent, launch_agents_dir=launch_agents_dir)

    # MUST 7 — clean restart if already present.
    if _is_bootstrapped(agent, runner=runner):
        teardown_launchd_agent(
            agent,
            launch_agents_dir=launch_agents_dir,
            runner=runner,
            remove_plist=False,
        )

    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_bytes(plist_bytes)

    cp = runner(["launchctl", "bootstrap", f"gui/{_uid()}", str(plist_path)])
    if cp.returncode != 0:
        # Clean up the plist we just wrote so a failed bootstrap does not leave
        # an orphan plist on disk (CLAUDE.md rule 8 — no half-finished state).
        try:
            plist_path.unlink()
        except OSError:
            pass
        stderr = (cp.stderr or "").strip()
        raise DeployLaunchdError(
            f"launchctl bootstrap failed for {label!r} (exit {cp.returncode}): "
            f"{stderr or '<no stderr>'}"
        )
    return plist_path


def _bootout_indicates_absent(cp: "subprocess.CompletedProcess[str]") -> bool:
    """True iff a non-zero ``launchctl bootout`` means "service not loaded".

    A bootout of an absent label is benign (idempotent teardown). launchctl
    signals this with a small set of known codes / messages, which vary across
    macOS versions:
      - exit 3 (ESRCH "No such process"),
      - exit 113 ("Could not find specified service"),
      - stderr naming "no such process" / "could not find" / "not find".
    Any OTHER non-zero return is a REAL failure (e.g. EPERM, a domain error)
    and MUST NOT be silently tolerated (spec/48 MUST 7/8/12).
    """
    if cp.returncode == 0:
        return True
    if cp.returncode in (3, 113):
        return True
    text = ((cp.stderr or "") + " " + (cp.stdout or "")).lower()
    return (
        "no such process" in text
        or "could not find" in text
        or "not find specified service" in text
    )


def teardown_launchd_agent(
    agent: str,
    *,
    launch_agents_dir: Path | None = None,
    runner: Runner = _default_runner,
    remove_plist: bool = True,
) -> None:
    """Bootout the launchd agent and (optionally) remove its plist.

    spec/48 MUST 12 (``down`` is complete): ``remove_plist=True`` removes the
    plist so the deployment record is fully torn down. Idempotent: a bootout of
    an absent label is a no-op (CLAUDE.md rule 8).

    MUST 7/8/12 — bootout failures MUST NOT be ignored. Only the known
    "service not loaded" case is tolerated; ANY other non-zero return raises
    ``DeployLaunchdError`` so redeploy / rollback / down cannot falsely claim
    success while the old service is still loaded. The plist is removed ONLY
    after a clean bootout, so a failed teardown does not orphan the launchd
    record while erasing its on-disk evidence.
    """
    label = label_for(agent)
    cp = runner(["launchctl", "bootout", f"gui/{_uid()}/{label}"])
    if not _bootout_indicates_absent(cp):
        stderr = (cp.stderr or "").strip()
        raise DeployLaunchdError(
            f"launchctl bootout failed for {label!r} (exit {cp.returncode}): "
            f"{stderr or '<no stderr>'}. The old service may still be loaded; "
            "deploy will not remove the plist while it cannot confirm teardown."
        )

    if remove_plist:
        plist_path = plist_path_for(agent, launch_agents_dir=launch_agents_dir)
        try:
            plist_path.unlink()
        except FileNotFoundError:
            pass


def _parse_launchctl_print(text: str) -> tuple[int | None, int | None]:
    """Parse ``pid`` and ``last exit status`` from ``launchctl print`` output.

    Returns ``(pid, last_exit_status)``; each is None when not present. The
    ``launchctl print`` format is line-oriented like ``\tpid = 1234`` and
    ``\tlast exit code = 0``. We parse defensively — format drift across macOS
    versions should degrade to None, not crash.
    """
    pid: int | None = None
    last_exit: int | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("pid ="):
            try:
                pid = int(line.split("=", 1)[1].strip())
            except (ValueError, IndexError):
                pid = None
        elif line.startswith("last exit code =") or line.startswith(
            "last exit status ="
        ):
            val = line.split("=", 1)[1].strip()
            # launchctl sometimes reports "(never exited)" or a signal note.
            try:
                last_exit = int(val)
            except (ValueError, IndexError):
                last_exit = None
    return pid, last_exit


def read_launchd_status(
    agent: str,
    *,
    launch_agents_dir: Path | None = None,
    runner: Runner = _default_runner,
) -> LaunchdStatus:
    """Derive the live DeployState for an agent from launchd (spec/48 MUST 12).

    State is derived at call time from:
      - plist existence on disk,
      - ``launchctl print gui/$UID/<label>`` (bootstrapped? PID? last exit?).

    Never reads a cached sidecar. State mapping::

        plist absent + not bootstrapped         → ABSENT
        bootstrapped + live PID                 → RUNNING
        bootstrapped + no PID + last_exit != 0  → CRASHED
        bootstrapped + no PID (else)            → LOADED
        plist present but not bootstrapped       → LOADED (installed, not loaded)
    """
    label = label_for(agent)
    plist_path = plist_path_for(agent, launch_agents_dir=launch_agents_dir)
    plist_exists = plist_path.exists()

    cp = runner(["launchctl", "print", f"gui/{_uid()}/{label}"])
    bootstrapped = cp.returncode == 0

    if not bootstrapped:
        state = DeployState.ABSENT if not plist_exists else DeployState.LOADED
        return LaunchdStatus(
            state=state,
            label=label,
            plist_path=str(plist_path),
        )

    pid, last_exit = _parse_launchctl_print(cp.stdout or "")
    if pid is not None and pid > 0:
        state = DeployState.RUNNING
    elif last_exit is not None and last_exit != 0:
        state = DeployState.CRASHED
    else:
        state = DeployState.LOADED

    return LaunchdStatus(
        state=state,
        label=label,
        plist_path=str(plist_path),
        pid=pid,
        last_exit_status=last_exit,
    )
