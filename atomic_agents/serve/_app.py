"""serve/_app.py — Starlette ASGI application with the four spec/37 routes.

Routes:
  POST /agents/<name>/call    — invoke agent.call(), return JSON response
  GET  /agents/<name>/healthz — cheap liveness check (MUST 4)
  GET  /agents/<name>/doctor  — full doctor run (off hot path)
  GET  /agents               — list available agent names

This module is only imported when starlette is installed (serve extra).
spec/37 MUST 1.
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .._io import safe_resolve_under
from .._model import parse_model_md
from .._platform import get_agents_root
from ..exceptions import AtomicAgentsError, LockBusy, PathTraversalError
from ._config import ServeConfig
from ._runner import LockBusyWithRunId, run_agent_call, shutdown_executor

_logger = logging.getLogger(__name__)


def make_app(
    agents_root: Path | None = None,
    agent_name: str | None = None,
    identity_header: str = ServeConfig.identity_header,
    max_body_bytes: int = ServeConfig.max_body_bytes,
) -> Starlette:
    """Build and return the Starlette app.

    ``agents_root`` defaults to ``get_agents_root()`` at request time so
    ATOMIC_AGENTS_ROOT env var changes in tests are respected.

    ``agent_name``: when set, only routes for that one agent are served — all
    other agent names return HTTP 404. When None, all agents in agents_root
    are served (``--all`` mode).

    ``identity_header``: the HTTP header name to read for caller identity
    (default: ``X-Goog-IAP-JWT-Assertion``). Setting it here makes the app
    self-contained for testing and embedding.

    ``max_body_bytes``: maximum request body size in bytes for POST /call.
    Requests whose Content-Length header exceeds this value are rejected with
    HTTP 413 before body streaming begins. Bodies without a Content-Length
    header are also capped at this limit while streaming, so a lying or absent
    header cannot bypass the guard. Default: 1 MiB (1_048_576 bytes).
    CWE-770 / Finding #401.
    """
    _agents_root: Path | None = agents_root
    _single_agent: str | None = agent_name
    _max_body_bytes: int = max_body_bytes

    def _root() -> Path:
        return _agents_root or get_agents_root()

    def _check_agent_allowed(name: str) -> bool:
        """Return True if name is reachable in the current serve mode."""
        return _single_agent is None or name == _single_agent

    # ── POST /agents/<name>/call ──────────────────────────────────────────
    async def call_agent(request: Request) -> Response:
        """Invoke agent.call() for the named agent.

        spec/37 §"POST /agents/<name>/call".
        MUST 5: critical hard-coded to False, never from request data.
        MUST 6+7: identity extracted from header, passed as caller_identity.
        MUST 9: dispatched via run_in_executor (thread-pool adapter).
        MUST 10: safe_resolve_under guards path traversal.
        """
        name = request.path_params["name"]
        root = _root()

        # Single-agent mode: 404 any agent that isn't the one being served.
        if not _check_agent_allowed(name):
            return JSONResponse(
                {"status": "error", "error": f"Agent not found: {name!r}"},
                status_code=404,
            )

        # MUST 10 — path traversal guard
        try:
            safe_resolve_under(name, root)
        except PathTraversalError:
            return JSONResponse(
                {"status": "error", "error": f"Invalid agent name: {name!r}"},
                status_code=400,
            )

        # Body-size guard (Finding #401 / CWE-770 OOM DoS).
        #
        # Stage 1: reject on Content-Length header when present. This is a
        # fast pre-flight that avoids touching the stream at all when the
        # caller is honest about the body size.
        raw_content_length = request.headers.get("content-length")
        if raw_content_length is not None:
            try:
                declared_length = int(raw_content_length)
            except ValueError:
                declared_length = 0
            if declared_length > _max_body_bytes:
                return JSONResponse(
                    {
                        "status": "error",
                        "error": (
                            f"Request body too large: {declared_length} bytes "
                            f"exceeds limit of {_max_body_bytes} bytes"
                        ),
                    },
                    status_code=413,
                )

        # Stage 2: enforce the cap while streaming the body. Content-Length
        # can be omitted or set to a lie, so the stream itself must be capped.
        # Accumulate chunks up to the limit + 1 to detect overflow without
        # buffering the full oversized body in memory.
        chunks: list[bytes] = []
        accumulated = 0
        async for chunk in request.stream():
            accumulated += len(chunk)
            if accumulated > _max_body_bytes:
                # Drain the rest of the stream so the client connection stays
                # clean, then return 413. We do NOT buffer the remaining bytes.
                # Starlette's stream() yields all chunks lazily; breaking here
                # leaves the stream unconsumed, which is fine for the ASGI
                # contract (the server closes the connection on 413 anyway).
                return JSONResponse(
                    {
                        "status": "error",
                        "error": (
                            f"Request body too large: exceeds limit of "
                            f"{_max_body_bytes} bytes"
                        ),
                    },
                    status_code=413,
                )
            chunks.append(chunk)
        raw_body = b"".join(chunks)

        # Parse JSON body
        try:
            body = _json.loads(raw_body)
        except Exception:
            return JSONResponse(
                {"status": "error", "error": "Request body must be valid JSON"},
                status_code=400,
            )

        if not isinstance(body, dict):
            return JSONResponse(
                {"status": "error", "error": "Request body must be a JSON object"},
                status_code=400,
            )

        work_item = body.get("work_item")
        if not isinstance(work_item, str) or not work_item.strip():
            return JSONResponse(
                {
                    "status": "error",
                    "error": "work_item is required (non-empty string)",
                },
                status_code=422,
            )

        # work_item length cap: mirror the 512-char identity-header cap
        # (CLAUDE.md principle 4). 32 KiB is generous for any real agent
        # prompt while preventing a single oversized field from driving
        # unbounded token spend before the cost guardrail fires.
        # Finding #401 / CWE-770.
        _MAX_WORK_ITEM_CHARS = 32_768
        if len(work_item) > _MAX_WORK_ITEM_CHARS:
            return JSONResponse(
                {
                    "status": "error",
                    "error": (
                        f"work_item too long: {len(work_item)} chars "
                        f"exceeds limit of {_MAX_WORK_ITEM_CHARS}"
                    ),
                },
                status_code=422,
            )

        # MUST 5: critical is structurally unavailable via HTTP. If present in
        # request body it is silently ignored (not forwarded). Return 422 on
        # explicit critical=true to make the refusal observable in conformance tests.
        if body.get("critical") is True:
            return JSONResponse(
                {
                    "status": "error",
                    "error": (
                        "critical=true is not available via the HTTP surface. "
                        "Per spec/37 MUST 5, the cost guardrail cannot be bypassed "
                        "from the network layer."
                    ),
                },
                status_code=422,
            )

        raw_model_override = body.get("model_override")
        # Type-and-content guard — mirrors the work_item .strip() guard above.
        # A non-string model_override (e.g. {"model_override": 42}) is a client
        # error and MUST return 422, not be silently dropped. An empty or
        # whitespace-only string is equally invalid: "" passes the isinstance
        # check but reaches `model = model_override or self.config.default_model`
        # and is silently treated as absent (the silent-drop the comment above
        # says we prevent). "   " passes both isinstance and the or-fallback but
        # reaches the backend as an unresolvable model name and surfaces as HTTP
        # 500 — a client error masked as a server error. Both must be 422.
        # spec/37 §"Request body"; CLAUDE.md coding preference: "Prefer proper
        # error handling over silent failures."
        if raw_model_override is not None and (
            not isinstance(raw_model_override, str) or not raw_model_override.strip()
        ):
            return JSONResponse(
                {
                    "status": "error",
                    "error": "model_override must be a non-empty string",
                },
                status_code=422,
            )
        model_override: str | None = raw_model_override

        # Validate and coerce numeric fields BEFORE dispatching to run_agent_call.
        # Non-numeric values from the client are 422 (client error), not 500.
        raw_max_tokens = body.get("max_tokens")
        raw_temperature = body.get("temperature")
        max_tokens: int | None = None
        temperature: float | None = None
        if raw_max_tokens is not None:
            # Type-check by JSON type before any coercion. The precedence:
            # 1. Reject booleans: JSON booleans are a distinct type. int(True)==1
            #    passes the >0 check silently. The comment "non-numeric values
            #    return 422" cannot be satisfied via coercion — the type check IS
            #    the enforcement. spec/37 §"Request body". CLAUDE.md principle 12.
            # 2. Reject non-number JSON types (strings, objects, arrays): int('500')
            #    and int('  500  ') succeed, letting {"max_tokens":"500"} sail
            #    through as 200. JSON strings are non-numeric input; rejecting by
            #    JSON type (not by coercion failure) is the right shape.
            # 3. Accept int and float only. Non-integral float handled after.
            if isinstance(raw_max_tokens, bool) or not isinstance(
                raw_max_tokens, (int, float)
            ):
                return JSONResponse(
                    {
                        "status": "error",
                        "error": "max_tokens must be a positive integer",
                    },
                    status_code=422,
                )
            # Reject non-integral floats (e.g. 4096.7): spec/37 says "positive
            # integer". Silently flooring a non-integer float would allow
            # 4096.7 → 4096 via int(), which is the same silent-coercion
            # inconsistency the type guard exists to prevent.
            if isinstance(raw_max_tokens, float) and not raw_max_tokens.is_integer():
                return JSONResponse(
                    {
                        "status": "error",
                        "error": "max_tokens must be a positive integer",
                    },
                    status_code=422,
                )
            try:
                max_tokens = int(raw_max_tokens)
                if max_tokens <= 0:
                    raise ValueError("must be a positive integer")
            except (ValueError, TypeError):
                return JSONResponse(
                    {
                        "status": "error",
                        "error": "max_tokens must be a positive integer",
                    },
                    status_code=422,
                )
        if raw_temperature is not None:
            # Same type-first shape as max_tokens: reject by JSON type, not by
            # coercion failure. float('0.5') succeeds, so {"temperature":"0.5"}
            # would silently coerce — the inline comment's "non-numeric values
            # return 422" claim cannot be satisfied via coercion.
            # 1. Reject booleans: float(True)==1.0 passes [0.0,1.0] silently.
            # 2. Reject non-number JSON types (strings, objects, arrays).
            if isinstance(raw_temperature, bool) or not isinstance(
                raw_temperature, (int, float)
            ):
                return JSONResponse(
                    {
                        "status": "error",
                        "error": "temperature must be a float in [0.0, 1.0]",
                    },
                    status_code=422,
                )
            try:
                temperature = float(raw_temperature)
                # Cap at 1.0: Anthropic rejects temperature > 1.0, so accepting
                # [0.0, 2.0] (OpenAI's range) would pass 422 validation but still
                # produce a 500 on Anthropic backends — defeating the purpose of
                # pre-dispatch validation. The framework's default backend is
                # Anthropic; operators on OpenAI can request a wider range when a
                # real need surfaces. CLAUDE.md: "don't add abstractions for
                # hypothetical future needs." spec/37 §"Request body".
                if not (0.0 <= temperature <= 1.0):
                    raise ValueError("must be in range [0.0, 1.0]")
            except (ValueError, TypeError):
                return JSONResponse(
                    {
                        "status": "error",
                        "error": "temperature must be a float in [0.0, 1.0]",
                    },
                    status_code=422,
                )

        # MUST 6: read identity header value, pass through — never verify.
        # The header name is baked into the app from serve.md / env var at startup.
        # Cap the logged identity at 512 chars to prevent log amplification on
        # the lock_busy / cost-skip paths (which have no LLM cost gate but still
        # write the identity to the JSONL audit record). 512 chars is generous
        # for any real JWT-assertion identifier; Uvicorn's h11 default max-header
        # size (~16-64KB) would otherwise allow attacker-controlled disk writes
        # of arbitrary size on refused calls. CLAUDE.md principle 4 (cost is
        # first-class). spec/37 §"Audit record shape".
        identity_header_name: str = request.app.state.identity_header
        raw_identity: str | None = request.headers.get(identity_header_name)
        caller_identity: str | None = (
            raw_identity[:512] if raw_identity is not None else None
        )

        try:
            run_id, response = await run_agent_call(
                name=name,
                work_item=work_item,
                model_override=model_override,  # already validated str | None above
                max_tokens=max_tokens,
                temperature=temperature,
                caller_identity=caller_identity,
                agents_root=root,
            )
        except LockBusyWithRunId as e:
            # Include run_id so the caller can correlate the 503 with the JSONL
            # audit record. agent.run_id was reset before lock acquisition (MUST 8),
            # so the audit record and this response body carry the same id.
            # CLAUDE.md principle 5 (audit trail is structural).
            return JSONResponse(
                {"status": "lock_busy", "reason": str(e), "run_id": e.run_id},
                status_code=503,
            )
        except LockBusy as e:
            # Fallback for LockBusy raised outside run_agent_call (shouldn't occur
            # in normal operation, but keeps the handler robust).
            return JSONResponse(
                {"status": "lock_busy", "reason": str(e)},
                status_code=503,
            )
        except AtomicAgentsError as e:
            msg = str(e)
            # Agent folder not found → 404. Only the canonical init error
            # ("Agent folder not found: ...") warrants 404. Other AtomicAgentsError
            # subtypes (PersonaNotFound, rubric errors, sub_goal not found) are
            # server-misconfiguration conditions → 500. Using a broad substring
            # match on "not found" would mis-label 500-class errors as 404,
            # corrupting retry semantics for the caller.
            #
            # MUST NOT echo the raw AtomicAgentsError message in any response body —
            # it may contain the absolute on-disk vault path (e.g.
            # "Agent folder not found: /var/folders/.../agents/ghost",
            # "AgentProfileNotFound: .../realagent: neither persona/IDENTITY.md
            # nor persona.link.md exists", profile/filesystem.py:280/630/662,
            # registry/filesystem.py:711, eval.py:210/227, etc.).
            # Log the full message server-side; return a generic body so callers
            # cannot infer vault layout from the HTTP response.
            # spec/37 §"POST /agents/<name>/call" → Confidentiality (MUST);
            # CLAUDE.md aesthetic "Things you can't do, you can't do for a
            # documented reason."
            if msg.startswith("Agent folder not found"):
                _logger.warning("Agent folder not found for name=%r: %s", name, msg)
                return JSONResponse(
                    {"status": "error", "error": f"Agent not found: {name!r}"},
                    status_code=404,
                )
            # All other AtomicAgentsError → 500. Log full message (including any
            # path) server-side; never echo it to the caller.
            _logger.warning(
                "AtomicAgentsError in call_agent %r: %s: %s",
                name,
                type(e).__name__,
                msg,
            )
            return JSONResponse(
                {
                    "status": "error",
                    "error": f"Internal error processing agent {name!r}",
                },
                status_code=500,
            )
        except Exception:  # noqa: BLE001
            # Log full traceback + exception message server-side; never echo raw
            # exception messages to the caller — they may embed absolute paths or
            # internal state. Generic body matches the AtomicAgentsError 500 shape.
            _logger.exception("Unexpected error in call_agent %r", name)
            return JSONResponse(
                {
                    "status": "error",
                    "error": f"Internal error processing agent {name!r}",
                },
                status_code=500,
            )

        # Cost-cap skipped (HTTP 402 — Payment Required is the closest semantic fit)
        # Include run_id so the caller can correlate the 402 with the JSONL audit
        # record. run_id was reset before the cost-guardrails check (MUST 8), so
        # audit record and response body carry the same id. CLAUDE.md principle 5.
        if response.skipped:
            return JSONResponse(
                {"status": "skipped", "reason": response.skip_reason, "run_id": run_id},
                status_code=402,
            )

        # spec/37 §"Response body": return only the operator-facing fields.
        # Internal bookkeeping (helper_provenance, tool_calls, captures,
        # delegations) stays in the JSONL audit log — not in the HTTP response.
        return JSONResponse(
            {
                "run_id": run_id,
                "status": "ok",
                "output": response.text,
                "model": response.model,
                "cost_usd": response.cost_usd,
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
            }
        )

    # ── GET /agents/<name>/healthz ────────────────────────────────────────
    async def healthz(request: Request) -> Response:
        """Cheap liveness check — three filesystem checks only.

        spec/37 MUST 4: no doctor(), no provider key probes, no MCP subprocess.
        Check 1: agents_root readable.
        Check 2: agent folder exists.
        Check 3: model.md present and parse_model_md() returns without raising.
          Note: parse_model_md() tolerates malformed embedded YAML (e.g. a bad
          cost_guardrails block) by falling back to defaults — only a hard
          IOError/UnicodeDecodeError triggers 503. A broken cost_guardrails block
          does NOT degrade healthz.
        """
        name = request.path_params["name"]
        root = _root()

        # Single-agent mode: 404 any agent that isn't the one being served.
        if not _check_agent_allowed(name):
            return JSONResponse(
                {"status": "error", "error": f"Agent not found: {name!r}"},
                status_code=404,
            )

        # MUST 10 — path traversal guard
        try:
            safe_resolve_under(name, root)
        except PathTraversalError:
            return JSONResponse(
                {"status": "error", "error": f"Invalid agent name: {name!r}"},
                status_code=400,
            )

        # Check 1: agents_root readable
        if not root.is_dir():
            return JSONResponse(
                {"status": "degraded", "reason": f"agents_root not found: {root}"},
                status_code=503,
            )

        # Check 2: agent folder exists
        agent_folder = root / name
        if not agent_folder.is_dir():
            return JSONResponse(
                {"status": "degraded", "reason": f"agent folder not found: {name!r}"},
                status_code=503,
            )

        # Check 3: model.md present and parse_model_md() returns without raising.
        # parse_model_md() tolerates malformed embedded YAML (e.g. a bad
        # cost_guardrails block) by falling back to defaults — a malformed
        # cost_guardrails block does NOT cause degraded. Only a missing or
        # unreadable/hard-parse-error model.md triggers 503. An absent model.md
        # means no cost config — report degraded so Cloud Run removes the
        # container from the load-balancer rotation rather than silently serving
        # calls that would use an unknown default.
        model_md_path = agent_folder / "model.md"
        if not model_md_path.is_file():
            return JSONResponse(
                {"status": "degraded", "reason": "model.md is missing"},
                status_code=503,
            )
        try:
            parse_model_md(model_md_path)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"status": "degraded", "reason": f"model.md parse failed: {e}"},
                status_code=503,
            )

        return JSONResponse({"status": "ok", "agent": name})

    # ── GET /agents/<name>/doctor ─────────────────────────────────────────
    async def doctor_agent(request: Request) -> Response:
        """Full doctor run for the named agent. Off the hot path — not for liveness.

        spec/37 §"GET /agents/<name>/doctor".
        """
        from .. import doctor as doctor_module

        name = request.path_params["name"]
        root = _root()

        # Single-agent mode: 404 any agent that isn't the one being served.
        if not _check_agent_allowed(name):
            return JSONResponse(
                {"status": "error", "error": f"Agent not found: {name!r}"},
                status_code=404,
            )

        # MUST 10 — path traversal guard
        try:
            safe_resolve_under(name, root)
        except PathTraversalError:
            return JSONResponse(
                {"status": "error", "error": f"Invalid agent name: {name!r}"},
                status_code=400,
            )

        loop = asyncio.get_running_loop()

        def _run_doctor():
            return doctor_module.run_doctor(
                agent_name=name,
                agents_root=root,
                skip_mcp=False,
            )

        try:
            results = await loop.run_in_executor(None, _run_doctor)
        except Exception:  # noqa: BLE001
            # Log full exception (including any embedded paths) server-side only.
            # /doctor's 200 body discloses paths by design (spec/37 §Security note),
            # but the error path can surface state beyond the curated 200 body
            # (e.g. internal module paths, MCP config). Mirror /call's masking
            # discipline for defense-in-depth + cross-route consistency.
            _logger.exception("doctor crashed for agent %r", name)
            return JSONResponse(
                {
                    "status": "error",
                    "error": f"Internal error running doctor for agent {name!r}",
                },
                status_code=500,
            )

        return Response(
            content=doctor_module.render_json(results),
            media_type="application/json",
        )

    # ── GET /agents ───────────────────────────────────────────────────────
    async def list_agents(request: Request) -> Response:
        """List available agent names in the vault root.

        In single-agent mode, returns only the one agent being served.
        In all-agents mode, returns every agent folder found in agents_root.
        Returns only names (not paths, config content, or model fields).
        spec/37 §"GET /agents".
        MUST 10: every name is validated via safe_resolve_under.
        """
        root = _root()
        names: list[str] = []
        if _single_agent is not None:
            # Single-agent mode: report only this agent (if its folder exists).
            if root.is_dir() and (root / _single_agent).is_dir():
                names = [_single_agent]
        elif root.is_dir():
            for folder in sorted(root.iterdir()):
                if not folder.is_dir():
                    continue
                try:
                    # Validate: name must stay under root (defense in depth).
                    safe_resolve_under(folder.name, root)
                except PathTraversalError:
                    continue
                names.append(folder.name)

        return JSONResponse({"agents": names})

    # Use {name:str} (single non-slash segment) not {name:path} (matches slashes).
    # Agent names are always single path segments; {name:path} would admit nested
    # paths like "realagent/subdir" which the framework never intends to serve.
    # safe_resolve_under() stays as defense-in-depth, but the route converter is
    # the correct first-line refusal. CLAUDE.md principle 9 (one-level constraints).
    routes = [
        Route("/agents/{name:str}/call", endpoint=call_agent, methods=["POST"]),
        Route("/agents/{name:str}/healthz", endpoint=healthz, methods=["GET"]),
        Route("/agents/{name:str}/doctor", endpoint=doctor_agent, methods=["GET"]),
        Route("/agents", endpoint=list_agents, methods=["GET"]),
    ]

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app_: Starlette):  # type: ignore[type-arg]
        yield
        shutdown_executor()

    app = Starlette(routes=routes, lifespan=lifespan)
    # Set identity_header on state so the app is self-contained: make_app()
    # callers (tests, embedders) get a fully-functional app without needing
    # to set app.state manually. _server.py may still override this if the
    # resolved config differs from the constructor default.
    app.state.identity_header = identity_header
    return app
