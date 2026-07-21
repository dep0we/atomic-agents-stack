"""``manage set-model <agent>`` verb tests (spec/55 #726 — the second verb).

Covers the verb-specific surface: the M9 composition chain (per-gate strip-RED
negative controls), the WARN-AND-WRITE policy-override interaction, the
surgical model.md writer's preservation invariants (byte-for-byte outside the
targeted value span, markup-style preservation, CRLF fidelity), the two
grammar-pinned deferred flags (--fallback/--provider), the absence/duplicate/
unparseable-heading refusals, restore's does-NOT-re-run-M9 behavior, and the
set-model-specific audit shape (distinct primitive, cost_usd=None, no
`created` key).

``tests/test_manage_spine.py`` covers the verb-agnostic SPINE guarantees
(lock/agent_busy, five-step ordering, snapshot orphan-on-failure, exit-code
ladder) — set-model's contribution there is a handful of joins proving the
hoisted spine actually serves this second verb, not a re-test of govern's
already-covered spine behavior.

Two refusal paths (model_md_absent, --show on an absent model.md) are
structurally UNREACHABLE through the full CLI/registry surface: the
AgentRegistryBackend discovery predicate (spec/37:314) requires model.md to
be PRESENT to resolve an agent at all (``get_agent()`` returns ``None`` when
model.md is absent — see ``atomic_agents/agent_registry/filesystem.py``), so
an end-to-end invocation can never reach set-model's own absence check with a
resolved ``ref``. Those two are tested via DIRECT calls to the internal
functions that contain the refusal, documented at each call site. Every other
test in this file drives the real verb entry point (``run_set_model``) or the
real CLI (``atomic_agents.cli.main``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from atomic_agents import cli as cli_mod
from atomic_agents.logs.types import (
    PRIMITIVE_MANAGE_RESTORE,
    PRIMITIVE_MANAGE_SET_MODEL,
)
from atomic_agents.manage.set_model import (
    _run_set_model,
    _show_model,
    run_set_model,
    write_default_model,
)

from tests._manage_test_helpers import (
    CANONICAL_MODEL_MD,
    collect_jsonl,
    get_fleet_log_dir,
    make_agent_dir,
    make_model_md,
    make_set_model_args,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _cost_guardrails_block(text: str) -> str:
    start = text.index("```yaml")
    end = text.index("```", start + 7) + 3
    return text[start:end]


def _prose_outside_value(text: str) -> str:
    """Everything except the '## Default model' value token itself.

    Used to assert byte-for-byte preservation of everything an applied
    --model write must NOT touch: the cost_guardrails block, every prose
    paragraph/comment/table, and (when the current value differs from the
    new one) everything except that one token.
    """
    import re

    return re.sub(
        r"(##\s+Default model[^\n]*\n+\s*\*{0,2}`?)([a-zA-Z0-9._/-]+)(`?\*{0,2})",
        r"\1<VALUE>\3",
        text,
        count=1,
    )


def _write_policy_md(agents_root: Path, content: str) -> None:
    (agents_root / "policy.md").write_text(content, encoding="utf-8")


# ── Group A: M9 composition gates — per-gate strip-RED negative controls ───


def test_unpriced_model_refuses_and_leaves_model_md_unchanged(tmp_path):
    agent_dir = make_agent_dir(tmp_path, "myagent")
    model_path = make_model_md(agent_dir)
    before = model_path.read_text(encoding="utf-8")

    exit_code = run_set_model(
        make_set_model_args("myagent", tmp_path, model="totally-unpriced-model-xyz"),
        tmp_path,
    )

    assert exit_code == 1
    assert model_path.read_text(encoding="utf-8") == before
    # No snapshot taken — the gate refuses BEFORE step 4 (M4).
    assert not (agent_dir / ".config-snapshots" / "set-model").exists()


def test_unpriced_model_refuses_json_error_type(tmp_path, capsys):
    agent_dir = make_agent_dir(tmp_path, "myagent")
    make_model_md(agent_dir)

    exit_code = run_set_model(
        make_set_model_args(
            "myagent", tmp_path, model="totally-unpriced-model-xyz", use_json=True
        ),
        tmp_path,
    )
    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error_type"] == "unpriced_model"
    assert "unpriced" in payload["reason"].lower()


def test_unknown_model_refuses_when_priced_but_unresolvable(tmp_path, capsys):
    """``vertex/gemini-2.5-flash`` is a real PRICING key but the vertex extra
    is not installed in this test environment, so find_backend_for_model()
    raises UnknownModelError — a REAL (not monkeypatched) exercise of gate
    (b) independent of gate (a).
    """
    from atomic_agents.core_api import get_model_rates

    priced_but_unresolvable = "vertex/gemini-2.5-flash"
    # sanity: gate (a) passes (routed through the core<->extension boundary
    # accessor, TENSIONS T17 — never `from atomic_agents import _costs` in
    # an extension-owned test file).
    assert get_model_rates(priced_but_unresolvable) is not None

    agent_dir = make_agent_dir(tmp_path, "myagent")
    model_path = make_model_md(agent_dir)
    before = model_path.read_text(encoding="utf-8")

    exit_code = run_set_model(
        make_set_model_args(
            "myagent", tmp_path, model=priced_but_unresolvable, use_json=True
        ),
        tmp_path,
    )
    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_type"] == "unknown_model"
    assert model_path.read_text(encoding="utf-8") == before


def test_unknown_model_message_distinguishes_zero_registered_backends(
    tmp_path, capsys, monkeypatch
):
    """A deployment with ZERO registered LLM backends gets a distinct hint
    ("no LLM backend is registered") rather than being misread as a typo'd
    model id (spec/55 P1 prep finding).
    """
    from atomic_agents import llm as llm_mod
    from atomic_agents.exceptions import UnknownModelError

    def _raise_unknown(model, *, preferred_provider=None):
        raise UnknownModelError(f"no registered LLM backend supports model {model!r}")

    monkeypatch.setattr(llm_mod, "find_backend_for_model", _raise_unknown)
    monkeypatch.setattr(llm_mod, "iter_registered_backends", lambda: iter(()))

    agent_dir = make_agent_dir(tmp_path, "myagent")
    make_model_md(agent_dir)

    exit_code = run_set_model(
        make_set_model_args(
            "myagent", tmp_path, model="claude-sonnet-4-6", use_json=True
        ),
        tmp_path,
    )
    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_type"] == "unknown_model"
    assert "no llm backend is registered" in payload["reason"].lower()


def test_ambiguous_backend_refuses_and_message_never_mentions_provider(
    tmp_path, capsys, monkeypatch
):
    """Maintainer ruling ``provider-disambiguation-posture``: the refusal
    MUST point at the deferred capability (#755), NEVER at ``--provider``
    (the upstream ``AmbiguousBackendError.__str__`` hardcodes exactly that
    guidance — this test proves set-model does NOT echo it).
    """
    from atomic_agents import llm as llm_mod
    from atomic_agents.exceptions import AmbiguousBackendError

    def _raise_ambiguous(model, *, preferred_provider=None):
        raise AmbiguousBackendError(model, ["anthropic", "openai"])

    monkeypatch.setattr(llm_mod, "find_backend_for_model", _raise_ambiguous)

    agent_dir = make_agent_dir(tmp_path, "myagent")
    model_path = make_model_md(agent_dir)
    before = model_path.read_text(encoding="utf-8")

    exit_code = run_set_model(
        make_set_model_args(
            "myagent", tmp_path, model="claude-sonnet-4-6", use_json=True
        ),
        tmp_path,
    )
    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_type"] == "ambiguous_backend"
    assert "--provider" not in payload["reason"]
    assert "#755" in payload["reason"]
    assert model_path.read_text(encoding="utf-8") == before


def test_policy_backend_unavailable_refuses_write(tmp_path, capsys, monkeypatch):
    """Tier B decision (fail-closed): a broken PolicyBackend construction
    refuses the write rather than silently skip the caps-compose consult /
    policy-override warn (spec/55 P1/P2 prep findings).
    """
    from atomic_agents.policy import backend as policy_backend_mod

    def _raise(scope_root):
        raise RuntimeError("simulated PolicyBackend construction failure")

    monkeypatch.setattr(policy_backend_mod, "get_default_policy_backend", _raise)

    agent_dir = make_agent_dir(tmp_path, "myagent")
    model_path = make_model_md(agent_dir)
    before = model_path.read_text(encoding="utf-8")

    exit_code = run_set_model(
        make_set_model_args(
            "myagent", tmp_path, model="claude-sonnet-4-6", use_json=True
        ),
        tmp_path,
    )
    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_type"] == "policy_backend_unavailable"
    assert model_path.read_text(encoding="utf-8") == before


def test_unwritable_model_id_refuses_before_any_write(tmp_path, capsys, monkeypatch):
    """Fix 2 (defense-in-depth, spec/55 P2 finding): a synthetic model id
    that passes M9 gates (a)/(b) — priced AND resolves to exactly one
    backend — but contains a character outside the surgical writer's value
    charset (``[a-zA-Z0-9._/-]+``) must still be refused BEFORE any write,
    not sliced into model.md where it would silently truncate on the next
    ``parse_model_md`` read. ``_costs.PRICING`` happens to have no such key
    today, so gates (a)/(b) are monkeypatched to simulate a future PRICING
    key this guard is the only thing that would catch.
    """
    from atomic_agents import llm as llm_mod
    from atomic_agents.manage import set_model as set_model_mod

    bad_model_id = "claude-sonnet+turbo"  # '+' is outside the writer charset

    monkeypatch.setattr(
        set_model_mod, "get_model_rates", lambda model_id: {"input": 1.0, "output": 2.0}
    )
    monkeypatch.setattr(
        llm_mod,
        "find_backend_for_model",
        lambda model, *, preferred_provider=None: object(),
    )

    agent_dir = make_agent_dir(tmp_path, "myagent")
    model_path = make_model_md(agent_dir)
    before = model_path.read_text(encoding="utf-8")

    exit_code = run_set_model(
        make_set_model_args("myagent", tmp_path, model=bad_model_id, use_json=True),
        tmp_path,
    )
    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_type"] == "unwritable_model_id"
    assert model_path.read_text(encoding="utf-8") == before
    # No snapshot taken — refused before step 4.
    assert not (agent_dir / ".config-snapshots" / "set-model").exists()


# ── Group B: WARN-AND-WRITE policy-override interaction ────────────────────


def test_policy_override_present_and_matching_writes_with_no_warning(tmp_path, capsys):
    agent_dir = make_agent_dir(tmp_path, "myagent")
    make_model_md(agent_dir)
    _write_policy_md(tmp_path, "model: claude-opus-4-8\n")

    exit_code = run_set_model(
        make_set_model_args(
            "myagent", tmp_path, model="claude-opus-4-8", use_json=True
        ),
        tmp_path,
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["policy_override"] is None


def test_policy_override_differing_writes_with_warning_exit_zero(tmp_path, capsys):
    """The maintainer's ruling: WARN, not refuse. The write applies, exit 0,
    a prominent warning names the overriding value.
    """
    agent_dir = make_agent_dir(tmp_path, "myagent")
    model_path = make_model_md(agent_dir)
    _write_policy_md(tmp_path, "model: claude-opus-4-8\n")

    exit_code = run_set_model(
        make_set_model_args(
            "myagent", tmp_path, model="claude-sonnet-4-6", use_json=True
        ),
        tmp_path,
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["policy_override"] is not None
    assert "claude-opus-4-8" in payload["policy_override"]
    # The write DID apply (WARN-AND-WRITE, not refuse).
    assert "claude-sonnet-4-6" in _extract_value(model_path.read_text(encoding="utf-8"))


def test_policy_override_warning_shown_at_preview_before_confirm(tmp_path, capsys):
    """The WARN must be visible BEFORE the operator confirms (P1 prep
    finding) — not only in the post-write success payload. Exercised via the
    human-readable (non-JSON) preview path since that is what a TTY operator
    sees before answering the confirm prompt.
    """
    agent_dir = make_agent_dir(tmp_path, "myagent")
    make_model_md(agent_dir)
    _write_policy_md(tmp_path, "model: claude-opus-4-8\n")

    run_set_model(
        make_set_model_args(
            "myagent", tmp_path, model="claude-sonnet-4-6", use_json=False, yes=True
        ),
        tmp_path,
    )
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "claude-opus-4-8" in out


def test_policy_override_at_write_recorded_in_audit(tmp_path):
    agent_dir = make_agent_dir(tmp_path, "myagent")
    make_model_md(agent_dir)  # default_model starts as claude-sonnet-4-6
    _write_policy_md(tmp_path, "model: claude-opus-4-8\n")

    exit_code = run_set_model(
        make_set_model_args("myagent", tmp_path, model="claude-haiku-4-5"),
        tmp_path,
    )
    assert exit_code == 0

    records = collect_jsonl(agent_dir / "log")
    applied = [r for r in records if r.get("primitive") == PRIMITIVE_MANAGE_SET_MODEL]
    assert len(applied) == 1
    # RunRecord.to_dict() FLATTENS extra{} into top-level keys (see
    # logs/types.py:274 docstring) — not nested under an "extra" key.
    assert applied[0]["policy_override_at_write"] == "claude-opus-4-8"


def test_post_write_policy_recompute_is_load_bearing(tmp_path, monkeypatch):
    """Fix 4(b) (spec/55 P1 finding): the post-write recompute must be
    LOAD-BEARING, not dead code — deleting the second ``_consult_policy``
    call inside ``_run_set_model`` (and reusing the pre-write value instead)
    must turn this test RED. A call-count-keyed side effect simulates a
    Policy change landing BETWEEN the pre-write consult (S2 step 1, gate
    (c)) and the post-write recompute (after the write lands) — e.g. an
    operator editing ``policy.md`` during an unbounded confirm-wait. The
    persisted audit's ``policy_override_at_write`` MUST reflect the SECOND
    (post-write) call's value, never the first — this is exactly the
    distinction ``test_policy_override_at_write_recorded_in_audit`` above
    does NOT exercise (it never changes Policy state between the two
    consults, so it would stay green even if the second call were deleted).
    """
    from atomic_agents.manage import set_model as set_model_mod

    agent_dir = make_agent_dir(tmp_path, "myagent")
    make_model_md(agent_dir)  # default_model starts as claude-sonnet-4-6

    call_count = {"n": 0}
    # Pre-write consult sees "claude-haiku-4-5"; post-write recompute sees a
    # DIFFERENT value, "claude-sonnet-4-6" — simulating Policy changing
    # underneath the write.
    values = ["claude-haiku-4-5", "claude-sonnet-4-6"]

    def _fake_consult(agents_root, agent_id):
        idx = min(call_count["n"], len(values) - 1)
        call_count["n"] += 1
        return None, values[idx]

    monkeypatch.setattr(set_model_mod, "_consult_policy", _fake_consult)
    exit_code = run_set_model(
        make_set_model_args("myagent", tmp_path, model="claude-opus-4-8"),
        tmp_path,
    )

    assert exit_code == 0
    # Both the pre-write consult AND the post-write recompute must have run.
    assert call_count["n"] == 2

    records = collect_jsonl(agent_dir / "log")
    applied = [r for r in records if r.get("primitive") == PRIMITIVE_MANAGE_SET_MODEL]
    assert len(applied) == 1
    # Reflects the SECOND (post-write) call's value, not the first — proves
    # the recompute (not the earlier pre-write consult) drives the audit.
    assert applied[0]["policy_override_at_write"] == "claude-sonnet-4-6"


def test_post_write_policy_recompute_failure_signals_recheck_ok_false(
    tmp_path, capsys, monkeypatch
):
    """Fix 1 (cross-family review finding): when the POST-write recompute
    fails (fail-OPEN — the write already landed), the applied write must
    still succeed (exit 0), but both the persisted audit record and the
    ``--json`` success payload must carry a machine-readable
    ``policy_recheck_ok: False`` so a copilot can tell an authoritative
    post-write value from a stale-preview fallback. The JSON payload must
    ALSO surface the fallback warning text in ``policy_override`` (not only
    in human-readable stderr/stdout prose) so a copilot reading `--json`
    output alone still sees the degraded-signal warning.

    Strip-RED: if ``policy_recheck_ok`` were hardcoded ``True`` (or the key
    omitted) on either the audit record or the JSON payload, this test goes
    RED on the corresponding assertion.
    """
    from atomic_agents.manage import set_model as set_model_mod
    from atomic_agents.manage.exceptions import ManagePolicyBackendUnavailableError

    agent_dir = make_agent_dir(tmp_path, "myagent")
    make_model_md(agent_dir)  # default_model starts as claude-sonnet-4-6

    call_count = {"n": 0}

    def _fake_consult(agents_root, agent_id):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # Pre-write consult (gate c) succeeds with no conflicting value.
            return None, None
        # Post-write recompute fails — simulates a PolicyBackend outage
        # occurring strictly AFTER the write already landed.
        raise ManagePolicyBackendUnavailableError("simulated post-write outage")

    monkeypatch.setattr(set_model_mod, "_consult_policy", _fake_consult)

    exit_code = run_set_model(
        make_set_model_args(
            "myagent", tmp_path, model="claude-opus-4-8", use_json=True
        ),
        tmp_path,
    )

    assert exit_code == 0
    assert call_count["n"] == 2

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["policy_recheck_ok"] is False
    assert payload["policy_override"] is not None
    assert "could not re-verify" in payload["policy_override"]

    records = collect_jsonl(agent_dir / "log")
    applied = [r for r in records if r.get("primitive") == PRIMITIVE_MANAGE_SET_MODEL]
    assert len(applied) == 1
    assert applied[0]["policy_recheck_ok"] is False
    # Falls back to the pre-write preview value (None here) since the
    # recompute itself raised.
    assert applied[0]["policy_override_at_write"] is None


def _extract_value(text: str) -> str:
    from atomic_agents.manage.set_model import _extract_default_model_value

    return _extract_default_model_value(text) or ""


# ── Group C: deferred flags (--fallback / --provider), PR1 scope ───────────


def test_fallback_flag_refuses_before_registry_resolve(tmp_path, capsys):
    """A NONEXISTENT agent still gets 'not_yet_settable_in_pr1', not
    'agent_not_found' — the refusal fires BEFORE S1 resolve (mirrors govern's
    grammar-pin ordering, spec/55 P2 prep finding).
    """
    exit_code = run_set_model(
        make_set_model_args(
            "does-not-exist", tmp_path, fallback="claude-opus-4-8", use_json=True
        ),
        tmp_path,
    )
    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_type"] == "not_yet_settable_in_pr1"
    assert "#754" in payload["reason"]


def test_provider_flag_refuses_before_registry_resolve(tmp_path, capsys):
    exit_code = run_set_model(
        make_set_model_args(
            "does-not-exist", tmp_path, provider="anthropic", use_json=True
        ),
        tmp_path,
    )
    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_type"] == "not_yet_settable_in_pr1"
    assert "#755" in payload["reason"]


def test_provider_flag_does_not_silently_resolve_a_genuinely_ambiguous_model(
    tmp_path, capsys, monkeypatch
):
    """Negative control (spec/55 P1 prep finding): supplying --provider
    alongside a --model that WOULD be ambiguous must still hit the
    'not_yet_settable_in_pr1' refusal — never a resolved write using the
    operator-supplied provider to disambiguate (that is #755's job).
    """
    from atomic_agents import llm as llm_mod
    from atomic_agents.exceptions import AmbiguousBackendError

    def _raise_ambiguous(model, *, preferred_provider=None):
        # If PR1 code silently threaded --provider through, this would never
        # be called with preferred_provider=None — it would resolve cleanly.
        assert preferred_provider is None
        raise AmbiguousBackendError(model, ["anthropic", "openai"])

    monkeypatch.setattr(llm_mod, "find_backend_for_model", _raise_ambiguous)

    agent_dir = make_agent_dir(tmp_path, "myagent")
    make_model_md(agent_dir)

    exit_code = run_set_model(
        make_set_model_args(
            "myagent",
            tmp_path,
            model="claude-sonnet-4-6",
            provider="anthropic",
            use_json=True,
        ),
        tmp_path,
    )
    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_type"] == "not_yet_settable_in_pr1"


# ── Group D: surgical preservation invariants ───────────────────────────────


def test_applied_write_preserves_cost_guardrails_block_byte_for_byte(tmp_path):
    agent_dir = make_agent_dir(tmp_path, "myagent")
    model_path = make_model_md(agent_dir)
    before_block = _cost_guardrails_block(model_path.read_text(encoding="utf-8"))

    exit_code = run_set_model(
        make_set_model_args("myagent", tmp_path, model="claude-opus-4-8"),
        tmp_path,
    )
    assert exit_code == 0
    after_text = model_path.read_text(encoding="utf-8")
    after_block = _cost_guardrails_block(after_text)
    assert after_block == before_block
    assert "claude-opus-4-8" in after_text


def test_applied_write_preserves_prose_and_provider_line_byte_for_byte(tmp_path):
    content = (
        CANONICAL_MODEL_MD.format(agent_name="myagent") + "\nprovider: anthropic\n"
    )
    agent_dir = make_agent_dir(tmp_path, "myagent")
    model_path = make_model_md(agent_dir, content=content)
    before_masked = _prose_outside_value(model_path.read_text(encoding="utf-8"))

    exit_code = run_set_model(
        make_set_model_args("myagent", tmp_path, model="claude-opus-4-8"),
        tmp_path,
    )
    assert exit_code == 0
    after_masked = _prose_outside_value(model_path.read_text(encoding="utf-8"))
    assert after_masked == before_masked
    assert "provider: anthropic" in model_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "wrapper_open,wrapper_close",
    [
        ("**`", "`**"),
        ("**", "**"),
        ("`", "`"),
        ("", ""),
    ],
)
def test_markup_style_preserved_as_found(tmp_path, wrapper_open, wrapper_close):
    """Maintainer-recommended resolution of open fork 5: PRESERVE the
    operator's existing wrapper markup as found — never normalize.
    """
    content = (
        "# MODEL: myagent\n\n"
        "## Default model\n\n"
        f"{wrapper_open}claude-sonnet-4-6{wrapper_close}\n\n"
        "## Fallback\n\n"
        "**`claude-opus-4-8`**\n"
    )
    agent_dir = make_agent_dir(tmp_path, "myagent")
    model_path = make_model_md(agent_dir, content=content)

    exit_code = run_set_model(
        make_set_model_args("myagent", tmp_path, model="claude-haiku-4-5"),
        tmp_path,
    )
    assert exit_code == 0
    after = model_path.read_text(encoding="utf-8")
    expected_line = f"{wrapper_open}claude-haiku-4-5{wrapper_close}"
    assert expected_line in after
    # The Fallback heading's markup (untouched field) also survives verbatim.
    assert "**`claude-opus-4-8`**" in after


def test_write_default_model_replaces_only_the_value_span_directly():
    """Unit-level guard on the pure writer function itself (belt-and-braces
    alongside the end-to-end markup tests above)."""
    text = "## Default model\n\n**`old-id`**\n"
    result = write_default_model(text, "agent", "new-id")
    assert result == "## Default model\n\n**`new-id`**\n"


def test_crlf_authored_model_md_round_trips_byte_for_byte_outside_value_span(tmp_path):
    """Maintainer ruling ``crlf-byte-fidelity-read``: mirror govern's
    newline='' CRLF-aware read. A CRLF fixture must round-trip byte-for-byte
    outside the touched value span — this is the dedicated CRLF test the P1
    prep finding calls for (the reader regex has never been proven against
    raw \\r bytes before this verb).
    """
    crlf_content = (
        "# MODEL: myagent\r\n\r\n"
        "## Default model\r\n\r\n"
        "**`claude-sonnet-4-6`**\r\n\r\n"
        "## Fallback\r\n\r\n"
        "**`claude-opus-4-8`**\r\n\r\n"
        "```yaml\r\n"
        "cost_guardrails:\r\n"
        "  enabled: true\r\n"
        "```\r\n"
    )
    agent_dir = make_agent_dir(tmp_path, "myagent")
    model_path = agent_dir / "model.md"
    with model_path.open("wb") as f:
        f.write(crlf_content.encode("utf-8"))

    exit_code = run_set_model(
        make_set_model_args("myagent", tmp_path, model="claude-haiku-4-5"),
        tmp_path,
    )
    assert exit_code == 0

    with model_path.open("rb") as f:
        after_bytes = f.read()
    after_text = after_bytes.decode("utf-8")

    # Every line in the result still ends \r\n (CRLF was not degraded to LF
    # anywhere, including around the rewritten value line).
    assert b"\r\n" in after_bytes
    for line in after_text.split("\n")[:-1]:
        assert line.endswith("\r"), f"non-CRLF line found: {line!r}"

    # The cost_guardrails block survives byte-for-byte.
    before_block = _cost_guardrails_block(crlf_content)
    after_block = _cost_guardrails_block(after_text)
    assert after_block == before_block

    assert "claude-haiku-4-5" in after_text
    assert "claude-sonnet-4-6" not in after_text


# ── Group E: absence / duplicate / unparseable-heading refusals ────────────


def test_heading_absent_in_present_file_refuses_cleanly(tmp_path, capsys):
    content = "# MODEL: myagent\n\nNo default-model heading in this file at all.\n"
    agent_dir = make_agent_dir(tmp_path, "myagent")
    model_path = make_model_md(agent_dir, content=content)
    before = model_path.read_text(encoding="utf-8")

    exit_code = run_set_model(
        make_set_model_args(
            "myagent", tmp_path, model="claude-sonnet-4-6", use_json=True
        ),
        tmp_path,
    )
    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_type"] == "default_model_heading_absent"
    assert model_path.read_text(encoding="utf-8") == before


def test_duplicate_default_model_heading_refuses_cleanly(tmp_path, capsys):
    content = (
        "## Default model\n\n**`claude-sonnet-4-6`**\n\n"
        "## Default model\n\n**`claude-opus-4-8`**\n"
    )
    agent_dir = make_agent_dir(tmp_path, "myagent")
    model_path = make_model_md(agent_dir, content=content)
    before = model_path.read_text(encoding="utf-8")

    exit_code = run_set_model(
        make_set_model_args(
            "myagent", tmp_path, model="claude-haiku-4-5", use_json=True
        ),
        tmp_path,
    )
    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_type"] == "duplicate_default_model_heading"
    assert model_path.read_text(encoding="utf-8") == before


def test_heading_regex_ignores_h3_subheading_and_prose_mention(tmp_path):
    """Fix 1 (line-anchoring, spec/55 P1 finding): before the anchor fix,
    ``_HEADING_RE``/``_VALUE_SPAN_RE`` had no ``^``/``re.MULTILINE`` anchor,
    so ``##\\s+Default model`` matched the LAST TWO ``#`` of an
    "### Default model" H3 subheading, and ALSO matched a "## Default model"
    substring embedded mid-sentence inside an ordinary prose paragraph —
    either would have been miscounted as a second heading (a false
    ``duplicate_default_model_heading`` refusal) or, worse, silently edited
    instead of the real H2. Both decoys sit BEFORE the real ``## Default
    model`` H2 in this fixture; a correctly-anchored regex counts exactly
    ONE heading (the real one), edits only its value, and leaves the decoy
    lines byte-for-byte untouched.
    """
    content = (
        "# MODEL: myagent\n\n"
        "### Default model (deprecated subsection, do not use)\n\n"
        "some-old-value\n\n"
        "See the ## Default model section below for the real setting.\n\n"
        "## Default model\n\n"
        "**`claude-sonnet-4-6`**\n\n"
        "## Fallback\n\n"
        "**`claude-opus-4-8`**\n"
    )
    agent_dir = make_agent_dir(tmp_path, "myagent")
    model_path = make_model_md(agent_dir, content=content)

    exit_code = run_set_model(
        make_set_model_args("myagent", tmp_path, model="claude-haiku-4-5"),
        tmp_path,
    )
    assert exit_code == 0
    after = model_path.read_text(encoding="utf-8")

    # The decoy H3 subheading and its value line survive byte-for-byte.
    assert "### Default model (deprecated subsection, do not use)" in after
    assert "some-old-value" in after
    # The decoy prose mention survives byte-for-byte.
    assert "See the ## Default model section below for the real setting." in after
    # Only the REAL H2's value changed.
    assert "**`claude-haiku-4-5`**" in after
    assert "**`claude-sonnet-4-6`**" not in after
    # The Fallback heading (untouched field) also survives verbatim.
    assert "**`claude-opus-4-8`**" in after


def test_value_unparseable_after_heading_refuses_cleanly(tmp_path, capsys):
    """Tier B decision (the third state neither ABSENCE ruling covers): a
    heading with no parseable value token immediately following it is
    refused, not scaffold-filled.
    """
    content = "## Default model\n\n## Fallback\n\n**`claude-opus-4-8`**\n"
    agent_dir = make_agent_dir(tmp_path, "myagent")
    model_path = make_model_md(agent_dir, content=content)
    before = model_path.read_text(encoding="utf-8")

    exit_code = run_set_model(
        make_set_model_args(
            "myagent", tmp_path, model="claude-haiku-4-5", use_json=True
        ),
        tmp_path,
    )
    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_type"] == "default_model_value_unparseable"
    assert model_path.read_text(encoding="utf-8") == before


def test_model_md_absent_refuses_via_direct_write_path_call(tmp_path, capsys):
    """DIRECT call to ``_run_set_model`` — see module docstring: the
    AgentRegistryBackend discovery predicate requires model.md to be present
    to resolve an agent at all, so this refusal is unreachable end-to-end via
    ``run_set_model``'s registry-resolve step (get_agent() returns None
    first). This test proves the write-path function itself refuses when
    handed an agent_dir with no model.md, which is the reachable case (a
    TOCTOU race: model.md deleted between registry resolve and this
    function's own check).
    """
    agent_dir = tmp_path / "myagent"
    agent_dir.mkdir()
    model_path = agent_dir / "model.md"
    assert not model_path.exists()

    exit_code = _run_set_model(
        agent_id="myagent",
        agent_dir=agent_dir,
        agents_root=tmp_path,
        model_path=model_path,
        model_id="claude-sonnet-4-6",
        use_json=True,
        dry_run=False,
        yes=True,
    )
    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_type"] == "model_md_absent"


def test_show_on_absent_model_md_reports_absent_state_at_exit_zero(tmp_path, capsys):
    """DIRECT call to ``_show_model`` — same registry-predicate reachability
    note as the absent-write test above. --show is a pure read; per the
    P1 prep finding it must NEVER refuse on an absent model.md, mirroring
    govern --show's absent-state precedent (exit 0, not exit 1).
    """
    agent_dir = tmp_path / "myagent"
    agent_dir.mkdir()
    model_path = agent_dir / "model.md"

    exit_code = _show_model("myagent", tmp_path, model_path, True)
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["model_md_present"] is False
    # parse_model_md(None) defaults still populate a well-formed shape.
    assert payload["default_model"]


# ── Group F: --show (reachable end-to-end) ──────────────────────────────────


def test_show_reports_resolved_config_and_policy_override_signal(tmp_path, capsys):
    agent_dir = make_agent_dir(tmp_path, "myagent")
    make_model_md(agent_dir)  # default_model = claude-sonnet-4-6
    _write_policy_md(tmp_path, "model: claude-opus-4-8\n")

    exit_code = run_set_model(
        make_set_model_args("myagent", tmp_path, show=True, use_json=True), tmp_path
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["default_model"] == "claude-sonnet-4-6"
    assert payload["fallback_model"] == "claude-opus-4-8"
    assert payload["policy_effective_model"] == "claude-opus-4-8"
    assert payload["policy_overrides_default_model"] is True


def test_show_never_acquires_the_manage_lease(tmp_path, monkeypatch):
    """Reads never contend with writes (spec/55 M11 note) — --show must not
    even construct the manage lock backend.
    """
    from atomic_agents.manage import _routine

    called = []
    original = _routine.get_manage_lock_backend

    def _tracking(agent_dir):
        called.append(agent_dir)
        return original(agent_dir)

    monkeypatch.setattr(_routine, "get_manage_lock_backend", _tracking)

    agent_dir = make_agent_dir(tmp_path, "myagent")
    make_model_md(agent_dir)

    exit_code = run_set_model(
        make_set_model_args("myagent", tmp_path, show=True, use_json=True), tmp_path
    )
    assert exit_code == 0
    assert called == []


# ── Group G: restore does NOT re-run M9 ─────────────────────────────────────


def test_restore_does_not_revalidate_against_current_pricing(tmp_path):
    """A snapshot whose ``default_model`` is NOT in ``_costs.PRICING``
    restores successfully anyway — maintainer ruling ``fallback-m9-bar``
    extends the same "restore does not re-run M9" rule set-model shares
    with govern.

    Fix 5 (load-bearing correction, spec/55 P2 finding): the ORIGINAL
    version of this test round-tripped through two normally-PRICED Claude
    models (claude-sonnet-4-6 -> claude-opus-4-8), so it would STILL PASS
    even if ``--restore`` accidentally re-ran the M9 composition chain —
    both models clear gate (a)/(b) trivially, so a regression reintroducing
    a revalidation call would never have turned this test red. This version
    snapshots a model id that is GENUINELY UNPRICED: restoring it MUST still
    succeed (M9 is restore-exempt by design), so a regression that makes
    ``--restore`` call ``_validate_model_composition`` would refuse this
    restore with ``error_type='unpriced_model'`` — turning this test RED,
    which is the point.
    """
    from atomic_agents.core_api import get_model_rates
    from atomic_agents.manage._routine import take_config_snapshot

    agent_dir = make_agent_dir(tmp_path, "myagent")
    model_path = make_model_md(agent_dir)  # default_model = claude-sonnet-4-6

    unpriced_model_id = "totally-unpriced-model-xyz"
    # Sanity: genuinely unpriced, not a fixture typo (M9 gate (a) would
    # refuse a fresh --model write with this id — see Group A above).
    assert get_model_rates(unpriced_model_id) is None

    snapshot_content = model_path.read_text(encoding="utf-8").replace(
        "claude-sonnet-4-6", unpriced_model_id
    )
    snapshot_path = take_config_snapshot(
        agent_dir, snapshot_content, subdir="set-model"
    )

    exit_code = run_set_model(
        make_set_model_args(
            "myagent", tmp_path, restore=snapshot_path.name, use_json=True
        ),
        tmp_path,
    )
    assert exit_code == 0
    assert unpriced_model_id in model_path.read_text(encoding="utf-8")


def test_restore_surfaces_advisory_when_snapshot_model_now_unpriced(tmp_path, capsys):
    agent_dir = make_agent_dir(tmp_path, "myagent")
    model_path = make_model_md(agent_dir)

    # Manually snapshot a state whose default_model is NOT in PRICING.
    from atomic_agents.manage._routine import take_config_snapshot

    stale_content = model_path.read_text(encoding="utf-8").replace(
        "claude-sonnet-4-6", "some-deprecated-unpriced-model"
    )
    snapshot_path = take_config_snapshot(agent_dir, stale_content, subdir="set-model")

    exit_code = run_set_model(
        make_set_model_args(
            "myagent", tmp_path, restore=snapshot_path.name, use_json=True
        ),
        tmp_path,
    )
    assert exit_code == 0  # non-blocking — restore never refuses on this
    payload = json.loads(capsys.readouterr().out)
    # Fix 6: the restore's M9-staleness note lives under ITS OWN key,
    # ``pricing_advisory`` — NEVER ``policy_override`` (that key is reserved
    # for --set's genuine Policy-vs-model.md conflict; see
    # test_restore_advisory_uses_distinct_json_key_not_policy_override below
    # for the negative-control half of this guard).
    assert payload["pricing_advisory"] is not None
    assert "some-deprecated-unpriced-model" in payload["pricing_advisory"]


def test_restore_advisory_uses_distinct_json_key_not_policy_override(tmp_path, capsys):
    """Fix 6 (JSON key collision, spec/55 P2 finding): negative control —
    a restore whose snapshot model is unpriced MUST leave ``policy_override``
    ``null`` (untouched) in the ``--json`` payload. Before this fix, the
    restore path wrote its M9-staleness note into ``policy_override`` — the
    SAME key ``--set`` uses for a genuine Policy conflict — so a copilot
    keyed on ``payload["policy_override"]`` after ``--restore`` would
    misread "this model is no longer priced" as "Policy overrides this
    model," two unrelated conditions.
    """
    agent_dir = make_agent_dir(tmp_path, "myagent")
    model_path = make_model_md(agent_dir)

    from atomic_agents.manage._routine import take_config_snapshot

    stale_content = model_path.read_text(encoding="utf-8").replace(
        "claude-sonnet-4-6", "some-deprecated-unpriced-model"
    )
    snapshot_path = take_config_snapshot(agent_dir, stale_content, subdir="set-model")

    exit_code = run_set_model(
        make_set_model_args(
            "myagent", tmp_path, restore=snapshot_path.name, use_json=True
        ),
        tmp_path,
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["policy_override"] is None
    assert payload["pricing_advisory"] is not None


def test_restore_emits_exactly_one_manage_restore_record_never_set_model(tmp_path):
    agent_dir = make_agent_dir(tmp_path, "myagent")
    make_model_md(agent_dir)

    run_set_model(
        make_set_model_args("myagent", tmp_path, model="claude-opus-4-8"), tmp_path
    )
    from atomic_agents.manage._routine import list_snapshots

    snapshot_id = list_snapshots(agent_dir, "set-model")[0].name

    run_set_model(
        make_set_model_args("myagent", tmp_path, restore=snapshot_id), tmp_path
    )

    records = collect_jsonl(agent_dir / "log")
    restore_records = [
        r for r in records if r.get("primitive") == PRIMITIVE_MANAGE_RESTORE
    ]
    set_model_records_from_restore = [
        r
        for r in records
        if r.get("primitive") == PRIMITIVE_MANAGE_SET_MODEL
        and "restore" in r.get("summary", "").lower()
    ]
    assert len(restore_records) == 1
    assert set_model_records_from_restore == []


# ── Group H: audit shape ────────────────────────────────────────────────────


def test_applied_write_uses_distinct_set_model_primitive_not_govern(tmp_path):
    agent_dir = make_agent_dir(tmp_path, "myagent")
    make_model_md(agent_dir)

    exit_code = run_set_model(
        make_set_model_args("myagent", tmp_path, model="claude-opus-4-8"), tmp_path
    )
    assert exit_code == 0

    records = collect_jsonl(agent_dir / "log")
    applied = [r for r in records if r["primitive"] == PRIMITIVE_MANAGE_SET_MODEL]
    assert len(applied) == 1
    assert applied[0]["primitive"] != "manage_govern"


def test_applied_write_record_has_no_cost_usd(tmp_path):
    agent_dir = make_agent_dir(tmp_path, "myagent")
    make_model_md(agent_dir)

    run_set_model(
        make_set_model_args("myagent", tmp_path, model="claude-opus-4-8"), tmp_path
    )

    records = collect_jsonl(agent_dir / "log")
    applied = [r for r in records if r["primitive"] == PRIMITIVE_MANAGE_SET_MODEL][0]
    assert applied.get("cost_usd") is None


def test_applied_write_record_has_no_created_key(tmp_path):
    agent_dir = make_agent_dir(tmp_path, "myagent")
    make_model_md(agent_dir)

    run_set_model(
        make_set_model_args("myagent", tmp_path, model="claude-opus-4-8"), tmp_path
    )

    records = collect_jsonl(agent_dir / "log")
    applied = [r for r in records if r["primitive"] == PRIMITIVE_MANAGE_SET_MODEL][0]
    assert "created" not in applied


def test_applied_write_appends_to_both_per_agent_and_fleet_log_scopes(tmp_path):
    agent_dir = make_agent_dir(tmp_path, "myagent")
    make_model_md(agent_dir)

    run_set_model(
        make_set_model_args("myagent", tmp_path, model="claude-opus-4-8"), tmp_path
    )

    per_agent = collect_jsonl(agent_dir / "log")
    fleet = collect_jsonl(get_fleet_log_dir(tmp_path))
    assert any(r["primitive"] == PRIMITIVE_MANAGE_SET_MODEL for r in per_agent)
    assert any(r["primitive"] == PRIMITIVE_MANAGE_SET_MODEL for r in fleet)


def test_snapshot_path_is_relative_and_restorable(tmp_path):
    agent_dir = make_agent_dir(tmp_path, "myagent")
    make_model_md(agent_dir)

    run_set_model(
        make_set_model_args("myagent", tmp_path, model="claude-opus-4-8"), tmp_path
    )

    records = collect_jsonl(agent_dir / "log")
    applied = [r for r in records if r["primitive"] == PRIMITIVE_MANAGE_SET_MODEL][0]
    snap_rel = applied["snapshot_path"]
    assert snap_rel is not None
    assert not Path(snap_rel).is_absolute()
    assert (agent_dir / snap_rel).is_file()


# ── Group I: real CLI surface (atomic_agents.cli.main) ──────────────────────


def test_cli_set_model_dry_run_end_to_end(tmp_path, capsys):
    agent_dir = make_agent_dir(tmp_path, "myagent")
    model_path = make_model_md(agent_dir)
    before = model_path.read_text(encoding="utf-8")

    exit_code = cli_mod.main(
        [
            "manage",
            "set-model",
            "myagent",
            "--model",
            "claude-opus-4-8",
            "--agents-root",
            str(tmp_path),
            "--dry-run",
            "--json",
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert model_path.read_text(encoding="utf-8") == before  # nothing written


def test_cli_set_model_applies_end_to_end(tmp_path, capsys):
    agent_dir = make_agent_dir(tmp_path, "myagent")
    model_path = make_model_md(agent_dir)

    exit_code = cli_mod.main(
        [
            "manage",
            "set-model",
            "myagent",
            "--model",
            "claude-opus-4-8",
            "--agents-root",
            str(tmp_path),
            "--yes",
            "--json",
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert "claude-opus-4-8" in model_path.read_text(encoding="utf-8")


def test_cli_set_model_fallback_flag_parses_and_refuses_cleanly(tmp_path, capsys):
    """Proves the CLI PARSER recognizes --fallback (grammar-pinned) even
    though PR1 refuses its semantics — never an argparse 'unrecognized
    arguments' error.
    """
    make_agent_dir(tmp_path, "myagent")

    exit_code = cli_mod.main(
        [
            "manage",
            "set-model",
            "myagent",
            "--fallback",
            "claude-opus-4-8",
            "--agents-root",
            str(tmp_path),
            "--json",
        ]
    )
    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_type"] == "not_yet_settable_in_pr1"


def test_cli_set_model_provider_flag_parses_and_refuses_cleanly(tmp_path, capsys):
    make_agent_dir(tmp_path, "myagent")

    exit_code = cli_mod.main(
        [
            "manage",
            "set-model",
            "myagent",
            "--model",
            "claude-opus-4-8",
            "--provider",
            "anthropic",
            "--agents-root",
            str(tmp_path),
            "--json",
        ]
    )
    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_type"] == "not_yet_settable_in_pr1"


def test_cli_set_model_show_end_to_end(tmp_path, capsys):
    agent_dir = make_agent_dir(tmp_path, "myagent")
    make_model_md(agent_dir)

    exit_code = cli_mod.main(
        [
            "manage",
            "set-model",
            "myagent",
            "--show",
            "--agents-root",
            str(tmp_path),
            "--json",
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["default_model"] == "claude-sonnet-4-6"
