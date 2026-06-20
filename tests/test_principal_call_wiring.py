"""agent.call() integration tests for PrincipalBackend wiring (spec/48).

Tests the HARD-REFUSE gate, is_verified enforcement, conversation isolation
by principal, and the principal kwarg threading to ConversationBackend.

Uses tmp_path via the agent fixture (the agent has filesystem state).
Per-invocation negative controls are included for load-bearing assertions.

Run: uv run pytest tests/test_principal_call_wiring.py -v
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from atomic_agents.conversation.filesystem import FilesystemConversationBackend
from atomic_agents.conversation.types import LOCAL_PRINCIPAL, Principal
from atomic_agents.exceptions import UnverifiedPrincipalConversationAccess
from atomic_agents.principal import (
    LocalPrincipalBackend,
    StaticClaimsPrincipalBackend,
)


# ──────────────────────────────────────────────────────────────────
# Fixtures


@pytest.fixture()
def agent_root(tmp_path):
    """Create a minimal agent directory for testing."""
    root = tmp_path / "agents"
    root.mkdir()
    agent_dir = root / "test_agent"
    agent_dir.mkdir()

    # Minimal required files
    (agent_dir / "persona").mkdir()
    (agent_dir / "persona" / "IDENTITY.md").write_text(
        "You are a test agent.", encoding="utf-8"
    )
    (agent_dir / "tools.md").write_text("# Tools\n\nNo tools.\n", encoding="utf-8")
    (agent_dir / "model.md").write_text(
        "# Model\n\ndefault_model: claude-haiku-4-5\n", encoding="utf-8"
    )
    (agent_dir / "memory").mkdir()
    (agent_dir / "memory" / "INDEX.md").write_text(
        "# Memory Index\n\nNo notes.\n", encoding="utf-8"
    )

    return agent_dir


@pytest.fixture()
def conv_backend(agent_root):
    """FilesystemConversationBackend bound to the test agent."""
    return FilesystemConversationBackend(agent_root)


def _make_verified_principal(provider: str, sub: str) -> Principal:
    """Helper: build a verified Principal with the canonical storage key."""
    identifier = hashlib.sha256(f"{provider}\x00{sub}".encode("utf-8")).hexdigest()
    return Principal(
        identifier=identifier, derivation_source="static_claims", is_verified=True
    )


def _make_unverified_principal() -> Principal:
    """Helper: build an unverified Principal (is_verified=False)."""
    return Principal(
        identifier="anonymous", derivation_source="static_claims", is_verified=False
    )


def _make_fabricated_local_principal(is_verified: bool = False) -> Principal:
    """Helper: build a Principal with LOCAL_PRINCIPAL's identifier but custom is_verified."""
    return Principal(
        identifier="local", derivation_source="local", is_verified=is_verified
    )


# ──────────────────────────────────────────────────────────────────
# HARD-REFUSE gate tests (no actual LLM call needed)


def test_hard_refuse_raises_for_unverified_principal_with_conversation_id(agent_root):
    """HARD-REFUSE: unverified principal + conversation_id -> UnverifiedPrincipalConversationAccess."""
    from atomic_agents.agent import AtomicAgent

    agent = AtomicAgent(
        name="test_agent",
        agents_root=agent_root.parent,
        conversation_backend=FilesystemConversationBackend(agent_root),
    )
    unverified = _make_unverified_principal()

    with pytest.raises(UnverifiedPrincipalConversationAccess) as exc_info:
        agent.call(
            work_item="hello",
            principal=unverified,
            conversation_id="conv-001",
        )

    exc = exc_info.value
    assert exc.conversation_id == "conv-001"
    assert exc.principal_id == "anonymous"


def test_hard_refuse_fires_before_llm_call(agent_root):
    """HARD-REFUSE fires before any LLM call (no billed tokens).

    We verify this by patching _dispatch_with_judge (the method that actually
    calls the LLM) and confirming it is never invoked when the gate fires.
    """
    from atomic_agents.agent import AtomicAgent

    agent = AtomicAgent(
        name="test_agent",
        agents_root=agent_root.parent,
        conversation_backend=FilesystemConversationBackend(agent_root),
    )
    unverified = _make_unverified_principal()

    # _dispatch_with_judge is where the LLM call actually happens. If it is
    # never called, no LLM spend occurred and the gate fired pre-dispatch.
    with patch.object(
        agent, "_dispatch_with_judge", side_effect=AssertionError("LLM was called")
    ) as mock_dispatch:
        with pytest.raises(UnverifiedPrincipalConversationAccess):
            agent.call(
                work_item="hello",
                principal=unverified,
                conversation_id="conv-001",
            )
        mock_dispatch.assert_not_called()


def test_hard_refuse_negative_control_verified_principal_passes(agent_root):
    """Negative control: verified principal + conversation_id does NOT trigger HARD-REFUSE."""
    from atomic_agents.agent import AtomicAgent

    agent = AtomicAgent(
        name="test_agent",
        agents_root=agent_root.parent,
        conversation_backend=FilesystemConversationBackend(agent_root),
    )
    verified = _make_verified_principal("google", "user123")

    # Should NOT raise UnverifiedPrincipalConversationAccess
    # (will raise something else when no real LLM key is available, but NOT the gate error)
    try:
        agent.call(
            work_item="hello",
            principal=verified,
            conversation_id="conv-001",
        )
    except UnverifiedPrincipalConversationAccess:
        pytest.fail("Verified principal should NOT trigger HARD-REFUSE gate")
    except Exception:
        pass  # Expected: no API key in test environment


def test_hard_refuse_negative_control_local_principal_passes(agent_root):
    """Negative control: LOCAL_PRINCIPAL + conversation_id does NOT trigger HARD-REFUSE.

    LOCAL_PRINCIPAL.is_verified=True, so the gate never fires for home-user callers.
    """
    from atomic_agents.agent import AtomicAgent

    agent = AtomicAgent(
        name="test_agent",
        agents_root=agent_root.parent,
        conversation_backend=FilesystemConversationBackend(agent_root),
    )

    try:
        agent.call(
            work_item="hello",
            principal=LOCAL_PRINCIPAL,
            conversation_id="conv-001",
        )
    except UnverifiedPrincipalConversationAccess:
        pytest.fail("LOCAL_PRINCIPAL should NOT trigger HARD-REFUSE gate")
    except Exception:
        pass  # Expected: no API key in test environment


def test_hard_refuse_without_conversation_id_does_not_fire(agent_root):
    """Unverified principal WITHOUT conversation_id does NOT trigger HARD-REFUSE (single-shot allowed)."""
    from atomic_agents.agent import AtomicAgent

    agent = AtomicAgent(
        name="test_agent",
        agents_root=agent_root.parent,
    )
    unverified = _make_unverified_principal()

    # No conversation_id — gate should NOT fire
    try:
        agent.call(
            work_item="hello",
            principal=unverified,
            # no conversation_id
        )
    except UnverifiedPrincipalConversationAccess:
        pytest.fail(
            "Unverified principal WITHOUT conversation_id must NOT trigger HARD-REFUSE"
        )
    except Exception:
        pass  # Expected: no API key


def test_hard_refuse_gates_on_is_verified_not_object_identity(agent_root):
    """Gate keys on is_verified boolean, not object identity with LOCAL_PRINCIPAL.

    A fabricated Principal(identifier='local', is_verified=False) MUST be refused,
    even though its identifier matches LOCAL_PRINCIPAL.identifier.
    """
    from atomic_agents.agent import AtomicAgent

    agent = AtomicAgent(
        name="test_agent",
        agents_root=agent_root.parent,
        conversation_backend=FilesystemConversationBackend(agent_root),
    )
    fabricated = _make_fabricated_local_principal(is_verified=False)
    assert fabricated.identifier == LOCAL_PRINCIPAL.identifier  # same identifier
    assert fabricated is not LOCAL_PRINCIPAL  # different object
    assert not fabricated.is_verified  # but is_verified=False

    with pytest.raises(UnverifiedPrincipalConversationAccess):
        agent.call(
            work_item="hello",
            principal=fabricated,
            conversation_id="conv-001",
        )


def test_hard_refuse_fires_before_idempotency_dedup_lookup(agent_root):
    """spec/48 MUST 10 — the HARD-REFUSE gate fires BEFORE the idempotency dedup
    lookup short-circuit.

    Security regression (Round 1 finding): with the gate placed AFTER the dedup
    Phase-1 lookup() COMPLETED short-circuit, an unverified caller that supplies
    BOTH a conversation_id AND an idempotency_key mapping to a COMPLETED ledger
    record was served the prior run's cached result_ref + replayed_run_id WITHOUT
    the principal check ever running — a cross-principal cached-run replay. Moving
    the gate BEFORE the lookup closes it.

    We mock idempotency_backend.lookup() to return a COMPLETED decision and assert
    the call RAISES UnverifiedPrincipalConversationAccess rather than returning a
    deduped Response. The lookup MUST NOT even be reached (gate is before it).
    """
    from atomic_agents.agent import AtomicAgent
    from atomic_agents.idempotency.types import COMPLETED, DedupDecision

    agent = AtomicAgent(
        name="test_agent",
        agents_root=agent_root.parent,
        conversation_backend=FilesystemConversationBackend(agent_root),
    )
    unverified = _make_unverified_principal()

    completed = DedupDecision(
        is_duplicate=True,
        state=COMPLETED,
        prior_run_id="victim-run",
        prior_result_ref="victim-result-ref",
    )
    with patch.object(
        agent.idempotency_backend, "lookup", return_value=completed
    ) as mock_lookup:
        with pytest.raises(UnverifiedPrincipalConversationAccess) as exc_info:
            agent.call(
                work_item="hello",
                principal=unverified,
                conversation_id="conv-001",
                idempotency_key="replayed-key",
            )
        # The gate is BEFORE the lookup — the dedup lookup must never run for a
        # refused caller. This is the load-bearing ordering assertion.
        mock_lookup.assert_not_called()
    assert exc_info.value.conversation_id == "conv-001"


def test_dedup_negative_control_verified_principal_still_serves_completed(agent_root):
    """Negative control for the gate-before-dedup ordering: a VERIFIED principal
    with a COMPLETED idempotency_key STILL gets the deduped Response.

    Strips the security fix's effect: if the gate wrongly refused verified callers
    too, this would fail. Confirms the gate keys on is_verified, not on the mere
    presence of conversation_id + idempotency_key.
    """
    from atomic_agents.agent import AtomicAgent
    from atomic_agents.idempotency.types import COMPLETED, DedupDecision

    agent = AtomicAgent(
        name="test_agent",
        agents_root=agent_root.parent,
        conversation_backend=FilesystemConversationBackend(agent_root),
    )
    verified = _make_verified_principal("google", "user123")

    completed = DedupDecision(
        is_duplicate=True,
        state=COMPLETED,
        prior_run_id="prior-run",
        prior_result_ref="prior-result-ref",
    )
    with patch.object(agent.idempotency_backend, "lookup", return_value=completed):
        # Must NOT raise the gate error; must return the deduped Response.
        resp = agent.call(
            work_item="hello",
            principal=verified,
            conversation_id="conv-001",
            idempotency_key="prior-key",
        )
    # Deduped Response replays the prior run.
    assert resp.replayed_run_id == "prior-run"


def test_hard_refuse_exception_carries_conversation_id_and_principal_id(agent_root):
    """UnverifiedPrincipalConversationAccess carries conversation_id and principal_id."""
    from atomic_agents.agent import AtomicAgent

    agent = AtomicAgent(
        name="test_agent",
        agents_root=agent_root.parent,
        conversation_backend=FilesystemConversationBackend(agent_root),
    )
    unverified = Principal(
        identifier="my-unverified-id", derivation_source="test", is_verified=False
    )

    with pytest.raises(UnverifiedPrincipalConversationAccess) as exc_info:
        agent.call(
            work_item="hello",
            principal=unverified,
            conversation_id="my-conversation",
        )

    exc = exc_info.value
    assert exc.conversation_id == "my-conversation"
    assert exc.principal_id == "my-unverified-id"


def test_hard_refuse_writes_security_refusal_audit_record(agent_root):
    """HARD-REFUSE persists a security-refusal JSONL audit record (CLAUDE.md rule #5).

    The refusal is the one durable record that a non-local caller attempted
    conversation access. The write is best-effort (try/except: pass) so this
    test reads the audit stream back via the agent's LogBackend and asserts the
    record exists with the expected status + fields. Per-invocation negative
    control below proves the assertion is load-bearing on the real _log write.
    """
    from atomic_agents.agent import AtomicAgent
    from atomic_agents.logs.types import LogQuery

    agent = AtomicAgent(
        name="test_agent",
        agents_root=agent_root.parent,
        conversation_backend=FilesystemConversationBackend(agent_root),
    )
    unverified = Principal(
        identifier="audit-unverified-id",
        derivation_source="static_claims",
        is_verified=False,
    )

    with pytest.raises(UnverifiedPrincipalConversationAccess):
        agent.call(
            work_item="hello",
            principal=unverified,
            conversation_id="audit-conv-001",
            idempotency_key="audit-idem-001",
            caller_identity="audit-http-caller",
        )

    records = agent.log_backend.query(LogQuery(status="principal_not_verified"))
    refusals = [r for r in records if r.conversation_id == "audit-conv-001"]
    assert len(refusals) == 1, (
        f"expected exactly one principal_not_verified record, got {len(refusals)}"
    )
    rec = refusals[0]
    assert rec.status == "principal_not_verified"
    assert rec.run_id  # non-null run_id correlates the refusal
    assert rec.conversation_id == "audit-conv-001"
    assert rec.idempotency_key == "audit-idem-001"
    # principal_id + http_caller are non-canonical → land in extra.
    assert rec.extra.get("principal_id") == "audit-unverified-id"
    assert rec.extra.get("http_caller") == "audit-http-caller"


def test_hard_refuse_audit_record_negative_control(agent_root):
    """Per-invocation negative control: with _log neutered, the audit record is
    absent — proving the persistence assertion above is load-bearing on the real
    self._log(...) call, not a coincidental side effect of some other write site.
    """
    from atomic_agents.agent import AtomicAgent
    from atomic_agents.logs.types import LogQuery

    agent = AtomicAgent(
        name="test_agent",
        agents_root=agent_root.parent,
        conversation_backend=FilesystemConversationBackend(agent_root),
    )
    unverified = Principal(
        identifier="audit-unverified-id",
        derivation_source="static_claims",
        is_verified=False,
    )

    with patch.object(agent, "_log", return_value=None) as mock_log:
        with pytest.raises(UnverifiedPrincipalConversationAccess):
            agent.call(
                work_item="hello",
                principal=unverified,
                conversation_id="audit-conv-neg",
                idempotency_key="audit-idem-neg",
                caller_identity="audit-http-caller",
            )
        # The gate DID attempt the audit write (best-effort), but it was neutered.
        mock_log.assert_called_once()

    records = agent.log_backend.query(LogQuery(status="principal_not_verified"))
    refusals = [r for r in records if r.conversation_id == "audit-conv-neg"]
    assert refusals == [], (
        "with _log neutered, no principal_not_verified record should be persisted"
    )


def test_hard_refuse_no_conv_backend_configured_still_fires(agent_root):
    """HARD-REFUSE fires even when no ConversationBackend is configured.

    The gate is unconditional on conversation_id presence, not gated on
    backend availability. An unverified caller with conversation_id but no
    backend must still be refused (spec/48: enforce at the door).
    """
    from atomic_agents.agent import AtomicAgent

    # No conversation_backend configured
    agent = AtomicAgent(
        name="test_agent",
        agents_root=agent_root.parent,
    )
    unverified = _make_unverified_principal()

    with pytest.raises(UnverifiedPrincipalConversationAccess):
        agent.call(
            work_item="hello",
            principal=unverified,
            conversation_id="conv-001",  # unverified + conversation_id -> refuse
        )


# ──────────────────────────────────────────────────────────────────
# principal_backend wired into AtomicAgent.__init__


def test_agent_has_principal_backend_attribute(agent_root):
    """AtomicAgent has a principal_backend attribute after construction."""
    from atomic_agents.agent import AtomicAgent

    agent = AtomicAgent(name="test_agent", agents_root=agent_root.parent)
    assert hasattr(agent, "principal_backend")


def test_agent_default_principal_backend_is_local(agent_root):
    """AtomicAgent defaults to LocalPrincipalBackend when no kwarg/env var set."""
    from atomic_agents.agent import AtomicAgent

    agent = AtomicAgent(name="test_agent", agents_root=agent_root.parent)
    assert isinstance(agent.principal_backend, LocalPrincipalBackend)


def test_agent_accepts_custom_principal_backend(agent_root):
    """AtomicAgent accepts a custom principal_backend via constructor kwarg."""
    from atomic_agents.agent import AtomicAgent

    custom_backend = StaticClaimsPrincipalBackend()
    agent = AtomicAgent(
        name="test_agent",
        agents_root=agent_root.parent,
        principal_backend=custom_backend,
    )
    assert agent.principal_backend is custom_backend


def test_agent_principal_backend_kwarg_wins_over_env(agent_root, monkeypatch):
    """Constructor kwarg wins over ATOMIC_AGENTS_PRINCIPAL_BACKEND env var."""
    from atomic_agents.agent import AtomicAgent

    monkeypatch.setenv("ATOMIC_AGENTS_PRINCIPAL_BACKEND", "static_claims")
    kwarg_backend = LocalPrincipalBackend()
    agent = AtomicAgent(
        name="test_agent",
        agents_root=agent_root.parent,
        principal_backend=kwarg_backend,
    )
    # kwarg wins: even though env var says static_claims, kwarg LocalPrincipalBackend is used
    assert agent.principal_backend is kwarg_backend


# ──────────────────────────────────────────────────────────────────
# Storage-key determinism and non-reassignability


def test_storage_key_determinism():
    """Same (provider, sub) always produces the same identifier."""
    backend = StaticClaimsPrincipalBackend()
    claims = {"provider": "google", "sub": "user@example.com"}
    r1 = backend.derive_principal(claims)
    r2 = backend.derive_principal(claims)
    assert r1.identifier == r2.identifier


def test_storage_key_non_reassignability():
    """Different (provider, sub) pairs always produce different identifiers."""
    backend = StaticClaimsPrincipalBackend()
    r1 = backend.derive_principal({"provider": "google", "sub": "user1"})
    r2 = backend.derive_principal({"provider": "google", "sub": "user2"})
    assert r1.identifier != r2.identifier


def test_storage_key_normative_example():
    """Normative encoding check: sha256(b'google\\x00user123').hexdigest()."""
    backend = StaticClaimsPrincipalBackend()
    result = backend.derive_principal({"provider": "google", "sub": "user123"})
    expected = hashlib.sha256(b"google\x00user123").hexdigest()
    assert result.identifier == expected


# ──────────────────────────────────────────────────────────────────
# Conversation isolation: different principals see different turns


def test_different_principals_isolated_in_conversation(conv_backend):
    """Two verified principals with the same conversation_id cannot see each other's turns.

    Writes turns for principal A, then loads for principal B — must get [].
    """
    from atomic_agents.conversation.types import Turn
    import datetime

    principal_a = _make_verified_principal("google", "user_a")
    principal_b = _make_verified_principal("google", "user_b")
    conv_id = "shared-conv-id"

    # Write a turn for principal A
    turn = Turn(
        role="user",
        content="Hello from A",
        ts=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        run_id="run-001",
        seq=0,
    )
    conv_backend.write_turn(principal_a, conv_id, turn)

    # Load for principal B — must return [] (not A's turns)
    from atomic_agents.exceptions import ConversationAccessDenied

    try:
        turns_b = conv_backend.load_turns(principal_b, conv_id)
        assert turns_b == [], (
            f"Principal B must NOT see principal A's turns, got {turns_b!r}"
        )
    except ConversationAccessDenied:
        pass  # Also acceptable — access denied is fail-closed


def test_same_principal_can_load_own_turns(conv_backend):
    """A verified principal can load its own turns."""
    from atomic_agents.conversation.types import Turn
    import datetime

    principal_a = _make_verified_principal("google", "user_a")
    conv_id = "my-conv"

    turn = Turn(
        role="user",
        content="Hello from A",
        ts=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        run_id="run-002",
        seq=0,
    )
    conv_backend.write_turn(principal_a, conv_id, turn)

    # Load for same principal — must return the turn
    turns = conv_backend.load_turns(principal_a, conv_id)
    assert len(turns) == 1
    assert turns[0].content == "Hello from A"


# ──────────────────────────────────────────────────────────────────
# Exception hierarchy correctness


def test_principal_backend_error_is_distinct_from_unverified():
    """PrincipalBackendError and UnverifiedPrincipalConversationAccess are distinct types."""
    from atomic_agents.exceptions import PrincipalBackendError

    assert not issubclass(PrincipalBackendError, UnverifiedPrincipalConversationAccess)
    assert not issubclass(UnverifiedPrincipalConversationAccess, PrincipalBackendError)


def test_principal_backend_error_is_atomic_agents_error():
    """PrincipalBackendError is an AtomicAgentsError subclass."""
    from atomic_agents.exceptions import AtomicAgentsError, PrincipalBackendError

    assert issubclass(PrincipalBackendError, AtomicAgentsError)


def test_unverified_principal_conversation_access_is_atomic_agents_error():
    """UnverifiedPrincipalConversationAccess is an AtomicAgentsError subclass."""
    from atomic_agents.exceptions import AtomicAgentsError

    assert issubclass(UnverifiedPrincipalConversationAccess, AtomicAgentsError)


def test_unverified_exception_attributes():
    """UnverifiedPrincipalConversationAccess carries conversation_id and principal_id."""
    exc = UnverifiedPrincipalConversationAccess(
        "test message",
        conversation_id="conv-xyz",
        principal_id="anon-123",
    )
    assert exc.conversation_id == "conv-xyz"
    assert exc.principal_id == "anon-123"
    assert "test message" in str(exc)


def test_principal_backend_not_raised_for_malformed_claims():
    """StaticClaimsPrincipalBackend does NOT raise PrincipalBackendError for malformed claims.

    Per spec/48 MUST 1: absent/malformed claims return is_verified=False, never raise.
    """
    from atomic_agents.exceptions import PrincipalBackendError

    backend = StaticClaimsPrincipalBackend()
    # None of these should raise PrincipalBackendError
    for bad_claims in [{}, {"provider": "google"}, {"provider": None, "sub": None}]:
        try:
            result = backend.derive_principal(bad_claims)
            assert result.is_verified is False
        except PrincipalBackendError:
            pytest.fail(
                f"derive_principal({bad_claims!r}) must NOT raise PrincipalBackendError — "
                "it should return is_verified=False"
            )


def test_serve_local_backend_misconfig_refuses_perimeter_verified_caller(
    agent_root, monkeypatch
):
    """Fix 1 — serve cross-tenant-collapse guard (integration, REAL run_agent_call).

    When perimeter-trust is enabled but the registered PrincipalBackend is
    is_local_only (the default LocalPrincipalBackend — operator turned on
    identity_is_perimeter_verified but forgot ATOMIC_AGENTS_PRINCIPAL_BACKEND),
    serve case (3) MUST refuse to mint a verified principal so the agent.call()
    HARD-REFUSE gate fires — instead of LocalPrincipalBackend collapsing every
    distinct verified caller onto LOCAL_PRINCIPAL and silently mixing their
    conversations. This exercises the REAL _runner case-(3) body
    (test_serve_app mocks run_agent_call, so this is the only coverage of the guard).

    Strip control: revert the `is_local_only` guard in serve/_runner.py case (3)
    and the local backend returns LOCAL_PRINCIPAL(is_verified=True), no refusal
    fires, and this raises nothing (the call proceeds toward the LLM).
    """
    import asyncio

    from atomic_agents.serve._runner import run_agent_call

    # Ensure the default LocalPrincipalBackend resolves (no stray env from a
    # sibling test); LocalPrincipalBackend.capabilities().is_local_only is True.
    monkeypatch.delenv("ATOMIC_AGENTS_PRINCIPAL_BACKEND", raising=False)

    with pytest.raises(UnverifiedPrincipalConversationAccess):
        asyncio.run(
            run_agent_call(
                name="test_agent",
                work_item="hello",
                agents_root=agent_root.parent,
                caller_identity="alice@corp",
                identity_perimeter_verified=True,
                verified_claims={"provider": "google", "sub": "alice"},
                conversation_id="conv-xyz",
            )
        )


def test_serve_perimeter_trusted_missing_header_fails_closed(agent_root, monkeypatch):
    """Codex #2 fix — header-less fail-open. In a perimeter-trusted (non-loopback,
    multi-tenant) deployment, a request that OMITS the identity header
    (caller_identity is None) MUST NOT collapse to the verified LOCAL_PRINCIPAL
    'local' namespace — it must produce an UNVERIFIED principal so a conversation_id
    caller is HARD-REFUSED. (The home shape — perimeter OFF + no header — still
    gets LOCAL_PRINCIPAL; that path is unchanged and covered elsewhere.)

    Strip control: revert `and not identity_perimeter_verified` in serve/_runner.py
    case (1) and the missing-header request gets LOCAL_PRINCIPAL(is_verified=True) —
    no refusal fires and this raises nothing (the fail-open Codex flagged).
    """
    import asyncio

    from atomic_agents.serve._runner import run_agent_call

    monkeypatch.delenv("ATOMIC_AGENTS_PRINCIPAL_BACKEND", raising=False)

    with pytest.raises(UnverifiedPrincipalConversationAccess):
        asyncio.run(
            run_agent_call(
                name="test_agent",
                work_item="hello",
                agents_root=agent_root.parent,
                caller_identity=None,  # identity header OMITTED
                identity_perimeter_verified=True,  # but perimeter-trust IS on
                verified_claims=None,
                conversation_id="conv-noheader",
            )
        )
