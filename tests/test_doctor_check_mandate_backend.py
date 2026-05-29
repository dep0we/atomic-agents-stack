"""Doctor coherence tests for ``check_mandate_backend`` (#235).

Mirrors the analog suite for ``check_policy_backend`` (see
``tests/test_policy_integration.py`` §"Doctor check_policy_backend"):
one PASS path, one WARN-no-config path, two FAIL paths covering operator
config typos (one bare id, one URL-shaped value pasted into the id env
var by mistake), one capability-snapshot assertion.

The mandates.md fixture is copied from the parametrized conformance suite
(``tests/test_mandate_protocol_conformance.py::_GOOD_MANDATE_FILE``) so
the fixture and the impl can't drift — the parser surface is the same one
the conformance suite already validates.
"""

from __future__ import annotations

from pathlib import Path

from atomic_agents.doctor import check_mandate_backend


# Minimal valid mandates.md content — copied from
# tests/test_mandate_protocol_conformance.py::_GOOD_MANDATE_FILE so the
# parser conformance suite owns its evolution.
_GOOD_MANDATE_FILE = """\
## procurement-q2-2026
granted_by: operator
granted_at: 2026-04-01T00:00:00Z
expires_at: 2026-06-30T23:59:59Z
revocable_by: operator
scope: |
  Purchase SaaS subscriptions on the approved-vendor list.
  Individual subscriptions ≤ $200/month.
constraints:
  daily_external_usd: 200
  monthly_external_usd: 2000
  cumulative_external_usd: 6000
  allowed_tools:
    - stripe.subscribe
    - vendor_lookup
revocation_state: active
revoked_at: null
revocation_reason: null
"""


def test_check_mandate_backend_passes_with_no_mandates_md(tmp_path: Path) -> None:
    """No ``mandates.md`` at ``scope_root`` → WARN (no-opinion informational).

    Per spec/27 §"mandate-backend" PASS/WARN/FAIL ladder + spec/29 — when
    no operator-granted authorities exist, the surface is opt-in and the
    backend is healthy. The doctor surfaces this as WARN (not FAIL — it's
    a valid operational state for single-agent home users) so operators
    who DO intend to author mandates know they haven't yet.
    """
    # No mandates.md written — tmp_path is an empty scope_root
    result = check_mandate_backend(tmp_path)

    assert result.status == "warn", (
        f"Expected WARN with mandates.md absent, "
        f"got {result.status!r}: {result.message}"
    )
    assert result.detail is not None
    assert result.detail.get("backend_id") == "filesystem"
    assert result.detail.get("mandates_md_exists") is False
    assert result.detail.get("mandate_count") == 0
    # Operator-facing fix hint should point at the right file
    assert "mandates.md" in (result.fix_hint or ""), (
        f"WARN fix_hint should reference mandates.md; got {result.fix_hint!r}"
    )


def test_check_mandate_backend_passes_with_mandates_md(tmp_path: Path) -> None:
    """Valid ``mandates.md`` at ``scope_root`` → PASS with mandate_count >= 1.

    Verifies the PASS path of ``check_mandate_backend`` — when the
    operator authored a valid mandates.md, the doctor reports the
    discovered count + capability snapshot.
    """
    (tmp_path / "mandates.md").write_text(_GOOD_MANDATE_FILE, encoding="utf-8")

    result = check_mandate_backend(tmp_path)

    assert result.status == "pass", (
        f"Expected PASS with mandates.md present, "
        f"got {result.status!r}: {result.message}"
    )
    assert result.detail is not None
    assert result.detail.get("backend_id") == "filesystem"
    assert result.detail.get("mandates_md_exists") is True
    assert result.detail.get("mandate_count", 0) >= 1


def test_check_mandate_backend_fails_on_unknown_env_var(
    tmp_path: Path, monkeypatch
) -> None:
    """Unknown backend id → FAIL with helpful message + no credential leak.

    ``ATOMIC_AGENTS_MANDATE_BACKEND=nonexistent`` is not registered; the
    doctor must surface this as FAIL (not crash, not WARN) so the operator
    knows their env var is wrong before any agent runs.
    """
    monkeypatch.setenv("ATOMIC_AGENTS_MANDATE_BACKEND", "nonexistent")

    result = check_mandate_backend(tmp_path)

    assert result.status == "fail", (
        f"Expected FAIL for unknown backend id, got {result.status!r}: {result.message}"
    )
    # The bare id "nonexistent" carries no credential — it should appear
    # verbatim in the surfaced message so operators recognize their typo.
    assert "nonexistent" in result.message, (
        f"FAIL message should echo the bad id; got {result.message!r}"
    )


def test_check_mandate_backend_fails_on_unknown_env_var_url_credential_redaction(
    tmp_path: Path, monkeypatch
) -> None:
    """URL-shaped value pasted into the id env var → FAIL with credentials
    stripped from the surfaced error text.

    If an operator accidentally pastes
    ``postgres://user:password@host/db`` into ``ATOMIC_AGENTS_MANDATE_BACKEND``
    (instead of the ``..._URL`` variant), the bad id contains a password.
    The doctor MUST redact at ``://`` before echoing so error-tracking
    services + CI logs don't leak the secret.
    """
    monkeypatch.setenv(
        "ATOMIC_AGENTS_MANDATE_BACKEND",
        "postgres://user:password@host/db",
    )

    result = check_mandate_backend(tmp_path)

    assert result.status == "fail", (
        f"Expected FAIL for URL pasted into id env var, "
        f"got {result.status!r}: {result.message}"
    )
    # Password MUST NOT appear in the surfaced message
    assert "password" not in result.message, (
        f"FAIL message must redact the password; got {result.message!r}"
    )
    # User component MUST NOT appear either (token-as-username is a real
    # managed-service pattern — Upstash, PlanetScale, Heroku)
    assert "user:password" not in result.message, (
        f"FAIL message must redact user:password; got {result.message!r}"
    )
    # The scheme is fine to surface — it tells the operator what shape
    # they pasted by mistake
    assert "postgres" in result.message, (
        f"FAIL message should surface the scheme so operator sees the "
        f"shape of their mistake; got {result.message!r}"
    )


def test_check_mandate_backend_capability_snapshot_in_detail(
    tmp_path: Path,
) -> None:
    """PASS path → detail carries the full MandateCapabilities snapshot.

    Per spec/27 the doctor surfaces the capability declaration so
    operators inspecting ``atomic-agents doctor`` see what their pinned
    backend actually supports — revocation, external-state-change
    notification, durability, crash recovery.
    """
    (tmp_path / "mandates.md").write_text(_GOOD_MANDATE_FILE, encoding="utf-8")

    result = check_mandate_backend(tmp_path)

    assert result.status == "pass"
    assert result.detail is not None
    # All four MandateCapabilities fields surface in detail
    assert "supports_revocation" in result.detail
    assert "supports_external_state_change_notification" in result.detail
    assert "durable" in result.detail
    assert "supports_crash_recovery" in result.detail
    # Plus the discovered mandate count
    assert "mandate_count" in result.detail
    # Filesystem backend declares these exact values (verified against
    # atomic_agents/mandate/types.py:300-337)
    assert result.detail["supports_revocation"] is True
    assert result.detail["supports_external_state_change_notification"] is False
    assert result.detail["durable"] is True
    assert result.detail["supports_crash_recovery"] is True
