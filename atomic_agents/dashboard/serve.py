"""Optional local web server for the Fleet Console and cost dashboard.

Run:
    python -m atomic_agents.dashboard serve

Serves the dashboard at http://127.0.0.1:8765/ with:
  GET  /           → Fleet Console home (index.html) — new landing page (spec/52 PR1)
  GET  /cost       → cost view (cost.html)
  GET  /activity   → activity tab
  GET  /quality    → quality tab
  GET  /memory     → memory tab
  GET  /goals      → goals tab
  POST /regenerate → rebuild all dashboards
  POST /alerts/ack    → ack an alert (loopback-only; closed-allowlist validation)
  POST /alerts/snooze → snooze an alert (loopback-only; closed-allowlist validation)

Loopback-only — the read GET routes are low-risk on a LAN; the write POST routes
(ack/snooze) refuse a non-loopback CLIENT PEER per request regardless of bind
address (a LAN caller is 403'd even under a 0.0.0.0 bind; the operator's own
127.0.0.1 writes keep working). The /regenerate endpoint gets the same per-request
peer guard (spec/52 closing both gaps).

Uses Python's stdlib http.server (no Flask dependency required).

BEHAVIOR CHANGE (spec/52 PR1): GET / now serves the Fleet Console Attention Queue
(index.html). The cost view moved to GET /cost (cost.html). Bookmarks / scripts
pointing at the old index.html cost view will now see the console home instead.
"""

from __future__ import annotations
import json
import logging
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .render import render_all, _write_rendered_alert_keys
from .._io import safe_resolve_under
from .._platform import get_agents_root
from ..exceptions import PathTraversalError

logger = logging.getLogger(__name__)

PORT = 8765
HOST = "127.0.0.1"

# Maximum body size for POST /alerts/ack and /alerts/snooze (spec/52 P1 prep finding).
_MAX_POST_BODY = 4096

# Alert-key format the hasher emits: "v1:" + 12 lowercase hex chars
# (attention._make_alert_key → "{_KEY_VERSION}:{sha256(...)[:12]}"). The
# closed-allowlist validation (MUST 4) requires the rendered_alert_keys.json
# sidecar to be a JSON LIST of strings each matching this shape. A JSON STRING
# value would otherwise be iterated character-by-character by frozenset(), so a
# forged 1-char key ("v") would pass the allowlist (#614 P2).
_ALERT_KEY_RE = re.compile(r"^v1:[0-9a-f]{12}$")

# Module-level lock serializing sidecar-append + re-render on ack/snooze POSTs.
# Ensures only one ack/snooze-triggered render runs at a time (resource efficiency
# — two concurrent acks would both invoke render_all on the same fleet data).
# Not a data-correctness lock (atomic_write prevents torn files); this is wall-clock
# efficiency only. Scope: covers the sidecar append + render sequence.
_render_lock = threading.Lock()


def _is_loopback(host: str) -> bool:
    """Return True if host is a loopback address (127.x.x.x or ::1).

    Write-endpoint guard (spec/52 MUST 3 + P0 prep finding). Applied at the top
    of do_POST against the per-request CLIENT PEER address (self.client_address[0]),
    not the server's bind address. This is real per-caller defense-in-depth:

      - A non-loopback caller is refused with 403 even when the server is bound
        to 0.0.0.0 (e.g. an operator exposing the read-only dashboard on a LAN).
      - The operator's OWN 127.0.0.1 ack/snooze/regenerate POSTs keep working in
        0.0.0.0 mode — checking the bind address would 403 every local write too,
        killing the write endpoints entirely under a 0.0.0.0 bind.

    self.client_address is the remote peer set per-request by
    socketserver.BaseRequestHandler, so the guard inspects who is actually calling.

    "localhost" is intentionally NOT in the match set: client_address[0] is a
    resolved socket peer address (a numeric IP), never the hostname string, so
    "localhost" can't arrive here. Matching it would be a code-vs-spec mismatch
    against MUST 3's literal "127.x / ::1" definition (#614 P2).
    """
    return host in ("127.0.0.1", "::1") or host.startswith("127.")


class DashboardHandler(BaseHTTPRequestHandler):
    """Tiny stdlib HTTP server for the Fleet Console + dashboard.

    Endpoints:
      GET /                       → <agents_root>/_dashboard/index.html (console home)
      GET /cost[.html]            → <agents_root>/_dashboard/cost.html
      GET /<filename>             → file from <agents_root>/_dashboard/
      GET /agents/<name>          → <agents_root>/<name>/dashboard.html
      POST /regenerate            → rebuild all dashboards, return 200
      POST /alerts/ack            → ack an alert (loopback-only)
      POST /alerts/snooze         → snooze an alert (loopback-only)

    All served paths are routed through safe_resolve_under() against the
    intended root before any file read, blocking path-traversal attacks.

    Write endpoints (POST /regenerate, /alerts/ack, /alerts/snooze) enforce
    a loopback-only CLIENT PEER per request regardless of bind address: a
    non-loopback caller is refused with 403 even under a 0.0.0.0 bind, while
    the operator's own 127.0.0.1 writes keep working.
    """

    agents_root: Path = None  # set by serve()

    # Dashboard tab filenames served at root-level paths.
    # BEHAVIOR CHANGE (spec/52 PR1): /cost → cost.html (was index.html).
    # / and /index.html still work (now serve the console home).
    _TAB_FILES = {
        "/cost": "cost.html",
        "/cost.html": "cost.html",
        "/activity": "activity.html",
        "/activity.html": "activity.html",
        "/quality": "quality.html",
        "/quality.html": "quality.html",
        "/memory": "memory.html",
        "/memory.html": "memory.html",
        "/goals": "goals.html",
        "/goals.html": "goals.html",
        # /console and /index.html route to the new console home (index.html)
        "/console": "index.html",
        "/console.html": "index.html",
        # Fleet Monitor (spec/56 MUST 1): /monitor and /monitor.html → monitor.html
        "/monitor": "monitor.html",
        "/monitor.html": "monitor.html",
    }

    # POST route dispatch table (explicit; preserves 404 fallthrough for unknowns)
    _POST_ROUTES = {"/regenerate", "/alerts/ack", "/alerts/snooze"}

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/" or path == "/index.html":
            self._serve_under_dashboard("index.html", "text/html")
            return

        # Named tab routes
        if path in self._TAB_FILES:
            self._serve_under_dashboard(self._TAB_FILES[path], "text/html")
            return

        if path.startswith("/agents/"):
            agent_name = path[len("/agents/") :].rstrip("/")
            # Reject agent names with path separators or traversal before resolve.
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

        if path not in self._POST_ROUTES:
            self.send_error(404)
            return

        # All POST routes are loopback-only (spec/52 MUST 3 + P0 prep finding).
        # Per-request guard on the CLIENT PEER address (self.client_address),
        # NOT the server bind address: a non-loopback caller is refused even under
        # a 0.0.0.0 bind, while the operator's own 127.0.0.1 writes keep working.
        client_host = self.client_address[0]
        if not _is_loopback(client_host):
            self.send_error(403, "Write endpoints require a loopback client")
            return

        if path == "/regenerate":
            self._handle_regenerate()
        elif path == "/alerts/ack":
            self._handle_alert_action("ack")
        elif path == "/alerts/snooze":
            self._handle_alert_action("snooze")

    def _handle_regenerate(self) -> None:
        """POST /regenerate — rebuild all dashboards."""
        try:
            written = render_all(self.agents_root)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok", "written": written}).encode())
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "error", "error": str(e)}).encode())

    def _read_post_body(self) -> dict | None:
        """Read and parse the JSON POST body.

        Returns None and sends an error response on any failure.
        Caps body at _MAX_POST_BODY to prevent unbounded reads (spec/52 P1).
        """
        content_length_str = self.headers.get("Content-Length", "0")
        try:
            content_length = int(content_length_str)
        except (ValueError, TypeError):
            self.send_error(400, "Invalid Content-Length")
            return None

        if content_length > _MAX_POST_BODY:
            self.send_error(413, "Request body too large")
            return None

        if content_length <= 0:
            self.send_error(400, "Missing request body")
            return None

        raw = self.rfile.read(content_length)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON body")
            return None

    def _load_rendered_alert_keys(self) -> frozenset | None:
        """Read the last-rendered alert_keys from the _console/ sidecar.

        Returns None and sends 503 if the sidecar is absent (console not yet
        rendered) or unreadable. The caller must return after None.

        The sidecar is written atomically by render_console() after every
        successful render (spec/52 MUST 4 closed-allowlist validation).
        """
        keys_path = self.agents_root / "_console" / "rendered_alert_keys.json"
        if not keys_path.exists():
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "status": "error",
                        "error": "Console not yet rendered. Run /regenerate first.",
                    }
                ).encode()
            )
            return None
        try:
            keys = json.loads(keys_path.read_text(encoding="utf-8"))
            # Require the parsed JSON to be a LIST of well-formed alert-key STRINGS.
            # A JSON string (e.g. "v1:abc") would be iterated char-by-char by
            # frozenset(), letting a forged 1-char key pass the allowlist — treat a
            # malformed shape exactly like an unreadable sidecar (503) (#614 P2).
            if not isinstance(keys, list) or not all(
                isinstance(k, str) and _ALERT_KEY_RE.match(k) for k in keys
            ):
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    json.dumps(
                        {
                            "status": "error",
                            "error": "rendered_alert_keys sidecar has an invalid shape.",
                        }
                    ).encode()
                )
                return None
            return frozenset(keys)
        except (OSError, json.JSONDecodeError):
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "status": "error",
                        "error": "rendered_alert_keys sidecar unreadable.",
                    }
                ).encode()
            )
            return None

    def _handle_alert_action(self, action: str) -> None:
        """POST /alerts/ack or /alerts/snooze — append to the alert state sidecar.

        Validates alert_key against the closed allowlist from the last render.
        Idempotent (MUST 5): a no-op (no extra event, no re-render) when the action
        leaves state unchanged — re-acking an already-acked item, an identical
        same-window re-snooze (same normalized snooze_until), or re-unsnoozing an
        already-open item. Re-renders only on effective state change.

        spec/52 MUSTs exercised here:
          MUST 1: append-under-flock (delegated to alert_state.append_alert_event)
          MUST 3: loopback-only (checked in do_POST before dispatch)
          MUST 4: closed-allowlist alert_key validation
          MUST 5: idempotency
          MUST 6: snooze_until UTC (delegated to alert_state._normalize_snooze_until)
        """
        from .alert_state import (
            _normalize_snooze_until,
            append_alert_event,
            read_alert_state,
        )

        body = self._read_post_body()
        if body is None:
            return  # error already sent

        alert_key = body.get("alert_key")
        if not alert_key or not isinstance(alert_key, str):
            self.send_error(400, "Missing or invalid alert_key")
            return

        snooze_until = body.get("snooze_until") if action == "snooze" else None
        if action == "snooze" and not snooze_until:
            self.send_error(400, "snooze_until required for snooze action")
            return

        # Validate snooze_until BEFORE the append. A present-but-unparseable value
        # ("garbage", "-1", a bad date) would otherwise flow into append_alert_event
        # → _normalize_snooze_until → ValueError → caught by the broad except below →
        # HTTP 500 + a logged traceback for attacker-controlled input. Validate here
        # and return a clean 400; the broad except stays for genuine I/O failures
        # (#614 P2; spec/52 MUST 6).
        if snooze_until is not None:
            try:
                _normalize_snooze_until(snooze_until)
            except (ValueError, TypeError):
                self.send_error(400, "invalid snooze_until")
                return

        # Closed-allowlist validation (spec/52 MUST 4).
        # Reading from the sidecar JSON (written atomically by render_console)
        # decouples render from serve — no shared in-memory state, no re-aggregation.
        rendered_keys = self._load_rendered_alert_keys()
        if rendered_keys is None:
            return  # 503 already sent

        if alert_key not in rendered_keys:
            self.send_error(422, "alert_key not in current rendered set")
            return

        # Idempotency check (spec/52 MUST 5): read current state before appending.
        # Hold _render_lock for the entire validate→append→re-render sequence so
        # concurrent ack POSTs don't both trigger render_all (efficiency) and so
        # the allowlist check and append are atomic with respect to other writers.
        with _render_lock:
            try:
                current_state = read_alert_state(self.agents_root)
            except Exception:
                current_state = {}

            current_entry = current_state.get(alert_key, {})
            current_status = current_entry.get("status", "open")

            # Determine if this action produces an effective state change (spec/52
            # MUST 5 idempotency). A no-op covers THREE cases — re-ack, an identical
            # same-window re-snooze, and a re-unsnooze of an already-open alert — so
            # an idempotent client retry never appends a duplicate audit event.
            if action == "ack" and current_status == "acked":
                # Already acked — idempotent, no new event, no re-render.
                self._send_json({"status": "ok", "changed": False})
                return
            if (
                action == "snooze"
                and current_status == "snoozed"
                and snooze_until is not None
                and _normalize_snooze_until(snooze_until)
                == current_entry.get("snooze_until")
            ):
                # Same-window re-snooze — identical normalized snooze_until: no-op.
                self._send_json({"status": "ok", "changed": False})
                return
            if action == "unsnooze" and current_status == "open":
                # Already open (not snoozed/acked) — unsnooze is a no-op.
                self._send_json({"status": "ok", "changed": False})
                return

            # Append the event under flock (delegated to alert_state module).
            try:
                append_alert_event(
                    self.agents_root,
                    alert_key=alert_key,
                    action=action,
                    snooze_until=snooze_until,
                )
            except Exception as e:
                logger.exception("alert_state append failed for key %s", alert_key)
                self.send_error(500, f"Alert state write failed: {e}")
                return

            # Re-render only on effective state change (spec/52 ruling).
            try:
                written = render_all(self.agents_root, tab="console")
                # Refresh the rendered_alert_keys sidecar so subsequent POSTs
                # validate against the just-rendered console.
                self._send_json({"status": "ok", "changed": True, "written": written})
            except Exception as e:
                logger.exception("re-render after alert action failed")
                # Append succeeded — state is persisted. Re-render failure is
                # non-fatal; operator can hit /regenerate manually.
                self._send_json(
                    {
                        "status": "ok",
                        "changed": True,
                        "warning": f"Re-render failed: {e}",
                    }
                )

    def _send_json(self, payload: dict) -> None:
        """Send a 200 JSON response."""
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

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
        print("Set ATOMIC_AGENTS_ROOT or pass --agents-root", file=sys.stderr)
        sys.exit(1)

    # Ensure dashboard exists before starting (regenerate it once).
    # Regenerate if ANY required file is absent. Checks:
    #   - index.html (console home, spec/52 PR1 landing page)
    #   - cost.html (cost view; absence = upgrade gap from pre-spec/52 installs)
    #   - monitor.html (Fleet Monitor, spec/56 MUST 1; absent on first install or
    #     pre-spec/56 dashboards — GET /monitor would 404 without this check)
    dashboard_dir = agents_root / "_dashboard"
    if (
        not (dashboard_dir / "index.html").exists()
        or not (dashboard_dir / "cost.html").exists()
        or not (dashboard_dir / "monitor.html").exists()
    ):
        print(f"No complete dashboard at {dashboard_dir} — generating now...")
        render_all(agents_root)

    DashboardHandler.agents_root = agents_root
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    print(f"Atomic Agents dashboard server listening on http://{host}:{port}/")
    print(f"  agents_root: {agents_root}")
    print("  Press Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    serve()
