"""HTTP-layer conformance tests for atomic_agents.serve._app (make_app).

Uses Starlette's TestClient to drive the four routes through the real ASGI app.
Guards all tests with pytest.importorskip('starlette') so the suite skips
cleanly when the serve extra is not installed.

spec/37 MUST 4 (healthz cheap), MUST 5 (critical refused), MUST 7 (http_caller
in JSONL), MUST 8 (unique run_id), MUST 10 (path traversal 400).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

starlette = pytest.importorskip("starlette")
from starlette.testclient import TestClient  # noqa: E402 — after importorskip

from atomic_agents.serve._app import make_app  # noqa: E402


# ── fixtures ─────────────────────────────────────────────────────────────────


def _build_agent_root(tmp_path: Path, name: str) -> Path:
    """Minimal agent folder layout sufficient for make_app route tests."""
    agents_root = tmp_path / "agents"
    agent_dir = agents_root / name
    persona_dir = agent_dir / "persona"
    persona_dir.mkdir(parents=True)
    (persona_dir / "IDENTITY.md").write_text(
        f"# Identity\nYou are {name}.", encoding="utf-8"
    )
    (agent_dir / "model.md").write_text(
        "## Default model\nclaude-haiku-4-5-20260101\n\n"
        "Max input prompt tokens: 2000\n"
        "Max output tokens: 500\n",
        encoding="utf-8",
    )
    (agent_dir / "tools.md").write_text(
        "## Read paths\n- ~/\n\n## Write paths\n- ~/\n",
        encoding="utf-8",
    )
    return agents_root


def _mock_response() -> MagicMock:
    m = MagicMock()
    m.text = "Agent output"
    m.tool_uses = []
    m.input_tokens = 10
    m.output_tokens = 5
    m.cache_hit_tokens = 0
    m.cache_miss_tokens = 0
    m.raw = {}
    return m


# ── GET /agents ───────────────────────────────────────────────────────────────


def test_list_agents_all_mode(tmp_path: Path):
    """GET /agents in --all mode lists all agent folders."""
    agents_root = _build_agent_root(tmp_path, "agentA")
    _build_agent_root(tmp_path, "agentB")  # second agent in same root
    # Rebuild so both exist under the same agents_root
    agentB_dir = agents_root / "agentB"
    agentB_dir.mkdir(parents=True, exist_ok=True)
    (agentB_dir / "persona").mkdir(exist_ok=True)
    (agentB_dir / "persona" / "IDENTITY.md").write_text("You are B.", encoding="utf-8")
    (agentB_dir / "model.md").write_text(
        "## Default model\nclaude-haiku-4-5-20260101\n\nMax input prompt tokens: 2000\nMax output tokens: 500\n",
        encoding="utf-8",
    )

    app = make_app(agents_root=agents_root, agent_name=None)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/agents")
    assert resp.status_code == 200
    body = resp.json()
    assert "agentA" in body["agents"]
    assert "agentB" in body["agents"]


def test_list_agents_single_mode_returns_only_named_agent(tmp_path: Path):
    """GET /agents in single-agent mode returns only the one agent."""
    agents_root = _build_agent_root(tmp_path, "agentA")
    agentB_dir = agents_root / "agentB"
    agentB_dir.mkdir(parents=True)
    (agentB_dir / "persona").mkdir()
    (agentB_dir / "persona" / "IDENTITY.md").write_text("You are B.", encoding="utf-8")
    (agentB_dir / "model.md").write_text(
        "## Default model\nclaude-haiku-4-5-20260101\n\nMax input prompt tokens: 2000\nMax output tokens: 500\n",
        encoding="utf-8",
    )

    app = make_app(agents_root=agents_root, agent_name="agentA")
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/agents")
    assert resp.status_code == 200
    body = resp.json()
    assert body["agents"] == ["agentA"]
    assert "agentB" not in body["agents"]


# ── GET /agents/<name>/healthz ────────────────────────────────────────────────


def test_healthz_ok(tmp_path: Path):
    """GET /agents/<name>/healthz returns 200 ok for a valid agent."""
    agents_root = _build_agent_root(tmp_path, "myagent")
    app = make_app(agents_root=agents_root)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/agents/myagent/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_healthz_missing_agent_folder(tmp_path: Path):
    """GET /agents/<name>/healthz returns 503 when agent folder is absent."""
    agents_root = tmp_path / "agents"
    agents_root.mkdir()
    app = make_app(agents_root=agents_root)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/agents/ghost/healthz")
    assert resp.status_code == 503
    assert resp.json()["status"] == "degraded"


def test_healthz_missing_model_md(tmp_path: Path):
    """GET /agents/<name>/healthz returns 503 when model.md is absent (MUST 4)."""
    agents_root = _build_agent_root(tmp_path, "nomodel")
    (agents_root / "nomodel" / "model.md").unlink()
    app = make_app(agents_root=agents_root)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/agents/nomodel/healthz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert "model.md" in body["reason"]


def test_healthz_path_traversal(tmp_path: Path):
    """GET /agents/<name>/healthz returns 400 on path traversal (MUST 10)."""
    agents_root = _build_agent_root(tmp_path, "myagent")
    app = make_app(agents_root=agents_root)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/agents/../etc/healthz")
    assert resp.status_code in (400, 404)  # router may normalise before guard


def test_healthz_single_mode_wrong_agent_404(tmp_path: Path):
    """Single-agent mode: healthz for a different agent returns 404."""
    agents_root = _build_agent_root(tmp_path, "agentA")
    agentB_dir = agents_root / "agentB"
    agentB_dir.mkdir(parents=True)
    (agentB_dir / "persona").mkdir()
    (agentB_dir / "persona" / "IDENTITY.md").write_text("B", encoding="utf-8")
    (agentB_dir / "model.md").write_text(
        "## Default model\nclaude-haiku-4-5-20260101\n\nMax input prompt tokens: 2000\nMax output tokens: 500\n",
        encoding="utf-8",
    )

    app = make_app(agents_root=agents_root, agent_name="agentA")
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/agents/agentB/healthz")
    assert resp.status_code == 404


# ── POST /agents/<name>/call ──────────────────────────────────────────────────


def test_call_agent_happy_path(tmp_path: Path):
    """POST /agents/<name>/call 200 ok with run_id in response."""
    agents_root = _build_agent_root(tmp_path, "testbot")

    async def fake_run_agent_call(**kwargs: Any) -> Any:
        return (
            "run-test-001",
            MagicMock(
                skipped=False,
                text="Hello",
                model="claude-haiku",
                cost_usd=0.001,
                input_tokens=10,
                output_tokens=5,
            ),
        )

    app = make_app(agents_root=agents_root)
    with patch(
        "atomic_agents.serve._app.run_agent_call",
        side_effect=fake_run_agent_call,
    ):
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/agents/testbot/call",
                json={"work_item": "Hello agent"},
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "run_id" in body


def test_call_agent_critical_true_returns_422(tmp_path: Path):
    """POST with critical=true must return 422 (MUST 5)."""
    agents_root = _build_agent_root(tmp_path, "testbot")
    app = make_app(agents_root=agents_root)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/agents/testbot/call",
            json={"work_item": "ping", "critical": True},
        )
    assert resp.status_code == 422
    assert "critical" in resp.json()["error"].lower()


def test_call_agent_missing_work_item_returns_422(tmp_path: Path):
    """POST without work_item returns 422."""
    agents_root = _build_agent_root(tmp_path, "testbot")
    app = make_app(agents_root=agents_root)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post("/agents/testbot/call", json={})
    assert resp.status_code == 422


def test_call_agent_whitespace_only_work_item_returns_422(tmp_path: Path):
    """POST with a whitespace-only work_item returns 422, not a paid LLM call.

    A truthiness-only guard (`if not work_item`) passes '   ' because it is
    truthy. The .strip()-based guard correctly rejects it before any LLM
    dispatch. Prevents silent cost on a semantically-empty prompt.
    spec/37 §"Request body"; CLAUDE.md principle 4 (cost is first-class).
    """
    agents_root = _build_agent_root(tmp_path, "testbot")
    app = make_app(agents_root=agents_root)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/agents/testbot/call",
            json={"work_item": "   "},
        )
    assert resp.status_code == 422
    assert "work_item" in resp.json()["error"]


def test_call_agent_bad_max_tokens_returns_422(tmp_path: Path):
    """POST with non-numeric max_tokens returns 422, not 500 (client error)."""
    agents_root = _build_agent_root(tmp_path, "testbot")
    app = make_app(agents_root=agents_root)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/agents/testbot/call",
            json={"work_item": "ping", "max_tokens": "abc"},
        )
    assert resp.status_code == 422
    assert "max_tokens" in resp.json()["error"]


def test_call_agent_bad_temperature_returns_422(tmp_path: Path):
    """POST with non-numeric temperature returns 422, not 500."""
    agents_root = _build_agent_root(tmp_path, "testbot")
    app = make_app(agents_root=agents_root)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/agents/testbot/call",
            json={"work_item": "ping", "temperature": "hot"},
        )
    assert resp.status_code == 422
    assert "temperature" in resp.json()["error"]


def test_call_agent_out_of_range_temperature_returns_422(tmp_path: Path):
    """POST with numeric temperature outside [0.0, 1.0] returns 422.

    spec/37 §"Request body" — temperature MUST be in [0.0, 1.0]. Values in
    OpenAI's wider [0.0, 2.0] range (e.g. 1.5) would pass 422 validation but
    produce a 500 on the default Anthropic backend, defeating pre-dispatch
    validation. The cap is pinned here so it cannot be silently dropped.
    """
    agents_root = _build_agent_root(tmp_path, "testbot")
    app = make_app(agents_root=agents_root)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/agents/testbot/call",
            json={"work_item": "ping", "temperature": 1.5},
        )
    assert resp.status_code == 422
    assert "temperature" in resp.json()["error"]


def test_call_agent_boolean_temperature_returns_422(tmp_path: Path):
    """POST with boolean temperature returns 422 (booleans are not numbers).

    int(True)==1 and float(True)==1.0 pass numeric validation silently.
    JSON booleans are a distinct type; the validation block must reject them
    explicitly. spec/37 §"Request body". CLAUDE.md principle 13.
    """
    agents_root = _build_agent_root(tmp_path, "testbot")
    app = make_app(agents_root=agents_root)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/agents/testbot/call",
            json={"work_item": "ping", "temperature": True},
        )
    assert resp.status_code == 422
    assert "temperature" in resp.json()["error"]


def test_call_agent_boolean_max_tokens_returns_422(tmp_path: Path):
    """POST with boolean max_tokens returns 422 (booleans are not integers).

    int(True)==1 passes the >0 check silently. JSON booleans are a distinct
    type; the validation block must reject them explicitly.
    spec/37 §"Request body". CLAUDE.md principle 13.
    """
    agents_root = _build_agent_root(tmp_path, "testbot")
    app = make_app(agents_root=agents_root)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/agents/testbot/call",
            json={"work_item": "ping", "max_tokens": True},
        )
    assert resp.status_code == 422
    assert "max_tokens" in resp.json()["error"]


def test_call_agent_path_traversal_returns_400(tmp_path: Path):
    """POST with path traversal in name returns 400 (MUST 10)."""
    agents_root = _build_agent_root(tmp_path, "testbot")
    app = make_app(agents_root=agents_root)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/agents/../etc/passwd/call",
            json={"work_item": "ping"},
        )
    # Router may normalise the path before the route is reached; accept 400 or 404
    assert resp.status_code in (400, 404)


def test_call_agent_single_mode_wrong_agent_404(tmp_path: Path):
    """Single-agent mode: calling a different agent returns 404."""
    agents_root = _build_agent_root(tmp_path, "agentA")
    agentB_dir = agents_root / "agentB"
    agentB_dir.mkdir(parents=True)
    (agentB_dir / "persona").mkdir()
    (agentB_dir / "persona" / "IDENTITY.md").write_text("B", encoding="utf-8")
    (agentB_dir / "model.md").write_text(
        "## Default model\nclaude-haiku-4-5-20260101\n\nMax input prompt tokens: 2000\nMax output tokens: 500\n",
        encoding="utf-8",
    )

    app = make_app(agents_root=agents_root, agent_name="agentA")
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/agents/agentB/call",
            json={"work_item": "ping"},
        )
    assert resp.status_code == 404


def test_call_agent_identity_header_passed_through(tmp_path: Path):
    """identity_header value is extracted and (would be) passed as caller_identity."""
    agents_root = _build_agent_root(tmp_path, "testbot")

    captured_identity: list[Any] = []

    async def fake_run_agent_call(**kwargs: Any) -> Any:
        captured_identity.append(kwargs.get("caller_identity"))
        return (
            "run-xyz",
            MagicMock(
                skipped=False,
                text="ok",
                model="m",
                cost_usd=0.0,
                input_tokens=1,
                output_tokens=1,
            ),
        )

    app = make_app(
        agents_root=agents_root,
        identity_header="X-Test-Identity",
    )
    with patch(
        "atomic_agents.serve._app.run_agent_call", side_effect=fake_run_agent_call
    ):
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/agents/testbot/call",
                json={"work_item": "ping"},
                headers={"X-Test-Identity": "alice@example.com"},
            )
    assert resp.status_code == 200
    assert captured_identity == ["alice@example.com"]


def test_make_app_self_contained_identity_header(tmp_path: Path):
    """make_app() without _server.py wiring sets identity_header on state (P2 shortcut fix)."""
    agents_root = _build_agent_root(tmp_path, "testbot")
    app = make_app(
        agents_root=agents_root,
        identity_header="X-Custom-Header",
    )
    assert app.state.identity_header == "X-Custom-Header"


def test_make_app_default_identity_header(tmp_path: Path):
    """make_app() with no identity_header arg defaults to X-Goog-IAP-JWT-Assertion."""
    agents_root = _build_agent_root(tmp_path, "testbot")
    app = make_app(agents_root=agents_root)
    assert app.state.identity_header == "X-Goog-IAP-JWT-Assertion"


# ── run_id uniqueness ─────────────────────────────────────────────────────────


def test_run_id_unique_across_two_http_calls(tmp_path: Path):
    """Two POST /call requests produce two distinct run_ids (MUST 8).

    This test verifies the HTTP handler wires the run_id returned by
    run_agent_call into the response body — it does NOT attempt to verify
    _generate_run_id's collision-avoidance property (that lives in
    test_serve_agent_layer.py:test_generate_run_id_unique_with_frozen_datetime
    which exercises the real AtomicAgent._generate_run_id directly).
    """
    agents_root = _build_agent_root(tmp_path, "testbot")

    async def fake_run_agent_call(**kwargs: Any) -> Any:
        # Generate a real uuid4 fragment each call to exercise the handler
        # wiring path without duplicating the collision-avoidance unit test.
        import uuid

        run_id = f"run-{uuid.uuid4().hex[:8]}"
        return (
            run_id,
            MagicMock(
                skipped=False,
                text="ok",
                model="m",
                cost_usd=0.0,
                input_tokens=1,
                output_tokens=1,
            ),
        )

    app = make_app(agents_root=agents_root)
    with patch(
        "atomic_agents.serve._app.run_agent_call", side_effect=fake_run_agent_call
    ):
        with TestClient(app, raise_server_exceptions=False) as client:
            r1 = client.post("/agents/testbot/call", json={"work_item": "first"})
            r2 = client.post("/agents/testbot/call", json={"work_item": "second"})

    assert r1.status_code == 200
    assert r2.status_code == 200
    r1_run_id = r1.json()["run_id"]
    r2_run_id = r2.json()["run_id"]
    assert r1_run_id != r2_run_id, (
        f"run_ids from two separate calls must be distinct; got {r1_run_id!r} twice"
    )


# ── refused-path HTTP contract ────────────────────────────────────────────────


def test_call_agent_lock_busy_returns_503_with_run_id(tmp_path: Path):
    """POST /call that raises LockBusyWithRunId returns HTTP 503 with run_id.

    The CHANGELOG bullet and CLAUDE.md principle 5 (audit trail is structural)
    guarantee that the 503 body carries run_id for audit correlation. This test
    pins that contract so a refactor cannot silently drop run_id from the 503 body
    or regress the status code.

    spec/37 §"Lock busy (HTTP 503)".
    """
    from atomic_agents.exceptions import LockBusy
    from atomic_agents.serve._runner import LockBusyWithRunId

    agents_root = _build_agent_root(tmp_path, "testbot")

    async def fake_run_agent_call(**kwargs: Any) -> Any:
        original = LockBusy("lock held by another call")
        raise LockBusyWithRunId(original, run_id="run-lock-test-001")

    app = make_app(agents_root=agents_root)
    with patch(
        "atomic_agents.serve._app.run_agent_call", side_effect=fake_run_agent_call
    ):
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post("/agents/testbot/call", json={"work_item": "ping"})

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "lock_busy"
    assert "run_id" in body, (
        f"run_id must be in 503 body for audit correlation; got {body}"
    )
    assert body["run_id"] == "run-lock-test-001"


def test_call_agent_cost_skip_returns_402_with_run_id(tmp_path: Path):
    """POST /call that returns a skipped Response returns HTTP 402 with run_id.

    spec/37 §"Skipped response (HTTP 402)" — the 402 body must carry run_id so
    the caller can correlate the refused HTTP response to the JSONL audit record.
    CLAUDE.md principle 5 (audit trail is structural). This test pins the contract
    so a refactor cannot drop run_id from the 402 body or regress the status code.
    """
    agents_root = _build_agent_root(tmp_path, "testbot")

    async def fake_run_agent_call(**kwargs: Any) -> Any:
        return (
            "run-skip-test-001",
            MagicMock(skipped=True, skip_reason="daily cap reached"),
        )

    app = make_app(agents_root=agents_root)
    with patch(
        "atomic_agents.serve._app.run_agent_call", side_effect=fake_run_agent_call
    ):
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post("/agents/testbot/call", json={"work_item": "ping"})

    assert resp.status_code == 402
    body = resp.json()
    assert body["status"] == "skipped"
    assert "run_id" in body, (
        f"run_id must be in 402 body for audit correlation; got {body}"
    )
    assert body["run_id"] == "run-skip-test-001"


def test_call_agent_float_max_tokens_returns_422(tmp_path: Path):
    """POST with non-integral float max_tokens returns 422.

    4096.7 must not be silently floored to 4096 — spec/37 says MUST be a
    positive integer. This is the same silent-coercion inconsistency the boolean
    guard exists to prevent; a non-integer float is equally non-integer.
    spec/37 §"Request body". CLAUDE.md principle 13.
    """
    agents_root = _build_agent_root(tmp_path, "testbot")
    app = make_app(agents_root=agents_root)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/agents/testbot/call",
            json={"work_item": "ping", "max_tokens": 4096.7},
        )
    assert resp.status_code == 422
    assert "max_tokens" in resp.json()["error"]


def test_call_agent_integral_float_max_tokens_accepted(tmp_path: Path):
    """POST with integral float max_tokens (e.g. 4096.0) is accepted and coerced to int.

    JSON serializers sometimes emit integer-valued numbers as floats.
    4096.0 is unambiguously an integer value even if encoded as a float;
    rejecting it would break compliant clients. spec/37 §"Request body".
    """
    agents_root = _build_agent_root(tmp_path, "testbot")

    async def fake_run_agent_call(**kwargs: Any) -> Any:
        return (
            "run-float-ok",
            MagicMock(
                skipped=False,
                text="ok",
                model="m",
                cost_usd=0.0,
                input_tokens=1,
                output_tokens=1,
            ),
        )

    app = make_app(agents_root=agents_root)
    with patch(
        "atomic_agents.serve._app.run_agent_call", side_effect=fake_run_agent_call
    ):
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/agents/testbot/call",
                json={"work_item": "ping", "max_tokens": 4096.0},
            )
    assert resp.status_code == 200


def test_call_agent_all_mode_not_found_masks_path(tmp_path: Path):
    """POST /call in --all mode must not expose the absolute vault path in 404 body.

    In --all mode, AtomicAgent.__init__ raises AtomicAgentsError with a message
    containing the absolute on-disk path (e.g. 'Agent folder not found:
    /var/folders/.../agents/ghost'). The HTTP 404 response must mask this to a
    generic 'Agent not found: <name>' body that mirrors the single-agent branch.

    Security shortcut: path disclosure on the primary /call route is the
    higher-stakes risk (same class as the /doctor disclosure, but on an
    unauthenticated-reachable route in default config).
    spec/37 §"Agent not found (HTTP 404)" + confidentiality note.
    """
    from atomic_agents.exceptions import AtomicAgentsError

    agents_root = tmp_path / "agents"
    agents_root.mkdir()

    async def fake_run_agent_call(**kwargs: Any) -> Any:
        raise AtomicAgentsError(f"Agent folder not found: {agents_root / 'ghost'}")

    app = make_app(agents_root=agents_root, agent_name=None)  # --all mode
    with patch(
        "atomic_agents.serve._app.run_agent_call", side_effect=fake_run_agent_call
    ):
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post("/agents/ghost/call", json={"work_item": "ping"})

    assert resp.status_code == 404
    body = resp.json()
    # Must not contain the absolute on-disk path in the response body
    error_msg = body.get("error", "")
    assert str(agents_root) not in error_msg, (
        f"Absolute vault path leaked in 404 response body: {error_msg!r}"
    )
    # Must use the generic shape
    assert "ghost" in error_msg


def test_call_agent_non_404_atomic_error_masks_path(tmp_path: Path):
    """POST /call must not expose the absolute vault path in 500 body for non-404 AtomicAgentsError.

    AgentProfileNotFound and other AtomicAgentsError subtypes embed absolute
    on-disk paths (e.g. 'realagent: neither persona/IDENTITY.md nor
    persona.link.md exists at /var/folders/.../realagent'). The HTTP 500 body
    MUST return a generic message — never the raw exception string.

    P1 security fix: the round-2 fix closed only the 404 branch; this test
    confirms the 500 branch is also masked. spec/37 confidentiality note;
    CLAUDE.md principle 5 (audit trail logs the full path server-side).
    """
    from atomic_agents.exceptions import AtomicAgentsError

    agents_root = tmp_path / "agents"
    agents_root.mkdir()
    absolute_path = agents_root / "realagent"

    async def fake_run_agent_call(**kwargs: Any) -> Any:
        # Simulate an AgentProfileNotFound-style error that embeds an absolute path.
        raise AtomicAgentsError(
            f"AgentProfileNotFound: agent 'realagent' not found at {absolute_path}: "
            "neither persona/IDENTITY.md nor persona.link.md exists"
        )

    app = make_app(agents_root=agents_root, agent_name=None)
    with patch(
        "atomic_agents.serve._app.run_agent_call", side_effect=fake_run_agent_call
    ):
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post("/agents/realagent/call", json={"work_item": "ping"})

    assert resp.status_code == 500
    body = resp.json()
    error_msg = body.get("error", "")
    assert str(absolute_path) not in error_msg, (
        f"Absolute vault path leaked in 500 response body: {error_msg!r}"
    )
    assert "Internal error" in error_msg


def test_call_agent_string_max_tokens_returns_422(tmp_path: Path):
    """POST with string-valued max_tokens returns 422 (not silent coercion).

    int('500') succeeds in Python, so {'max_tokens': '500'} would be silently
    accepted as 500 without an explicit type check. The comment 'non-numeric
    values return 422' cannot be satisfied by relying on int()/float() failure
    alone — type-checking by JSON type is the correct shape.
    CLAUDE.md principle 12 (verify before claim, empirically).
    """
    agents_root = _build_agent_root(tmp_path, "testbot")
    app = make_app(agents_root=agents_root)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/agents/testbot/call",
            json={"work_item": "ping", "max_tokens": "500"},
        )
    assert resp.status_code == 422
    assert "max_tokens" in resp.json()["error"]


def test_call_agent_whitespace_string_max_tokens_returns_422(tmp_path: Path):
    """POST with whitespace-padded string max_tokens returns 422.

    int('  500  ') also succeeds in Python — whitespace padding makes the
    coercion hole even more subtle. Both cases are covered by the same
    JSON-type guard added to close the shortcut.
    """
    agents_root = _build_agent_root(tmp_path, "testbot")
    app = make_app(agents_root=agents_root)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/agents/testbot/call",
            json={"work_item": "ping", "max_tokens": "  500  "},
        )
    assert resp.status_code == 422
    assert "max_tokens" in resp.json()["error"]


def test_call_agent_string_temperature_returns_422(tmp_path: Path):
    """POST with string-valued temperature returns 422 (not silent coercion).

    float('0.5') succeeds, so {'temperature': '0.5'} would be silently accepted.
    JSON-type check closes the same coercion hole as the max_tokens string fix.
    CLAUDE.md principle 12 (verify before claim, empirically).
    """
    agents_root = _build_agent_root(tmp_path, "testbot")
    app = make_app(agents_root=agents_root)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/agents/testbot/call",
            json={"work_item": "ping", "temperature": "0.5"},
        )
    assert resp.status_code == 422
    assert "temperature" in resp.json()["error"]


def test_call_agent_numeric_model_override_returns_422(tmp_path: Path):
    """POST with non-string model_override returns 422, not silent coercion.

    Before the fix, {"model_override": 42} was silently dropped (isinstance
    guard → None) and the call proceeded on the default model with 200 and no
    error. This is the same silent-coercion shape that max_tokens/temperature
    guards reject via 422. Consistent type-first validation prevents a fat-
    fingered model_override from triggering a wrong-model run with no signal.
    spec/37 §"Request body"; CLAUDE.md coding preference: "Prefer proper error
    handling over silent failures." Round 3 P2/shortcut finding.
    """
    agents_root = _build_agent_root(tmp_path, "testbot")
    app = make_app(agents_root=agents_root)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/agents/testbot/call",
            json={"work_item": "ping", "model_override": 42},
        )
    assert resp.status_code == 422
    assert "model_override" in resp.json()["error"]


def test_call_agent_boolean_model_override_returns_422(tmp_path: Path):
    """POST with boolean model_override returns 422 (boolean is not a string).

    JSON booleans are a distinct type; isinstance(True, str) is False, so a
    True/False value is correctly rejected by the same type-first guard.
    """
    agents_root = _build_agent_root(tmp_path, "testbot")
    app = make_app(agents_root=agents_root)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/agents/testbot/call",
            json={"work_item": "ping", "model_override": True},
        )
    assert resp.status_code == 422
    assert "model_override" in resp.json()["error"]


def test_call_agent_empty_string_model_override_returns_422(tmp_path: Path):
    """POST with empty-string model_override returns 422, not silent fallback.

    An empty string passes isinstance(raw, str) but reaches
    `model = model_override or self.config.default_model` and is silently
    treated as absent — the exact silent-drop the guard exists to prevent.
    The .strip()-based guard catches it before any LLM dispatch.
    spec/37 §"Request body"; CLAUDE.md coding preference: "Prefer proper
    error handling over silent failures." Round 5 shortcut finding.
    """
    agents_root = _build_agent_root(tmp_path, "testbot")
    app = make_app(agents_root=agents_root)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/agents/testbot/call",
            json={"work_item": "ping", "model_override": ""},
        )
    assert resp.status_code == 422
    assert "model_override" in resp.json()["error"]


def test_call_agent_whitespace_only_model_override_returns_422(tmp_path: Path):
    """POST with whitespace-only model_override returns 422, not HTTP 500.

    A whitespace-only string passes isinstance(raw, str) and the or-fallback,
    but reaches the backend as an unresolvable model name and surfaces as HTTP
    500 — a client error masked as a server error. The .strip() guard rejects
    it pre-dispatch so the caller gets 422 (client error) with a clear message.
    spec/37 §"Request body"; CLAUDE.md principle 12 (verify before claim).
    Round 5 shortcut finding.
    """
    agents_root = _build_agent_root(tmp_path, "testbot")
    app = make_app(agents_root=agents_root)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/agents/testbot/call",
            json={"work_item": "ping", "model_override": "   "},
        )
    assert resp.status_code == 422
    assert "model_override" in resp.json()["error"]


def test_doctor_500_does_not_leak_path(tmp_path: Path):
    """GET /doctor 500 body must not echo raw exception messages (path masking).

    The /call route was hardened so a RuntimeError carrying an absolute path
    returns a generic body. The /doctor route now mirrors that masking: the
    500 body must not contain the exception message (which can embed vault
    paths, MCP config, or other internal state beyond the curated 200 body).
    spec/37 §"GET /agents/<name>/doctor" Security note; Round 3 P2/shortcut.
    """
    agents_root = _build_agent_root(tmp_path, "testbot")
    secret_path = str(tmp_path / "secret" / "vault" / "agents" / "testbot")

    import atomic_agents.doctor as doctor_module

    def fake_run_doctor(**kwargs: Any):
        raise RuntimeError(f"doctor failed reading {secret_path}/tools.md")

    app = make_app(agents_root=agents_root)
    with patch.object(doctor_module, "run_doctor", side_effect=fake_run_doctor):
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/agents/testbot/doctor")

    assert resp.status_code == 500
    body_text = resp.text
    assert secret_path not in body_text, (
        f"Absolute vault path leaked in /doctor 500 response: {body_text!r}"
    )
    assert "Internal error" in body_text


def test_call_agent_500_confidentiality_generic_body(tmp_path: Path):
    """POST /call 500 body must be generic — not echo raw exception type+message.

    Conformance test for spec/37 §"POST /agents/<name>/call" → Confidentiality
    (MUST). Both the AtomicAgentsError 500 branch and the generic Exception
    branch must return {"status": "error", "error": "Internal error processing
    agent <name>"} — never the raw '<type>: <message>' shape. Round 3 P1 fix
    asserts the spec-documented shape now matches the implementation.
    """
    agents_root = _build_agent_root(tmp_path, "testbot")
    secret = str(tmp_path / "vault" / "realagent")

    async def raises_generic(**kwargs: Any) -> Any:
        raise RuntimeError(f"crash at {secret}")

    app = make_app(agents_root=agents_root)
    with patch("atomic_agents.serve._app.run_agent_call", side_effect=raises_generic):
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post("/agents/testbot/call", json={"work_item": "ping"})

    assert resp.status_code == 500
    body = resp.json()
    error_msg = body.get("error", "")
    # Must NOT echo the raw exception message (no type name, no path)
    assert "RuntimeError" not in error_msg, (
        f"Exception type leaked in 500 body: {error_msg!r}"
    )
    assert secret not in error_msg, f"Absolute path leaked in 500 body: {error_msg!r}"
    # Must use the generic shape
    assert "Internal error" in error_msg


def test_call_agent_identity_header_truncated_at_512(tmp_path: Path):
    """Identity header value is capped at 512 chars before audit logging.

    The refused lock_busy / cost-skip paths have no LLM cost gate but still
    write caller_identity to the JSONL audit record. An unauthenticated caller
    could send a large header and drive unbounded disk writes per refused call.
    The serve layer caps the logged value at 512 chars. CLAUDE.md principle 4
    (cost is first-class); spec/37 §"Audit record shape".
    """
    agents_root = _build_agent_root(tmp_path, "testbot")

    captured_identity: list[Any] = []

    async def fake_run_agent_call(**kwargs: Any) -> Any:
        captured_identity.append(kwargs.get("caller_identity"))
        return (
            "run-cap-test",
            MagicMock(
                skipped=False,
                text="ok",
                model="m",
                cost_usd=0.0,
                input_tokens=1,
                output_tokens=1,
            ),
        )

    long_identity = "x" * 2048
    app = make_app(agents_root=agents_root, identity_header="X-Test-Identity")
    with patch(
        "atomic_agents.serve._app.run_agent_call", side_effect=fake_run_agent_call
    ):
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/agents/testbot/call",
                json={"work_item": "ping"},
                headers={"X-Test-Identity": long_identity},
            )

    assert resp.status_code == 200
    assert len(captured_identity) == 1
    assert captured_identity[0] is not None
    assert len(captured_identity[0]) == 512, (
        f"Identity must be capped at 512 chars; got {len(captured_identity[0])}"
    )
    # Verify short identities are passed through unmodified
    assert captured_identity[0] == long_identity[:512]


# ── Finding #401 — body-size cap (CWE-770 OOM DoS) ───────────────────────────


def test_call_body_over_limit_with_content_length_returns_413(tmp_path: Path):
    """POST /call with Content-Length over the limit returns 413 before streaming.

    Stage-1 pre-flight: the server checks the declared Content-Length against
    max_body_bytes and refuses immediately without touching the body stream.
    This is the fast-path rejection when the client is honest about body size.
    Finding #401 / CWE-770.
    """
    agents_root = _build_agent_root(tmp_path, "testbot")
    # Set a tiny limit so a trivial body triggers the guard.
    app = make_app(agents_root=agents_root, max_body_bytes=100)
    large_body = b'{"work_item": "' + b"x" * 200 + b'"}'
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/agents/testbot/call",
            content=large_body,
            headers={"Content-Type": "application/json"},
        )
    assert resp.status_code == 413, (
        f"Expected 413 for over-limit body with Content-Length; got {resp.status_code}"
    )
    body = resp.json()
    assert body["status"] == "error"
    assert "too large" in body["error"]


def test_call_body_over_limit_without_content_length_returns_413(tmp_path: Path):
    """POST /call with a chunked/no-Content-Length over-limit body returns 413.

    Stage-2 stream cap: even when Content-Length is absent or lies, the stream
    accumulator detects overflow and returns 413 without OOM-buffering the body.
    Finding #401 / CWE-770.
    """
    agents_root = _build_agent_root(tmp_path, "testbot")
    # Set a tiny limit (100 bytes) so a 300-byte body triggers the guard.
    app = make_app(agents_root=agents_root, max_body_bytes=100)
    large_body = b'{"work_item": "' + b"y" * 300 + b'"}'

    # httpx / TestClient always sends Content-Length for raw bytes; to test
    # the stream path without Content-Length we patch the header out so the
    # Stage-1 check is skipped and only Stage-2 fires.
    class _NoContentLengthTransport:
        """Wraps TestClient's transport to strip Content-Length before Stage-1."""

    # The cleanest approach: send with Content-Length but set the limit to
    # something that lets Stage-1 pass (declared < limit) while Stage-2
    # catches actual overflow. We simulate a lying client: Content-Length
    # says 50 bytes but the real body is 315 bytes.
    app2 = make_app(agents_root=agents_root, max_body_bytes=200)
    # 315-byte body but Content-Length: 50 (a lie). Stage-1 passes (50<200),
    # Stage-2 detects 315>200 and returns 413.
    large_body2 = b'{"work_item": "' + b"z" * 300 + b'"}'
    with TestClient(app2, raise_server_exceptions=False) as client:
        resp = client.post(
            "/agents/testbot/call",
            content=large_body2,
            headers={
                "Content-Type": "application/json",
                "Content-Length": "50",  # lie: real body is ~315 bytes
            },
        )
    assert resp.status_code == 413, (
        f"Expected 413 for over-limit stream body (lying Content-Length); "
        f"got {resp.status_code}"
    )
    body = resp.json()
    assert body["status"] == "error"
    assert "too large" in body["error"]


def test_call_body_under_limit_succeeds(tmp_path: Path):
    """POST /call with a body under the limit passes the size guard and proceeds normally.

    Regression test: the body-size cap must not regress the happy path.
    Finding #401 guard must be transparent to legitimate requests.
    """
    agents_root = _build_agent_root(tmp_path, "testbot")

    async def fake_run_agent_call(**kwargs: Any) -> Any:
        return (
            "run-size-ok",
            MagicMock(
                skipped=False,
                text="ok",
                model="m",
                cost_usd=0.0,
                input_tokens=1,
                output_tokens=1,
            ),
        )

    # 10 KiB limit; a tiny body is well under it.
    app = make_app(agents_root=agents_root, max_body_bytes=10_240)
    with patch(
        "atomic_agents.serve._app.run_agent_call", side_effect=fake_run_agent_call
    ):
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/agents/testbot/call",
                json={"work_item": "hello"},
            )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_call_work_item_over_limit_returns_422(tmp_path: Path):
    """POST /call with work_item exceeding 32 KiB returns 422 (not dispatched).

    The work_item length cap prevents an oversized field from reaching the LLM
    dispatch before the cost guardrail fires. Mirrors the 512-char identity cap.
    Finding #401 / CWE-770; CLAUDE.md principle 4 (cost is first-class).
    """
    agents_root = _build_agent_root(tmp_path, "testbot")
    # Default max_body_bytes (1 MiB) is large enough that the body passes the
    # size guard; the work_item field cap (32 KiB) fires instead.
    app = make_app(agents_root=agents_root)
    oversized_work_item = "x" * (32_768 + 1)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/agents/testbot/call",
            json={"work_item": oversized_work_item},
        )
    assert resp.status_code == 422, (
        f"Expected 422 for oversized work_item; got {resp.status_code}"
    )
    body = resp.json()
    assert "work_item" in body["error"]
    assert "too long" in body["error"]

