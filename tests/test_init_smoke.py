"""Smoke tests for the atomic-agents init wizard -- end-to-end flows with mocked LLM.

Each test exercises the full chain from cli.main() through wizard.run_init(),
_from_template(), _doctor_handoff(), and _maybe_test_call()/_test_call().

All network calls, AtomicAgent construction, and doctor checks are mocked so
no real API key or agent directory is required.

Coverage:
    1. Happy-path from-template with test call accepted -- exit 0, files written.
    2. Doctor-pass path offers the "Want to try a test call now?" prompt.
    3. Doctor-fail path blocks the test call prompt entirely.
    4. RateLimitError during test call -- graceful exit 0 with message.
    5. APIConnectionError during test call -- graceful exit 0 with message.
    6. Operator declines test call -- exit 0 without invoking AtomicAgent.call.
    7. from-template researcher -- exit 0, researcher-specific content written.
    8. from-template writer -- exit 0, writer-specific content written.
    9. from-template works without API key (P3 lock test).
"""

from __future__ import annotations

import pytest

from atomic_agents import cli as cli_module
from atomic_agents.init import constants as C
from atomic_agents.doctor import CheckResult, PASS, FAIL


# ---------------------------------------------------------------------------
# Shared fake objects
# ---------------------------------------------------------------------------


class FakeResponse:
    """Minimal stand-in for the real AtomicAgent.call() Response object."""

    text = "Hello! I am test-agent, your personal advisor."
    skipped = False
    skip_reason = None
    model = "claude-opus-4-7"
    input_tokens = 10
    output_tokens = 20
    cost_usd = 0.01


def _fake_call_ok(self, work_item, **kwargs):
    """AtomicAgent.call replacement that returns a successful FakeResponse."""
    return FakeResponse()


# ---------------------------------------------------------------------------
# Shared fixture: wire up the TTY guard, API-key preflight, and doctor pass.
# Every smoke test needs all three to get past the wizard's guard rails.
# ---------------------------------------------------------------------------


def _patch_common(monkeypatch, tmp_path, confirm_returns=True):
    """Apply the core mocks every smoke test requires.

    - sys.stdin.isatty -> True (passes the non-TTY guard at the top of run_init)
    - atomic_agents._llm._get_key -> returns a fake key (passes API-key preflight)
    - atomic_agents.doctor.run_doctor -> returns a single PASS result
    - atomic_agents.doctor.render_human -> returns empty string
    - atomic_agents.doctor.overall_exit_code -> returns 0
    - rich.prompt.Confirm.ask -> returns confirm_returns (True = accept test call)

    Doctor functions are patched on the canonical module (atomic_agents.doctor)
    because wizard._doctor_handoff() does `from .. import doctor` at call time,
    importing the module object by reference. Patching the module's attributes
    is the correct intercept point.
    """
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    monkeypatch.setattr(
        "atomic_agents._llm._get_key",
        lambda env_vars=None, keychain_name=None, config_key=None: "sk-ant-test-key",
    )

    passing_result = CheckResult(name="env", status=PASS, message="ok")

    _patch_doctor(monkeypatch, results=[passing_result], exit_code=0)

    monkeypatch.setattr(
        "rich.prompt.Confirm.ask",
        lambda *a, **kw: confirm_returns,
    )


def _patch_doctor(monkeypatch, results, exit_code):
    """Patch the three doctor functions tests depend on."""
    monkeypatch.setattr(
        "atomic_agents.doctor.run_doctor",
        lambda agent_name=None, agents_root=None, skip_mcp=False: results,
    )
    monkeypatch.setattr(
        "atomic_agents.doctor.render_human",
        lambda r: "",
    )
    monkeypatch.setattr(
        "atomic_agents.doctor.overall_exit_code",
        lambda r: exit_code,
    )


# ---------------------------------------------------------------------------
# Test 1: happy path -- files written, exit 0
# ---------------------------------------------------------------------------


def test_smoke_from_template_advisor_happy_path(monkeypatch, tmp_path):
    """From-template advisor scaffolds files and exits 0 when test call succeeds."""
    _patch_common(monkeypatch, tmp_path, confirm_returns=True)
    monkeypatch.setattr("atomic_agents.agent.AtomicAgent.call", _fake_call_ok)

    exit_code = cli_module.main(
        [
            "init",
            "test-agent",
            "--from-template",
            "advisor",
            "--agents-root",
            str(tmp_path),
        ]
    )

    assert exit_code == 0

    identity_path = tmp_path / "test-agent" / "persona" / "IDENTITY.md"
    assert identity_path.exists(), f"Expected IDENTITY.md at {identity_path}"


# ---------------------------------------------------------------------------
# Test 2: doctor pass offers the test call prompt
# ---------------------------------------------------------------------------


def test_smoke_doctor_pass_offers_test_call_prompt(monkeypatch, tmp_path, capsys):
    """When doctor passes, the wizard prints the test call offer to stdout."""
    _patch_common(monkeypatch, tmp_path, confirm_returns=False)
    monkeypatch.setattr("atomic_agents.agent.AtomicAgent.call", _fake_call_ok)

    # Track whether Confirm.ask was called with the test-call prompt text.
    confirm_calls = []

    def capturing_confirm(prompt, *a, **kw):
        confirm_calls.append(prompt)
        return False  # decline so AtomicAgent.call is never invoked

    monkeypatch.setattr("rich.prompt.Confirm.ask", capturing_confirm)

    exit_code = cli_module.main(
        [
            "init",
            "test-agent",
            "--from-template",
            "advisor",
            "--agents-root",
            str(tmp_path),
        ]
    )

    assert exit_code == 0

    test_call_prompts = [p for p in confirm_calls if "test call" in p.lower()]
    assert test_call_prompts, (
        f"Expected a Confirm.ask prompt mentioning 'test call'; "
        f"got prompts: {confirm_calls!r}"
    )


# ---------------------------------------------------------------------------
# Test 3: doctor FAIL blocks the test call prompt
# ---------------------------------------------------------------------------


def test_smoke_doctor_fail_blocks_test_call_prompt(monkeypatch, tmp_path):
    """When doctor returns a FAIL result, the wizard exits 1 and never calls AtomicAgent."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(
        "atomic_agents._llm._get_key",
        lambda env_vars=None, keychain_name=None, config_key=None: "sk-ant-test-key",
    )
    monkeypatch.setattr(
        "rich.prompt.Confirm.ask",
        lambda *a, **kw: True,
    )

    failing_result = CheckResult(
        name="vault",
        status=FAIL,
        message="persona/IDENTITY.md missing",
    )
    _patch_doctor(monkeypatch, results=[failing_result], exit_code=1)

    call_invocations = []

    def sentinel_call(self, work_item, **kwargs):
        call_invocations.append(work_item)
        return FakeResponse()

    monkeypatch.setattr("atomic_agents.agent.AtomicAgent.call", sentinel_call)

    exit_code = cli_module.main(
        [
            "init",
            "test-agent",
            "--from-template",
            "advisor",
            "--agents-root",
            str(tmp_path),
        ]
    )

    assert exit_code == 1
    assert call_invocations == [], (
        "AtomicAgent.call should not be invoked when doctor fails; "
        f"got {call_invocations!r}"
    )


# ---------------------------------------------------------------------------
# Test 4: RateLimitError during test call -- graceful exit 0
# ---------------------------------------------------------------------------


def test_smoke_test_call_rate_limit_graceful_exit_0(monkeypatch, tmp_path, capsys):
    """RateLimitError during test call prints the rate-limit message and exits 0."""
    _patch_common(monkeypatch, tmp_path, confirm_returns=True)

    import anthropic as _anthropic

    class FakeRateLimitError(_anthropic.RateLimitError):
        def __init__(self, message):
            Exception.__init__(self, message)

    def raising_rate_limit(self, work_item, **kwargs):
        raise FakeRateLimitError("Too many requests")

    monkeypatch.setattr("atomic_agents.agent.AtomicAgent.call", raising_rate_limit)

    exit_code = cli_module.main(
        [
            "init",
            "test-agent",
            "--from-template",
            "advisor",
            "--agents-root",
            str(tmp_path),
        ]
    )

    assert exit_code == 0

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    expected_fragment = "busy right now"
    assert expected_fragment in combined, (
        f"Expected rate-limit message containing '{expected_fragment}'; "
        f"got:\n{combined}"
    )


# ---------------------------------------------------------------------------
# Test 5: APIConnectionError during test call -- graceful exit 0
# ---------------------------------------------------------------------------


def test_smoke_test_call_network_error_graceful_exit_0(monkeypatch, tmp_path, capsys):
    """APIConnectionError during test call prints the network message and exits 0."""
    _patch_common(monkeypatch, tmp_path, confirm_returns=True)

    import anthropic as _anthropic

    class FakeAPIConnectionError(_anthropic.APIConnectionError):
        def __init__(self, message):
            Exception.__init__(self, message)

    def raising_network(self, work_item, **kwargs):
        raise FakeAPIConnectionError("Network unreachable")

    monkeypatch.setattr("atomic_agents.agent.AtomicAgent.call", raising_network)

    exit_code = cli_module.main(
        [
            "init",
            "test-agent",
            "--from-template",
            "advisor",
            "--agents-root",
            str(tmp_path),
        ]
    )

    assert exit_code == 0

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    expected_fragment = "network connection"
    assert expected_fragment.lower() in combined.lower(), (
        f"Expected network error message containing '{expected_fragment}'; "
        f"got:\n{combined}"
    )


# ---------------------------------------------------------------------------
# Test 6: operator declines test call -- exit 0, AtomicAgent.call not invoked
# ---------------------------------------------------------------------------


def test_smoke_test_call_decline_exits_0(monkeypatch, tmp_path):
    """Declining the test call prompt exits 0 without invoking AtomicAgent.call."""
    _patch_common(monkeypatch, tmp_path, confirm_returns=False)

    call_invocations = []

    def sentinel_call(self, work_item, **kwargs):
        call_invocations.append(work_item)
        return FakeResponse()

    monkeypatch.setattr("atomic_agents.agent.AtomicAgent.call", sentinel_call)

    exit_code = cli_module.main(
        [
            "init",
            "test-agent",
            "--from-template",
            "advisor",
            "--agents-root",
            str(tmp_path),
        ]
    )

    assert exit_code == 0
    assert call_invocations == [], (
        "AtomicAgent.call should not be invoked when operator declines the test call; "
        f"got {call_invocations!r}"
    )


# ---------------------------------------------------------------------------
# Test 7: from-template researcher -- researcher-specific content written
# ---------------------------------------------------------------------------


def test_smoke_from_template_researcher_happy_path(monkeypatch, tmp_path):
    """From-template researcher scaffolds files with researcher-specific content."""
    _patch_common(monkeypatch, tmp_path, confirm_returns=True)
    monkeypatch.setattr("atomic_agents.agent.AtomicAgent.call", _fake_call_ok)

    exit_code = cli_module.main(
        [
            "init",
            "test-agent",
            "--from-template",
            "researcher",
            "--agents-root",
            str(tmp_path),
        ]
    )

    assert exit_code == 0

    identity_path = tmp_path / "test-agent" / "persona" / "IDENTITY.md"
    assert identity_path.exists(), f"Expected IDENTITY.md at {identity_path}"

    content = identity_path.read_text(encoding="utf-8")
    # Researcher template contains these markers (visible in the raw template).
    assert (
        "curiosity" in content.lower()
        or "Research integrity" in content
        or "investigation" in content.lower()
    ), f"Expected researcher-specific markers in IDENTITY.md; got:\n{content[:500]}"


# ---------------------------------------------------------------------------
# Test 8: from-template writer -- writer-specific content written
# ---------------------------------------------------------------------------


def test_smoke_from_template_writer_happy_path(monkeypatch, tmp_path):
    """From-template writer scaffolds files with writer-specific content."""
    _patch_common(monkeypatch, tmp_path, confirm_returns=True)
    monkeypatch.setattr("atomic_agents.agent.AtomicAgent.call", _fake_call_ok)

    exit_code = cli_module.main(
        [
            "init",
            "test-agent",
            "--from-template",
            "writer",
            "--agents-root",
            str(tmp_path),
        ]
    )

    assert exit_code == 0

    identity_path = tmp_path / "test-agent" / "persona" / "IDENTITY.md"
    assert identity_path.exists(), f"Expected IDENTITY.md at {identity_path}"

    content = identity_path.read_text(encoding="utf-8")
    # Writer template contains these markers (visible in the raw template).
    assert (
        "voice" in content.lower()
        or "drafts" in content.lower()
        or "the agent IS the writer" in content
    ), f"Expected writer-specific markers in IDENTITY.md; got:\n{content[:500]}"


# ---------------------------------------------------------------------------
# Test 9: from-template works without API key (P3 lock test)
# ---------------------------------------------------------------------------


def test_smoke_from_template_works_without_api_key(monkeypatch, tmp_path):
    """--from-template does not require an API key at scaffold time (P3 lock).

    Per spec/35 MUST 7 amendment (P3 lock): --from-template writes file content
    only and does not make LLM calls. An AtomicAgentsError from _get_key must
    not prevent the scaffold from succeeding (exit 0).
    """
    from atomic_agents.exceptions import AtomicAgentsError

    # TTY guard must pass for run_init to proceed.
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    # _get_key raises -- simulates no API key configured anywhere.
    def _raising_get_key(env_vars=None, keychain_name=None, config_key=None):
        raise AtomicAgentsError("No API key found")

    monkeypatch.setattr("atomic_agents._llm._get_key", _raising_get_key)

    # Doctor and AtomicAgent.call still mocked so we exercise the scaffold path.
    from atomic_agents.doctor import CheckResult, PASS

    passing_result = CheckResult(name="env", status=PASS, message="ok")
    _patch_doctor(monkeypatch, results=[passing_result], exit_code=0)

    monkeypatch.setattr(
        "rich.prompt.Confirm.ask",
        lambda *a, **kw: True,
    )
    monkeypatch.setattr("atomic_agents.agent.AtomicAgent.call", _fake_call_ok)

    exit_code = cli_module.main(
        [
            "init",
            "test",
            "--from-template",
            "advisor",
            "--agents-root",
            str(tmp_path),
        ]
    )

    assert exit_code == 0, (
        "--from-template should exit 0 even when _get_key raises; "
        f"got exit_code={exit_code}"
    )

    identity_path = tmp_path / "test" / "persona" / "IDENTITY.md"
    assert identity_path.exists(), (
        f"Scaffold files should be written even without an API key; "
        f"IDENTITY.md not found at {identity_path}"
    )
