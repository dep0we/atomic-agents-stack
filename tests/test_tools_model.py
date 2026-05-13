"""Tests for atomic_agents._tools and ._model parsers."""

from pathlib import Path

import pytest

from atomic_agents._tools import parse_tools_md
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


def test_parse_model_md_provider_outside_yaml_still_matches_when_yaml_also_has_provider(tmp_path):
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
