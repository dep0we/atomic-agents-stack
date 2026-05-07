"""Parse model.md to extract default model, fallback, token caps, cost guardrails.

model.md is markdown but a few key fields appear in well-known formats:
- "## Default model" followed by a code block or bare line with the model id
- "## Fallback" same pattern
- A "cost_guardrails:" YAML block somewhere

This is a lightweight parser using regex + YAML for the guardrails block.
"""

from __future__ import annotations
import re
from pathlib import Path

import yaml


def parse_model_md(path: Path) -> dict:
    """Parse model.md into a dict.

    Returns:
        {
          "default_model": str,
          "fallback_model": str | None,
          "max_input_tokens": int,
          "max_output_tokens": int,
          "cost_guardrails_enabled": bool,
          "daily_cap_usd": float,
          "monthly_cap_usd": float,
          "daily_cap_action": str,
          "monthly_cap_action": str,
          "warning_thresholds": list[float],
          "alert_channel": str,
        }
    """
    defaults = {
        "default_model": "claude-sonnet-4-6-20260101",
        "fallback_model": None,
        "max_input_tokens": 12_000,
        "max_output_tokens": 4_000,
        "cost_guardrails_enabled": False,
        "daily_cap_usd": 0.0,
        "monthly_cap_usd": 0.0,
        "daily_cap_action": "skip",
        "monthly_cap_action": "alert",
        "warning_thresholds": [0.50, 0.80],
        "alert_channel": "log_only",
    }

    if not path.exists():
        return defaults

    text = path.read_text(encoding="utf-8")

    # Find "## Default model" section — accepts `**\`model-id\`**`, `**model-id**`,
    # `\`model-id\``, or bare model-id on its own line
    m = re.search(
        r"##\s+Default model[^\n]*\n+\s*\*{0,2}`?([a-zA-Z0-9._/-]+)`?\*{0,2}",
        text,
    )
    if m:
        defaults["default_model"] = m.group(1).strip("`* ")

    m = re.search(
        r"##\s+Fallback[^\n]*\n+\s*\*{0,2}`?([a-zA-Z0-9._/-]+)`?\*{0,2}",
        text,
    )
    if m:
        defaults["fallback_model"] = m.group(1).strip("`* ")

    # Find any embedded YAML block
    yaml_blocks = re.findall(r"```yaml\s*\n(.*?)```", text, re.DOTALL)
    for block in yaml_blocks:
        try:
            parsed = yaml.safe_load(block)
        except yaml.YAMLError:
            continue
        if not isinstance(parsed, dict):
            continue

        # cost_guardrails block
        cg = parsed.get("cost_guardrails")
        if isinstance(cg, dict):
            defaults["cost_guardrails_enabled"] = bool(cg.get("enabled", False))
            defaults["daily_cap_usd"] = float(cg.get("daily_cap_usd", 0.0))
            defaults["monthly_cap_usd"] = float(cg.get("monthly_cap_usd", 0.0))
            defaults["daily_cap_action"] = str(cg.get("daily_cap_action", "skip"))
            defaults["monthly_cap_action"] = str(cg.get("monthly_cap_action", "alert"))
            wt = cg.get("warning_thresholds")
            if isinstance(wt, list):
                defaults["warning_thresholds"] = [float(x) for x in wt]
            defaults["alert_channel"] = str(cg.get("alert_channel", "log_only"))

    # Token caps from markdown table or bullets — look for the first int after these labels
    for key, label_pattern in [
        ("max_input_tokens", r"Max\s+(?:system\s+prompt|input)[^\d]*(\d[\d,]*)"),
        ("max_output_tokens", r"Max\s+output[^\d]*(\d[\d,]*)"),
    ]:
        m = re.search(label_pattern, text, re.IGNORECASE)
        if m:
            defaults[key] = int(m.group(1).replace(",", ""))

    return defaults
