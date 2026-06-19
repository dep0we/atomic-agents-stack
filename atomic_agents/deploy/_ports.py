"""deploy/_ports.py — port resolution + pre-bootstrap socket-bind probe.

spec/48 §"Port resolution" + MUST 10. Port precedence (highest first):

    deploy --port  >  ATOMIC_AGENTS_SERVE_PORT  >  serve.md Bind Port  >  default

The resolved value is passed EXPLICITLY via ``--port`` in the launchd
``ProgramArguments`` so serve (running inside launchd, where deploy cannot read
its bind error) binds the port deploy verified. Because serve binds in a
separate process, a conflict is detected by a PRE-bootstrap socket-bind probe;
on conflict deploy fails loud naming the port and MUST NOT silently rebind.

Testability: the bind probe is routed through an injectable ``binder`` callable
so tests assert "conflict → clear error, no rebind" without opening a real
socket.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import Callable

from ..serve._config import ServeConfig, load_serve_config

# Default serve port (mirrors ServeConfig.port / spec/37).
DEFAULT_PORT = ServeConfig.port

# A binder takes (host, port) and returns True if the port is bindable (free),
# False if it is already in use. socket-based default; tests inject a fake.
Binder = Callable[[str, int], bool]


class PortConflictError(Exception):
    """Raised when the resolved port is already in use (spec/48 MUST 10).

    Carries the conflicting port so callers can name it in the failure message
    and never silently rebind.
    """

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        super().__init__(
            f"port {port} on {host} is already in use; "
            f"override with `deploy --port <N>` or ATOMIC_AGENTS_SERVE_PORT. "
            f"deploy will NOT silently pick a different port."
        )


def resolve_port(
    agent_root: Path,
    *,
    cli_port: int | None = None,
    environ: dict[str, str] | None = None,
) -> int:
    """Resolve the serve port using serve's own precedence (spec/48 MUST 10).

    Order: ``cli_port`` (deploy --port) > ``ATOMIC_AGENTS_SERVE_PORT`` env >
    ``serve.md`` Bind Port > ``DEFAULT_PORT``.

    Note: ``load_serve_config`` already applies the env-var override on top of
    serve.md, so the file+env+default tail is resolved by serve itself; this
    function only layers the explicit ``deploy --port`` on top, keeping deploy's
    precedence identical to serve's.

    Raises ``ValueError`` (propagated from the serve.md parser) when serve.md or
    the env var carries a malformed integer — MUST NOT silently fall back.
    """
    if cli_port is not None:
        return cli_port

    # Temporarily honour a passed-in environ for testability without mutating
    # the real process env. load_serve_config reads os.environ directly, so we
    # swap it for the duration of the call when an override is supplied.
    if environ is None:
        cfg = load_serve_config(agent_root)
        return cfg.port

    saved = dict(os.environ)
    try:
        os.environ.clear()
        os.environ.update(environ)
        cfg = load_serve_config(agent_root)
        return cfg.port
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _socket_binder(host: str, port: int) -> bool:
    """Production binder: try to bind (host, port); return True if free.

    Binds with SO_REUSEADDR off-by-default semantics: we want a true conflict
    signal, so we attempt an actual bind and release it immediately. A
    successful bind means the port is free; ``OSError`` (EADDRINUSE) means it is
    taken.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def probe_port_free(
    host: str,
    port: int,
    *,
    binder: Binder = _socket_binder,
) -> None:
    """Pre-bootstrap probe: raise ``PortConflictError`` if the port is taken.

    spec/48 MUST 10: a bind conflict detected by this probe MUST fail loud
    naming the port and MUST NOT silently rebind.
    """
    if not binder(host, port):
        raise PortConflictError(host, port)
