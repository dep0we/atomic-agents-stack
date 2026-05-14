"""AtomicAgent class — the main runtime per spec/04.

Loads persona/tools/model/memory/journal in canonical order, calls the LLM,
extracts captures, writes them helper-mediated, logs the run.

Usage:

    from atomic_agents import AtomicAgent

    agent = AtomicAgent(name="caldwell", trigger="cron")
    response = agent.call(work_item="Daily morning brief")
    print(response.text)
"""

from __future__ import annotations
import concurrent.futures
import json
import logging
import os
import re
import time
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .llm.types import LLMToolDefinition  # noqa: F401 — used in str type hints

_logger = logging.getLogger(__name__)

import frontmatter

from . import _capture, _cascade, _costs, _llm, _model, _roster, _tools
from ._io import atomic_append_jsonl, atomic_write, safe_resolve_under
from .memory.filesystem import FilesystemBackend
from .memory.backend import WritePolicy
from .mcp import MCPClientPool, parse_mcp_md
from ._locks import AgentLock
from ._platform import get_agents_root
from ._schema import validate_atomic_note_frontmatter
from .goal import parse_agent_mode
from .exceptions import (
    AgentLockBusy,
    AtomicAgentsError,
    CostGuardrailBlocked,
    HelperBatchPartialFailure,
    NestedDelegationRefused,
    NotInRoster,
    PathTraversalError,
    SelfDelegationError,
    ToolNotRegistered,
)
from .tools import (
    DEFAULT_MAX_TOOL_ITERATIONS,
    MAX_TOOL_ITERATIONS,
    ToolCallResult,
    ToolDefinition,
    ToolRegistry,
)
from .skills import SkillManifest, discover_skills, load_skill_body, load_skill_referenced_file
from .types import (
    AgentConfig,
    Capture,
    CostCheckResult,
    HelperResult,
    Response,
)


PINNED_MAX = 5
RECENT_NOTES_DEFAULT = 5
RECENT_JOURNAL_DEFAULT = 1


def _canonicalize_tool_loop(raw_tool_uses, tool_results):
    """Translate provider-shape ``raw.tool_uses`` + framework ``ToolCallResult``
    list into canonical ``LLMToolUse`` + ``LLMToolResult`` lists.

    Lifted out of ``_build_tool_loop_messages`` (#87 PR 2.5 review Finding 8)
    so PR 3 can reuse it when ``OpenAICompatibleLLMBackend.format_tool_results``
    takes ownership of the OpenAI/Moonshot branch — the same translation
    happens before either backend's call.

    Content normalization: ``LLMToolResult.content`` is set to ``tr.output``
    verbatim for success cases and to a ``"[tool error] ..."`` string for
    errors. The backend's ``format_tool_results`` does any provider-
    specific serialization (json.dumps with str() fallback) so wire bytes
    stay byte-equivalent to the pre-#87 ``build_tool_result_blocks_*``
    helpers — a PR 2.5 review caught the gap empirically.
    """
    from .llm.types import LLMToolResult, LLMToolUse
    canonical_tool_uses = [
        LLMToolUse(id=tu["id"], name=tu["name"], input=tu.get("input", {}))
        for tu in raw_tool_uses
    ]
    canonical_tool_results = []
    for tr in tool_results:
        if tr.error is not None:
            canonical_tool_results.append(LLMToolResult(
                tool_use_id=tr.tool_use_id,
                content=f"[tool error] {tr.error}",
                is_error=True,
            ))
        else:
            canonical_tool_results.append(LLMToolResult(
                tool_use_id=tr.tool_use_id,
                content=tr.output,
                is_error=False,
            ))
    return canonical_tool_uses, canonical_tool_results


class AtomicAgent:
    """The main agent runtime.

    Responsible for:
    - Loading agent files in canonical order (per spec/04)
    - Calling the LLM with cost-guardrail enforcement
    - Extracting and writing captures (helper-mediated, atomic)
    - Logging every run to log/YYYY-MM/YYYY-MM-DD.jsonl
    - Helper calls (sequential and parallel) per spec/10
    """

    def __init__(
        self,
        name: str,
        trigger: str = "manual",
        agents_root: Path | None = None,
        run_id: str | None = None,
        tools: ToolRegistry | None = None,
        max_tool_iterations: int = DEFAULT_MAX_TOOL_ITERATIONS,
    ):
        self.name = name
        self.trigger = trigger
        self.agents_root = agents_root or get_agents_root()
        self.agent_root = self.agents_root / name
        self.run_id = run_id or self._generate_run_id()
        # Custom tool registry (spec/17). Empty registry = no custom tools.
        self.tool_registry = tools if tools is not None else ToolRegistry()
        # Bound the multi-turn tool loop. Clamped to [1, MAX_TOOL_ITERATIONS].
        self.max_tool_iterations = max(1, min(max_tool_iterations, MAX_TOOL_ITERATIONS))

        if not self.agent_root.exists():
            raise AtomicAgentsError(
                f"Agent folder not found: {self.agent_root}. "
                f"Set ATOMIC_AGENTS_ROOT env var or create the agent."
            )

        # Cascade detection — None for single-agent layouts (load behaves as before),
        # populated for paths shaped <system>/projects/<project>/agents/<role>/.
        self.cascade: _cascade.CascadePaths | None = _cascade.detect_cascade(self.agent_root)

        # Skills (spec/18) — discover at init so metadata is available for
        # system-prompt assembly. Empty list when no skills/ directory exists.
        self.skills: list[SkillManifest] = discover_skills(self.agent_root)

        # Register built-in skill tools if any skills were discovered.
        # This runs after tool_registry is created but before _load_config
        # so operators can still register their own tools on the same registry.
        if self.skills:
            self._register_skill_tools()

        # Per-call helper-provenance rollup (spec/13 Layer 3). Reset at the
        # start of each call(); appended to by helper_call(). Empty list
        # means either no helpers ran or the call started outside call().
        self._helpers_this_run: list[dict] = []

        # Per-call delegation rollup. Reset at the start of each call();
        # appended to by delegate(). Embedded in the parent's run log record
        # as `delegations: [...]` when non-empty.
        self._delegations_this_run: list[dict] = []

        # Cumulative cost of all delegate() calls made during the current run.
        # Reset at the start of each call(). Passed as extra_in_flight_cost_usd
        # to _check_cost_guardrails before each delegation so sequential
        # delegations correctly see prior delegated spend. (fix R2-A2)
        self._delegated_cost_this_run: float = 0.0

        # MCP client pool (spec/19). Lazy-initialized on first call() when
        # mcp_servers are declared. Torn down in call()'s finally block.
        # None means either no MCP servers declared or pool not yet initialized.
        self.mcp_pool: MCPClientPool | None = None

        # Judge layer (#112 PR 2a + 2b + 3a). PolicyJudge + LLMJudgeBackend
        # instances are lazy-built on first dispatch — cached per-agent
        # so policy_version is stable within a run. ``_llm_judge`` may
        # remain ``None`` when no LLM key is configured for the default
        # judge model (PR 2b: ``gpt-5-nano``); the ensemble runs
        # PolicyJudge only in that case. ``tool_classifications`` parsed
        # from tools.md ``## Tool classification`` section; empty dict
        # otherwise (everything defaults to external_side_effect in
        # ``_resolve_classification``).
        #
        # ``self.judges_config`` is the parsed ``judges.md`` operator
        # config (PR 3a). ``None`` when judges.md is absent — the
        # judge dispatch falls back to ``_default_class_policy_snapshot``
        # (PR 2a/2b's hardcoded JUDGE_REQUIRED defaults). When present,
        # ``_dispatch_with_judge`` uses parsed class_policy +
        # per-class failure_policy + timeout + budget configuration.
        self._policy_judge = None
        self._llm_judge = None  # type: ignore[assignment]
        self._llm_judge_constructed = False  # distinct from None-cache
        self._tool_classifications: dict[str, str] = {}
        self.judges_config = None  # type: ignore[assignment]

        # Loaded later via load() — populated in __init__ for clarity
        self._persona_text: str = ""
        self._tools_text: str = ""
        self._memory_index_text: str = ""
        self._wiki_index_text: str = ""
        self._pinned_notes: list[str] = []
        self._recent_notes: list[str] = []
        self._recent_journal: list[str] = []
        # Cascade-only sections (empty when not cascaded)
        self._role_prompt_text: str = ""
        self._project_canon_text: str = ""
        self._project_style_guide_text: str = ""
        self._project_goal_text: str = ""
        self._project_policy_text: str = ""
        # Single-agent goal context (per spec/04 step 3.5; empty for cascaded agents)
        self._goal_text: str = ""

        # Agent operating mode (reactive / goal-driven / hybrid), parsed from
        # IDENTITY.md at init time. Defaults to "reactive" if no IDENTITY.md or
        # no Operating-mode section.
        identity_path = self.agent_root / "persona" / "IDENTITY.md"
        self.agent_mode: str = parse_agent_mode(identity_path)

        # Parse config files
        self.config = self._load_config()

        # Memory backend (spec/20 — routes all memory I/O through the protocol)
        self.memory: FilesystemBackend = FilesystemBackend(
            agent_root=self.agent_root,
            memory_subdir="memory",
        )

    def _register_skill_tools(self) -> None:
        """Register load_skill and load_skill_file as built-in tools in the registry.

        Called once during __init__ when skills are present. Handlers close over
        ``self.skills`` so they work with the skills discovered at init time.
        """
        # Build lookup index by name for fast handler access
        skill_index: dict[str, SkillManifest] = {m.name: m for m in self.skills}
        skill_names = sorted(skill_index.keys())

        def _handle_load_skill(inp: dict) -> str:
            skill_name = inp.get("skill_name", "")
            manifest = skill_index.get(skill_name)
            if manifest is None:
                from .exceptions import ToolHandlerError
                raise ToolHandlerError(
                    f"Unknown skill {skill_name!r}. "
                    f"Available skills: {skill_names}"
                )
            return load_skill_body(manifest)

        def _handle_load_skill_file(inp: dict) -> str:
            skill_name = inp.get("skill_name", "")
            relative_path = inp.get("relative_path", "")
            manifest = skill_index.get(skill_name)
            if manifest is None:
                from .exceptions import ToolHandlerError
                raise ToolHandlerError(
                    f"Unknown skill {skill_name!r}. "
                    f"Available skills: {skill_names}"
                )
            return load_skill_referenced_file(manifest, relative_path)

        self.tool_registry.register(ToolDefinition(
            name="load_skill",
            description=(
                "Loads the full instructions for a skill by name. "
                "Use this when a skill listed in the system prompt is relevant "
                "to the current task and you need its detailed guidance."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "description": "Name of the skill to load (as listed in Available skills).",
                    }
                },
                "required": ["skill_name"],
            },
            handler=_handle_load_skill,
        ))

        self.tool_registry.register(ToolDefinition(
            name="load_skill_file",
            description=(
                "Loads a supporting file referenced by a skill (one level deep from "
                "the skill's SKILL.md). Use after calling load_skill when you need "
                "extended reference material that was not included in the main body."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "description": "Name of the skill that owns the file.",
                    },
                    "relative_path": {
                        "type": "string",
                        "description": (
                            "Path to the file relative to the skill directory "
                            "(e.g. 'reference.md', 'examples.md'). "
                            "Must be one level deep — no subdirectory traversal."
                        ),
                    },
                },
                "required": ["skill_name", "relative_path"],
            },
            handler=_handle_load_skill_file,
        ))

    @staticmethod
    def _generate_run_id() -> str:
        return f"run-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}"

    @staticmethod
    def _capture_tool_definitions(model: str) -> "list[LLMToolDefinition] | None":
        """Return the atomic_capture tool definition as canonical ``LLMToolDefinition``.

        Returns None for providers without tool-call support — the agent then
        falls back to Path 2 fenced-block parsing only. Today every supported
        provider (Anthropic, OpenAI, Moonshot) has tool calls, so the only
        None path is for unrecognized model prefixes.

        Returns a single-element ``list[LLMToolDefinition]``. Backends
        translate canonical → provider format inside their ``call()`` —
        the agent layer no longer branches on ``model.startswith`` (the
        codex P1 fix from the #87 LLMBackend plan, landed in PR 2.5).
        """
        if (model.startswith("claude-")
                or model.startswith("gpt-")
                or model.startswith("moonshot/")):
            return [_capture.canonical_tool_definition()]
        return None

    def _all_tool_definitions(self, model: str) -> "list[LLMToolDefinition] | None":
        """Return all tool definitions (atomic_capture + custom tools) as canonical.

        Includes:
        - atomic_capture (built-in, always included for supported providers)
        - All tools registered in self.tool_registry (operator-supplied)

        Returns ``list[LLMToolDefinition]`` ready to hand to any backend's
        ``call()`` (the backend translates to provider format internally).
        Returns None for providers without tool-call support — the agent
        then falls back to Path 2 fenced-block parsing only.
        """
        if (model.startswith("claude-")
                or model.startswith("gpt-")
                or model.startswith("moonshot/")):
            defs = [_capture.canonical_tool_definition()]
            defs.extend(self.tool_registry.to_canonical_definitions())
            # Judge layer side-channel marker (spec/28 + #112 PR 2a).
            # Always included alongside atomic_capture for supported
            # providers. The actor emits atomic_action in the same turn
            # as any side-effectful tool call to justify it; if the
            # judge layer is disabled (no judges.md, no env var) the
            # markers are silently ignored at proposal-assembly time.
            from .judge.atomic_action import canonical_tool_definition as _action_def
            defs.append(_action_def())
            return defs
        return None

    # ────────────────────────────────────────────────────────────
    # Judge layer (#112 PR 2a — opt-in dispatch)

    def _judge_enabled(self) -> bool:
        """Return True when the judge layer should run for this agent.

        Per CLAUDE.md rule #14 (backward compatibility by default), the
        judge dispatch is opt-in. Existing v0.13.0 deployments that
        pip-upgrade to this version see today's behavior unchanged
        unless either signal below is set.

        Two signals enable dispatch:

        1. ``judges.md`` exists in ``agent_root``. PR 2a treats mere
           presence as opt-in; PR 3's parser layers on operator
           configuration. Operators authoring the file in PR 2a get
           PolicyJudge coverage with framework defaults.
        2. ``AGENT_JUDGE_ENABLED`` environment variable is truthy.
           Escape hatch for experiments / smoke tests before
           authoring a judges.md.
        """
        if os.environ.get("AGENT_JUDGE_ENABLED", "").strip().lower() in (
            "1", "true", "yes", "on",
        ):
            return True
        if (self.agent_root / "judges.md").exists():
            return True
        # Inherited project-floor judges.md counts as opt-in (Codex
        # round-2 P1) — without this check, a cascade project floor
        # would parse but the gate would keep dispatch off, leaving
        # the floor unenforced unless every delegate also authors its
        # own judges.md.
        if getattr(self, "judges_config", None) is not None:
            return True
        return False

    def _resolve_classification(self, tool_name: str) -> tuple[str, str]:
        """Look up the per-tool ``ActionClass`` value for proposal
        assembly. Returns ``(class_value, classification_source)``.

        Lookup order:

        1. ``ToolDefinition.classification`` on the registered tool
           (set in code when the tool was registered). Source =
           ``"tools.py"``.
        2. ``self._tool_classifications`` from
           ``tools.md ## Tool classification`` section. Source =
           ``"tools.md"``.
        3. Default to ``"external_side_effect"`` per spec/28's safe
           default. Source = ``"default_unknown"``.

        Returns the string value (not a typed enum) so the caller —
        which is in agent.py — doesn't introduce a runtime dependency
        on the judge module beyond what it already has.
        """
        registered = self.tool_registry.get(tool_name)
        if registered is not None and registered.classification:
            return registered.classification, "tools.py"
        mapped = self._tool_classifications.get(tool_name)
        if mapped is not None:
            return mapped, "tools.md"
        return "external_side_effect", "default_unknown"

    def _default_class_policy_snapshot(self):
        """Return the default ``ClassPolicySnapshot`` used by PR 2a
        when no ``judges.md`` parser is available.

        Every class defaults to ``JUDGE_REQUIRED`` — judge runs and
        enforces. PR 3's ``judges.md`` parser reads operator overrides
        per class.
        """
        from .judge.types import ClassPolicySnapshot, ClassPolicyValue
        return ClassPolicySnapshot(
            read_only=ClassPolicyValue.JUDGE_REQUIRED,
            reversible_write=ClassPolicyValue.JUDGE_REQUIRED,
            external_side_effect=ClassPolicyValue.JUDGE_REQUIRED,
            high_risk=ClassPolicyValue.JUDGE_REQUIRED,
            source={
                "read_only": "default",
                "reversible_write": "default",
                "external_side_effect": "default",
                "high_risk": "default",
            },
        )

    def _ensure_policy_judge(self):
        """Lazy-construct and cache the default ``PolicyJudge`` for
        this agent. Returns the cached instance on subsequent calls.

        PolicyJudge is registered against this agent's tools.md
        (write paths + read-only paths + the raw text for
        policy_version computation).
        """
        if self._policy_judge is not None:
            return self._policy_judge
        from .judge.rules import make_default_policy_judge
        # Read the tools.md content for policy_version derivation.
        # Use the resolved tools.md (cascade-aware) when available; fall
        # back to the agent_root file otherwise.
        tools_md_text = ""
        if self.cascade:
            _, tools_md_text = _cascade.resolve_tools_md(self.cascade)
        else:
            tools_md_path = self.agent_root / "tools.md"
            if tools_md_path.exists():
                tools_md_text = tools_md_path.read_text(encoding="utf-8")
        self._policy_judge = make_default_policy_judge(
            tools_md_text=tools_md_text,
            allowed_write_paths=[Path(p) for p in (self.config.write_paths or [])],
            read_only_paths=[Path(p) for p in (self.config.read_only_paths or [])],
        )
        return self._policy_judge

    def _ensure_llm_judge(self):
        """Lazy-construct and cache the default ``LLMJudgeBackend`` for
        this agent (#112 PR 2b). Returns the cached instance or ``None``
        when the LLM backend isn't reachable (no key for the default
        judge model).

        Per spec/28's correlated-judgment mitigation, the default model
        is ``gpt-5-nano`` (OpenAI) so the judge family differs from the
        default Anthropic actor. Operators in Claude-only deployments
        get ``None`` here — the ensemble runs PolicyJudge only.

        Distinct from ``_ensure_policy_judge`` because ``None`` is a
        legitimate cached result (no key configured). Tracks
        construction state via ``_llm_judge_constructed`` so a
        ``None`` cache doesn't re-attempt on every dispatch.
        """
        if self._llm_judge_constructed:
            return self._llm_judge
        self._llm_judge_constructed = True
        from .judge.llm import make_default_llm_judge
        # Read tools.md text for policy_version derivation (cascade-aware).
        tools_md_text = ""
        if self.cascade:
            _, tools_md_text = _cascade.resolve_tools_md(self.cascade)
        else:
            tools_md_path = self.agent_root / "tools.md"
            if tools_md_path.exists():
                tools_md_text = tools_md_path.read_text(encoding="utf-8")
        # judges.md text is None in PR 2b (parser lands in PR 3) —
        # compute_policy_version writes ``judges.md@sha256:absent``.
        self._llm_judge = make_default_llm_judge(
            tools_md_text=tools_md_text,
            judges_md_text=None,
        )
        return self._llm_judge

    # ────────────────────────────────────────────────────────────
    # Escalation polling (#112 PR 3b)

    def poll_escalations(self) -> list:
        """Scan the escalation queue for operator resolutions and
        execute / audit them.

        Called from the top of ``agent.call()`` once per iteration (the
        first iteration if the throttle window has elapsed). Returns
        the list of ``ResolutionEvent``s that fired this poll cycle
        (mostly useful for tests; production callers ignore the value).

        **Standalone-invocation caveat (Codex round-1 P2-1).** This
        method is public and operators may call it directly (e.g., from
        a future ``atomic-agents poll-escalations`` CLI). When invoked
        standalone (NOT via ``agent.call()``), the MCP client pool is
        NOT initialized: ``call()`` is what wires it up after the cost
        gate. If an Approved escalation's tool is an MCP tool, the
        tool registry lookup returns None and the resolution is
        recorded as ``approved_stale_tool_definition`` — safe (fail
        closed) but misleading (the tool isn't stale; the framework
        just hasn't loaded MCP yet). Wire MCP init into a standalone
        CLI before relying on Approved MCP-tool execution; see #166.

        Behavior:
        - Throttled by ``judges_config.escalation.resolution_poll_cycle_seconds``
          (default 60s). The .last-poll mtime marker lives in the
          escalation destination directory.
        - For Approved resolutions: re-verifies the tool_definition_hash
          against the current tool registry, refuses execution on
          mismatch (``approved_stale_tool_definition``), then executes
          the bound action via the loaded tool registry. Result charged
          to the actor's original ``parent_run_id`` (cost_source="actor").
        - For Denied / Redacted resolutions: audits without executing.
        - For Auto-decided resolutions: applies the operator's
          fallback_on_timeout policy.
        - For Body-tampered files: refuses execution; emits
          ``proposal_body_tampered`` audit.
        - For Revised resolutions: PR 3b treats as Denied (REVISE flow
          ships in PR 3c). The audit event records the operator's
          intent so PR 3c can surface stale-deferred revisions.
        """
        from .judge import escalation as _esc
        from .judge.backend import Judgment, JudgmentOutcome

        if self.judges_config is None:
            return []
        cfg = self.judges_config.escalation
        # Throttle: skip if recent poll happened within cycle window.
        if _esc.is_within_throttle(
            agent_root=self.agent_root,
            judges_config_escalation=cfg,
        ):
            return []
        try:
            events = _esc.poll_resolutions(
                agent_root=self.agent_root,
                judges_config_escalation=cfg,
                log_warning=lambda msg: _logger.warning(
                    "agent %r poll_escalations: %s", self.name, msg
                ),
            )
        except Exception as exc:  # noqa: BLE001
            _logger.exception(
                "agent %r: poll_escalations raised; skipping cycle: %s",
                self.name, exc,
            )
            # Do NOT touch_last_poll on error (Codex round-1 P1-5).
            # Persistent failures (e.g., disk full) should not silently
            # throttle subsequent retries.
            return []
        # Only update throttle marker after a successful scan. Errors
        # leave .last-poll untouched so the next call() retries.
        _esc.touch_last_poll(
            agent_root=self.agent_root,
            judges_config_escalation=cfg,
        )

        for event in events:
            self._process_resolution_event(event)
        return events

    def _process_resolution_event(self, event) -> None:
        """Handle one ResolutionEvent: emit JSONL audit + execute if
        Approved + tool_definition_hash matches the current registry.
        """
        from .judge import escalation as _esc
        from .judge.proposal import compute_tool_definition_hash

        decision = event.decision
        proposal = event.proposal
        fm = event.frontmatter
        enforcement = event.enforcement_action

        # Re-verify tool_definition_hash for Approved cases. The
        # current tool registry may have evolved since the PENDING was
        # written (tool dropped, schema changed). Mismatch → refuse,
        # promote enforcement to approved_stale_tool_definition.
        if decision is _esc.ResolutionDecision.APPROVED:
            registered = self.tool_registry.get(proposal.tool_name)
            if registered is None:
                enforcement = "approved_stale_tool_definition"
                stale_reason = (
                    f"tool {proposal.tool_name!r} no longer registered "
                    "at execution time"
                )
            else:
                current_hash = compute_tool_definition_hash(
                    proposal.tool_name,
                    registered.input_schema,
                    registered.handler,
                )
                if current_hash != proposal.tool_definition_hash:
                    enforcement = "approved_stale_tool_definition"
                    stale_reason = (
                        "tool_definition_hash mismatch: PENDING="
                        f"{proposal.tool_definition_hash[:12]}..., "
                        f"current={current_hash[:12]}..."
                    )
                else:
                    stale_reason = None
        else:
            stale_reason = None

        # Build the RESOLVED audit record. We synthesize a Judgment
        # so the JSONL line has the standard judge-event shape.
        from .judge.backend import Judgment, JudgmentOutcome
        from .judge.types import ProposalBinding

        # P1 #1: OPERATOR_REVISED's "resolved" line is intent-recorded,
        # not execution-recorded. _process_operator_revise emits the
        # actual operator_revise_executed line AFTER the handler runs.
        # Promote enforcement to operator_revise_pending here so the
        # resolved-event line doesn't claim execution-already-happened.
        if decision is _esc.ResolutionDecision.OPERATOR_REVISED and (
            enforcement == "operator_revise_executed"
        ):
            enforcement = "operator_revise_pending"
        judgment = Judgment(
            outcome=(
                JudgmentOutcome.ALLOW
                if decision is _esc.ResolutionDecision.APPROVED
                and stale_reason is None
                else JudgmentOutcome.BLOCK
            ),
            reason=(stale_reason or event.reason or decision.value),
            judge_id=event.operator,
            policy_version=fm.policy_version,
        )
        binding = ProposalBinding(
            tool_call_id=proposal.tool_call_id,
            tool_definition_hash=proposal.tool_definition_hash,
            arguments_hash=proposal.arguments_hash,
        )
        # Pseudo tool_use payload for the event builder.
        tool_use_stub = {"name": proposal.tool_name, "id": proposal.tool_call_id}
        record = self._build_judgment_event_dict(
            proposal=proposal,
            tool_use=tool_use_stub,
            judgment=judgment,
            enforcement_action=enforcement,
            binding=binding,
        )
        record["resolved_at"] = event.resolved_at
        record["resolution_operator"] = event.operator
        record["escalation_file"] = str(event.file_path)
        record["escalation_queue_id"] = fm.proposal_id
        record["trigger"] = "escalation_resolved"
        self._log(record)

        # Execute the bound action for Approved + fresh tool. The
        # ToolCallResult is appended to a deferred_execution JSONL
        # line; no actor-loop replay happens (the original run is
        # closed). cost_source="actor" keeps the spend on the
        # proposing actor's ledger per the operator-approval-as-consent
        # discipline.
        if (
            decision is _esc.ResolutionDecision.APPROVED
            and stale_reason is None
        ):
            try:
                tool_use_payload = {
                    "name": proposal.tool_name,
                    "id": proposal.tool_call_id,
                    "input": proposal.tool_arguments,
                }
                tool_result = self.tool_registry.execute(tool_use_payload)
                self._log({
                    "trigger": "escalation_deferred_execution",
                    "parent_run_id": fm.parent_run_id,
                    "escalation_queue_id": fm.proposal_id,
                    "tool_name": tool_result.tool_name,
                    "latency_ms": tool_result.latency_ms,
                    "error": tool_result.error,
                    "cost_source": "actor",
                })
            except Exception as exc:  # noqa: BLE001
                _logger.exception(
                    "agent %r: escalation_deferred_execution failed for "
                    "proposal_id=%r: %s",
                    self.name, proposal.proposal_id, exc,
                )
                self._log({
                    "trigger": "escalation_deferred_execution",
                    "parent_run_id": fm.parent_run_id,
                    "escalation_queue_id": fm.proposal_id,
                    "tool_name": proposal.tool_name,
                    "error": f"{type(exc).__name__}: {exc}",
                    "cost_source": "actor",
                })

        # PR 3c: operator-revise execution path.
        if decision is _esc.ResolutionDecision.OPERATOR_REVISED:
            self._process_operator_revise(event)

    def _process_operator_revise(self, event) -> None:
        """Execute an operator's Revised resolution.

        Flow:
        1. amend_proposal with the operator's amendment (already
           parsed in escalation._claim_operator_resolution).
        2. validate_amended_args + enforce_amended_write_paths.
        3. Gate on **amended.classification** (Codex round-1 P1-4 —
           NOT original.classification — otherwise the operator can
           swap tool_name to upgrade reversible_write → delete_files
           and skip re-judge):
           - amended.classification == high_risk → re-judge through
             a fresh ensemble; only execute if ALLOW.
           - other classes → schema/policy validation alone is
             sufficient; execute on success.
        4. Execute via tool_registry; emit deferred_execution audit
           line with cost_source="actor".
        5. Invalid amendment → enforcement promoted to
           operator_revise_invalid_amendment via escalation.py; this
           method skips execution but still emits the audit record
           (already emitted above in the synthesized Judgment flow).
        """
        from .judge import _revise as _rv
        from .judge.types import ActionClass
        from .exceptions import JudgeAmendedProposalRejected

        amendment = event.amendment
        fm = event.frontmatter
        original = event.proposal

        if amendment is None:
            # operator_revise_invalid_amendment — audit already emitted.
            return

        try:
            amended = _rv.amend_proposal(
                original=original,
                amendment=amendment,
                tool_registry=self.tool_registry,
                tool_classifications=self._tool_classifications,
            )
            _rv.validate_amended_args(
                amended,
                self.tool_registry,
                agent_name=self.name,
                validation_mode=(
                    self.judges_config.validation
                    if self.judges_config is not None
                    else "weakened"
                ),
            )
            _rv.enforce_amended_write_paths(
                amended,
                write_paths=list(self.config.write_paths or []),
                read_only_paths=list(self.config.read_only_paths or []),
            )
        except JudgeAmendedProposalRejected as exc:
            _logger.warning(
                "agent %r: operator_revise validation failed for "
                "proposal_id=%r: %s",
                self.name, fm.proposal_id, exc,
            )
            self._log({
                "trigger": "escalation_operator_revise_invalid_amendment",
                "parent_run_id": fm.parent_run_id,
                "escalation_queue_id": fm.proposal_id,
                "tool_name": original.tool_name,
                "error": f"{type(exc).__name__}: {exc}",
            })
            return

        # Gate on AMENDED classification (Codex round-1 P1-4 fix).
        re_judged = amended.classification == ActionClass.HIGH_RISK
        if re_judged:
            # Build the same config the agent uses at first-dispatch
            # time. Reuses _dispatch_with_judge's config-resolution
            # logic by constructing a synthetic tu/markers, but we
            # skip atomic_action marker validation since the operator
            # is the trust anchor here.
            if self.judges_config is not None:
                class_policy = self.judges_config.class_policy
                timeout_ms = self.judges_config.timeout_ms
                judge_budget = self.judges_config.budget
                escalation_cfg = self.judges_config.escalation
                flat_failure_policy = dict(
                    self.judges_config.failure_policy[
                        ActionClass.EXTERNAL_SIDE_EFFECT
                    ]
                )
                backend_name = self.judges_config.default_backend
            else:
                class_policy = self._default_class_policy_snapshot()
                from .judge.types import EscalationConfig as _EC
                from .judge.types import BudgetConfig as _BC
                timeout_ms = 5000
                judge_budget = _BC()
                escalation_cfg = _EC()
                flat_failure_policy = {
                    "JudgeUnavailable": "block",
                    "JudgePolicyInvalid": "block",
                    "JudgeBudgetExhausted": "block",
                    "JudgeProposalInvalid": "block",
                    "JudgeAmendedProposalRejected": "block",
                }
                backend_name = "ensemble"

            from .judge.types import ProposalBinding

            binding = ProposalBinding(
                tool_call_id=amended.tool_call_id,
                tool_definition_hash=amended.tool_definition_hash,
                arguments_hash=amended.arguments_hash,
            )
            tu_stub = {
                "name": amended.tool_name,
                "id": amended.tool_call_id,
                "input": amended.tool_arguments,
            }
            allow, events, queue_id = self._run_ensemble(
                proposal_obj=amended,
                tu=tu_stub,
                binding=binding,
                class_policy=class_policy,
                timeout_ms=timeout_ms,
                judge_budget=judge_budget,
                escalation_cfg=escalation_cfg,
                flat_failure_policy=flat_failure_policy,
                backend_name=backend_name,
                revise_iteration=0,
                original_proposal=original,
            )
            for ev in events:
                ev["trigger"] = "escalation_operator_revise_re_judge"
                ev["escalation_queue_id"] = fm.proposal_id
                ev["original_proposal_id"] = original.proposal_id
                ev["re_judged"] = True
                # P1 #2: re-judge audit events must link to the
                # ORIGINAL actor's run, not the poller's. cost_source
                # discipline + forensic chains break if these get
                # bound to whichever agent.call() happened to fire
                # the poll.
                ev["parent_run_id"] = fm.parent_run_id
                self._log(ev)
            if not allow:
                _logger.info(
                    "agent %r: operator_revise high_risk re-judge BLOCKed "
                    "for proposal_id=%r (queue_id=%r); refusing execution",
                    self.name, original.proposal_id, fm.proposal_id,
                )
                return
        else:
            re_judged = False

        # Execute the amended bound action.
        try:
            tool_use_payload = {
                "name": amended.tool_name,
                "id": amended.tool_call_id,
                "input": amended.tool_arguments,
            }
            tool_result = self.tool_registry.execute(tool_use_payload)
            self._log({
                "trigger": "escalation_operator_revise_executed",
                "parent_run_id": fm.parent_run_id,
                "escalation_queue_id": fm.proposal_id,
                "original_proposal_id": original.proposal_id,
                "amended_proposal_id": amended.proposal_id,
                "tool_name": tool_result.tool_name,
                "latency_ms": tool_result.latency_ms,
                "error": tool_result.error,
                "re_judged": re_judged,
                "cost_source": "actor",
            })
        except Exception as exc:  # noqa: BLE001
            _logger.exception(
                "agent %r: operator_revise execution failed for "
                "proposal_id=%r: %s",
                self.name, original.proposal_id, exc,
            )
            self._log({
                "trigger": "escalation_operator_revise_executed",
                "parent_run_id": fm.parent_run_id,
                "escalation_queue_id": fm.proposal_id,
                "original_proposal_id": original.proposal_id,
                "amended_proposal_id": amended.proposal_id,
                "tool_name": amended.tool_name,
                "error": f"{type(exc).__name__}: {exc}",
                "re_judged": re_judged,
                "cost_source": "actor",
            })

    def _dispatch_with_judge(
        self,
        tu: dict,
        atomic_action_markers: dict[str, dict],
    ):
        """Run the judge ensemble against one tool_use. Returns
        ``(allow: bool, events: list[dict])`` — one JudgmentEvent
        record per invoked judge (the caller logs each verbatim per
        spec/28 §"Audit shape").

        Spec/28 §"Where the judge sits in agent.call()" places this
        between LLM tool_use parsing and tool handler dispatch.

        PR 2b ensemble: ``PolicyJudge`` (rule engine) first; if
        ALLOW, then ``LLMJudgeBackend`` if registered. First BLOCK in
        the ensemble short-circuits remaining judges (spec/28:694
        "If it blocks, the ensemble blocks — no LLM cost incurred").
        PR 3 reads ordering from ``judges.md``.
        """
        from .judge import proposal as _proposal_mod
        from .judge.backend import JudgmentOutcome
        from .judge.types import (
            ActionClass,
            JudgmentContext,
            JudgePolicyContext,
            JudgeRuntimeConfig,
            BudgetConfig,
            EscalationConfig,
            PersonaDigest,
            ProposalBinding,
            ToolPolicyEntry,
        )
        from .exceptions import JudgeError, JudgeProposalInvalid

        tool_name = tu.get("name", "")
        tool_call_id = tu.get("id", "")
        cls_str, cls_source = self._resolve_classification(tool_name)
        try:
            classification = ActionClass(cls_str)
        except ValueError:
            classification = ActionClass.EXTERNAL_SIDE_EFFECT
            cls_source = "default_unknown"

        # Resolve the side-channel marker, if any.
        marker = atomic_action_markers.get(tool_call_id)

        # Resolve handler / tool_definition_hash inputs.
        registered = self.tool_registry.get(tool_name)
        input_schema = registered.input_schema if registered else {}
        handler = registered.handler if registered else None
        tdef_hash = _proposal_mod.compute_tool_definition_hash(
            tool_name,
            input_schema,
            handler,
        )

        # Build the proposal. JudgeProposalInvalid bubbles to BLOCK via
        # the failure-policy default (spec/28:567).
        try:
            proposal_obj = _proposal_mod.assemble_proposal(
                tu,
                marker,
                classification=classification,
                classification_source=cls_source,
                tool_definition_hash=tdef_hash,
                actor_agent=self.name,
                actor_run_id=self.run_id,
                actor_model_id=getattr(self.config, "model", None),
            )
        except JudgeProposalInvalid as exc:
            # Fail-closed per spec/28. Synthesize a BLOCK judgment and
            # a JudgmentEvent so the audit trail records the refusal.
            # Single-event return — proposal-assembly failure means no
            # ensemble judges ran.
            from .judge.backend import Judgment
            judgment = Judgment(
                outcome=JudgmentOutcome.BLOCK,
                reason=f"JudgeProposalInvalid: {exc}",
                judge_id="framework",
                policy_version="unimplemented",
            )
            event = self._build_judgment_event_dict(
                proposal=None,
                tool_use=tu,
                judgment=judgment,
                enforcement_action="block_executed",
                binding=ProposalBinding(
                    tool_call_id=tool_call_id,
                    tool_definition_hash=tdef_hash,
                    # Empty sentinel — proposal-assembly failed so no
                    # canonical args hash exists. ProposalBinding's
                    # str-typed field carries an empty string rather
                    # than a non-hex string per round-2 review (the
                    # "proposal_assembly_failed" string violated the
                    # implicit sha256-hex contract that audit-log
                    # tooling relies on). Failure reason lives in
                    # ``judgment.reason`` and on ``event["judgment_reason"]``.
                    arguments_hash="",
                ),
            )
            return False, [event], None

        # Build the JudgmentContext once; both judges see the same
        # context per spec/28 idempotency invariants. When judges.md
        # was parsed at load time (PR 3a), use its values; otherwise
        # fall back to the PR 2a hardcoded defaults so existing
        # deployments without judges.md keep working.
        if self.judges_config is not None:
            class_policy = self.judges_config.class_policy
            timeout_ms = self.judges_config.timeout_ms
            judge_budget = self.judges_config.budget
            escalation_cfg = self.judges_config.escalation
            # JudgeRuntimeConfig.failure_policy stays a flat
            # ``dict[str, str]`` per its existing Protocol-side
            # consumer; per-class lookups happen on
            # ``JudgesConfig.failure_policy_for`` separately. For the
            # context payload, expose the external_side_effect
            # bucket (most-common class) so any backend reading the
            # flat field gets a reasonable default. Per-class
            # enforcement lives in agent.py post-judge.
            flat_failure_policy = dict(
                self.judges_config.failure_policy[ActionClass.EXTERNAL_SIDE_EFFECT]
            )
            backend_name = self.judges_config.default_backend
        else:
            class_policy = self._default_class_policy_snapshot()
            timeout_ms = 5000
            judge_budget = BudgetConfig()
            escalation_cfg = EscalationConfig()
            flat_failure_policy = {
                "JudgeUnavailable": "block",
                "JudgePolicyInvalid": "block",
                "JudgeBudgetExhausted": "block",
                "JudgeProposalInvalid": "block",
                "JudgeAmendedProposalRejected": "block",
            }
            backend_name = "ensemble"

        # Build the initial JudgmentContext + Binding. PR 3c factors
        # the ensemble loop into ``_run_ensemble`` so REVISE outcomes
        # can recurse against the amended proposal with a fresh
        # context derived from the amended classification / tool name.
        binding = ProposalBinding(
            tool_call_id=tool_call_id,
            tool_definition_hash=tdef_hash,
            arguments_hash=proposal_obj.arguments_hash,
        )

        return self._run_ensemble(
            proposal_obj=proposal_obj,
            tu=tu,
            binding=binding,
            class_policy=class_policy,
            timeout_ms=timeout_ms,
            judge_budget=judge_budget,
            escalation_cfg=escalation_cfg,
            flat_failure_policy=flat_failure_policy,
            backend_name=backend_name,
            revise_iteration=0,
            original_proposal=None,
        )

    def _run_ensemble(
        self,
        *,
        proposal_obj,
        tu: dict,
        binding,
        class_policy,
        timeout_ms: int,
        judge_budget,
        escalation_cfg,
        flat_failure_policy: dict,
        backend_name: str,
        revise_iteration: int = 0,
        original_proposal=None,
    ):
        """Run the judge ensemble against one ``ActionProposal``.

        Factored out of ``_dispatch_with_judge`` in PR 3c so the REVISE
        branch can recurse against an amended proposal with a fresh
        context (CLASS NON-DOWNGRADE BY EXPLOIT defense — the second
        judgment's effective_class_policy is computed from the AMENDED
        classification, not the original).

        ``revise_iteration`` is the spec/28:276 ``max_revise_iterations``
        bound: 0 for the first judgment, 1 for the second. The third
        judgment is impossible by construction — when iteration ≥ 1
        and a judge returns REVISE, the framework BLOCKs with reason
        ``revise_loop_exhausted_blocked``.

        ``original_proposal`` is non-None on the second judgment.
        Carried into audit events as ``original_proposal`` for
        forensic linkage; the audit consumer sees both proposals
        inline plus the amendment yields.
        """
        from .judge import proposal as _proposal_mod  # noqa: F401
        from .judge.backend import JudgmentOutcome, Judgment
        from .judge.types import (
            ActionClass,
            JudgmentContext,
            JudgePolicyContext,
            JudgeRuntimeConfig,
            PersonaDigest,
            ProposalBinding,
            ToolPolicyEntry,
        )
        from .exceptions import JudgeError, JudgeAmendedProposalRejected
        from .judge.types import ClassPolicyValue as _CPV

        tool_name = proposal_obj.tool_name
        tool_call_id = proposal_obj.tool_call_id
        classification = proposal_obj.classification

        policy_ctx = JudgePolicyContext(
            agent_name=self.name,
            persona_digest=PersonaDigest(agent_name=self.name),
            tools_md_entry=ToolPolicyEntry(
                tool_name=tool_name,
                classification=classification,
                write_paths=list(self.config.write_paths or []),
            ),
            class_policy=class_policy,
        )
        runtime_cfg = JudgeRuntimeConfig(
            backend_name=backend_name,
            timeout_ms=timeout_ms,
            budget=judge_budget,
            escalation_config=escalation_cfg,
            failure_policy=flat_failure_policy,
        )
        context = JudgmentContext(policy=policy_ctx, runtime=runtime_cfg)

        # Class-policy short-circuits (PR 3a — Codex round-2 P2 fix).
        # Operators who set class_policy.<X>: bypass don't want the
        # ensemble to run at all (LLM judge would incur cost for a
        # class the operator declared safe). Operators who set
        # allow_with_audit want every judge's decision recorded but
        # never enforced. Both short-circuit BEFORE building the
        # ensemble loop.
        from .judge.types import ClassPolicyValue as _CPV
        effective_class_policy = {
            ActionClass.READ_ONLY: class_policy.read_only,
            ActionClass.REVERSIBLE_WRITE: class_policy.reversible_write,
            ActionClass.EXTERNAL_SIDE_EFFECT: class_policy.external_side_effect,
            ActionClass.HIGH_RISK: class_policy.high_risk,
        }[classification]

        if effective_class_policy == _CPV.ESCALATE:
            # PR 3b: synthesize ESCALATE from operator class_policy.
            # No judge ensemble runs — the operator's class_policy is
            # itself the decision. Write PENDING + signal defer.
            from .judge.backend import Judgment
            from .judge import escalation as _esc
            escalate_judgment = Judgment(
                outcome=JudgmentOutcome.ESCALATE,
                reason=(
                    f"class_policy.{classification.value} = escalate — "
                    "operator-configured pre-action gate"
                ),
                judge_id="framework",
                policy_version=(
                    self.judges_config.judges_md_hash
                    if self.judges_config is not None
                    else "unimplemented"
                ),
            )
            _pending_path, queue_id = _esc.write_pending_escalation(
                proposal=proposal_obj,
                judgment_reason=escalate_judgment.reason,
                judge_id="framework",
                agent_root=self.agent_root,
                agent_name=self.name,
                parent_run_id=self.run_id,
                policy_version=escalate_judgment.policy_version,
                judges_config_escalation=escalation_cfg,
                synthesis_source="class_policy",
            )
            escalate_judgment = Judgment(
                outcome=JudgmentOutcome.ESCALATE,
                reason=escalate_judgment.reason,
                judge_id="framework",
                policy_version=escalate_judgment.policy_version,
                escalation_queue_id=queue_id,
            )
            event = self._build_judgment_event_dict(
                proposal=proposal_obj,
                tool_use=tu,
                judgment=escalate_judgment,
                enforcement_action="escalate_pending",
                binding=binding,
                synthesis_source="class_policy",
            )
            return False, [event], queue_id

        if effective_class_policy == _CPV.BYPASS:
            # Synthesize a single bypass-recording event. No judge
            # ensemble runs. Tool executes immediately.
            from .judge.backend import Judgment
            bypass_judgment = Judgment(
                outcome=JudgmentOutcome.ALLOW,
                reason=(
                    f"class_policy.{classification.value} = bypass — "
                    "judge ensemble not invoked"
                ),
                judge_id="framework",
                policy_version=(
                    self.judges_config.judges_md_hash
                    if self.judges_config is not None
                    else "unimplemented"
                ),
            )
            bypass_event = self._build_judgment_event_dict(
                proposal=proposal_obj,
                tool_use=tu,
                judgment=bypass_judgment,
                enforcement_action="allow_executed",
                binding=binding,
            )
            return True, [bypass_event], None

        # Build the judge ensemble. PR 2b: PolicyJudge first
        # (microsecond latency, free), then LLMJudgeBackend if
        # available. First BLOCK short-circuits remaining judges per
        # spec/28:694. ``allow_with_audit`` mode runs the ensemble
        # but does not let BLOCKs gate execution (audit-only).
        audit_mode = effective_class_policy == _CPV.ALLOW_WITH_AUDIT
        judges = [self._ensure_policy_judge()]
        llm_judge = self._ensure_llm_judge()
        if llm_judge is not None:
            judges.append(llm_judge)

        # Helper for failure_policy lookup. When judges.md was parsed,
        # consult its per-class-per-exception map; otherwise fall back
        # to the spec/28 default ("block" for every exception). Codex
        # round-2 P2: unconditional BLOCK on JudgeError ignored
        # operator failure_policy configuration entirely.
        def _outcome_for_failure(exception_name: str) -> JudgmentOutcome:
            if self.judges_config is not None:
                raw = self.judges_config.failure_policy_for(classification, exception_name)
            else:
                raw = "block"
            try:
                return JudgmentOutcome(raw)
            except ValueError:
                return JudgmentOutcome.BLOCK

        events: list[dict] = []
        final_allow = True
        escalation_queue_id: str | None = None
        for judge in judges:
            start = time.time()
            failure_synthesis: str | None = None
            triggered_by_failure: str | None = None
            try:
                judgment = judge.evaluate(proposal_obj, context)
            except JudgeError as exc:
                # Map exception via per-class failure_policy. Operator
                # may configure "allow" / "block" / "escalate" per-class
                # per-exception. PR 3b: "escalate" outcome now produces
                # a real PENDING file + deferred Response.
                from .judge.backend import Judgment
                outcome = _outcome_for_failure(type(exc).__name__)
                if outcome == JudgmentOutcome.ESCALATE:
                    failure_synthesis = "failure_policy"
                    triggered_by_failure = f"failure_policy:{type(exc).__name__}"
                judgment = Judgment(
                    outcome=outcome,
                    reason=f"{type(exc).__name__}: {exc}",
                    judge_id=judge.judge_id,
                    policy_version=judge.policy_version,
                    latency_ms=int((time.time() - start) * 1000),
                )

            # REVISE handling (PR 3c). The judge proposes an amendment;
            # the framework builds an amended proposal, re-validates,
            # and re-runs the ensemble (max_revise_iterations=1 per
            # spec/28:276). Audit_mode skips REVISE — operators in
            # audit-only mode see the judge's intent but no execution
            # path takes effect.
            if judgment.outcome == JudgmentOutcome.REVISE and not audit_mode:
                if revise_iteration >= 1:
                    # Spec/28:276 — second judgment must return ALLOW;
                    # REVISE again is the loop-exhausted case.
                    block_judgment = Judgment(
                        outcome=JudgmentOutcome.BLOCK,
                        reason="revise_loop_exhausted: second judgment "
                        "returned REVISE; max_revise_iterations=1",
                        judge_id=judgment.judge_id,
                        policy_version=judgment.policy_version,
                        latency_ms=judgment.latency_ms,
                    )
                    event = self._build_judgment_event_dict(
                        proposal=proposal_obj,
                        tool_use=tu,
                        judgment=block_judgment,
                        enforcement_action="revise_loop_exhausted_blocked",
                        binding=binding,
                    )
                    event["original_proposal_id"] = (
                        original_proposal.proposal_id
                        if original_proposal is not None
                        else None
                    )
                    event["revise_iteration"] = revise_iteration
                    events.append(event)
                    final_allow = False
                    break
                # First REVISE — build amended proposal + recurse.
                from .judge import _revise as _rv

                amendment = judgment.amendment
                if amendment is None:
                    # Judge advertised REVISE outcome but returned no
                    # amendment payload. Treat as invalid_amendment.
                    block_judgment = Judgment(
                        outcome=JudgmentOutcome.BLOCK,
                        reason="revise_invalid_amendment: judge returned "
                        "REVISE but Judgment.amendment is None",
                        judge_id=judgment.judge_id,
                        policy_version=judgment.policy_version,
                        latency_ms=judgment.latency_ms,
                    )
                    event = self._build_judgment_event_dict(
                        proposal=proposal_obj,
                        tool_use=tu,
                        judgment=block_judgment,
                        enforcement_action="revise_invalid_amendment",
                        binding=binding,
                    )
                    events.append(event)
                    final_allow = False
                    break
                try:
                    amended = _rv.amend_proposal(
                        original=proposal_obj,
                        amendment=amendment,
                        tool_registry=self.tool_registry,
                        tool_classifications=self._tool_classifications,
                    )
                    _rv.validate_amended_args(
                        amended,
                        self.tool_registry,
                        agent_name=self.name,
                        validation_mode=(
                            self.judges_config.validation
                            if self.judges_config is not None
                            else "weakened"
                        ),
                    )
                    _rv.enforce_amended_write_paths(
                        amended,
                        write_paths=list(self.config.write_paths or []),
                        read_only_paths=list(self.config.read_only_paths or []),
                    )
                except JudgeAmendedProposalRejected as exc:
                    block_judgment = Judgment(
                        outcome=JudgmentOutcome.BLOCK,
                        reason=f"revise_invalid_amendment: {exc}",
                        judge_id=judgment.judge_id,
                        policy_version=judgment.policy_version,
                        latency_ms=judgment.latency_ms,
                    )
                    event = self._build_judgment_event_dict(
                        proposal=proposal_obj,
                        tool_use=tu,
                        judgment=block_judgment,
                        enforcement_action="revise_invalid_amendment",
                        binding=binding,
                    )
                    events.append(event)
                    final_allow = False
                    break
                # Audit the first-judgment REVISE outcome before
                # recursing. Enforcement = revise_pending_second_judgment
                # so the audit shape distinguishes "judge wanted to
                # revise" from "framework executed the revision".
                first_event = self._build_judgment_event_dict(
                    proposal=proposal_obj,
                    tool_use=tu,
                    judgment=judgment,
                    enforcement_action="revise_pending_second_judgment",
                    binding=binding,
                )
                first_event["revise_iteration"] = revise_iteration
                first_event["amendment"] = {
                    "judge_note": amendment.judge_note,
                    "tool_name": amendment.tool_name,
                    "tool_arguments": amendment.tool_arguments,
                }
                events.append(first_event)
                # Recurse against the amended proposal. Fresh binding
                # reflects amended hashes; class-policy + ensemble
                # re-evaluate from amended classification.
                amended_binding = ProposalBinding(
                    tool_call_id=amended.tool_call_id,
                    tool_definition_hash=amended.tool_definition_hash,
                    arguments_hash=amended.arguments_hash,
                )
                allow2, events2, queue2 = self._run_ensemble(
                    proposal_obj=amended,
                    tu=tu,
                    binding=amended_binding,
                    class_policy=class_policy,
                    timeout_ms=timeout_ms,
                    judge_budget=judge_budget,
                    escalation_cfg=escalation_cfg,
                    flat_failure_policy=flat_failure_policy,
                    backend_name=backend_name,
                    revise_iteration=1,
                    original_proposal=proposal_obj,
                )
                # Tag every second-judgment event with the linkage.
                for ev in events2:
                    ev["revise_iteration"] = 1
                    ev["original_proposal_id"] = proposal_obj.proposal_id
                # Promote the last event to revise_executed when the
                # second judgment ALLOWed (the action that actually
                # ran is the amended one).
                if allow2 and events2:
                    events2[-1]["enforcement_action"] = "revise_executed"
                events.extend(events2)
                return allow2, events, queue2

            # ESCALATE handling: a real judge (or failure_policy
            # synthesis) returned ESCALATE. Write PENDING + emit one
            # audit event with enforcement_action=escalate_pending.
            # Ensemble short-circuits — downstream judges do not run.
            if judgment.outcome == JudgmentOutcome.ESCALATE and not audit_mode:
                from .judge import escalation as _esc
                _pending_path, queue_id = _esc.write_pending_escalation(
                    proposal=proposal_obj,
                    judgment_reason=judgment.reason,
                    judge_id=judgment.judge_id,
                    agent_root=self.agent_root,
                    agent_name=self.name,
                    parent_run_id=self.run_id,
                    policy_version=judgment.policy_version,
                    judges_config_escalation=escalation_cfg,
                    synthesis_source=failure_synthesis,
                    triggered_by=triggered_by_failure,
                    # P1 #3 (PR 3c): if this ESCALATE fires during a
                    # second judgment (revise_iteration >= 1), thread
                    # the original proposal_id into the new PENDING so
                    # forensic chains stay walkable from amended
                    # back to actor-original.
                    revised_from_proposal_id=(
                        original_proposal.proposal_id
                        if original_proposal is not None
                        else None
                    ),
                )
                # Update judgment with the queue_id we just minted.
                judgment = Judgment(
                    outcome=JudgmentOutcome.ESCALATE,
                    reason=judgment.reason,
                    judge_id=judgment.judge_id,
                    policy_version=judgment.policy_version,
                    latency_ms=judgment.latency_ms,
                    escalation_queue_id=queue_id,
                )
                event = self._build_judgment_event_dict(
                    proposal=proposal_obj,
                    tool_use=tu,
                    judgment=judgment,
                    enforcement_action="escalate_pending",
                    binding=binding,
                    synthesis_source=failure_synthesis,
                    triggered_by=triggered_by_failure,
                )
                events.append(event)
                final_allow = False
                escalation_queue_id = queue_id
                break

            judge_allow = judgment.outcome == JudgmentOutcome.ALLOW
            # audit_mode (class_policy=allow_with_audit): every event
            # is recorded with ``audit_bypass`` enforcement and the
            # action ALWAYS proceeds, regardless of judge outcome.
            # Otherwise the standard ensemble logic applies.
            if audit_mode:
                enforcement = "audit_bypass"
            else:
                enforcement = (
                    "allow_pending_next_judge" if judge_allow else "block_executed"
                )
            event = self._build_judgment_event_dict(
                proposal=proposal_obj,
                tool_use=tu,
                judgment=judgment,
                enforcement_action=enforcement,
                binding=binding,
            )
            events.append(event)

            if not judge_allow and not audit_mode:
                # First BLOCK wins; skip remaining judges (no
                # cost incurred for downstream judges). All prior
                # events keep their ``allow_pending_next_judge``
                # enforcement — they were ALLOWed by their judge but
                # the ensemble blocked.
                final_allow = False
                break

        # audit_mode: ensemble result is always ALLOW regardless of
        # individual judges' outcomes — the BLOCKs were recorded for
        # forensics but do not gate the action.
        if audit_mode:
            return True, events, None

        # Ensemble-final fixup: when the whole ensemble allowed, the
        # LAST event represents the action that actually runs — promote
        # its enforcement from ``allow_pending_next_judge`` to
        # ``allow_executed``. Intermediate ALLOWs stay
        # ``allow_pending_next_judge`` (they were judged but did not
        # gate the action). Both values are canonical in spec/28's
        # enforcement_action enum (PR 4 lock).
        if final_allow and events:
            events[-1]["enforcement_action"] = "allow_executed"

        return final_allow, events, escalation_queue_id

    def _build_judgment_event_dict(
        self,
        *,
        proposal,
        tool_use: dict,
        judgment,
        enforcement_action: str,
        binding,
        synthesis_source: str | None = None,
        triggered_by: str | None = None,
    ) -> dict:
        """Construct the JSONL-shaped JudgmentEvent record for the
        audit trail. Mirrors spec/28's audit shape (line 833).

        ``synthesis_source`` distinguishes framework-synthesized ESCALATE
        from real-judge ESCALATE: ``"class_policy"`` for operator-set
        class_policy=escalate, ``"failure_policy"`` for exception-mapped
        escalate, ``None`` for actual ensemble verdicts. ``triggered_by``
        carries the exception class name for failure_policy synthesis.
        Both fields are canonical in spec/28's audit shape (PR 4 lock).
        Stored inline via ``self._log({...})``.
        """
        proposal_dict = None
        if proposal is not None:
            proposal_dict = asdict(proposal)
        record = {
            "trigger": "judgment",
            "event": "judgment",
            "parent_run_id": self.run_id,
            "proposal_id": getattr(proposal, "proposal_id", None),
            "agent": self.name,
            "judge_id": judgment.judge_id,
            "policy_version": judgment.policy_version,
            "proposal": proposal_dict,
            "judgment_outcome": judgment.outcome.value,
            "judgment_reason": judgment.reason,
            "raw_outcome": judgment.outcome.value,
            "enforcement_action": enforcement_action,
            "binding": asdict(binding),
            "latency_ms": judgment.latency_ms,
            "cost_usd": judgment.cost_usd,
            "cost_source": "judge",
            "tool_name": tool_use.get("name", ""),
        }
        if synthesis_source is not None:
            record["synthesis_source"] = synthesis_source
        if triggered_by is not None:
            record["triggered_by"] = triggered_by
        # Carry the escalation_queue_id through when populated so
        # operators auditing the trail see the PENDING file linkage.
        queue_id = getattr(judgment, "escalation_queue_id", None)
        if queue_id is not None:
            record["escalation_queue_id"] = queue_id
        return record

    # ────────────────────────────────────────────────────────────
    # Config loading

    def _load_config(self) -> AgentConfig:
        if self.cascade:
            # model.md: instance overrides role; if neither, defaults
            model_path = _cascade.resolve_model_md(self.cascade)
            model_data = _model.parse_model_md(model_path)
            # tools.md: instance .override.md merges with role; instance tools.md replaces role
            _, tools_text = _cascade.resolve_tools_md(self.cascade)
            tools_data = _tools.parse_tools_md_text(tools_text)
            # Judge-layer per-tool classifications from tools.md (#112 PR 2a).
            # Lookup at proposal-assembly time via _resolve_classification.
            self._tool_classifications = _tools.parse_tool_classifications_text(
                tools_text
            )
        else:
            model_data = _model.parse_model_md(self.agent_root / "model.md")
            tools_data = _tools.parse_tools_md(self.agent_root / "tools.md")
            self._tool_classifications = _tools.parse_tool_classifications(
                self.agent_root / "tools.md"
            )
            tools_text = ""
            tools_md_path = self.agent_root / "tools.md"
            if tools_md_path.exists():
                tools_text = tools_md_path.read_text(encoding="utf-8")

        # judges.md operator config (#112 PR 3a). Cascade-aware: own
        # judges.md + project-floor judges.md (when cascade); the
        # floor is non-relaxable per spec/28:408. ``None`` when no
        # judges.md exists — PR 2a/2b's hardcoded defaults run.
        # ``JudgePolicyInvalid`` at parse time stops _load_config
        # with a clear diagnostic per spec/28 fail-loud discipline.
        from . import judges_md as _judges_md_mod
        self.judges_config = _judges_md_mod.load_judges_config(
            agent_root=self.agent_root,
            cascade=self.cascade,
            tools_md_text=tools_text,
        )

        # roster.md lives at the instance root (same for cascaded + single-agent layouts)
        roster = _roster.parse_roster_md(self.agent_root / "roster.md")

        # mcp.md lives at the instance root (same for cascaded + single-agent layouts).
        # Empty list when no mcp.md exists — that's fine.
        # Pass read_paths so path-shaped args are validated at parse time (spec/19).
        mcp_servers = parse_mcp_md(
            self.agent_root / "mcp.md",
            read_paths=tools_data["read_paths"],
        )

        return AgentConfig(
            default_model=model_data["default_model"],
            fallback_model=model_data["fallback_model"],
            provider=model_data.get("provider"),
            max_input_tokens=model_data["max_input_tokens"],
            max_output_tokens=model_data["max_output_tokens"],
            cost_guardrails_enabled=model_data["cost_guardrails_enabled"],
            daily_cap_usd=model_data["daily_cap_usd"],
            monthly_cap_usd=model_data["monthly_cap_usd"],
            daily_cap_action=model_data["daily_cap_action"],
            monthly_cap_action=model_data["monthly_cap_action"],
            warning_thresholds=model_data["warning_thresholds"],
            alert_channel=model_data["alert_channel"],
            read_paths=tools_data["read_paths"],
            write_paths=tools_data["write_paths"],
            read_only_paths=tools_data.get("read_only_paths", []),
            external_apis=tools_data["external_apis"],
            hard_nos=tools_data["hard_nos"],
            roster=roster,
            mcp_servers=mcp_servers,
        )

    # ────────────────────────────────────────────────────────────
    # File loaders (per spec/04 canonical order)

    def load(self) -> None:
        """Load all the agent's files for this run. Idempotent."""
        if self.cascade:
            self._load_role_prompt()
            self._load_project_layer_text()
        else:
            self._load_goal_text()
        self._load_persona()
        self._load_tools_text()
        self._load_indexes()
        self._load_pinned_notes()
        self._load_recent_notes(n=RECENT_NOTES_DEFAULT)
        self._load_recent_journal(n=RECENT_JOURNAL_DEFAULT)

    def _load_role_prompt(self) -> None:
        if self.cascade:
            self._role_prompt_text = _cascade.load_role_prompt(self.cascade)

    def _load_project_layer_text(self) -> None:
        if self.cascade:
            layer = _cascade.load_project_layer(self.cascade)
            self._project_canon_text = layer["canon"]
            self._project_style_guide_text = layer["style_guide"]
            self._project_goal_text = layer["goal"]
            self._project_policy_text = layer["policy"]

    def _load_goal_text(self) -> None:
        """Load single-agent goal.md if present (spec/04 step 3.5).

        Only called for non-cascaded agents. Cascaded agents pick up the
        project-level goal via _load_project_layer_text(); loading the
        instance goal.md on top would create duplicate sections.
        """
        goal_path = self.agent_root / "goal.md"
        if goal_path.exists():
            self._goal_text = goal_path.read_text(encoding="utf-8")
        else:
            self._goal_text = ""

    def _load_persona(self) -> None:
        parts = []
        for filename in ("IDENTITY.md", "SOUL.md", "USER.md"):
            path = self.agent_root / "persona" / filename
            if path.exists():
                parts.append(f"# {filename}\n\n" + path.read_text(encoding="utf-8").strip())
        self._persona_text = "\n\n".join(parts)

    def _load_tools_text(self) -> None:
        if self.cascade:
            _, self._tools_text = _cascade.resolve_tools_md(self.cascade)
            return
        path = self.agent_root / "tools.md"
        if path.exists():
            self._tools_text = path.read_text(encoding="utf-8")
        else:
            self._tools_text = ""

    def _load_indexes(self) -> None:
        summary = self.memory.render_index_summary()
        if summary and summary.strip() != "# Memory Index\n":
            self._memory_index_text = summary
        wiki_index = self.agent_root / "wiki" / "INDEX.md"
        if wiki_index.exists():
            self._wiki_index_text = wiki_index.read_text(encoding="utf-8")

    def _load_pinned_notes(self) -> None:
        if not (self.agent_root / "memory").exists():
            return
        pinned_refs = self.memory.list_pinned()
        pinned = []
        for ref in pinned_refs[:PINNED_MAX]:
            note = self.memory.read_note(ref.name)
            if note is None:
                continue
            pinned.append(self._render_note_from_model(ref.name, note))
        self._pinned_notes = pinned

    def _load_recent_notes(self, n: int = RECENT_NOTES_DEFAULT) -> None:
        if not (self.agent_root / "memory").exists():
            return
        recent_refs = self.memory.list_recent(n=n, exclude_pinned=True)
        self._recent_notes = []
        for ref in recent_refs:
            note = self.memory.read_note(ref.name)
            if note is None:
                continue
            self._recent_notes.append(self._render_note_from_model(ref.name, note))

    def _load_recent_journal(self, n: int = RECENT_JOURNAL_DEFAULT) -> None:
        journal_dir = self.agent_root / "journal"
        if not journal_dir.exists():
            return
        entries = sorted(journal_dir.rglob("*.md"), reverse=True)[:n]
        self._recent_journal = [
            f"# Journal — {p.stem}\n\n" + p.read_text(encoding="utf-8")
            for p in entries
        ]

    @staticmethod
    def _render_note_for_context(path: Path, parsed: frontmatter.Post) -> str:
        """Format an atomic note for inclusion in the system prompt."""
        meta_summary = (
            f"name: {parsed.metadata.get('name', 'unnamed')}\n"
            f"type: {parsed.metadata.get('type', '?')}\n"
            f"confidence: {parsed.metadata.get('confidence', '?')}\n"
            f"last_seen: {parsed.metadata.get('last_seen', '?')}"
        )
        return f"# {path.name}\n\n{meta_summary}\n\n{parsed.content}"

    @staticmethod
    def _render_note_from_model(filename: str, note: "Any") -> str:
        """Format a Note model for inclusion in the system prompt.

        Mirrors _render_note_for_context but reads from a Note dataclass instead
        of a raw frontmatter.Post. Called by _load_pinned_notes and
        _load_recent_notes after the P2.1 migration to agent.memory.read_note().
        """
        meta_summary = (
            f"name: {note.name}\n"
            f"type: {note.type}\n"
            f"confidence: {note.confidence}\n"
            f"last_seen: {note.last_seen}"
        )
        return f"# {filename}\n\n{meta_summary}\n\n{note.body}"

    # ────────────────────────────────────────────────────────────
    # System prompt assembly

    def assemble_system_prompt(self) -> str:
        """Assemble the full system prompt.

        Single-agent layout uses spec/04 order. Cascaded multi-agent project
        agents use spec/06 order:

            [1] role PROMPT.md
            [2-4] instance IDENTITY/SOUL/USER (loaded into _persona_text)
            [5/5b] role tools.md (or instance override) — already merged in _tools_text
            [7] project canon.md
            [7.5] project goal.md (optional, if present)
            [8] project style_guide.md
            [9] project policy/* (all)
            [10-13] memory/INDEX, wiki/INDEX, pinned, recent atomic notes
            [14] recent journal
        """
        sections: list[str] = []

        if self.cascade and self._role_prompt_text:
            sections.append("# role PROMPT.md\n\n" + self._role_prompt_text)
        if self._persona_text:
            sections.append(self._persona_text)
        # spec/04 step 3.5 — single-agent goal.md injected between persona and tools.
        # Cascaded agents already get project-level goal via _project_goal_text below.
        if not self.cascade and self._goal_text:
            sections.append("# goal.md\n\n" + self._goal_text)
        if self._tools_text:
            sections.append("# tools.md\n\n" + self._tools_text)
        # spec/18 — skills metadata injected after tools, before memory.
        # Only metadata (name + description) lands here; full body is loaded
        # on demand via the load_skill tool.
        if self.skills:
            skill_lines = [
                "# Available skills",
                "",
                "The following skills are available. Use the load_skill tool to load a "
                "skill's full instructions when relevant to the task.",
                "",
            ]
            for skill in self.skills:
                skill_lines.append(f"- **{skill.name}**: {skill.description}")
            sections.append("\n".join(skill_lines))
        if self.cascade:
            if self._project_canon_text:
                sections.append("# project canon.md\n\n" + self._project_canon_text)
            if self._project_goal_text:
                sections.append("# project goal.md\n\n" + self._project_goal_text)
            if self._project_style_guide_text:
                sections.append("# project style_guide.md\n\n" + self._project_style_guide_text)
            if self._project_policy_text:
                sections.append("# project policy/\n\n" + self._project_policy_text)
        if self._memory_index_text:
            sections.append("# memory/INDEX.md\n\n" + self._memory_index_text)
        if self._wiki_index_text:
            sections.append("# wiki/INDEX.md\n\n" + self._wiki_index_text)
        if self._pinned_notes:
            sections.append("# Pinned atomic notes\n\n" + "\n\n---\n\n".join(self._pinned_notes))
        if self._recent_notes:
            sections.append("# Recent atomic notes\n\n" + "\n\n---\n\n".join(self._recent_notes))
        if self._recent_journal:
            sections.append("# Recent journal\n\n" + "\n\n---\n\n".join(self._recent_journal))
        return "\n\n═══════════════════════════\n\n".join(sections)

    # ────────────────────────────────────────────────────────────
    # The main call

    def call(
        self,
        work_item: str,
        model_override: str | None = None,
        critical: bool = False,
        max_tokens: int | None = None,
        temperature: float | None = None,
        write_captures: bool = True,
        parent_remaining_headroom_usd: float | None = None,
    ) -> Response:
        """Make the LLM call. Returns a Response with captures populated.

        critical=True bypasses cost guardrails (still logged with critical: true).
        write_captures=False extracts but doesn't persist captures (dry-run mode).

        parent_remaining_headroom_usd: when set (by a coordinator's delegate()),
        this call's own cap is clamped to min(own remaining, parent headroom).
        This enforces the coordinator's cap as a true tree-cap (spec/15).

        When self.tool_registry has registered tools, call() runs a multi-turn
        loop (up to self.max_tool_iterations iterations):
          1. LLM call with tool definitions
          2. Parse tool_uses from response
          3. Execute custom tools via registry (atomic_capture handled separately)
          4. Build follow-up message with tool_result blocks
          5. Repeat until no custom tool_uses returned OR cap hit

        Each iteration counts against the same cost cap. The final Response has:
          - tool_calls: list of all ToolCallResult from all iterations
          - tool_iterations: how many LLM turns were made (1 = no tools)
          - tool_iterations_maxed: True if loop was stopped by cap
        """
        # Lazy load if not already
        if not self._persona_text:
            self.load()

        # Acquire agent lock
        try:
            lock = AgentLock(self.agent_root, wait_seconds=30 if self.trigger == "skill" else 0)
            lock.acquire()
        except AgentLockBusy as e:
            self._log({
                "trigger": self.trigger,
                "model": self.config.default_model,
                "input_tokens": 0,
                "output_tokens": 0,
                "status": "lock_busy",
                "summary": str(e),
            })
            raise

        # Track MCP tool names registered this call so we can clean them up in
        # finally even if an exception occurs mid-call (spec/19 fix M3).
        _mcp_registered_names: list[str] = []

        try:
            # Reset helper-provenance rollup for this run (spec/13 Layer 3)
            self._helpers_this_run = []
            # Reset delegation rollup for this run
            self._delegations_this_run = []
            # Reset cumulative delegated cost for this run (fix R2-A2)
            self._delegated_cost_this_run = 0.0

            # Cost guardrails check FIRST — before spinning up MCP subprocesses.
            # A call that will be skipped due to cost cap should not pay the
            # subprocess startup cost. (spec/19 fix M6)
            check = self._check_cost_guardrails(
                critical=critical,
                parent_remaining_headroom_usd=parent_remaining_headroom_usd,
            )
            if not check.allow:
                self._log({
                    "trigger": self.trigger,
                    "model": self.config.default_model,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "status": "skipped",
                    "summary": f"Skipped: {check.reason}",
                })
                return Response.skipped_response(check.reason, self.config.default_model)

            # MCP client pool — lazy init (spec/19).
            # Only spin up when mcp.md declares servers and pool not yet live.
            # Discover tools and register them into the tool registry before
            # the first LLM call so the model sees the full tool list.
            if self.config.mcp_servers and self.mcp_pool is None:
                self.mcp_pool = MCPClientPool(
                    server_specs=self.config.mcp_servers,
                    agents_root=self.agents_root,
                )
                self.mcp_pool.connect_all()
                mcp_tool_defs = self.mcp_pool.discover_tools()
                for td in mcp_tool_defs:
                    self.tool_registry.register(td, allow_overwrite=True)
                    _mcp_registered_names.append(td.name)
                _logger.debug(
                    "agent %r: MCP pool ready — %d tools from %d server(s)",
                    self.name, len(mcp_tool_defs), len(self.config.mcp_servers),
                )

            # PR 3b ESCALATE: opportunistic throttled poll of the
            # escalation queue. Operators (and the auto-decide-timeout
            # branch) resolve PENDING files asynchronously; on each
            # call() we check whether any are ready, emit RESOLVED audit
            # events, and execute Approved actions inline. Throttle
            # caps disk I/O at one scan per
            # judges_config.escalation.resolution_poll_cycle_seconds
            # (default 60s) per agent. Standalone CLI / cron poller
            # is tracked as a follow-up issue.
            if self._judge_enabled():
                self.poll_escalations()

            # Pick model — fallback if guardrail says so, else override, else default
            if check.action == "fallback" and check.fallback_model:
                model = check.fallback_model
            else:
                model = model_override or self.config.default_model

            # Build prompt
            system_prompt = self.assemble_system_prompt()
            messages: list[dict] = [{"role": "user", "content": work_item}]

            # Tool definitions for the LLM — includes atomic_capture + custom tools.
            # None for providers without tool-call support.
            tool_definitions = self._all_tool_definitions(model)

            # Accumulators across multi-turn loop iterations
            all_tool_call_results: list[ToolCallResult] = []
            all_captures: list[Capture] = []
            all_parse_failures: list = []
            total_input_tokens = 0
            total_output_tokens = 0
            total_cache_hit_tokens = 0
            total_cache_miss_tokens = 0
            total_cost = 0.0
            cost_fallback = False
            tool_iterations_maxed = False
            iteration_count = 0
            last_raw = None
            # In-flight cost accumulator — tracks spend from current loop iterations
            # that hasn't landed in the persisted log yet. Passed to
            # _check_cost_guardrails so mid-loop cap checks see the true running
            # total, not just the pre-call on-disk snapshot. (fix R2-A1)
            accumulated_loop_cost_usd: float = 0.0
            # PR 3b ESCALATE: accumulate queue_ids across iterations
            # (a single tool-loop iteration can produce multiple
            # deferred tool_uses; the loop terminates immediately, but
            # Response.escalation_queue_ids reflects all of them).
            accumulated_escalation_queue_ids: list[str] = []

            # ── Multi-turn tool loop ──────────────────────────────
            start_total = time.time()
            while True:
                iteration_count += 1

                # Pre-check cost cap before each iteration (except first, already checked).
                # Pass the in-flight accumulator so the guardrail sees spend that
                # has not yet been persisted to the log file. (fix R2-A1)
                if iteration_count > 1:
                    iter_check = self._check_cost_guardrails(
                        critical=critical,
                        extra_in_flight_cost_usd=accumulated_loop_cost_usd,
                        parent_remaining_headroom_usd=parent_remaining_headroom_usd,
                    )
                    if not iter_check.allow:
                        # Cap hit mid-loop — return what we have with skipped=True
                        latency_ms = int((time.time() - start_total) * 1000)
                        skip_reason = f"cost cap hit at iteration {iteration_count}: {iter_check.reason}"
                        response = Response(
                            text=last_raw.text if last_raw else "",
                            model=model,
                            input_tokens=total_input_tokens,
                            output_tokens=total_output_tokens,
                            cache_hit_tokens=total_cache_hit_tokens,
                            cache_miss_tokens=total_cache_miss_tokens,
                            cost_usd=total_cost,
                            cost_estimated_via_fallback=cost_fallback,
                            latency_ms=latency_ms,
                            summary=self._derive_summary(work_item),
                            raw=last_raw.raw or {} if last_raw else {},
                            captures=all_captures,
                            skipped=True,
                            skip_reason=skip_reason,
                            tool_calls=all_tool_call_results,
                            tool_iterations=iteration_count - 1,
                        )
                        self._log({
                            "trigger": self.trigger,
                            "model": model,
                            "input_tokens": total_input_tokens,
                            "output_tokens": total_output_tokens,
                            "cost_usd": total_cost,
                            "cost_source": "actor",
                            "latency_ms": latency_ms,
                            "status": "skipped",
                            "summary": skip_reason,
                            "run_id": self.run_id,
                        })
                        return response

                iter_start = time.time()
                raw = _llm.call_llm(
                    model=model,
                    system_prompt=system_prompt,
                    messages=messages,
                    max_tokens=max_tokens or self.config.max_output_tokens,
                    temperature=temperature if temperature is not None else 0.6,
                    cache_control_breakpoints=[len(system_prompt)] if iteration_count == 1 else None,
                    tools=tool_definitions,
                    preferred_provider=self.config.provider,
                )
                iter_latency_ms = int((time.time() - iter_start) * 1000)
                last_raw = raw

                iter_cost, iter_cost_fallback = _costs.calc_cost(
                    model, raw.input_tokens, raw.output_tokens, raw.cache_hit_tokens
                )
                total_input_tokens += raw.input_tokens
                total_output_tokens += raw.output_tokens
                total_cache_hit_tokens += raw.cache_hit_tokens
                total_cache_miss_tokens += raw.cache_miss_tokens
                total_cost += iter_cost
                # Track in-flight spend so mid-loop cap checks see the running
                # total before the parent log line is written. (fix R2-A1)
                accumulated_loop_cost_usd += iter_cost
                if iter_cost_fallback:
                    cost_fallback = True

                # Extract captures from this iteration (Path 1 + Path 2)
                iter_captures, iter_failures = _capture.extract_all_captures(
                    raw.text, tool_uses=raw.tool_uses,
                )
                all_captures.extend(iter_captures)
                all_parse_failures.extend(iter_failures)

                # Partition tool_uses: framework-managed (atomic_capture,
                # atomic_action) handled above / by judge dispatch, vs
                # custom tools the operator registered.
                from .judge.proposal import is_framework_managed_tool
                custom_tool_uses = [
                    tu for tu in raw.tool_uses
                    if not is_framework_managed_tool(tu.get("name", ""))
                    and self.tool_registry.get(tu.get("name", "")) is not None
                ]
                unknown_tool_uses = [
                    tu for tu in raw.tool_uses
                    if not is_framework_managed_tool(tu.get("name", ""))
                    and self.tool_registry.get(tu.get("name", "")) is None
                    and tu.get("name", "")  # non-empty name
                ]

                # Log any tool calls to unknown tools (model hallucinated a tool name)
                for tu in unknown_tool_uses:
                    _logger.warning(
                        "agent %r: LLM called unknown tool %r (not in registry)",
                        self.name, tu.get("name", ""),
                    )
                    self._log({
                        "trigger": "tool_call",
                        "parent_run_id": self.run_id,
                        "tool_name": tu.get("name", ""),
                        "latency_ms": 0,
                        "error": "ToolNotRegistered",
                    })

                # Judge layer (#112 PR 2a). Extract atomic_action markers and
                # dispatch the judge ensemble for each side-effectful tool_use
                # BEFORE handler execution. Spec/28 §"Where the judge sits in
                # agent.call()". Opt-in per _judge_enabled() — when disabled
                # (no judges.md, no AGENT_JUDGE_ENABLED env var) the dispatch
                # is skipped entirely and today's pre-#112 behavior runs.
                judge_blocked: dict[str, str] = {}  # tool_call_id -> block reason
                # PR 3b ESCALATE: tool_call_id -> escalation_queue_id.
                # Deferred tool_uses don't execute this turn; the actor's
                # call() returns deferred=True after the iteration.
                judge_deferred: dict[str, str] = {}
                if self._judge_enabled() and custom_tool_uses:
                    from .judge.atomic_action import extract_atomic_action_markers
                    from .exceptions import JudgeProposalInvalid
                    try:
                        markers = extract_atomic_action_markers(raw.tool_uses)
                    except JudgeProposalInvalid as exc:
                        # Marker-level malformation (duplicate / missing
                        # for_tool_call_id). Per failure_policy default,
                        # block ALL side-effectful tool_uses this iteration.
                        _logger.warning(
                            "agent %r: judge layer marker extraction failed: %s",
                            self.name, exc,
                        )
                        markers = {}
                        for tu in custom_tool_uses:
                            tcid = tu.get("id", "")
                            judge_blocked[tcid] = (
                                f"JudgeProposalInvalid (marker extraction): {exc}"
                            )
                    if not judge_blocked:
                        for tu in custom_tool_uses:
                            try:
                                allow, events, queue_id = self._dispatch_with_judge(
                                    tu, markers,
                                )
                            except Exception as exc:  # noqa: BLE001
                                # Defensive: any uncaught judge-path error
                                # fail-closes per spec/28's failure_policy
                                # default. Better to block than silently let
                                # the action run with no audit record.
                                _logger.exception(
                                    "agent %r: judge dispatch raised; "
                                    "fail-closing to BLOCK for tool_call_id=%r",
                                    self.name, tu.get("id", ""),
                                )
                                judge_blocked[tu.get("id", "")] = (
                                    f"judge dispatch error: "
                                    f"{type(exc).__name__}: {exc}"
                                )
                                continue
                            # Per-judge audit lines (#112 PR 2b ensemble).
                            # First BLOCK in the ensemble short-circuits
                            # remaining judges; this loop records every
                            # judge that actually ran.
                            for event in events:
                                self._log(event)
                            if queue_id is not None:
                                # PR 3b ESCALATE: PENDING file already
                                # written by _dispatch_with_judge. Mark
                                # this tool_use as deferred so it does
                                # not execute this turn; the actor's
                                # call() returns with deferred=True.
                                judge_deferred[tu.get("id", "")] = queue_id
                            elif not allow:
                                # The LAST event in the list is the BLOCKing
                                # judge — the others ALLOWed. Its reason is
                                # what flows back to the actor.
                                judge_blocked[tu.get("id", "")] = (
                                    events[-1].get("judgment_reason", "judge_blocked")
                                )

                # Execute custom tools
                iter_tool_results: list[ToolCallResult] = []
                for tu in custom_tool_uses:
                    tcid = tu.get("id", "")
                    if tcid in judge_blocked:
                        # Judge BLOCKed — synthesize an error tool_result so
                        # the LLM sees the refusal on the next turn, without
                        # running the handler. The reason flows back to the
                        # actor verbatim per spec/28 §"Block".
                        from .tools import ToolCallResult as _TCR
                        blocked_result = _TCR(
                            tool_name=tu.get("name", ""),
                            tool_use_id=tcid,
                            input=tu.get("input", {}) or {},
                            output=None,
                            error=f"judge_blocked: {judge_blocked[tcid]}",
                            latency_ms=0,
                        )
                        all_tool_call_results.append(blocked_result)
                        iter_tool_results.append(blocked_result)
                        self._log({
                            "trigger": "tool_call",
                            "parent_run_id": self.run_id,
                            "tool_name": blocked_result.tool_name,
                            "latency_ms": 0,
                            "error": blocked_result.error,
                        })
                        continue
                    if tcid in judge_deferred:
                        # PR 3b ESCALATE: PENDING file already written.
                        # Synthesize a "deferred" tool_result for the
                        # audit trail but do NOT execute the handler.
                        # The actor's call() returns deferred=True after
                        # this iteration; no further multi-turn loop.
                        # ``deferred=True`` is the structural signal
                        # consumers iterate on (Codex round-1 P2-4 fix);
                        # ``error`` carries the same info as prose for
                        # humans reading the JSONL log. Distinct trigger
                        # ``tool_call_deferred`` keeps dashboard failure
                        # counts honest (P2-5 fix).
                        from .tools import ToolCallResult as _TCR
                        deferred_result = _TCR(
                            tool_name=tu.get("name", ""),
                            tool_use_id=tcid,
                            input=tu.get("input", {}) or {},
                            output=None,
                            error=(
                                f"judge_deferred: ESCALATE — see "
                                f"escalation_queue_id={judge_deferred[tcid]}"
                            ),
                            latency_ms=0,
                            deferred=True,
                        )
                        all_tool_call_results.append(deferred_result)
                        iter_tool_results.append(deferred_result)
                        self._log({
                            "trigger": "tool_call_deferred",
                            "parent_run_id": self.run_id,
                            "tool_name": deferred_result.tool_name,
                            "latency_ms": 0,
                            "error": deferred_result.error,
                            "escalation_queue_id": judge_deferred[tcid],
                        })
                        continue
                    tool_result = self.tool_registry.execute(tu)
                    all_tool_call_results.append(tool_result)
                    iter_tool_results.append(tool_result)
                    # Per-tool JSONL log line
                    self._log({
                        "trigger": "tool_call",
                        "parent_run_id": self.run_id,
                        "tool_name": tool_result.tool_name,
                        "latency_ms": tool_result.latency_ms,
                        "error": tool_result.error,
                    })

                # If no custom tools were called, the loop is done
                if not custom_tool_uses:
                    break

                # PR 3b ESCALATE: any deferred tool_use breaks the
                # multi-turn loop. ALLOWed tool_results stay in
                # all_tool_call_results, deferred ones already recorded
                # an audit-only error result. Actor's call() returns
                # with deferred=True so the caller (operator harness,
                # parent agent, CLI) sees the run paused.
                if judge_deferred:
                    accumulated_escalation_queue_ids.extend(judge_deferred.values())
                    break

                # Check if we've hit the iteration cap
                if iteration_count >= self.max_tool_iterations:
                    tool_iterations_maxed = True
                    break

                # Build follow-up messages with tool_result blocks so the LLM
                # can incorporate results in the next turn.
                # We build the assistant's tool_use blocks + the tool_result blocks
                # and append them to the running messages list.
                messages = self._build_tool_loop_messages(
                    messages, raw, iter_tool_results, model
                )

            # ── End of multi-turn loop ────────────────────────────
            latency_ms = int((time.time() - start_total) * 1000)

            # Write captures if enabled (dedupe across all iterations already done
            # by extract_all_captures, which uses a seen-set per call. But we
            # accumulated across iterations so need to dedupe manually.)
            written_captures: list[Capture] = []
            seen_capture_keys: set[tuple] = set()
            if write_captures:
                policy = WritePolicy(
                    write_paths=self.config.write_paths,
                    read_only_paths=self.config.read_only_paths,
                )
                for c in all_captures:
                    key = (c.type, c.name, hash(c.body))
                    if key in seen_capture_keys:
                        continue
                    seen_capture_keys.add(key)
                    try:
                        self.memory.write_note(c, policy)
                        written_captures.append(c)
                    except Exception as e:
                        self._log({
                            "trigger": "capture_write_error",
                            "parent_run_id": self.run_id,
                            "model": "n/a",
                            "input_tokens": 0,
                            "output_tokens": 0,
                            "status": "error",
                            "summary": f"capture write failed for {c.name}: {e}",
                        })

            # Build response
            response = Response(
                text=last_raw.text if last_raw else "",
                model=model,
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
                cache_hit_tokens=total_cache_hit_tokens,
                cache_miss_tokens=total_cache_miss_tokens,
                cost_usd=total_cost,
                cost_estimated_via_fallback=cost_fallback,
                latency_ms=latency_ms,
                summary=self._derive_summary(work_item),
                raw=last_raw.raw or {} if last_raw else {},
                captures=written_captures,
                tool_calls=all_tool_call_results,
                tool_iterations=iteration_count,
                tool_iterations_maxed=tool_iterations_maxed,
                deferred=bool(accumulated_escalation_queue_ids),
                escalation_queue_ids=accumulated_escalation_queue_ids,
            )

            # Log run record
            log_record: dict = {
                "trigger": self.trigger,
                "model": model,
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens,
                "cache_hit_tokens": total_cache_hit_tokens,
                "cache_miss_tokens": total_cache_miss_tokens,
                "cost_usd": total_cost,
                "cost_source": "actor",
                "latency_ms": latency_ms,
                "status": "ok",
                "summary": response.summary,
                "run_id": self.run_id,
                "agent_mode": self.agent_mode,
            }
            if check.action == "fallback":
                log_record["fallback"] = True
            if cost_fallback:
                log_record["cost_estimated_via_fallback"] = True
            if critical:
                log_record["critical"] = True
            if all_parse_failures:
                log_record["capture_parse_failures"] = len(all_parse_failures)
            if self._helpers_this_run:
                # Spec/13 Layer 3 — research log: roll up helper provenance
                # into the parent run record so an audit can trace every fact
                # back to the helper invocation that produced it.
                log_record["helper_provenance"] = list(self._helpers_this_run)
            if self._delegations_this_run:
                log_record["delegations"] = list(self._delegations_this_run)
            if all_tool_call_results:
                log_record["tool_calls"] = [
                    {
                        "tool_name": r.tool_name,
                        "tool_use_id": r.tool_use_id,
                        "latency_ms": r.latency_ms,
                        "error": r.error,
                    }
                    for r in all_tool_call_results
                ]
            if iteration_count > 1:
                log_record["tool_iterations"] = iteration_count
            if tool_iterations_maxed:
                log_record["tool_iterations_maxed"] = True
            self._log(log_record)

            return response

        finally:
            # Tear down MCP pool after each call so subprocesses don't linger.
            # disconnect_all() is idempotent — safe to call even if connect_all()
            # was never reached (e.g. if an exception occurred before it).
            if self.mcp_pool is not None:
                self.mcp_pool.disconnect_all()
                self.mcp_pool = None
            # Unregister MCP tools that were registered for this call (spec/19 fix M3).
            # Prevents stale tools from accumulating in the long-lived tool_registry
            # when a later call's server fails to reconnect.
            for _mcp_name in _mcp_registered_names:
                self.tool_registry.unregister(_mcp_name)
            lock.release()

    def _build_tool_loop_messages(
        self,
        prior_messages: list[dict],
        raw: Any,
        tool_results: list[ToolCallResult],
        model: str,
    ) -> list[dict]:
        """Build the updated messages list for the next iteration of the tool loop.

        Appends the assistant's response (with tool_use blocks) and the
        tool_result messages so the LLM can incorporate results in its
        next turn.

        Post-#87 PR 3: every supported provider routes through the
        registry. The backend's ``format_tool_results`` translates
        canonical types to whatever message shape that provider's API
        requires (Anthropic gets an assistant message with tool_use
        blocks + a user message with tool_result blocks; OpenAI/Moonshot
        get an assistant message with ``tool_calls`` + N tool-role
        messages). The agent layer no longer branches by provider.
        """
        from .llm import find_backend_for_model

        messages = list(prior_messages)
        canonical_tool_uses, canonical_tool_results = _canonicalize_tool_loop(
            raw.tool_uses, tool_results,
        )
        # Thread the agent's ``model.md provider:`` preference (#87 PR 3)
        # so a multi-iteration tool loop on an ambiguously-claimed model
        # id resolves consistently across all iterations. Without this,
        # iteration 1's call resolves correctly via call_llm but
        # iteration 2's format_tool_results crashes mid-loop with
        # AmbiguousBackendError. Bug caught by Opus subagent review of
        # this PR (Finding 1); regression test in test_codex_r2_agent.py.
        backend = find_backend_for_model(model, preferred_provider=self.config.provider)
        messages.extend(backend.format_tool_results(
            tool_uses=canonical_tool_uses,
            tool_results=canonical_tool_results,
            assistant_text=raw.text or "",
        ))
        return messages

    # ────────────────────────────────────────────────────────────
    # Helpers (Patterns A + B per spec/10)

    HELPER_PROVENANCE_PROMPT = (
        "When summarizing or extracting facts from a source document, cite the "
        "location (section, page, or paragraph) of each fact. If you can't "
        "pinpoint a location, say so explicitly. Do not return facts without "
        "provenance — the calling agent depends on traceability for citation in "
        "its response."
    )

    def helper_call(
        self,
        prompt: str,
        model: str = "claude-haiku-4-5-20251001",
        max_tokens: int = 1024,
        temperature: float = 0.3,
        summary: str = "",
        sources: list[str] | None = None,
    ) -> HelperResult:
        """One sequential helper call. Bound by parent's cost guardrails.

        When ``sources`` is passed (per spec/10 Wave 8 helper provenance),
        the helper system prompt includes citation instructions and the
        source list. The result echoes ``sources`` and sets
        ``provenance_preserved=False`` if the output appears to lack
        citation-like markers, so the parent can decide whether to trust
        the helper output as citable facts or treat it as uncited prose.

        Returns HelperResult with text + cost + token counts + provenance.
        """
        check = self._check_cost_guardrails(critical=False)
        if not check.allow:
            raise CostGuardrailBlocked(f"Helper call blocked: {check.reason}")

        actual_model = check.fallback_model if check.action == "fallback" else model
        sources_list = list(sources) if sources else []
        system_prompt = self._build_helper_system_prompt(sources_list)

        start = time.time()
        raw = _llm.call_llm(
            model=actual_model,
            system_prompt=system_prompt,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=temperature,
            preferred_provider=self.config.provider,
        )
        latency_ms = int((time.time() - start) * 1000)
        cost, _cost_fallback = _costs.calc_cost(actual_model, raw.input_tokens, raw.output_tokens)

        provenance_preserved = self._detect_provenance(raw.text, sources_list)

        log_record: dict = {
            "trigger": "helper",
            "parent_agent": self.name,
            "parent_run_id": self.run_id,
            "model": actual_model,
            "input_tokens": raw.input_tokens,
            "output_tokens": raw.output_tokens,
            "cost_usd": cost,
            "cost_source": "actor",
            "latency_ms": latency_ms,
            "status": "ok",
            "summary": summary or "helper call",
        }
        if sources_list:
            log_record["sources"] = sources_list
            log_record["provenance_preserved"] = provenance_preserved
        self._log(log_record)

        # Append to the in-memory rollup for spec/13 Layer 3 (research log).
        # The parent run's log record will include this list at end-of-call.
        rollup_entry = {
            "model": actual_model,
            "summary": summary or "helper call",
            "cost_usd": cost,
            "latency_ms": latency_ms,
        }
        if sources_list:
            rollup_entry["sources_summarized"] = sources_list
            rollup_entry["provenance_preserved"] = provenance_preserved
        self._helpers_this_run.append(rollup_entry)

        return HelperResult(
            text=raw.text,
            model=actual_model,
            input_tokens=raw.input_tokens,
            output_tokens=raw.output_tokens,
            cost_usd=cost,
            latency_ms=latency_ms,
            sources=sources_list,
            provenance_preserved=provenance_preserved,
        )

    def helper_call_parallel(
        self,
        prompts: list[str],
        model: str = "claude-haiku-4-5-20251001",
        max_tokens: int = 1024,
        temperature: float = 0.3,
        max_concurrent: int = 5,
        summary_template: str = "helper call {idx} of {total}",
        sources_per_prompt: list[list[str]] | None = None,
        sources: list[str] | None = None,
    ) -> list[HelperResult]:
        """Parallel helper calls. Pre-checks guardrails ONCE; if cap hit, refuses the batch.

        Provenance options (mutually exclusive — passing both raises ValueError):

        - ``sources``: same source list applied to every prompt (e.g., one
          source document being analyzed N different ways).
        - ``sources_per_prompt``: list of source lists, aligned 1:1 with
          ``prompts`` (e.g., each prompt is a different document).

        Either way, each result's ``sources`` and ``provenance_preserved``
        fields are populated as in ``helper_call``.
        """
        if sources is not None and sources_per_prompt is not None:
            raise ValueError(
                "pass either `sources` (shared) or `sources_per_prompt` (per-prompt), not both"
            )
        if sources_per_prompt is not None and len(sources_per_prompt) != len(prompts):
            raise ValueError(
                f"sources_per_prompt has {len(sources_per_prompt)} entries; "
                f"expected {len(prompts)} (one per prompt)"
            )

        check = self._check_cost_guardrails(critical=False)
        if not check.allow:
            raise CostGuardrailBlocked(
                f"Parallel helper batch blocked: {check.reason}"
            )

        # Worst-case reservation: check that the parent's remaining headroom can
        # cover all helpers at max_tokens output each. This prevents the "each
        # thread sees the same pre-batch snapshot" race where collective cost
        # overruns the cap even though no individual thread sees a breach.
        actual_model = check.fallback_model if check.action == "fallback" else model
        reserved_usd = self._estimate_batch_cost(actual_model, max_tokens, len(prompts))
        self._check_batch_reservation(reserved_usd)

        total = len(prompts)
        results: list[Any] = [None] * total  # list[HelperResult | Exception]

        # Log the reservation so an audit trail can see what was reserved.
        self._log({
            "trigger": "helper_batch_reservation",
            "parent_agent": self.name,
            "parent_run_id": self.run_id,
            "model": actual_model,
            "input_tokens": 0,
            "output_tokens": 0,
            "reserved_usd": reserved_usd,
            "batch_size": total,
            "status": "ok",
            "summary": f"reserved worst-case ${reserved_usd:.6f} for {total}-helper batch",
        })

        def sources_for(idx: int) -> list[str] | None:
            if sources_per_prompt is not None:
                return sources_per_prompt[idx]
            return sources

        def call_one(idx: int, prompt: str):
            return idx, self.helper_call(
                prompt=prompt,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                summary=summary_template.format(idx=idx + 1, total=total),
                sources=sources_for(idx),
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_concurrent) as pool:
            futures = {pool.submit(call_one, i, p): i for i, p in enumerate(prompts)}
            for future in concurrent.futures.as_completed(futures):
                try:
                    idx, helper_result = future.result()
                    results[idx] = helper_result
                except Exception as e:
                    idx = futures[future]
                    results[idx] = e

        failures = [(i, r) for i, r in enumerate(results) if isinstance(r, Exception)]
        if failures:
            raise HelperBatchPartialFailure(failures, results)

        # Log the release: actual aggregate cost vs what was reserved.
        actual_usd = sum(r.cost_usd for r in results if isinstance(r, HelperResult))
        self._log({
            "trigger": "helper_batch_release",
            "parent_agent": self.name,
            "parent_run_id": self.run_id,
            "model": actual_model,
            "input_tokens": 0,
            "output_tokens": 0,
            "reserved_usd": reserved_usd,
            "actual_usd": actual_usd,
            "batch_size": total,
            "status": "ok",
            "summary": (
                f"batch complete: actual ${actual_usd:.6f} vs "
                f"reserved ${reserved_usd:.6f}"
            ),
        })

        return results  # type: ignore

    # ────────────────────────────────────────────────────────────
    # Delegation (runtime agent-to-agent, per spec/15)

    def _resolve_delegated_agent_path(self, target_name: str) -> Path:
        """Resolve the filesystem path for a target agent.

        In a cascaded layout (<system>/projects/<project>/agents/<role>/),
        the target resolves as a peer under the same project:
            <system>/projects/<project>/agents/<target>/

        In a single-agent layout (<agents_root>/<role>/), the target resolves
        as a top-level sibling:
            <agents_root>/<target>/

        Raises NotInRoster (mapped from PathTraversalError) if target_name
        contains path-traversal sequences (e.g. ``../other``) that would
        resolve outside the agents root.
        """
        if self.cascade:
            agents_dir = self.cascade.instance_root.parent
        else:
            agents_dir = self.agents_root
        try:
            return safe_resolve_under(target_name, agents_dir)
        except PathTraversalError as exc:
            raise NotInRoster(
                f"target '{target_name}' resolves outside agents root "
                f"({agents_dir}) — path traversal refused"
            ) from exc

    def _enforce_roster_membership(self, target: str) -> None:
        """Raise NotInRoster if target is not in this coordinator's roster."""
        if target not in self.config.roster:
            raise NotInRoster(
                f"target '{target}' not in coordinator's roster: {self.config.roster}"
            )

    def delegate(
        self,
        target_agent_name: str,
        work_item: str,
        max_tokens: int | None = None,
        temperature: float | None = None,
        critical: bool = False,
        summary: str = "",
    ) -> Response:
        """Synchronously dispatch a work item to another agent in the roster.

        Loads <target_agent_name> as a fresh AtomicAgent instance with its own
        persona, memory, wiki, journal, and config. Calls it with the work_item.
        Returns its Response.

        The coordinator's cost guardrails apply to the total call tree — a
        pre-check runs before the delegate call, and the delegation is refused
        if the cap is hit (unless critical=True).

        Each delegate call also writes a JSONL log line with trigger=delegate,
        parent_agent, delegated_agent, parent_run_id, and delegated_run_id.
        The Response captures (if any) are written to the target's memory, not
        the coordinator's.

        Raises:
            NotInRoster: target_agent_name is not in self.config.roster.
            SelfDelegationError: target_agent_name == self.name (one-level only).
            NestedDelegationRefused: self.trigger == 'delegate' (nested delegation
                forbidden — spec/15 enforces one-level only).
            CostGuardrailBlocked: parent's cost cap is hit and critical=False.
        """
        # Nested delegation guard — spec/15 one-level limit. (fix R2-A3)
        if self.trigger == "delegate":
            raise NestedDelegationRefused(
                f"agent '{self.name}' is already running as a delegated agent "
                f"(trigger='delegate') and cannot delegate further — "
                f"nested delegation refused per spec/15 (one-level only)"
            )

        self._enforce_roster_membership(target_agent_name)
        if target_agent_name == self.name:
            raise SelfDelegationError(
                f"agent '{self.name}' cannot delegate to itself — one-level delegation only"
            )

        # Pass prior delegated cost as extra_in_flight so the guardrail sees
        # tree-spend that landed in the target's log dir, not the coordinator's.
        # (fix R2-A2)
        check = self._check_cost_guardrails(
            critical=critical,
            extra_in_flight_cost_usd=self._delegated_cost_this_run,
        )
        if not check.allow:
            raise CostGuardrailBlocked(
                f"Delegation to '{target_agent_name}' blocked: {check.reason}"
            )

        # Compute coordinator's remaining headroom to pass to the delegate.
        # This enforces the coordinator cap as a true tree-cap. (fix R2-A2)
        # Include already-delegated spend so the headroom accounts for it.
        remaining_headroom: float | None = None
        if self.config.cost_guardrails_enabled and not critical:
            log_dir = self.agent_root / "log"
            today_cost = (
                _costs.sum_cost_for_period(log_dir, "today", source="actor")
                + self._delegated_cost_this_run
            )
            month_cost = (
                _costs.sum_cost_for_period(log_dir, "this_month", source="actor")
                + self._delegated_cost_this_run
            )
            daily_remaining = (
                self.config.daily_cap_usd - today_cost
                if self.config.daily_cap_usd > 0
                else float("inf")
            )
            monthly_remaining = (
                self.config.monthly_cap_usd - month_cost
                if self.config.monthly_cap_usd > 0
                else float("inf")
            )
            headroom = min(daily_remaining, monthly_remaining)
            if headroom < float("inf"):
                remaining_headroom = headroom

        target_path = self._resolve_delegated_agent_path(target_agent_name)
        # Build the target agent; it inherits no state from the coordinator
        target_agent = AtomicAgent(
            name=target_agent_name,
            trigger="delegate",
            agents_root=target_path.parent,
            run_id=None,  # generates its own fresh run_id
        )

        start = time.time()
        response = target_agent.call(
            work_item=work_item,
            max_tokens=max_tokens,
            temperature=temperature if temperature is not None else None,
            critical=critical,
            parent_remaining_headroom_usd=remaining_headroom,
        )
        latency_ms = int((time.time() - start) * 1000)

        # Add delegated cost to coordinator's accumulator so subsequent
        # delegate() calls see the true tree-spend. (fix R2-A2)
        self._delegated_cost_this_run += response.cost_usd

        # Log the delegation in the COORDINATOR's log
        log_record: dict = {
            "trigger": "delegate",
            "parent_agent": self.name,
            "delegated_agent": target_agent_name,
            "parent_run_id": self.run_id,
            "model": response.model,
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "cost_usd": response.cost_usd,
            "cost_source": "actor",
            "latency_ms": latency_ms,
            "status": "ok" if not response.skipped else "skipped",
            "summary": summary or f"delegate to {target_agent_name}",
            "delegate_run_id": target_agent.run_id,
        }
        if critical:
            log_record["critical"] = True
        self._log(log_record)

        # Append to per-run delegation rollup
        rollup_entry = {
            "target": target_agent_name,
            "summary": summary or f"delegate to {target_agent_name}",
            "cost_usd": response.cost_usd,
            "latency_ms": latency_ms,
            "delegated_run_id": target_agent.run_id,
            "captures_count": len(response.captures),
        }
        self._delegations_this_run.append(rollup_entry)

        return response

    def delegate_parallel(
        self,
        calls: list[tuple[str, str]],
        max_concurrent: int = 5,
        max_tokens: int | None = None,
        temperature: float | None = None,
        summary_template: str = "delegate {idx} of {total}: {target}",
    ) -> list[Response]:
        """Parallel fan-out to multiple agents.

        Pre-reserves worst-case batch cost against parent headroom (mirrors
        helper_call_parallel pattern). Concurrency capped at 25 (matching
        Anthropic's thread limit). Calls are executed in parallel via
        ThreadPoolExecutor with max_concurrent workers (default 5, hard cap 25).
        Returns Responses in the same order as `calls`.

        All same refusal conditions apply per-call (in-roster, no self).

        Args:
            calls: list of (target_agent_name, work_item) tuples.
            max_concurrent: thread pool size. Clamped to [1, 25].
            max_tokens: output token cap forwarded to each delegate call.
            temperature: temperature forwarded to each delegate call.
            summary_template: template for per-call summaries; supports
                {idx} (1-based), {total}, {target}.

        Raises:
            ValueError: max_concurrent is 0 or > 25.
            NotInRoster / SelfDelegationError: any call fails roster check.
            CostGuardrailBlocked: batch reservation exceeds headroom.
        """
        # Nested delegation guard — spec/15 one-level limit. (fix R2-A3)
        if self.trigger == "delegate":
            raise NestedDelegationRefused(
                f"agent '{self.name}' is already running as a delegated agent "
                f"(trigger='delegate') and cannot delegate further — "
                f"nested delegation refused per spec/15 (one-level only)"
            )

        if max_concurrent < 1 or max_concurrent > 25:
            raise ValueError(
                f"max_concurrent must be between 1 and 25 inclusive; got {max_concurrent}"
            )

        # Validate all targets before reserving cost or spawning threads
        for target, _ in calls:
            self._enforce_roster_membership(target)
            if target == self.name:
                raise SelfDelegationError(
                    f"agent '{self.name}' cannot delegate to itself — one-level delegation only"
                )

        check = self._check_cost_guardrails(critical=False)
        if not check.allow:
            raise CostGuardrailBlocked(
                f"Parallel delegation batch blocked: {check.reason}"
            )

        total = len(calls)

        # Worst-case reservation: each target's max_output_tokens × its output rate.
        # We use the coordinator's default model rate as a conservative proxy
        # (target models may differ, but we don't load targets just for pricing).
        reserved_usd = self._estimate_batch_cost(
            self.config.default_model,
            max_tokens or self.config.max_output_tokens,
            total,
        )
        self._check_batch_reservation(reserved_usd)

        self._log({
            "trigger": "delegate_batch_reservation",
            "parent_agent": self.name,
            "parent_run_id": self.run_id,
            "model": self.config.default_model,
            "input_tokens": 0,
            "output_tokens": 0,
            "reserved_usd": reserved_usd,
            "batch_size": total,
            "status": "ok",
            "summary": f"reserved worst-case ${reserved_usd:.6f} for {total}-delegate batch",
        })

        results: list[Any] = [None] * total

        def call_one(idx: int, target: str, work_item: str):
            summ = summary_template.format(idx=idx + 1, total=total, target=target)
            return idx, self.delegate(
                target_agent_name=target,
                work_item=work_item,
                max_tokens=max_tokens,
                temperature=temperature,
                summary=summ,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_concurrent) as pool:
            futures = {
                pool.submit(call_one, i, target, work_item): i
                for i, (target, work_item) in enumerate(calls)
            }
            for future in concurrent.futures.as_completed(futures):
                try:
                    idx, response = future.result()
                    results[idx] = response
                except Exception as e:
                    idx = futures[future]
                    results[idx] = e

        failures = [(i, r) for i, r in enumerate(results) if isinstance(r, Exception)]
        if failures:
            raise HelperBatchPartialFailure(failures, results)

        actual_usd = sum(
            r.cost_usd for r in results if isinstance(r, Response)
        )
        self._log({
            "trigger": "delegate_batch_release",
            "parent_agent": self.name,
            "parent_run_id": self.run_id,
            "model": self.config.default_model,
            "input_tokens": 0,
            "output_tokens": 0,
            "reserved_usd": reserved_usd,
            "actual_usd": actual_usd,
            "batch_size": total,
            "status": "ok",
            "summary": (
                f"delegate batch complete: actual ${actual_usd:.6f} vs "
                f"reserved ${reserved_usd:.6f}"
            ),
        })

        return results  # type: ignore

    def _estimate_batch_cost(self, model: str, max_tokens: int, batch_size: int) -> float:
        """Compute a worst-case USD estimate for a helper batch.

        Uses max_tokens output per helper at the model's output rate. Input
        tokens are omitted from the estimate (conservative in the other direction,
        but output dominates for short prompts against a haiku-class model).
        Unknown models return 0 — can't estimate, so we don't block.
        """
        output_rate = _costs.PRICING.get(model, {}).get("output", 0.0)
        return round(output_rate * max_tokens / 1_000_000 * batch_size, 6)

    def _check_batch_reservation(self, reserved_usd: float) -> None:
        """Raise CostGuardrailBlocked if the reservation exceeds remaining headroom.

        Remaining headroom is the lower of (daily_cap - today_cost) and
        (monthly_cap - month_cost). If cost_guardrails_enabled is False or
        the reservation is zero, the check is skipped.
        """
        if not self.config.cost_guardrails_enabled or reserved_usd <= 0:
            return
        log_dir = self.agent_root / "log"
        today_cost = _costs.sum_cost_for_period(log_dir, "today", source="actor")
        month_cost = _costs.sum_cost_for_period(log_dir, "this_month", source="actor")
        daily_remaining = (
            self.config.daily_cap_usd - today_cost
            if self.config.daily_cap_usd > 0
            else float("inf")
        )
        monthly_remaining = (
            self.config.monthly_cap_usd - month_cost
            if self.config.monthly_cap_usd > 0
            else float("inf")
        )
        headroom = min(daily_remaining, monthly_remaining)
        if reserved_usd > headroom:
            raise CostGuardrailBlocked(
                f"Parallel helper batch reservation ${reserved_usd:.6f} exceeds "
                f"remaining headroom ${headroom:.6f}"
            )

    def _build_helper_system_prompt(self, sources: list[str]) -> str:
        """Build the helper's system prompt. Empty when no sources are passed."""
        if not sources:
            return ""
        bullet_list = "\n".join(f"- {s}" for s in sources)
        return f"{self.HELPER_PROVENANCE_PROMPT}\n\nSources you are working from:\n{bullet_list}"

    @staticmethod
    def _detect_provenance(text: str, sources: list[str]) -> bool:
        """Heuristic: did the helper preserve attribution back to the sources?

        Returns True when no sources were passed (nothing to preserve) or when
        the output contains attribution-shaped signals — bracketed citations
        ([§2, p3], [section 4], [memo §1]), explicit attribution phrases
        ("according to", "per memo", "§3"), or a verbatim mention of any
        source's basename.

        **Deliberate trade-off — prefers false-positives over false-negatives.**
        The parent agent treats ``provenance_preserved=False`` as "not citable",
        which silently downgrades the quality of every fact in that helper
        output. A false-negative (missing real provenance loss) therefore has a
        much higher consequence than a false-positive (letting a borderline
        output through unchallenged). The heuristic is intentionally lenient:
        any attribution-shaped signal in the output counts, even if it's weak.

        **Consequences for operators:** if your use-case requires strict
        provenance verification, override ``HelperResult.provenance_preserved``
        to ``False`` after inspecting the output before passing facts downstream.

        This behaviour is specified in spec/10 Wave 8 (helper provenance) and
        was reviewed and retained intentionally — do not tighten the heuristic
        without updating that spec section and re-evaluating the false-negative
        rate on the existing eval corpus.
        """
        if not sources:
            return True
        if not text or not text.strip():
            return False

        # Bracketed-citation check: at least one [...] containing common citation
        # markers (section symbol, "p<digit>", "section", "page", "paragraph").
        bracket_pattern = re.compile(
            r"\[[^\]]*(?:§|sect|page|p\.|p\s*\d|para|para\.)[^\]]*\]",
            re.IGNORECASE,
        )
        if bracket_pattern.search(text):
            return True

        # Inline attribution phrases.
        inline_pattern = re.compile(
            r"(?:\baccording to\b|\bper\s+\w|\bcited in\b|§\s*\d|\(p\.?\s*\d)",
            re.IGNORECASE,
        )
        if inline_pattern.search(text):
            return True

        # Verbatim source basename mention (last path component, stem).
        for src in sources:
            stem = src.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            if stem and len(stem) >= 3 and stem.lower() in text.lower():
                return True

        return False

    # ────────────────────────────────────────────────────────────
    # Cost guardrails

    def _check_cost_guardrails(
        self,
        critical: bool = False,
        extra_in_flight_cost_usd: float = 0.0,
        parent_remaining_headroom_usd: float | None = None,
    ) -> CostCheckResult:
        """Run before each LLM call. Returns CostCheckResult.

        extra_in_flight_cost_usd: accumulated spend from the current tool loop
            that has not yet been persisted to the log file. Added to the
            disk-read total before cap comparison so mid-loop iterations see
            the true running spend. (fix R2-A1)

        parent_remaining_headroom_usd: when set by a delegating coordinator,
            this call's effective cap is clamped to min(own remaining, parent
            headroom), enforcing the coordinator's cap as a tree-cap. (fix R2-A2)
        """
        if not self.config.cost_guardrails_enabled:
            return CostCheckResult(allow=True)

        if critical:
            return CostCheckResult(allow=True, reason="critical_override")

        log_dir = self.agent_root / "log"
        today_cost = _costs.sum_cost_for_period(log_dir, "today", source="actor") + extra_in_flight_cost_usd
        month_cost = _costs.sum_cost_for_period(log_dir, "this_month", source="actor") + extra_in_flight_cost_usd

        daily_pct = (today_cost / self.config.daily_cap_usd) if self.config.daily_cap_usd > 0 else 0
        monthly_pct = (month_cost / self.config.monthly_cap_usd) if self.config.monthly_cap_usd > 0 else 0

        # Fire warnings (idempotent — won't fire twice for same threshold/day)
        self._maybe_fire_warning("daily", daily_pct)
        self._maybe_fire_warning("monthly", monthly_pct)

        if daily_pct >= 1.0:
            return self._cap_action(
                self.config.daily_cap_action,
                f"daily cap hit (${today_cost:.2f}/${self.config.daily_cap_usd:.2f})",
            )
        if monthly_pct >= 1.0:
            return self._cap_action(
                self.config.monthly_cap_action,
                f"monthly cap hit (${month_cost:.2f}/${self.config.monthly_cap_usd:.2f})",
            )

        # Parent headroom check: coordinator's remaining budget caps the delegate.
        # (fix R2-A2) — clamp to min(own remaining, parent headroom)
        if parent_remaining_headroom_usd is not None:
            daily_remaining = (
                self.config.daily_cap_usd - today_cost
                if self.config.daily_cap_usd > 0
                else float("inf")
            )
            monthly_remaining = (
                self.config.monthly_cap_usd - month_cost
                if self.config.monthly_cap_usd > 0
                else float("inf")
            )
            own_remaining = min(daily_remaining, monthly_remaining)
            effective_remaining = min(own_remaining, parent_remaining_headroom_usd)
            if effective_remaining <= 0:
                return CostCheckResult(
                    allow=False,
                    action="skip",
                    reason=(
                        f"parent coordinator headroom exhausted "
                        f"(${parent_remaining_headroom_usd:.6f} remaining)"
                    ),
                )

        return CostCheckResult(allow=True)

    def _maybe_fire_warning(self, period: str, pct: float) -> None:
        state_path = self.agent_root / ".cost-warnings.json"
        state = _costs.load_warning_state(state_path)
        today_key = date.today().isoformat() if period == "daily" else date.today().strftime("%Y-%m")

        for threshold in self.config.warning_thresholds:
            already = state.get(period, {}).get(today_key, {}).get(str(threshold), False)
            if pct >= threshold and not already:
                # Fire (just log to journal/log for v1; future: telegram/email)
                severity = "WARN" if threshold >= 0.80 else "INFO"
                self._log({
                    "trigger": "cost_warning",
                    "model": "n/a",
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "status": "ok",
                    "summary": f"{severity}: {period} cost at {pct*100:.0f}% of cap (threshold {threshold*100:.0f}%)",
                })
                state.setdefault(period, {}).setdefault(today_key, {})[str(threshold)] = True

        _costs.save_warning_state(state_path, state)

    def _cap_action(self, action: str, reason: str) -> CostCheckResult:
        if action == "skip":
            return CostCheckResult(allow=False, action="skip", reason=reason)
        if action == "fallback":
            return CostCheckResult(
                allow=True, action="fallback", reason=reason,
                fallback_model=self.config.fallback_model,
            )
        if action == "alert":
            return CostCheckResult(allow=True, action="alert", reason=reason)
        raise ValueError(f"unknown cap action: {action}")

    # ────────────────────────────────────────────────────────────
    # Logging

    def _log(self, record: dict) -> None:
        """Append one JSONL line to log/YYYY-MM/YYYY-MM-DD.jsonl."""
        record = {"ts": datetime.now().astimezone().isoformat(), **record}
        today = date.today()
        log_path = self.agent_root / "log" / today.strftime("%Y-%m") / f"{today.isoformat()}.jsonl"
        atomic_append_jsonl(log_path, json.dumps(record))

    def _derive_summary(self, work_item: str) -> str:
        """Short summary of the work item for log records."""
        if len(work_item) <= 80:
            return work_item.strip()
        return work_item[:77].strip() + "..."

    # ────────────────────────────────────────────────────────────
    # Convenience

    def __repr__(self):
        return f"AtomicAgent(name={self.name!r}, trigger={self.trigger!r}, root={self.agent_root})"
