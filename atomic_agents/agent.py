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
import re
import time
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

import frontmatter

from . import _capture, _cascade, _costs, _llm, _model, _tools
from ._io import atomic_append_jsonl, atomic_write
from ._locks import AgentLock
from ._platform import get_agents_root
from ._schema import validate_atomic_note_frontmatter
from .goal import parse_agent_mode
from .exceptions import (
    AgentLockBusy,
    AtomicAgentsError,
    CostGuardrailBlocked,
    HelperBatchPartialFailure,
)
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
    ):
        self.name = name
        self.trigger = trigger
        self.agents_root = agents_root or get_agents_root()
        self.agent_root = self.agents_root / name
        self.run_id = run_id or self._generate_run_id()

        if not self.agent_root.exists():
            raise AtomicAgentsError(
                f"Agent folder not found: {self.agent_root}. "
                f"Set ATOMIC_AGENTS_ROOT env var or create the agent."
            )

        # Cascade detection — None for single-agent layouts (load behaves as before),
        # populated for paths shaped <system>/projects/<project>/agents/<role>/.
        self.cascade: _cascade.CascadePaths | None = _cascade.detect_cascade(self.agent_root)

        # Per-call helper-provenance rollup (spec/13 Layer 3). Reset at the
        # start of each call(); appended to by helper_call(). Empty list
        # means either no helpers ran or the call started outside call().
        self._helpers_this_run: list[dict] = []

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

    @staticmethod
    def _generate_run_id() -> str:
        return f"run-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}"

    @staticmethod
    def _capture_tool_definitions(model: str) -> list[dict] | None:
        """Return the atomic_capture tool definition formatted for the agent's provider.

        Returns None for providers without tool-call support — the agent then
        falls back to Path 2 fenced-block parsing only.
        """
        if model.startswith("claude-"):
            return [_capture.anthropic_tool_definition()]
        if model.startswith("gpt-") or model.startswith("moonshot/"):
            return [_capture.openai_tool_definition()]
        return None

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
        else:
            model_data = _model.parse_model_md(self.agent_root / "model.md")
            tools_data = _tools.parse_tools_md(self.agent_root / "tools.md")

        return AgentConfig(
            default_model=model_data["default_model"],
            fallback_model=model_data["fallback_model"],
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
            external_apis=tools_data["external_apis"],
            hard_nos=tools_data["hard_nos"],
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
        memory_index = self.agent_root / "memory" / "INDEX.md"
        if memory_index.exists():
            self._memory_index_text = memory_index.read_text(encoding="utf-8")
        wiki_index = self.agent_root / "wiki" / "INDEX.md"
        if wiki_index.exists():
            self._wiki_index_text = wiki_index.read_text(encoding="utf-8")

    def _load_pinned_notes(self) -> None:
        memory_dir = self.agent_root / "memory"
        if not memory_dir.exists():
            return
        pinned = []
        for path in sorted(memory_dir.glob("*.md")):
            if path.name == "INDEX.md":
                continue
            try:
                parsed = frontmatter.load(path)
                if parsed.metadata.get("pinned") is True:
                    pinned.append(self._render_note_for_context(path, parsed))
            except Exception:
                continue
        self._pinned_notes = pinned[:PINNED_MAX]

    def _load_recent_notes(self, n: int = RECENT_NOTES_DEFAULT) -> None:
        memory_dir = self.agent_root / "memory"
        if not memory_dir.exists():
            return
        notes_with_dates = []
        for path in memory_dir.glob("*.md"):
            if path.name == "INDEX.md":
                continue
            try:
                parsed = frontmatter.load(path)
                # Skip pinned (already loaded) and archived
                if parsed.metadata.get("pinned"):
                    continue
                if parsed.metadata.get("archived"):
                    continue
                if parsed.metadata.get("superseded_by"):
                    continue
                last_seen = parsed.metadata.get("last_seen", "0000-00-00")
                notes_with_dates.append((str(last_seen), path, parsed))
            except Exception:
                continue
        notes_with_dates.sort(reverse=True)
        self._recent_notes = [
            self._render_note_for_context(path, parsed)
            for _, path, parsed in notes_with_dates[:n]
        ]

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
    ) -> Response:
        """Make the LLM call. Returns a Response with captures populated.

        critical=True bypasses cost guardrails (still logged with critical: true).
        write_captures=False extracts but doesn't persist captures (dry-run mode).
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

        try:
            # Reset helper-provenance rollup for this run (spec/13 Layer 3)
            self._helpers_this_run = []
            # Cost guardrails check
            check = self._check_cost_guardrails(critical=critical)
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

            # Pick model — fallback if guardrail says so, else override, else default
            if check.action == "fallback" and check.fallback_model:
                model = check.fallback_model
            else:
                model = model_override or self.config.default_model

            # Build prompt
            system_prompt = self.assemble_system_prompt()
            messages = [{"role": "user", "content": work_item}]

            # Call LLM with the atomic_capture tool available — agent can use
            # Path 1 (tool call) or Path 2 (fenced JSON block) per spec/05; we
            # extract from both and dedupe.
            tool_definitions = self._capture_tool_definitions(model)

            start = time.time()
            raw = _llm.call_llm(
                model=model,
                system_prompt=system_prompt,
                messages=messages,
                max_tokens=max_tokens or self.config.max_output_tokens,
                temperature=temperature if temperature is not None else 0.6,
                cache_control_breakpoints=[len(system_prompt)],
                tools=tool_definitions,
            )
            latency_ms = int((time.time() - start) * 1000)

            cost = _costs.calc_cost(
                model, raw.input_tokens, raw.output_tokens, raw.cache_hit_tokens
            )

            # Extract captures from BOTH text (Path 2) and tool_use blocks (Path 1)
            captures, parse_failures = _capture.extract_all_captures(
                raw.text, tool_uses=raw.tool_uses,
            )

            # Write captures if enabled
            written_captures = []
            if write_captures and captures:
                for c in captures:
                    try:
                        path = _capture.write_atomic_note(
                            self.agent_root, c, self.config.write_paths
                        )
                        written_captures.append(c)
                    except Exception as e:
                        # Log capture write failure but don't fail the whole call
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
                text=raw.text,
                model=model,
                input_tokens=raw.input_tokens,
                output_tokens=raw.output_tokens,
                cache_hit_tokens=raw.cache_hit_tokens,
                cache_miss_tokens=raw.cache_miss_tokens,
                cost_usd=cost,
                latency_ms=latency_ms,
                summary=self._derive_summary(work_item),
                raw=raw.raw or {},
                captures=written_captures,
            )

            # Log
            log_record: dict = {
                "trigger": self.trigger,
                "model": model,
                "input_tokens": raw.input_tokens,
                "output_tokens": raw.output_tokens,
                "cache_hit_tokens": raw.cache_hit_tokens,
                "cache_miss_tokens": raw.cache_miss_tokens,
                "cost_usd": cost,
                "latency_ms": latency_ms,
                "status": "ok",
                "summary": response.summary,
                "run_id": self.run_id,
                "agent_mode": self.agent_mode,
            }
            if check.action == "fallback":
                log_record["fallback"] = True
            if critical:
                log_record["critical"] = True
            if parse_failures:
                log_record["capture_parse_failures"] = len(parse_failures)
            if self._helpers_this_run:
                # Spec/13 Layer 3 — research log: roll up helper provenance
                # into the parent run record so an audit can trace every fact
                # back to the helper invocation that produced it.
                log_record["helper_provenance"] = list(self._helpers_this_run)
            self._log(log_record)

            return response

        finally:
            lock.release()

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
        )
        latency_ms = int((time.time() - start) * 1000)
        cost = _costs.calc_cost(actual_model, raw.input_tokens, raw.output_tokens)

        provenance_preserved = self._detect_provenance(raw.text, sources_list)

        log_record: dict = {
            "trigger": "helper",
            "parent_agent": self.name,
            "parent_run_id": self.run_id,
            "model": actual_model,
            "input_tokens": raw.input_tokens,
            "output_tokens": raw.output_tokens,
            "cost_usd": cost,
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

        total = len(prompts)
        results: list[Any] = [None] * total  # list[HelperResult | Exception]

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

        return results  # type: ignore

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

        Conservative: prefers a False-positive over a False-negative — we'd
        rather rare flag something as preserved when it wasn't than miss real
        provenance loss.
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

    def _check_cost_guardrails(self, critical: bool = False) -> CostCheckResult:
        """Run before each LLM call. Returns CostCheckResult."""
        if not self.config.cost_guardrails_enabled:
            return CostCheckResult(allow=True)

        if critical:
            return CostCheckResult(allow=True, reason="critical_override")

        log_dir = self.agent_root / "log"
        today_cost = _costs.sum_cost_for_period(log_dir, "today")
        month_cost = _costs.sum_cost_for_period(log_dir, "this_month")

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
