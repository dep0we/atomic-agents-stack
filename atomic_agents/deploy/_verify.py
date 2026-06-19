"""deploy/_verify.py — non-mutating, unbilled, predicate-based verification.

spec/48 §"Verification" + MUST 9. After the launchd agent is installed, deploy
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
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

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
    """

    ok: bool
    checks: list[tuple[str, bool, str]] = field(default_factory=list)
    called: bool = False


def _default_http_get(url: str) -> "tuple[int, str]":
    """Production GET: returns (status, body). Never raises on HTTP error codes.

    urllib raises ``HTTPError`` for 4xx/5xx; we catch it and return its code +
    body so the predicate logic (which inspects the JSON body) can run on a
    non-2xx response (e.g. /healthz returns 503 with a JSON body).
    """
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310
            return resp.getcode(), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:  # 4xx / 5xx still carry a JSON body
        body = e.read().decode("utf-8", "replace") if e.fp else ""
        return e.code, body


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


def _check_healthz(status: int, body_text: str) -> tuple[bool, str]:
    """Pass iff the JSON ``status`` field == "ok" (spec/48 MUST 9).

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
    """Pass iff ``overall_exit_code(results) == 0`` (spec/48 MUST 9).

    The /doctor route returns 200 with a JSON list of check results even when
    checks fail, so we MUST recompute the exit code from the result list rather
    than trust the HTTP status. We import doctor lazily and feed it parsed
    CheckResult-shaped dicts.
    """
    from .. import doctor as doctor_module

    try:
        payload = _json.loads(body_text)
    except (ValueError, TypeError):
        return False, f"doctor returned non-JSON body (HTTP {status})"

    # render_json emits a top-level object; tolerate either a bare list of
    # checks or an object with a "checks"/"results" key.
    if isinstance(payload, dict):
        results_raw = (
            payload.get("checks")
            or payload.get("results")
            or payload.get("checks_results")
            or []
        )
    elif isinstance(payload, list):
        results_raw = payload
    else:
        results_raw = []

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
    """Verify a deployed agent on loopback (spec/48 MUST 9).

    Runs healthz + doctor predicates; both must pass. With ``verify_call``,
    additionally fires a real billed ``POST /call`` (opt-in only).

    ``retries`` allows a brief warm-up window for the launchd-started serve to
    bind before the first probe; tests pass ``retries=1`` for determinism.
    """
    base = f"http://{host}:{port}/agents/{agent}"
    checks: list[tuple[str, bool, str]] = []

    # healthz — retried because launchd may not have bound the socket yet.
    h_ok, h_msg = False, "healthz not probed"
    for attempt in range(max(1, retries)):
        status, body = http_get(f"{base}/healthz")
        h_ok, h_msg = _check_healthz(status, body)
        if h_ok:
            break
        if attempt + 1 < retries and retry_delay_s > 0:
            time.sleep(retry_delay_s)
    checks.append(("healthz", h_ok, h_msg))

    if not h_ok:
        # Short-circuit: no point probing /doctor if the server is not even up.
        return VerifyResult(ok=False, checks=checks)

    # doctor predicate
    d_status, d_body = http_get(f"{base}/doctor")
    d_ok, d_msg = _check_doctor(d_status, d_body)
    checks.append(("doctor", d_ok, d_msg))

    overall = h_ok and d_ok
    called = False
    if verify_call and overall:
        # Opt-in billed end-to-end probe (spec/48 §"Verification").
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
