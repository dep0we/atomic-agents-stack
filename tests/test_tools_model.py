"""Tests for atomic_agents._tools and ._model parsers."""

import os
import warnings
from pathlib import Path

from atomic_agents._platform import resolve_under_agent_root
from atomic_agents._tools import parse_tools_md, parse_tools_md_text
from atomic_agents._model import parse_model_md


def test_parse_tools_md_basic(tmp_path):
    tools_path = tmp_path / "tools.md"
    tools_path.write_text("""# TOOLS — Caldwell

## Read paths

- `~/docs/agents/caldwell/`                          (own folder, full read)
- `~/docs/finance/`                                  (operator's financial vault)

## Write paths (own folder ONLY)

- `~/docs/agents/caldwell/memory/`
- `~/docs/agents/caldwell/wiki/`

## External APIs

- **Anthropic API** — Claude calls per `model.md`
- **Tavily search** — used occasionally

## Hard NOs

- Never write outside own folder
- Never recommend specific securities by ticker
""")
    parsed = parse_tools_md(tools_path)
    assert len(parsed["read_paths"]) == 2
    assert len(parsed["write_paths"]) == 2
    assert len(parsed["external_apis"]) == 2
    assert len(parsed["hard_nos"]) == 2


def test_parse_tools_md_missing_file(tmp_path):
    parsed = parse_tools_md(tmp_path / "nonexistent.md")
    assert parsed == {
        "read_paths": [],
        "write_paths": [],
        "read_only_paths": [],
        "external_apis": [],
        "hard_nos": [],
    }


def test_parse_model_md_basic(tmp_path):
    model_path = tmp_path / "model.md"
    model_path.write_text("""# MODEL — Caldwell

## Default model

**`claude-opus-4-7-20260101`**

## Fallback

**`claude-sonnet-4-6-20260101`**

## Token budget

| Limit | Value |
|---|---|
| Max system prompt | 12,000 tokens |
| Max output per turn | 4,000 tokens |

## Cost guardrails

```yaml
cost_guardrails:
  enabled: true
  daily_cap_usd: 5.00
  monthly_cap_usd: 100.00
  daily_cap_action: skip
  monthly_cap_action: alert
  warning_thresholds: [0.50, 0.80]
  alert_channel: telegram
```
""")
    parsed = parse_model_md(model_path)
    assert parsed["default_model"] == "claude-opus-4-7-20260101"
    assert parsed["fallback_model"] == "claude-sonnet-4-6-20260101"
    assert parsed["max_input_tokens"] == 12000
    assert parsed["max_output_tokens"] == 4000
    assert parsed["cost_guardrails_enabled"] is True
    assert parsed["daily_cap_usd"] == 5.00
    assert parsed["monthly_cap_usd"] == 100.00
    assert parsed["daily_cap_action"] == "skip"
    assert parsed["alert_channel"] == "telegram"
    assert parsed["warning_thresholds"] == [0.50, 0.80]


def test_parse_model_md_missing_file(tmp_path):
    parsed = parse_model_md(tmp_path / "nonexistent.md")
    # Should return defaults, not crash
    assert parsed["cost_guardrails_enabled"] is False
    assert parsed["default_model"] == "claude-sonnet-4-6-20260101"


# ──────────────────────────────────────────────────────────────────
# #87 PR 3 — model.md `provider:` field for LLMBackend disambiguation


def test_parse_model_md_provider_field_extracted(tmp_path):
    """A ``provider: <id>`` line in model.md gets parsed into the config.
    Lets operators disambiguate when multiple LLMBackends claim the same
    model id (azure-openai + openai both match ``gpt-5``).
    """
    model_path = tmp_path / "model.md"
    model_path.write_text("""## Default model

gpt-5

provider: azure-openai
""")
    parsed = parse_model_md(model_path)
    assert parsed["provider"] == "azure-openai"
    # default_model still parsed correctly
    assert parsed["default_model"] == "gpt-5"


def test_parse_model_md_provider_field_optional(tmp_path):
    """Absent ``provider:`` line → ``provider`` is None (the registry uses
    its single-match resolution; no preferred provider).
    """
    model_path = tmp_path / "model.md"
    model_path.write_text("## Default model\n\ngpt-5\n")
    parsed = parse_model_md(model_path)
    assert parsed["provider"] is None


def test_parse_model_md_provider_field_with_whitespace(tmp_path):
    """Operator whitespace around the value is tolerated (operators write
    by hand; framework normalizes).
    """
    model_path = tmp_path / "model.md"
    model_path.write_text("""## Default model

gpt-5

provider:   openai
""")
    parsed = parse_model_md(model_path)
    assert parsed["provider"] == "openai"


def test_parse_model_md_provider_alone_returns_default_model(tmp_path):
    """An empty model.md returns the framework default with provider=None
    — confirms the new field doesn't disrupt the existing defaults path.
    """
    parsed = parse_model_md(tmp_path / "nonexistent.md")
    assert parsed.get("provider") is None


def test_parse_model_md_provider_in_middle_of_file_still_found(tmp_path):
    """The ``provider:`` line is found via a multiline regex anywhere in
    the file. Operators may put it anywhere — typically near the model
    declaration but not strictly required.
    """
    model_path = tmp_path / "model.md"
    model_path.write_text("""# Some preamble

Notes about this agent.

## Default model

gpt-5

## Some other section

provider: openai

And more notes.
""")
    parsed = parse_model_md(model_path)
    assert parsed["provider"] == "openai"


def test_parse_model_md_provider_inside_yaml_block_does_not_match(tmp_path):
    """A ``provider:`` key inside a fenced ```yaml block belongs to that
    block's config, not to the framework's LLMBackend disambiguator.
    The parser must NOT hoist it into ``defaults["provider"]``.

    Caught by Opus subagent review of #87 PR 3 (Finding 2). Real-world
    failure mode: an operator's ``cost_guardrails:`` YAML block happens
    to mention ``provider:`` for any reason — silent misroute of LLM
    dispatch with no diagnostic.
    """
    model_path = tmp_path / "model.md"
    model_path.write_text("""## Default model

gpt-5

```yaml
cost_guardrails:
  enabled: true
  provider: should-not-match
```
""")
    parsed = parse_model_md(model_path)
    assert parsed["provider"] is None


def test_parse_model_md_provider_outside_yaml_still_matches_when_yaml_also_has_provider(
    tmp_path,
):
    """Even if a YAML block contains a ``provider:`` line, a top-level
    ``provider:`` outside the block still wins. Confirms the YAML-strip
    doesn't accidentally consume the top-level line.
    """
    model_path = tmp_path / "model.md"
    model_path.write_text("""## Default model

gpt-5

provider: openai

```yaml
cost_guardrails:
  provider: ignored
```
""")
    parsed = parse_model_md(model_path)
    assert parsed["provider"] == "openai"


# ──────────────────────────────────────────────────────────────────
# resolve_under_agent_root unit tests (issue #541)
# ──────────────────────────────────────────────────────────────────


def test_resolve_under_agent_root_bare_relative(tmp_path):
    """Bare-relative paths resolve under agent_root, not process CWD."""
    agent_root = tmp_path / "agents" / "myagent"
    result = resolve_under_agent_root("memory/", agent_root)
    assert result == (agent_root / "memory").resolve()
    assert result != (Path(os.getcwd()) / "memory").resolve()


def test_resolve_under_agent_root_dot_slash(tmp_path):
    """'./' resolves to agent_root itself."""
    agent_root = tmp_path / "agents" / "myagent"
    result = resolve_under_agent_root("./", agent_root)
    assert result == agent_root.resolve()


def test_resolve_under_agent_root_absolute_path_untouched(tmp_path):
    """Absolute paths are not re-anchored to agent_root."""
    agent_root = tmp_path / "agents" / "myagent"
    result = resolve_under_agent_root("/some/absolute/path", agent_root)
    assert result == Path("/some/absolute/path").resolve()


def test_resolve_under_agent_root_tilde_path_untouched(tmp_path):
    """Tilde paths are expanded normally, not re-anchored to agent_root."""
    agent_root = tmp_path / "agents" / "myagent"
    result = resolve_under_agent_root("~/docs/shared", agent_root)
    assert result == Path("~/docs/shared").expanduser().resolve()
    assert not str(result).startswith(str(agent_root))


def test_resolve_under_agent_root_expands_unexpanded_agent_root():
    """An unexpanded '~'-prefixed agent_root is expanded before the join.

    The helper bills itself as the framework-wide anchor, so it must be robust
    to any agent_root form. Passing 'agent_root=~/agents/foo' (literal tilde)
    must NOT leave a '~' segment under the process CWD — it must resolve under
    the real home directory.
    """
    result = resolve_under_agent_root("memory/", Path("~/agents/myagent"))
    expected = (Path("~/agents/myagent").expanduser() / "memory").resolve()
    assert result == expected
    assert "~" not in str(result), (
        f"literal '~' leaked into the resolved path: {result}"
    )


# ──────────────────────────────────────────────────────────────────
# parse_tools_md agent_root anchoring tests (issue #541)
# ──────────────────────────────────────────────────────────────────

_TEMPLATE_TOOLS_TEXT = """\
## Write paths (own folder ONLY)

- memory/ -- atomic note capture
- wiki/ -- wiki page authoring

## Read paths

- ./ -- full read access to own context
- raw/ -- source intake
"""


def test_parse_tools_md_bare_relative_anchors_to_agent_root(tmp_path, monkeypatch):
    """Bare-relative write/read paths resolve under agent_root when supplied.

    Runs from a decoy CWD that is NOT under agent_root, and asserts both the
    positive (anchored-under-agent_root) AND the negative (CWD-anchored form
    ABSENT) so the negative control bites regardless of collection order or the
    process CWD a prior test leaked. If the agent_root branch were dead, every
    bare-relative token would CWD-anchor to <decoy>/memory and these
    assertions would fail deterministically.
    """
    agent_root = tmp_path / "agents" / "myagent"
    decoy_cwd = tmp_path / "decoy_cwd"
    decoy_cwd.mkdir(parents=True)
    monkeypatch.chdir(decoy_cwd)

    result = parse_tools_md_text(_TEMPLATE_TOOLS_TEXT, agent_root=agent_root)

    write_paths = result["write_paths"]
    assert len(write_paths) == 2
    assert (agent_root / "memory").resolve() in write_paths
    assert (agent_root / "wiki").resolve() in write_paths
    # Negative control: the CWD-anchored form must be ABSENT.
    assert (Path.cwd() / "memory").resolve() not in write_paths, (
        "bare-relative 'memory/' CWD-anchored instead of anchoring under "
        "agent_root — the agent_root branch is dead"
    )
    assert (Path.cwd() / "wiki").resolve() not in write_paths

    read_paths = result["read_paths"]
    assert agent_root.resolve() in read_paths
    assert (agent_root / "raw").resolve() in read_paths
    assert (Path.cwd() / "raw").resolve() not in read_paths


def test_parse_tools_md_re_anchor_emits_audit_log(tmp_path, caplog):
    """Anchoring a bare-relative path under agent_root emits a DEBUG audit line.

    The audit trail is structural (CLAUDE.md principle 5): an operator who
    enables DEBUG must be able to trace that a bare-relative token was anchored
    under agent_root. Assert the distinctive 'anchored bare-relative path' log
    message, not just the result — a layered/silent anchor would otherwise be
    false-green.

    The level is DEBUG (not WARNING): bare-relative tokens are the canonical
    default-template shape, so this is the correct happy path, not an operator
    warning — a WARNING here would spam stderr on every agent load.
    """
    import logging

    agent_root = tmp_path / "agents" / "myagent"
    with caplog.at_level(logging.DEBUG, logger="atomic_agents._tools"):
        parse_tools_md_text(_TEMPLATE_TOOLS_TEXT, agent_root=agent_root)

    anchor_lines = [
        r for r in caplog.records if "anchored bare-relative path" in r.getMessage()
    ]
    assert anchor_lines, (
        "expected an 'anchored bare-relative path' audit log line for bare-"
        f"relative paths; got: {[r.getMessage() for r in caplog.records]}"
    )
    # The audit line is emitted at DEBUG (the happy-path level), not WARNING.
    assert all(r.levelno == logging.DEBUG for r in anchor_lines), (
        "anchor audit line must be DEBUG (correct happy path, not a warning); "
        f"got levels: {[r.levelname for r in anchor_lines]}"
    )
    # The specific path token must appear in the audit line.
    assert any("memory/" in r.getMessage() for r in anchor_lines)


def test_parse_tools_md_no_re_anchor_log_for_absolute(tmp_path, caplog):
    """Negative control: absolute paths are NOT anchored, so NO audit line.

    Pairs with the audit-log test above: if the anchor log fired
    unconditionally (not gated on bare-relative), this would also see it.
    Captures at DEBUG (the audit-line level) so the control genuinely bites —
    a WARNING-level capture would vacuously miss a stray DEBUG anchor line.
    """
    import logging

    agent_root = tmp_path / "agents" / "myagent"
    text = "## Write paths\n\n- /absolute/only -- already absolute\n"
    with caplog.at_level(logging.DEBUG, logger="atomic_agents._tools"):
        parse_tools_md_text(text, agent_root=agent_root)

    anchor_lines = [
        r for r in caplog.records if "anchored bare-relative path" in r.getMessage()
    ]
    assert not anchor_lines, (
        "absolute paths must NOT trigger an anchor audit line; "
        f"got: {[r.getMessage() for r in anchor_lines]}"
    )


def test_parse_tools_md_bare_relative_without_agent_root_warns(tmp_path):
    """When agent_root is omitted, bare-relative paths emit a UserWarning."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = parse_tools_md_text(_TEMPLATE_TOOLS_TEXT, agent_root=None)

    warning_messages = [str(w.message) for w in caught]
    assert any("agent_root" in m for m in warning_messages), (
        f"Expected a warning mentioning agent_root; got: {warning_messages}"
    )
    assert len(result["write_paths"]) == 2


def test_from_dict_bare_relative_tools_md_warns_no_agent_root():
    """AgentProfile.from_dict re-parses tools_md_raw WITHOUT agent_root (the DB
    round-trip context has no filesystem path), so a bare-relative write_path
    must trigger the documented CWD-anchoring UserWarning (#546).

    Pins the distinctive typed behavior of the documented fallback contract at
    the from_dict call site, not just the parser: a future refactor that
    silently drops the warning, or accidentally threads agent_root from a
    pathless dict, would otherwise pass unnoticed. The parser-level warning is
    tested separately; this asserts the call site that relies on it.
    """
    from atomic_agents.profile import AgentProfile

    d = {
        "name": "db-agent",
        "agent_mode": "operator",
        "persona_identity": "# Identity\n\nA test agent.\n",
        "persona_soul": "",
        "persona_user": "",
        "goal_text": "",
        "model_md_raw": "",
        "tools_md_raw": "## Write paths\n\n- memory/ -- bare-relative token\n",
        "judges_md_raw": None,
        "roster_md_raw": "",
        "mcp_md_raw": "",
        "model_config": {},
        "tool_config": {},
        "tool_classifications": {},
        "judges_config": None,
        "roster": [],
        "mcp_servers": [],
    }

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        AgentProfile.from_dict(d)

    warning_messages = [str(w.message) for w in caught]
    assert any("agent_root" in m for m in warning_messages), (
        "from_dict re-parse of a bare-relative tools.md must emit the documented "
        f"agent_root CWD-anchoring warning; got: {warning_messages}"
    )


def test_parse_tools_md_tilde_path_not_re_anchored(tmp_path):
    """Tilde paths in tools.md are expanded to home, not re-anchored to agent_root."""
    agent_root = tmp_path / "agents" / "myagent"
    text = "## Write paths\n\n- ~/docs/shared -- shared reference\n"
    result = parse_tools_md_text(text, agent_root=agent_root)
    assert len(result["write_paths"]) == 1
    assert result["write_paths"][0] == Path("~/docs/shared").expanduser().resolve()
    assert not str(result["write_paths"][0]).startswith(str(agent_root))


def test_parse_tools_md_absolute_path_not_re_anchored(tmp_path):
    """Absolute paths in tools.md are returned unchanged regardless of agent_root."""
    agent_root = tmp_path / "agents" / "myagent"
    text = "## Write paths\n\n- /absolute/shared -- shared\n"
    result = parse_tools_md_text(text, agent_root=agent_root)
    assert result["write_paths"][0] == Path("/absolute/shared").resolve()


def test_parse_tools_md_file_with_agent_root(tmp_path, monkeypatch):
    """parse_tools_md() on-disk function threads agent_root correctly.

    Runs from a decoy CWD and asserts the CWD-anchored form is ABSENT so the
    negative control bites regardless of process CWD.
    """
    agent_root = tmp_path / "agents" / "myagent"
    tools_path = tmp_path / "tools.md"
    tools_path.write_text(_TEMPLATE_TOOLS_TEXT, encoding="utf-8")
    decoy_cwd = tmp_path / "decoy_cwd"
    decoy_cwd.mkdir(parents=True)
    monkeypatch.chdir(decoy_cwd)

    result = parse_tools_md(tools_path, agent_root=agent_root)
    assert (agent_root / "memory").resolve() in result["write_paths"]
    assert (Path.cwd() / "memory").resolve() not in result["write_paths"], (
        "on-disk parse CWD-anchored 'memory/' instead of anchoring under agent_root"
    )


def test_parse_tools_md_bare_relative_negative_control_no_agent_root(tmp_path):
    """Without agent_root, 'memory/' must NOT resolve under agent_root."""
    agent_root = tmp_path / "agents" / "myagent"
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        result = parse_tools_md_text(_TEMPLATE_TOOLS_TEXT, agent_root=None)

    expected_under_agent_root = (agent_root / "memory").resolve()
    assert expected_under_agent_root not in result["write_paths"], (
        "Without agent_root, paths must NOT resolve under agent_root"
    )
