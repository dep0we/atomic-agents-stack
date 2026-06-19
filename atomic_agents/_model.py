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


def parse_model_md(path: Path | None) -> dict:
    """Parse model.md from disk. Thin wrapper around parse_model_md_text.

    Accepts None (meaning "neither cascade layer has model.md") and returns
    pure defaults.
    """
    if path is None or not path.exists():
        return parse_model_md_text("")
    return parse_model_md_text(path.read_text(encoding="utf-8"))


def parse_model_md_text(text: str) -> dict:
    """Parse model.md content into a config dict.

    Returns:
        {
          "default_model": str,
          "fallback_model": str | None,
          "provider": str | None,          # #87: optional LLMBackend disambiguator
          "max_input_tokens": int,
          "max_output_tokens": int,
          "cost_guardrails_enabled": bool,
          "daily_cap_usd": float,
          "monthly_cap_usd": float,
          "daily_cap_action": str,
          "monthly_cap_action": str,
          "warning_thresholds": list[float],
          "alert_channel": str,
          "dedup_body_hash_enabled": bool,  # spec/45 PR2; default False (opt-in)
        }

    Empty input returns pure defaults.
    """
    defaults = {
        "default_model": "claude-sonnet-4-6-20260101",
        "fallback_model": None,
        "provider": None,
        "max_input_tokens": 12_000,
        "max_output_tokens": 4_000,
        "cost_guardrails_enabled": False,
        "daily_cap_usd": 0.0,
        "monthly_cap_usd": 0.0,
        "daily_cap_action": "skip",
        "monthly_cap_action": "alert",
        "warning_thresholds": [0.50, 0.80],
        "alert_channel": "log_only",
        # spec/45 PR2: when True, agent.call() derives an implicit idempotency key
        # as sha256(work_item+model+max_tokens+temperature) when no explicit key is
        # supplied. Default False — operator must opt in via model.md.
        "dedup_body_hash_enabled": False,
        # spec/47 PR1 (PROVISIONAL — see spec/47 §"Three-channel seam"): backend id
        # for the conversation backend. None when the section is absent (single-shot
        # default). The section name, key, and parsing location may change before
        # spec/47 LOCK. Do NOT depend on this field for stable deployments until LOCKED.
        "conversation_backend_id": None,
    }

    if not text:
        return defaults

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

    # Optional ``provider:`` line — disambiguates when multiple LLMBackends
    # claim the same model id (e.g., openai + azure-openai both claim gpt-5).
    # See ``atomic_agents.llm.find_backend_for_model(preferred_provider=...)``
    # and the ``AmbiguousBackendError`` it raises when this field is missing
    # under ambiguity.
    #
    # Strip fenced YAML blocks before searching — operator config blocks
    # (cost_guardrails, etc.) may have ``provider:`` nested under another
    # key, which is unrelated and must not hoist into the framework's
    # provider setting. Caught by Opus subagent review of #87 PR 3
    # (Finding 2).
    text_outside_yaml = re.sub(r"```yaml\s*\n.*?```", "", text, flags=re.DOTALL)
    m = re.search(
        r"^\s*provider\s*:\s*([a-zA-Z0-9._-]+)\s*$",
        text_outside_yaml,
        re.MULTILINE,
    )
    if m:
        defaults["provider"] = m.group(1).strip()

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

    # spec/45 PR2: "## Dedup Body Hash" section — presence (any body or empty)
    # enables implicit sha256(work_item+model+max_tokens+temperature) key derivation.
    # Mirrors the "## Allow No Auth" convention in serve.md: the section's presence
    # is the signal; the body text (if any) is ignored. Default is False (opt-in).
    # Anchored to a standalone h2 line (^## ... $, MULTILINE) so that an h3/h4
    # heading like "### Dedup Body Hash" or a longer heading such as
    # "## Dedup Body Hash Strategy" does NOT spuriously enable dedup — matching
    # the exact-equality discipline serve.md's "## Allow No Auth" parser uses.
    if re.search(r"^##\s+Dedup Body Hash\s*$", text, re.IGNORECASE | re.MULTILINE):
        defaults["dedup_body_hash_enabled"] = True

    # spec/47 PR1 (PROVISIONAL): parse '## Conversation Backend' section.
    # Value is the backend_id string on the next non-empty line (e.g. 'filesystem').
    # The section name, key syntax, and parsing location are PROVISIONAL and may
    # change before spec/47 LOCK. Do NOT depend on this for stable deployments.
    m = re.search(
        r"^##\s+Conversation Backend\s*$\n+\s*([a-zA-Z0-9_-]+)\s*$",
        text,
        re.IGNORECASE | re.MULTILINE,
    )
    if m:
        defaults["conversation_backend_id"] = m.group(1).strip().lower()

    return defaults
