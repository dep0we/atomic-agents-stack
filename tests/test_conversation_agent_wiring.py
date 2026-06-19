"""agent.call(conversation_id=...) wiring integration tests (spec/47 PR1).

These exercise the load-bearing agent-side wiring that the conformance suite
cannot reach: turn injection into messages[], post-call write-back, the
user+assistant same-call seq disambiguation, continuity_persisted on failure,
conversation_id tagging on terminal JSONL records, the three-channel backend
resolution seam, and the messages[] normalization that keeps the provider API
from rejecting same-role / empty-content sequences.

Harness mirrors tests/test_idempotency_pr2_wiring.py's in-memory fakes so the
tests do not depend on real lock/log filesystem timing (the documented macOS
APFS WAL flake). The LLM is stubbed and captures the messages[] it received.

Per project lessons:
- feedback_false_green_test_needs_per_invocation_negative_control: each
  independent tag / behavior gets a strip-and-RED control.
- feedback_layered_except_typed_branch_false_green: continuity_persisted=False
  is asserted to flip when the failure is removed.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from atomic_agents.conversation import (
    ConversationBackendError,
    FilesystemConversationBackend,
    LOCAL_PRINCIPAL,
    Turn,
)


# ──────────────────────────────────────────────────────────────────
# In-memory fakes (mirrors test_idempotency_pr2_wiring._FakeLockBackend/_FakeLogBackend)


class _FakeLockBackend:
    backend_id = "fake-inmemory"

    def __init__(self) -> None:
        self._held = False

    def acquire(self, name: str = "", timeout: float = 0.0):
        import time as _time

        from atomic_agents.exceptions import LockBusy
        from atomic_agents.locks.types import LockHandle

        if self._held:
            raise LockBusy(f"lock {name!r} already held (fake)")
        self._held = True
        handle = LockHandle(
            name=name, acquired_at=_time.time(), holder_pid=0, backend_state=object()
        )
        object.__setattr__(handle, "_backend", self)
        return handle

    def release(self, handle) -> None:
        self._held = False
        try:
            object.__setattr__(handle, "backend_state", None)
        except Exception:
            pass

    def renew(self, handle) -> bool:
        return True

    def is_held(self, name: str = "") -> bool:
        return self._held

    def capabilities(self):
        from atomic_agents.locks.types import LockCapabilities

        return LockCapabilities()

    def scope(self, sub_path: str):
        return self


class _FakeLogBackend:
    backend_id = "fake-inmemory"

    def __init__(self) -> None:
        self.records: list[dict] = []

    def append(self, record: dict) -> None:
        self.records.append(dict(record))

    def query(self, q):  # pragma: no cover
        return list(self.records)

    def tail(self, n: int = 50):  # pragma: no cover
        return list(self.records)[-n:]

    def aggregate(self, *a, **k):  # pragma: no cover
        return {}


def _build_agent_root_full(agents_root: Path, name: str = "convbot") -> Path:
    agent_dir = agents_root / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "persona").mkdir(exist_ok=True)
    (agent_dir / "persona" / "IDENTITY.md").write_text("# Identity\n", encoding="utf-8")
    (agent_dir / "tools.md").write_text(
        "## Write paths\n- memory/\n\n## Read-only paths\n(none)\n", encoding="utf-8"
    )
    (agent_dir / "memory").mkdir(exist_ok=True)
    (agent_dir / "model.md").write_text(
        "## Default model\nclaude-haiku-4-5-20251001\n", encoding="utf-8"
    )
    return agents_root


def _fake_llm_response(text: str = "assistant-reply"):
    resp = MagicMock()
    resp.text = text
    resp.tool_uses = []
    resp.input_tokens = 7
    resp.output_tokens = 3
    resp.cache_hit_tokens = 0
    resp.cache_miss_tokens = 0
    resp.raw = {}
    return resp


def _make_agent(
    tmp_path: Path,
    *,
    conversation_backend: Any = None,
    name: str = "convbot",
):
    agents_root = _build_agent_root_full(tmp_path, name)
    from atomic_agents.agent import AtomicAgent

    return AtomicAgent(
        name=name,
        trigger="manual",
        agents_root=agents_root,
        conversation_backend=conversation_backend,
        lock_backend=_FakeLockBackend(),
        log_backend=_FakeLogBackend(),
    )


def _run_call(
    agent,
    *,
    work_item: str = "ping",
    conversation_id=None,
    llm_mock=None,
    log_sink=None,
    cost_allow=True,
):
    """Run agent.call() with LLM + heavy loading patched. Returns Response.

    llm_mock (if given) is installed as atomic_agents._llm.call_llm so the test
    can capture the messages[] it received.
    """

    def fake_log(record: dict) -> None:
        if log_sink is not None:
            log_sink.append(dict(record))

    kwargs: dict[str, Any] = {"work_item": work_item}
    if conversation_id is not None:
        kwargs["conversation_id"] = conversation_id

    _llm_patch = (
        patch("atomic_agents._llm.call_llm", llm_mock)
        if llm_mock is not None
        else patch("atomic_agents._llm.call_llm", return_value=_fake_llm_response())
    )

    with (
        _llm_patch,
        patch.object(agent, "_log", side_effect=fake_log),
        patch.object(agent, "load"),
        patch.object(agent, "assemble_system_prompt", return_value="You are ConvBot."),
        patch.object(
            agent,
            "_check_cost_guardrails",
            return_value=MagicMock(
                allow=cost_allow,
                action="ok",
                reason="cap",
                cost_data_degraded=False,
            ),
        ),
    ):
        return agent.call(**kwargs)


# ──────────────────────────────────────────────────────────────────
# MUST 6 — prior turns injected into messages[], NOT the system prompt


def test_prior_turns_injected_into_messages_role_tagged(tmp_path):
    """Two sequential call()s on the same conversation_id: turn 2's messages[]
    contains turn-1's user + assistant entries (role-tagged, oldest first)
    BEFORE the new work_item."""
    backend = FilesystemConversationBackend(tmp_path / "convbot")
    agent = _make_agent(tmp_path, conversation_backend=backend)

    # Turn 1
    _run_call(agent, work_item="first question", conversation_id="c1")

    # Turn 2 — capture messages[] the LLM receives
    spy = MagicMock(return_value=_fake_llm_response("second-reply"))
    agent2 = _make_agent(tmp_path, conversation_backend=backend)
    _run_call(agent2, work_item="second question", conversation_id="c1", llm_mock=spy)

    assert spy.call_count == 1
    messages = spy.call_args.kwargs["messages"]
    # Expect: [user(first question), assistant(reply), user(second question)]
    roles = [m["role"] for m in messages]
    assert roles == ["user", "assistant", "user"]
    assert messages[0]["content"] == "first question"
    assert messages[-1]["content"] == "second question"


def test_both_same_call_turns_survive_and_load_in_order(tmp_path):
    """Regression for the seq-collision P0: BOTH the user and assistant turn of
    turn 1 survive (the assistant must NOT overwrite the user turn file)."""
    backend = FilesystemConversationBackend(tmp_path / "convbot")
    agent = _make_agent(tmp_path, conversation_backend=backend)
    _run_call(agent, work_item="Q1", conversation_id="c1")

    turns = backend.load_turns(LOCAL_PRINCIPAL, "c1", budget_tokens=8000)
    assert [t.role for t in turns] == ["user", "assistant"]
    assert turns[0].content == "Q1"
    assert turns[0].seq == 0
    assert turns[1].seq == 1


def test_prior_turns_not_in_system_prompt(tmp_path):
    """Prior turns go to messages[], never to assemble_system_prompt()."""
    backend = FilesystemConversationBackend(tmp_path / "convbot")
    agent = _make_agent(tmp_path, conversation_backend=backend)
    _run_call(agent, work_item="first", conversation_id="c1")

    spy = MagicMock(return_value=_fake_llm_response())
    agent2 = _make_agent(tmp_path, conversation_backend=backend)
    _run_call(agent2, work_item="second", conversation_id="c1", llm_mock=spy)
    system_prompt = spy.call_args.kwargs["system_prompt"]
    assert "first" not in system_prompt  # prior turn content NOT in system prompt


# ──────────────────────────────────────────────────────────────────
# messages[] normalization (P1): no same-role collision / empty content


def test_messages_normalization_drops_orphan_trailing_user(tmp_path):
    """An orphaned trailing user turn (failed assistant write-back) is dropped so
    messages[] does not present [user, user(work_item)] to the provider."""
    backend = FilesystemConversationBackend(tmp_path / "convbot")
    # Plant ONLY a user turn for c1 (simulates a crash between user+assistant write).
    backend.write_turn(
        LOCAL_PRINCIPAL,
        "c1",
        Turn(
            role="user",
            content="orphan",
            ts="2026-01-01T00:00:00+00:00",
            run_id="r0",
            seq=0,
        ),
    )
    agent = _make_agent(tmp_path, conversation_backend=backend)
    spy = MagicMock(return_value=_fake_llm_response())
    _run_call(agent, work_item="new question", conversation_id="c1", llm_mock=spy)

    messages = spy.call_args.kwargs["messages"]
    roles = [m["role"] for m in messages]
    # The orphan trailing user is dropped; only the new work_item user remains.
    assert roles == ["user"]
    assert messages[0]["content"] == "new question"


def test_messages_normalization_drops_empty_assistant_content(tmp_path):
    """An assistant turn with empty content (tool-only response) is not injected
    as an empty content block."""
    backend = FilesystemConversationBackend(tmp_path / "convbot")
    backend.write_turn(
        LOCAL_PRINCIPAL,
        "c1",
        Turn(
            role="user", content="q", ts="2026-01-01T00:00:00+00:00", run_id="r0", seq=0
        ),
    )
    backend.write_turn(
        LOCAL_PRINCIPAL,
        "c1",
        Turn(
            role="assistant",
            content="",
            ts="2026-01-01T00:00:00+00:00",
            run_id="r0",
            seq=1,
        ),
    )
    agent = _make_agent(tmp_path, conversation_backend=backend)
    spy = MagicMock(return_value=_fake_llm_response())
    _run_call(agent, work_item="next", conversation_id="c1", llm_mock=spy)

    messages = spy.call_args.kwargs["messages"]
    # No empty content block anywhere.
    assert all(m["content"] for m in messages)
    roles = [m["role"] for m in messages]
    # Empty assistant dropped; the now-trailing user 'q' then dropped (it would
    # collide with the work_item user turn) -> only the new work_item remains.
    assert roles == ["user"]
    assert messages[-1]["content"] == "next"


def test_messages_normalization_drops_leading_assistant(tmp_path):
    """SYMMETRIC counterpart to the trailing-user drop: when budget eviction cuts
    mid-pair (newest-first) the oldest KEPT turn can be an assistant turn, so
    messages[] would start with role 'assistant' — which the Anthropic API rejects
    ('first message must use the user role'). The leading assistant turn(s) MUST be
    dropped so messages[0] is role 'user'.

    Topology: a [user, asst, user, asst] history with a budget tight enough to keep
    exactly the newest 3 turns -> kept=[asst, user, asst]; after normalization the
    array would be [assistant, user, assistant, user(work_item)] WITHOUT the fix.
    Negative control: removing the leading-assistant drop makes messages[0] role
    'assistant' and this assertion goes RED.
    """
    backend = FilesystemConversationBackend(tmp_path / "convbot")
    # Four turns across two prior calls; long content so the token budget bites.
    big = "x" * 4000  # ~1000 tokens each at chars/4
    pairs = [
        ("user", "u1", "r1", 0),
        ("assistant", big, "r1", 1),
        ("user", "u2", "r2", 0),
        ("assistant", big, "r2", 1),
    ]
    for i, (role, content, rid, seq) in enumerate(pairs):
        backend.write_turn(
            LOCAL_PRINCIPAL,
            "c1",
            Turn(
                role=role,
                content=content,
                ts=f"2026-01-01T00:00:0{i}+00:00",
                run_id=rid,
                seq=seq,
            ),
        )

    # Sanity: with a budget that keeps ~3 turns, the oldest kept is the assistant.
    kept = backend.load_turns(LOCAL_PRINCIPAL, "c1", budget_tokens=2008)
    assert kept and kept[0].role == "assistant", (
        "test topology precondition: budget must evict the leading user so the "
        "oldest kept turn is an assistant (otherwise this test proves nothing)"
    )

    agent = _make_agent(tmp_path, conversation_backend=backend)
    spy = MagicMock(return_value=_fake_llm_response())
    # Force the in-call load to use the tight budget regardless of call()'s
    # default 8000, so eviction cuts mid-pair and the oldest kept turn is the
    # assistant — the exact condition the leading-assistant drop must handle.
    with patch.object(
        backend,
        "load_turns",
        side_effect=lambda p, c, budget_tokens=8000: (
            FilesystemConversationBackend.load_turns(backend, p, c, budget_tokens=2008)
        ),
    ):
        _run_call(agent, work_item="new", conversation_id="c1", llm_mock=spy)

    messages = spy.call_args.kwargs["messages"]
    assert messages[0]["role"] == "user", (
        "messages[] MUST NOT start with an assistant turn (provider 400); the "
        "leading-assistant drop is missing"
    )
    assert messages[-1]["content"] == "new"


# ──────────────────────────────────────────────────────────────────
# MUST 7 — write-back failure => continuity_persisted=False, still billed


def test_writeback_failure_sets_continuity_false_still_returns(tmp_path):
    """write_turn raising ConversationBackendError -> continuity_persisted=False,
    Response still returned (billed)."""

    class _FailingBackend:
        backend_id = "failing"

        def load_turns(self, *a, **k):
            return []

        def write_turn(self, *a, **k):
            raise ConversationBackendError("disk full (fake)")

    agent = _make_agent(tmp_path, conversation_backend=_FailingBackend())
    resp = _run_call(agent, work_item="q", conversation_id="c1")
    assert resp.continuity_persisted is False
    assert resp.text == "assistant-reply"  # still billed/returned


def test_writeback_success_keeps_continuity_true_negative_control(tmp_path):
    """Per-invocation negative control: with a WORKING backend the same path keeps
    continuity_persisted=True. If this passed regardless of write success the
    failure test above would be false-green."""
    backend = FilesystemConversationBackend(tmp_path / "convbot")
    agent = _make_agent(tmp_path, conversation_backend=backend)
    resp = _run_call(agent, work_item="q", conversation_id="c1")
    assert resp.continuity_persisted is True


def test_bad_conversation_id_degrades_not_crash(tmp_path):
    """A malformed conversation_id raises PathTraversalError inside load/write,
    which is a sibling of ConversationBackendError. The call must NOT crash — it
    degrades to single-shot (load) and continuity_persisted=False (write)."""
    backend = FilesystemConversationBackend(tmp_path / "convbot")
    agent = _make_agent(tmp_path, conversation_backend=backend)
    # '../evil' is rejected by _validate_conversation_component -> PathTraversalError
    resp = _run_call(agent, work_item="q", conversation_id="../evil")
    assert resp.text == "assistant-reply"  # no uncaught traceback
    assert resp.continuity_persisted is False  # write-back failed-soft


# ──────────────────────────────────────────────────────────────────
# conversation_id JSONL tagging on terminal records + negative control


def test_ok_path_record_carries_conversation_id(tmp_path):
    backend = FilesystemConversationBackend(tmp_path / "convbot")
    agent = _make_agent(tmp_path, conversation_backend=backend)
    sink: list[dict] = []
    _run_call(agent, work_item="q", conversation_id="c-ok", log_sink=sink)
    terminal = [r for r in sink if r.get("status") not in ("error",)]
    assert terminal, "expected at least one terminal record"
    assert any(r.get("conversation_id") == "c-ok" for r in sink)


def test_ok_path_record_no_conversation_id_negative_control(tmp_path):
    """Negative control: a call WITHOUT conversation_id produces records with NO
    conversation_id field (proves the tag is conditional, not always-present)."""
    backend = FilesystemConversationBackend(tmp_path / "convbot")
    agent = _make_agent(tmp_path, conversation_backend=backend)
    sink: list[dict] = []
    _run_call(agent, work_item="q", log_sink=sink)
    assert all("conversation_id" not in r for r in sink)


def test_mid_loop_cost_skip_record_carries_conversation_id(tmp_path, monkeypatch):
    """The mid-loop cost-cap skip terminal record carries conversation_id (this
    record was previously untagged — see the P0 finding) and the skipped Response
    sets continuity_persisted=False (no write-back ran)."""
    from atomic_agents.tools import ToolDefinition

    # A leaked env var could perturb backend resolution even though we pass an
    # explicit kwarg backend below; clear it so this test is order-independent.
    monkeypatch.delenv("ATOMIC_AGENTS_CONVERSATION_BACKEND", raising=False)
    backend = FilesystemConversationBackend(tmp_path / "convbot")
    agent = _make_agent(tmp_path, conversation_backend=backend)
    # Register a benign read-only tool the loop can dispatch on iteration 1 so a
    # second iteration runs and hits the (now-denying) mid-loop cost gate.
    agent.tool_registry.register(
        ToolDefinition(
            name="noop_tool",
            description="returns a constant",
            input_schema={"type": "object"},
            handler=lambda i: "ok",
            classification="read_only",
        )
    )
    sink: list[dict] = []

    # Deny on SHAPE, not call position: the mid-loop gate (agent.py:4558) passes
    # extra_in_flight_cost_usd; the pre-loop gate (agent.py:4070) does not. Keying
    # on that kwarg survives any future extra pre-loop guardrail call (the
    # feedback_mock_side_effect_shape_keyed_not_positional lesson — a positional
    # counter silently shifts when a new pre-loop call is added). call_n is kept
    # only as a structural sanity check (both gate sites must fire).
    call_n = {"i": 0}

    def _gate(*a, **k):
        call_n["i"] += 1
        # mid-loop call carries extra_in_flight_cost_usd → deny; pre-loop → allow.
        allow = "extra_in_flight_cost_usd" not in k
        return MagicMock(
            allow=allow, action="ok", reason="cap hit", cost_data_degraded=False
        )

    # Iteration 1: emit a tool_use (dict shape — the loop reads tu["name"]/["id"])
    # so the loop continues to iteration 2.
    first = _fake_llm_response("")
    first.tool_uses = [{"name": "noop_tool", "id": "tool-1", "input": {}}]

    def _llm(*a, **k):
        return first

    with (
        patch("atomic_agents._llm.call_llm", side_effect=_llm),
        patch.object(agent, "_log", side_effect=lambda r: sink.append(dict(r))),
        patch.object(agent, "load"),
        patch.object(agent, "assemble_system_prompt", return_value="sp"),
        patch.object(agent, "_check_cost_guardrails", side_effect=_gate),
    ):
        resp = agent.call(work_item="q", conversation_id="c-skip")

    # Structural sanity: both gate sites must have fired (pre-loop + mid-loop).
    # If a future change skips the mid-loop call this fails loudly instead of the
    # shape-keyed gate silently never denying.
    assert call_n["i"] >= 2, "expected both pre-loop and mid-loop cost gate calls"
    skip_records = [r for r in sink if r.get("status") == "skipped"]
    assert skip_records, "expected a mid-loop skip record"
    assert all(r.get("conversation_id") == "c-skip" for r in skip_records)
    assert resp.skipped is True
    assert resp.continuity_persisted is False


# ──────────────────────────────────────────────────────────────────
# MUST 9 — None default: no backend => no conversations/ dir, single-shot


def test_no_backend_no_conversations_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("ATOMIC_AGENTS_CONVERSATION_BACKEND", raising=False)
    agent = _make_agent(tmp_path, conversation_backend=None)
    spy = MagicMock(return_value=_fake_llm_response())
    resp = _run_call(agent, work_item="q", conversation_id="c1", llm_mock=spy)
    # No backend resolved -> single-shot: only the work_item user message.
    messages = spy.call_args.kwargs["messages"]
    assert [m["role"] for m in messages] == ["user"]
    assert not (tmp_path / "convbot" / "conversations").exists()
    # continuity_persisted MUST be False: a conversation_id was REQUESTED but no
    # backend exists to persist into, so True would falsely tell the caller the
    # turn was stored. (Negative control for the ok-path continuity default.)
    assert resp.continuity_persisted is False


def test_misconfigured_env_var_with_conversation_id_none_succeeds(
    tmp_path, monkeypatch
):
    """MUST 9 backward-compat: a bad ATOMIC_AGENTS_CONVERSATION_BACKEND must NOT
    crash a single-shot call (conversation_id=None). Resolution is gated on
    conversation_id AND fails soft internally — either way the call must succeed.

    Negative control: before the fix, _resolve_conversation_backend() ran
    unconditionally and raised BackendNotRegistered on the unknown id, crashing
    EVERY call (even ones that never asked for a conversation)."""
    monkeypatch.setenv("ATOMIC_AGENTS_CONVERSATION_BACKEND", "postgres")  # unregistered
    agent = _make_agent(tmp_path, conversation_backend=None)
    spy = MagicMock(return_value=_fake_llm_response())
    resp = _run_call(agent, work_item="q", conversation_id=None, llm_mock=spy)
    assert resp.text == "assistant-reply"  # no uncaught BackendNotRegistered
    messages = spy.call_args.kwargs["messages"]
    assert [m["role"] for m in messages] == ["user"]


def test_misconfigured_env_var_with_conversation_id_degrades_single_shot(
    tmp_path, monkeypatch
):
    """Defense-in-depth: even a CONVERSATION call with a bad env var degrades to
    single-shot (load fails soft to None) rather than crashing the billed run."""
    monkeypatch.setenv("ATOMIC_AGENTS_CONVERSATION_BACKEND", "nonsense-backend")
    agent = _make_agent(tmp_path, conversation_backend=None)
    spy = MagicMock(return_value=_fake_llm_response())
    resp = _run_call(agent, work_item="q", conversation_id="c1", llm_mock=spy)
    assert resp.text == "assistant-reply"
    # No backend -> single-shot messages; continuity not persisted.
    messages = spy.call_args.kwargs["messages"]
    assert [m["role"] for m in messages] == ["user"]
    assert resp.continuity_persisted is False


# ──────────────────────────────────────────────────────────────────
# Three-channel resolution seam


def test_resolution_kwarg_wins(tmp_path, monkeypatch):
    monkeypatch.delenv("ATOMIC_AGENTS_CONVERSATION_BACKEND", raising=False)
    backend = FilesystemConversationBackend(tmp_path / "convbot")
    agent = _make_agent(tmp_path, conversation_backend=backend)
    assert agent._resolve_conversation_backend() is backend


def test_resolution_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("ATOMIC_AGENTS_CONVERSATION_BACKEND", "filesystem")
    agent = _make_agent(tmp_path, conversation_backend=None)
    resolved = agent._resolve_conversation_backend()
    assert resolved is not None
    assert resolved.backend_id == "filesystem"


def test_resolution_all_absent_is_none(tmp_path, monkeypatch):
    monkeypatch.delenv("ATOMIC_AGENTS_CONVERSATION_BACKEND", raising=False)
    agent = _make_agent(tmp_path, conversation_backend=None)
    assert agent._resolve_conversation_backend() is None


def test_resolution_model_md_field(tmp_path, monkeypatch):
    """Channel (3): model.md '## Conversation Backend' field resolves end-to-end.

    Neither a constructor kwarg (channel 1) nor the env var (channel 2) is set;
    the backend comes solely from AgentConfig.conversation_backend_id parsed from
    model.md. Channel 3 instantiates DIFFERENTLY from channel 2 — it calls
    get_conversation_backend(id) to get the CLASS then cls(agent_root); channel 2
    calls get_default_conversation_backend(agent_root) for an INSTANCE. This pins
    the class-then-construct path that no other wiring test exercises.

    Negative control: without the '## Conversation Backend' section the parser
    leaves conversation_backend_id=None and this returns None (asserted by
    test_resolution_all_absent_is_none above and the parser unit test below).
    """
    monkeypatch.delenv("ATOMIC_AGENTS_CONVERSATION_BACKEND", raising=False)
    agents_root = _build_agent_root_full(tmp_path, "convbot")
    # Append the PROVISIONAL section to model.md (channel 3 source).
    model_md = agents_root / "convbot" / "model.md"
    model_md.write_text(
        model_md.read_text(encoding="utf-8")
        + "\n## Conversation Backend\n\nfilesystem\n",
        encoding="utf-8",
    )
    from atomic_agents.agent import AtomicAgent

    agent = AtomicAgent(
        name="convbot",
        trigger="manual",
        agents_root=agents_root,
        conversation_backend=None,  # channel 1 absent
        lock_backend=_FakeLockBackend(),
        log_backend=_FakeLogBackend(),
    )
    # Sanity: the parser populated the config field from model.md.
    assert agent.config.conversation_backend_id == "filesystem"

    resolved = agent._resolve_conversation_backend()
    assert resolved is not None, "channel 3 must resolve a backend from model.md"
    assert resolved.backend_id == "filesystem"
    assert isinstance(resolved, FilesystemConversationBackend)
    # Channel 3 constructs the backend bound to THIS agent's root.
    assert resolved._agent_root == agent.agent_root


def test_parse_model_md_conversation_backend_field(tmp_path):
    """Unit pin for the PROVISIONAL '## Conversation Backend' parser.

    Present -> backend_id lowercased; absent -> None; an h3 (not h2) heading or a
    longer 'Conversation Backend Strategy' heading MUST NOT match (exact-h2
    discipline, mirroring the '## Dedup Body Hash' parser). This locks the
    provisional behavior so the LOCK PR inherits documented semantics.
    """
    from atomic_agents._model import parse_model_md_text

    present = parse_model_md_text(
        "## Default model\nclaude-haiku-4-5\n\n## Conversation Backend\n\nFileSystem\n"
    )
    assert present["conversation_backend_id"] == "filesystem"  # lowercased

    absent = parse_model_md_text("## Default model\nclaude-haiku-4-5\n")
    assert absent["conversation_backend_id"] is None

    # h3 heading must NOT match the h2-anchored parser.
    h3 = parse_model_md_text(
        "## Default model\nx\n\n### Conversation Backend\n\nfilesystem\n"
    )
    assert h3["conversation_backend_id"] is None

    # A longer h2 heading must NOT spuriously match (exact-equality discipline).
    longer = parse_model_md_text(
        "## Default model\nx\n\n## Conversation Backend Strategy\n\nfilesystem\n"
    )
    assert longer["conversation_backend_id"] is None


# ──────────────────────────────────────────────────────────────────
# Idempotency interaction (spec/47 + spec/45): conversation calls must not
# auto-dedup on the body hash (which omits prior turns).


def test_conversation_call_skips_auto_body_hash_dedup(tmp_path):
    """spec/47: with dedup_body_hash_enabled + an external-delivery trigger
    (http/queue/cron), a conversation call (conversation_id set) MUST NOT
    auto-derive the implicit body-hash idempotency key.

    The implicit hash covers only work_item + model + max_tokens + temperature
    and OMITS the prior turns loaded AFTER the dedup gate, so two different
    conversations with the same work_item text — or one conversation after its
    history grows — would hash-collide and replay a stale / cross-conversation
    result. The guard skips auto-derivation when conversation_id is set, so the
    idempotency backend is never consulted. Found by cross-family review (Codex).

    Negative control: strip the `and conversation_id is None` guard and the
    implicit key IS derived → idempotency_backend.lookup fires and this asserts RED.
    """
    backend = FilesystemConversationBackend(tmp_path / "convbot")
    agent = _make_agent(tmp_path, conversation_backend=backend)
    # Force the auto-derive preconditions that, pre-fix, would derive a key.
    agent.trigger = "http"
    agent.config.dedup_body_hash_enabled = True
    spy_idem = MagicMock()
    agent.idempotency_backend = spy_idem

    _run_call(agent, work_item="same text", conversation_id="c1")

    spy_idem.lookup.assert_not_called()
    spy_idem.begin.assert_not_called()
