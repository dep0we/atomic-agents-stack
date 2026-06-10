"""Path-traversal security tests for atomic_agents.dashboard.serve.DashboardHandler.

Verifies GHSA-rm43-82j9-r4mj fix: every served path is routed through
safe_resolve_under() so literal ``../`` traversal sequences are rejected
with HTTP 404 before any file read occurs.

Test matrix
-----------
- GET /../../<file>                    → 404  (root-level traversal)
- GET /../../../../etc/hosts           → 404  (deep traversal, absolute target)
- GET /_dashboard/../../<file>         → 404  (escape via static branch)
- GET /agents/../../../<file>          → 404  (escape via agents branch)
- GET /                                → 200  (legitimate index route still works)
- GET /_dashboard/index.html           → 200  (legitimate static route still works)
- GET /agents/<name>                   → 200  (legitimate agent dashboard still works)
"""

from __future__ import annotations

import io
import socket
from http.server import HTTPServer
from pathlib import Path
from threading import Thread

import pytest

from atomic_agents.dashboard.serve import DashboardHandler


# ---------------------------------------------------------------------------
# Test harness — minimal in-process HTTP server backed by a tmp agents_root
# ---------------------------------------------------------------------------


class _Handler(DashboardHandler):
    """Subclass that suppresses request logging during tests."""

    def log_message(self, fmt, *args):  # noqa: D401
        pass  # silence per-request stderr output in the test suite


def _make_server(agents_root: Path) -> HTTPServer:
    """Bind an ephemeral-port server against *agents_root*."""
    _Handler.agents_root = agents_root
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    return server


def _get(server: HTTPServer, path: str) -> int:
    """Issue a raw GET *path* against *server* and return the HTTP status code."""
    host, port = server.server_address
    sock = socket.create_connection((host, port), timeout=5)
    try:
        request = f"GET {path} HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n"
        sock.sendall(request.encode())
        response = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response += chunk
    finally:
        sock.close()

    # Status line is "HTTP/1.x NNN ...\r\n"
    first_line = response.split(b"\r\n", 1)[0].decode(errors="replace")
    return int(first_line.split()[1])


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def agents_root(tmp_path: Path) -> Path:
    """Minimal agents_root sufficient for the dashboard serve tests.

    Layout::

        agents_root/
          _dashboard/
            index.html
          myagent/
            dashboard.html
        secret.txt          ← file that MUST NOT be reachable via traversal
    """
    dashboard_dir = tmp_path / "_dashboard"
    dashboard_dir.mkdir()
    (dashboard_dir / "index.html").write_text(
        "<!DOCTYPE html><html><body>Dashboard</body></html>", encoding="utf-8"
    )

    agent_dir = tmp_path / "myagent"
    agent_dir.mkdir()
    (agent_dir / "dashboard.html").write_text(
        "<!DOCTYPE html><html><body>Agent</body></html>", encoding="utf-8"
    )

    # Sentinel file that traversal attacks would try to reach
    (tmp_path / "secret.txt").write_text("TOP SECRET", encoding="utf-8")

    return tmp_path


@pytest.fixture()
def server(agents_root: Path):
    """Ephemeral-port HTTP server; runs in a daemon thread for the test duration."""
    srv = _make_server(agents_root)
    t = Thread(target=srv.handle_request, daemon=True)
    # We'll handle one request at a time by calling handle_request in a loop.
    # Use serve_forever in a daemon thread instead so multiple requests work.
    srv_thread = Thread(target=srv.serve_forever, daemon=True)
    srv_thread.start()
    yield srv
    srv.shutdown()


# ---------------------------------------------------------------------------
# Security tests — traversal attacks MUST return 404
# ---------------------------------------------------------------------------


def test_root_level_traversal_returns_404(server, agents_root):
    """GET /../../secret.txt must return 404 (root-level traversal)."""
    status = _get(server, "/../../secret.txt")
    assert status == 404, f"Expected 404, got {status} — traversal not blocked"


def test_deep_traversal_to_etc_hosts_returns_404(server, agents_root):
    """GET /../../../../etc/hosts must return 404 (deep multi-segment traversal)."""
    status = _get(server, "/../../../../etc/hosts")
    assert status == 404, f"Expected 404, got {status} — traversal not blocked"


def test_static_branch_traversal_returns_404(server, agents_root):
    """GET /_dashboard/../../secret.txt must return 404 (escape via static branch)."""
    status = _get(server, "/_dashboard/../../secret.txt")
    assert status == 404, f"Expected 404, got {status} — traversal not blocked"


def test_agents_branch_traversal_returns_404(server, agents_root):
    """GET /agents/../../../secret.txt must return 404 (escape via agents branch)."""
    status = _get(server, "/agents/../../../secret.txt")
    assert status == 404, f"Expected 404, got {status} — traversal not blocked"


def test_agents_dotdot_in_name_returns_404(server, agents_root):
    """GET /agents/../../secret.txt must return 404 (.. in agent name segment)."""
    status = _get(server, "/agents/../../secret.txt")
    assert status == 404, f"Expected 404, got {status} — traversal not blocked"


# ---------------------------------------------------------------------------
# Positive tests — legitimate routes MUST still return 200
# ---------------------------------------------------------------------------


def test_index_route_returns_200(server, agents_root):
    """GET / must return 200 (legitimate index route unaffected by fix)."""
    status = _get(server, "/")
    assert status == 200, f"Expected 200 for GET /, got {status}"


def test_static_index_html_returns_200(server, agents_root):
    """GET /_dashboard/index.html must return 200 (static route unaffected)."""
    status = _get(server, "/_dashboard/index.html")
    assert status == 200, f"Expected 200 for GET /_dashboard/index.html, got {status}"


def test_agent_dashboard_returns_200(server, agents_root):
    """GET /agents/myagent must return 200 (agent dashboard route unaffected)."""
    status = _get(server, "/agents/myagent")
    assert status == 200, f"Expected 200 for GET /agents/myagent, got {status}"
