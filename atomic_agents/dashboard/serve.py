"""Optional local web server for the cost dashboard.

Run:
    python -m atomic_agents.dashboard serve

Serves the dashboard at http://127.0.0.1:8765/ with a /regenerate endpoint
that the Refresh button hits to rebuild the HTML from current log JSONL.

Loopback-only — never exposed to the network.

Uses Python's stdlib http.server (no Flask dependency required).
"""

from __future__ import annotations
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .render import render_all
from .._io import safe_resolve_under
from .._platform import get_agents_root
from ..exceptions import PathTraversalError


PORT = 8765
HOST = "127.0.0.1"


class DashboardHandler(BaseHTTPRequestHandler):
    """Tiny stdlib HTTP server for the dashboard.

    Endpoints:
      GET /                       → <agents_root>/_dashboard/index.html
      GET /<filename>             → file from <agents_root>/_dashboard/
      GET /agents/<name>          → <agents_root>/<name>/dashboard.html
      POST /regenerate            → rebuild all dashboards, return 200

    All served paths are routed through safe_resolve_under() against the
    intended root before any file read, blocking path-traversal attacks.
    """

    agents_root: Path = None  # set by serve()

    # Dashboard tab filenames served at root-level paths
    _TAB_FILES = {
        "/activity": "activity.html",
        "/activity.html": "activity.html",
        "/quality": "quality.html",
        "/quality.html": "quality.html",
        "/memory": "memory.html",
        "/memory.html": "memory.html",
        "/goals": "goals.html",
        "/goals.html": "goals.html",
    }

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/" or path == "/index.html":
            self._serve_under_dashboard("index.html", "text/html")
            return

        # New tab routes
        if path in self._TAB_FILES:
            self._serve_under_dashboard(self._TAB_FILES[path], "text/html")
            return

        if path.startswith("/agents/"):
            agent_name = path[len("/agents/") :].rstrip("/")
            # Reject agent names that contain path separators or traversal
            # segments before resolve — safe_resolve_under will also catch
            # these, but an early explicit check avoids leaking which path
            # component caused the rejection.
            if "/" in agent_name or "\\" in agent_name or ".." in agent_name:
                self.send_error(404, "Not found")
                return
            try:
                resolved = safe_resolve_under(agent_name, self.agents_root)
            except PathTraversalError:
                self.send_error(404, "Not found")
                return
            self._serve_file_contained(resolved / "dashboard.html", "text/html")
            return

        # Static files under /_dashboard/
        if path.startswith("/_dashboard/"):
            rel = path[len("/_dashboard/") :]
            self._serve_under_dashboard(rel, _content_type_for(Path(rel)))
            return

        # Bare filename → look in _dashboard/
        bare = path.lstrip("/")
        self._serve_under_dashboard(bare, _content_type_for(Path(bare)))

    def _serve_under_dashboard(self, relative: str, content_type: str) -> None:
        """Resolve *relative* under <agents_root>/_dashboard/ and serve it.

        Returns 404 on PathTraversalError or if the file does not exist.
        Never leaks which path component caused a traversal rejection.
        """
        dashboard_root = self.agents_root / "_dashboard"
        try:
            resolved = safe_resolve_under(relative, dashboard_root)
        except PathTraversalError:
            self.send_error(404, "Not found")
            return
        self._serve_file_contained(resolved, content_type)

    def _serve_file_contained(self, path: Path, content_type: str) -> None:
        """Serve *path* if it exists and is a regular file; 404 otherwise."""
        if not path.exists() or not path.is_file():
            self.send_error(404, "Not found")
            return
        self._serve_file(path, content_type)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/regenerate":
            try:
                written = render_all(self.agents_root)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(
                    json.dumps({"status": "ok", "written": written}).encode()
                )
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    json.dumps({"status": "error", "error": str(e)}).encode()
                )
            return
        self.send_error(404)

    def _serve_file(self, path: Path, content_type: str) -> None:
        if not path.exists():
            self.send_error(404, f"Not found: {path}")
            return
        try:
            content = path.read_bytes()
        except OSError as e:
            self.send_error(500, str(e))
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format, *args):
        # Suppress default per-request stderr logging for cleaner output
        sys.stderr.write(f"[{self.log_date_time_string()}] {format % args}\n")


def _content_type_for(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".html": "text/html",
        ".css": "text/css",
        ".js": "application/javascript",
        ".json": "application/json",
        ".png": "image/png",
        ".svg": "image/svg+xml",
    }.get(suffix, "application/octet-stream")


def serve(agents_root: Path | None = None, host: str = HOST, port: int = PORT) -> None:
    """Start the dashboard server. Blocks until interrupted (Ctrl-C)."""
    agents_root = agents_root or get_agents_root()

    if not agents_root.exists():
        print(f"Error: agents_root does not exist: {agents_root}", file=sys.stderr)
        print(f"Set ATOMIC_AGENTS_ROOT or pass --agents-root", file=sys.stderr)
        sys.exit(1)

    # Ensure dashboard exists before starting (regenerate it once)
    dashboard_dir = agents_root / "_dashboard"
    if not (dashboard_dir / "index.html").exists():
        print(f"No dashboard at {dashboard_dir}/index.html — generating now...")
        render_all(agents_root)

    DashboardHandler.agents_root = agents_root
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    print(f"Atomic Agents dashboard server listening on http://{host}:{port}/")
    print(f"  agents_root: {agents_root}")
    print(f"  Press Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    serve()
