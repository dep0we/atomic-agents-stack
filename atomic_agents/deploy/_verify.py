"""deploy/_verify.py — non-mutating, unbilled, predicate-based verification.

spec/49 §"Verification" + MUST 9. After the launchd agent is installed, deploy
verifies the running serve on loopback with a DEFINED pass predicate (a 200
response is NOT enough — ``/doctor`` returns 200 even when checks fail):

  1. ``GET /agents/<agent>/healthz`` — pass iff JSON ``status == "ok"``.
  2. ``GET /agents/<agent>/doctor``  — pass iff
     ``doctor.overall_exit_code(results) == 0`` (no failing check).

``--verify-call`` additionally fires a real ``POST /agents/<agent>/call`` which
bills tokens — opt-in, never the default.

Testability: every HTTP probe is routed through an injectable ``http_get`` /
``http_post`` callable so unit tests assert the pass/fail predicate logic
without a real network call or a running server.

    http_get(/healthz) ─► status=="ok"? ──┐
                                          ├─► verify() pass/fail
    http_get(/doctor)  ─► exit_code==0? ──┘
"""

from __future__ import annotations

import json as _json
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

# Sentinel HTTP status returned by the production http_get/http_post when the
# transport itself failed (connection refused, DNS, timeout) — i.e. the server
# was not reachable at all. It is NOT a real HTTP status; the predicate helpers
# below treat it as a clean FAIL (the body is empty), never an exception. This
# is what keeps a not-yet-bound launchd serve from crashing verify uncaught and
# leaving the agent installed (spec/49 MUST 8).
TRANSPORT_FAILURE_STATUS = 0

# An http_get takes a URL and returns (status_code, body_text).
HttpGet = Callable[[str], "tuple[int, str]"]
# An http_post takes (url, json_body) and returns (status_code, body_text).
HttpPost = Callable[[str, dict], "tuple[int, str]"]


@dataclass
class VerifyResult:
    """The outcome of verification.

    ``ok``        True iff every required predicate passed.
    ``checks``    ordered list of (name, passed, message) tuples for reporting.
    ``called``    True iff a real /call probe was fired (``--verify-call``).
    ``healthz_transport_failure``  True iff the FINAL healthz probe could not
                  reach the server at all (the transport sentinel, not a 503 or a
                  bad JSON body). This distinguishes "server unreachable — a
                  foreign holder of the port is plausible" from "our serve bound
                  the port but failed healthz/doctor" — the planner uses it to
                  decide whether an address-in-use diagnostic is warranted on
                  rollback (spec/49 MUST 10).
    """

    ok: bool
    checks: list[tuple[str, bool, str]] = field(default_factory=list)
    called: bool = False
    healthz_transport_failure: bool = False


def _default_http_get(url: str) -> "tuple[int, str]":
    """Production GET: returns (status, body). Never raises.

    urllib raises ``HTTPError`` for 4xx/5xx; we catch it and return its code +
    body so the predicate logic (which inspects the JSON body) can run on a
    non-2xx response (e.g. /healthz returns 503 with a JSON body).

    A freshly-``bootstrap``ed serve may not have bound the socket when the first
    probe fires, so the GET raises ``URLError`` (connection refused) or times
    out — a TRANSPORT failure, not an HTTP status. We MUST catch those too and
    return ``(TRANSPORT_FAILURE_STATUS, "")`` so the predicate FAILS cleanly
    rather than propagating an exception that would skip rollback and leave the
    launchd agent installed (spec/49 MUST 8). The retry loop then re-probes
    within the warm-up window.
    """
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310
            return resp.getcode(), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:  # 4xx / 5xx still carry a JSON body
        body = e.read().decode("utf-8", "replace") if e.fp else ""
        return e.code, body
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError):
        # Connection refused / DNS / timeout — the server is not reachable yet.
        # Return a sentinel so the predicate fails cleanly (never throws).
        return TRANSPORT_FAILURE_STATUS, ""


def _default_http_post(url: str, body: dict) -> "tuple[int, str]":
    """Production POST: JSON body. Returns (status, body)."""
    data = _json.dumps(body).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310
            return resp.getcode(), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", "replace") if e.fp else ""
        return e.code, body_text
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError):
        return TRANSPORT_FAILURE_STATUS, ""


def _check_healthz(status: int, body_text: str) -> tuple[bool, str]:
    """Pass iff the JSON ``status`` field == "ok" (spec/49 MUST 9).

    A 200 alone is not sufficient; the predicate is the body field.
    """
    try:
        payload = _json.loads(body_text)
    except (ValueError, TypeError):
        return False, f"healthz returned non-JSON body (HTTP {status})"
    if isinstance(payload, dict) and payload.get("status") == "ok":
        return True, "healthz status == ok"
    reason = payload.get("reason") if isinstance(payload, dict) else None
    return False, f"healthz status != ok (HTTP {status}): {reason or body_text[:120]}"


def _check_doctor(status: int, body_text: str) -> tuple[bool, str]:
    """Pass iff ``overall_exit_code(results) == 0`` (spec/49 MUST 9).

    The /doctor route returns 200 with a JSON list of check results even when
    checks fail, so we MUST recompute the exit code from the result list rather
    than trust the HTTP status. We import doctor lazily and feed it parsed
    CheckResult-shaped dicts.

    Fail-closed posture (spec/49 MUST 9 — a non-2xx or error-shaped body MUST
    NOT pass): we MUST NOT conflate "no checks parsed" with "no checks failed".
    A non-2xx HTTP status (the route errored / a transport sentinel), an
    error-shaped body (``{"status":"error"}`` / ``{"error":...}`` / ``{"detail":
    ...}``), or a payload that carries NO recognizable results key are all FAILs
    — only a well-formed results list whose ``overall_exit_code`` is 0 passes.
    """
    from .. import doctor as doctor_module

    # A non-2xx status means the /doctor route itself did not return its normal
    # 200+results payload (it returns 200 even when checks fail). Treat any
    # non-2xx — including the transport-failure sentinel (0) — as a FAIL.
    if not (200 <= status < 300):
        return False, f"doctor returned HTTP {status} (expected 2xx with results)"

    try:
        payload = _json.loads(body_text)
    except (ValueError, TypeError):
        return False, f"doctor returned non-JSON body (HTTP {status})"

    # An error-shaped body is a FAIL even at HTTP 200 (e.g. a serve handler that
    # caught an exception and rendered {"status":"error","error":...}).
    if isinstance(payload, dict):
        if payload.get("status") == "error" or "error" in payload:
            reason = payload.get("error") or payload.get("detail") or "unknown error"
            return False, f"doctor reported an error body (HTTP {status}): {reason}"

    # render_json emits a top-level object; tolerate either a bare list of
    # checks or an object with a "checks"/"results" key. A MISSING/unrecognized
    # results key is NOT "zero failures" — it is a FAIL, because we cannot prove
    # the deployment is healthy from a body we could not parse into checks.
    if isinstance(payload, list):
        results_raw = payload
    elif isinstance(payload, dict):
        results_raw = None
        for candidate in ("checks", "results", "checks_results"):
            if candidate in payload:
                results_raw = payload[candidate]
                break
        if results_raw is None:
            return False, (
                "doctor body had no recognizable results key "
                "(checks/results/checks_results)"
            )
    else:
        return False, "doctor body was neither a results list nor an object"

    if not isinstance(results_raw, list):
        return False, "doctor results key was not a list"

    # Rebuild CheckResult objects so we reuse the canonical predicate
    # (overall_exit_code) rather than re-implementing "any failed" here — keeps
    # deploy's verdict and doctor's exit-code semantics in lockstep.
    rebuilt = [
        doctor_module.CheckResult(
            name=str(item.get("name", "")),
            status=str(item.get("status", "")),
            message=str(item.get("message", "")),
        )
        for item in results_raw
        if isinstance(item, dict)
    ]
    # Fail-closed: an empty results list (``{"results": []}``) or one whose every
    # item was malformed/unrecognized (``{"checks": [null]}`` → rebuilt == [])
    # rebuilds to nothing. ``overall_exit_code([]) == 0`` would PASS, conflating
    # "no checks parsed" with "no checks failed" — exactly the false-pass MUST 9
    # forbids. We require at least one well-formed check before trusting the 0.
    if not rebuilt:
        return False, (
            "doctor returned no well-formed check results "
            f"(parsed {len(results_raw)} raw item(s)); cannot prove the "
            "deployment is healthy"
        )
    code = doctor_module.overall_exit_code(rebuilt)
    if code == 0:
        return True, "doctor overall_exit_code == 0"
    failed = [r.name for r in rebuilt if r.failed]
    return False, f"doctor reported failing checks: {', '.join(failed) or 'unknown'}"


def verify_deployment(
    agent: str,
    host: str,
    port: int,
    *,
    verify_call: bool = False,
    http_get: HttpGet = _default_http_get,
    http_post: HttpPost = _default_http_post,
    retries: int = 1,
    retry_delay_s: float = 0.0,
) -> VerifyResult:
    """Verify a deployed agent on loopback (spec/49 MUST 9).

    Runs healthz + doctor predicates; both must pass. With ``verify_call``,
    additionally fires a real billed ``POST /call`` (opt-in only).

    ``retries`` allows a brief warm-up window for the launchd-started serve to
    bind before the first probe; tests pass ``retries=1`` for determinism.
    """
    base = f"http://{host}:{port}/agents/{agent}"
    checks: list[tuple[str, bool, str]] = []

    # healthz — retried because launchd may not have bound the socket yet.
    h_ok, h_msg = False, "healthz not probed"
    h_transport_fail = False
    for attempt in range(max(1, retries)):
        status, body = http_get(f"{base}/healthz")
        # Track whether the FINAL probe was a transport failure (server
        # unreachable) vs a real HTTP response (503 / bad body). Only the former
        # is consistent with "a foreign process holds the port, our serve never
        # bound" — the planner's address-in-use diagnostic keys off this.
        h_transport_fail = status == TRANSPORT_FAILURE_STATUS
        h_ok, h_msg = _check_healthz(status, body)
        if h_ok:
            break
        if attempt + 1 < retries and retry_delay_s > 0:
            time.sleep(retry_delay_s)
    checks.append(("healthz", h_ok, h_msg))

    if not h_ok:
        # Short-circuit: no point probing /doctor if the server is not even up.
        return VerifyResult(
            ok=False,
            checks=checks,
            healthz_transport_failure=h_transport_fail,
        )

    # doctor predicate
    d_status, d_body = http_get(f"{base}/doctor")
    d_ok, d_msg = _check_doctor(d_status, d_body)
    checks.append(("doctor", d_ok, d_msg))

    overall = h_ok and d_ok
    called = False
    if verify_call and overall:
        # Opt-in billed end-to-end probe (spec/49 §"Verification").
        c_status, c_body = http_post(
            f"{base}/call",
            {"work_item": "deploy verify ping"},
        )
        try:
            c_payload = _json.loads(c_body)
        except (ValueError, TypeError):
            c_payload = {}
        c_ok = c_status == 200 and (
            isinstance(c_payload, dict) and c_payload.get("status") == "ok"
        )
        checks.append(
            (
                "call",
                c_ok,
                f"/call returned status={c_payload.get('status') if isinstance(c_payload, dict) else '?'} (HTTP {c_status})",
            )
        )
        called = True
        overall = overall and c_ok

    return VerifyResult(ok=overall, checks=checks, called=called)
