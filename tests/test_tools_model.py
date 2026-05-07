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
