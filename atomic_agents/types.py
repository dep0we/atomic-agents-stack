"""Shared dataclasses for the atomic_agents package."""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .tools import ToolCallResult
    from .mcp import MCPServerSpec


@dataclass
class AgentConfig:
    """Loaded from model.md + tools.md at agent init."""

    # From model.md
    default_model: str
    fallback_model: str | None
    # Optional LLMBackend disambiguator (#87): when multiple registered
    # backends claim the same model id (e.g., openai + azure-openai both
    # match ``gpt-5``), this names which one wins. None → registry uses
    # the unambiguous match or raises ``AmbiguousBackendError``.
    provider: str | None = None
    max_input_tokens: int = 12_000
    max_output_tokens: int = 4_000
    temperature: float = 0.6

    # cost_guardrails block from model.md
    cost_guardrails_enabled: bool = False
    daily_cap_usd: float = 0.0
    monthly_cap_usd: float = 0.0
    daily_cap_action: str = "skip"      # skip | fallback | alert
    monthly_cap_action: str = "alert"
    warning_thresholds: list[float] = field(default_factory=lambda: [0.50, 0.80])
    alert_channel: str = "log_only"     # telegram | email | journal | log_only

    # From tools.md (parsed)
    read_paths: list[Path] = field(default_factory=list)
    write_paths: list[Path] = field(default_factory=list)
    read_only_paths: list[Path] = field(default_factory=list)
    external_apis: list[str] = field(default_factory=list)
    hard_nos: list[str] = field(default_factory=list)

    # From roster.md (parsed) — agent names this coordinator may delegate to.
    # Empty list = no delegation allowed.
    roster: list[str] = field(default_factory=list)

    # From mcp.md (parsed) — MCP servers this agent may connect to.
    # Empty list = no MCP servers declared (that's fine; pool not created).
    mcp_servers: list["MCPServerSpec"] = field(default_factory=list)


@dataclass
class CostCheckResult:
    allow: bool
    action: str | None = None         # 'skip' | 'fallback' | 'alert' | None
    reason: str = ""
    fallback_model: str | None = None


@dataclass
class Capture:
    """One atomic note to be written, parsed from a capture marker."""
    type: str                          # user | feedback | project | decision | reference
    name: str
    description: str
    confidence: str                    # high | medium | low
    sources: list[str]
    body: str
    supersedes: str | None = None
    merge_into: str | None = None
    pinned: bool = False
    expires_at: str | None = None
    tags: list[str] = field(default_factory=list)


@dataclass
class Response:
    """Result of an LLM call via AtomicAgent.call()."""
    text: str
    model: str                         # the model actually used (may differ if fallback)
    input_tokens: int
    output_tokens: int
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    cost_usd: float = 0.0
    cost_estimated_via_fallback: bool = False  # True when model id was not in PRICING
    latency_ms: int = 0
    summary: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    captures: list[Capture] = field(default_factory=list)
    skipped: bool = False              # True if cost guardrail blocked the call
    skip_reason: str = ""
    # Custom tools fields (spec/17) — populated when tool_registry has tools
    tool_calls: list["ToolCallResult"] = field(default_factory=list)
    tool_iterations: int = 1           # 1 = no tools used, 2+ = multi-turn loop
    tool_iterations_maxed: bool = False  # True when max_iterations cap was hit

    @classmethod
    def skipped_response(cls, reason: str, model: str) -> "Response":
        """Build a Response that represents a skipped (guardrailed) call."""
        return cls(
            text="",
            model=model,
            input_tokens=0,
            output_tokens=0,
            skipped=True,
            skip_reason=reason,
            summary=f"skipped: {reason}",
        )


@dataclass
class HelperResult:
    """One helper_call result (returned to the parent agent)."""
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: int
    # Provenance fields (per spec/10 Wave 8)
    # `sources` echoes the sources passed in so the parent agent can
    # cite them in its response without keeping the original list around.
    sources: list[str] = field(default_factory=list)
    # True when the helper output appears to preserve attribution (citation-like
    # brackets or named source mentions). Heuristic — defaults to True when
    # no sources were passed (no provenance to preserve in that case).
    provenance_preserved: bool = True
