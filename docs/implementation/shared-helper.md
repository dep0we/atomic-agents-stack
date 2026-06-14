# Shared Helper — `atomic_agents`

The Python module both the cron and skill runtimes can call to do the boilerplate of being an Atomic Agent. Lives in `~/projects/automations/atomic_agents` (deployed to `<deployed copy of the package>`).

This doc sketches the public API. Implementation lives in the automations repo.

---

## Why a shared helper

Without it, every agent's cron script duplicates the same code:
- Reading persona files
- Loading INDEXes
- Selecting recent atomic notes
- Calling Anthropic API with the right model
- Parsing capture markers
- Writing the capture + updating INDEX
- Writing journal + log

That's ~300 lines per agent script. With the helper, each script is ~50 lines.

The helper also enforces the spec — `tools.md` write paths, frontmatter validation, runtime assembly order. It's the spec made executable.

---

## Public API

### `AtomicAgent` class

```python
from automations.lib.atomic_agents import AtomicAgent

agent = AtomicAgent(
    name: str,                  # e.g. "caldwell"
    vault_root: Path,            # Path.home() / "docs"
    trigger: Literal["cron", "skill", "manual"],
    config: AgentConfig | None = None,  # overrides for testing
)
```

### Loading methods

```python
agent.load_persona() -> None
    # Reads IDENTITY.md, SOUL.md, USER.md from persona/
    # Stores in agent._persona_blocks for prompt assembly

agent.load_tools() -> None
    # Reads tools.md
    # Validates and stores read/write paths, hard NOs
    # Used at write-time to enforce boundaries

agent.load_model() -> None
    # Reads model.md
    # Sets agent.model_id, fallback_model_id, token_caps

agent.load_indexes() -> None
    # Reads memory/INDEX.md and wiki/INDEX.md (if exists)

agent.load_pinned_atomic_units() -> None
    # Scans memory/ and wiki/ for files with frontmatter pinned: true
    # Loads them all

agent.load_recent_atomic_units(n: int = 5) -> None
    # Loads the N most recently captured atomic units (memory/ only,
    # wiki/ pages aren't "captured" the same way)

agent.load_recent_journal(n: int = 1) -> None
    # Loads the N most recent journal entries by date

agent.load_atomic_unit(filename: str) -> AtomicUnit
    # Loads a specific note or wiki page by filename
    # Used during conversation when agent asks for a specific memory
```

### Calling methods

```python
agent.call(
    work_item: str,
    model_override: str | None = None,
    streaming: bool = False,
    max_tokens: int | None = None,  # defaults to model.md value
) -> Response
    # Assembles system prompt per runtime-assembly spec
    # Calls Anthropic API
    # Returns Response with .text, .input_tokens, .output_tokens, .cost_usd, etc.
    # Auto-falls-back to model.md fallback on failure
    # Auto-skips if daily cap hit
```

### Capture methods

```python
agent.extract_captures(response: Response) -> list[Capture]
    # Parses <atomic_capture>...</atomic_capture> blocks from response.text
    # Validates each against the schema
    # Returns list of Capture objects (or raises ValidationError on bad capture)

agent.write_atomic_note(capture: Capture) -> Path
    # Writes capture to memory/{type}_{topic}.md with full frontmatter
    # Updates memory/INDEX.md
    # Refuses to write outside the agent's own folder (enforces tools.md)
    # Returns the written path

agent.write_wiki_page(page: WikiPage) -> Path
    # Same pattern for wiki/ pages
```

### Output methods

```python
agent.append_journal(content: dict) -> Path
    # Appends to journal/YYYY-MM/YYYY-MM-DD.md
    # Creates the file + month folder if needed

agent.write_log(record: dict) -> None
    # Appends one JSON line to log/YYYY-MM/YYYY-MM-DD.jsonl
    # Always called at end of run; even errors get logged
```

### Validation methods

```python
agent.validate_atomic_unit(unit: AtomicUnit | dict) -> ValidationResult
    # Checks frontmatter against schema_version 1 spec
    # Returns ValidationResult with .ok, .errors

agent.lint() -> LintReport
    # Runs the lint pass per spec/05-capture-rules.md
    # Returns dict of duplicates, contradictions, stale, expired, orphans, drift
```

---

## Type definitions

```python
@dataclass
class AtomicUnit:
    schema_version: int
    name: str
    description: str
    type: Literal["user", "feedback", "project", "decision", "reference", "wiki_page"]
    captured: date
    last_seen: date
    sources: list[str]
    confidence: Literal["high", "medium", "low"]
    pinned: bool = False
    expires_at: date | None = None
    supersedes: str | None = None
    superseded_by: str | None = None
    tags: list[str] = field(default_factory=list)
    body: str = ""
    filename: str = ""

@dataclass
class Capture:
    type: str
    name: str
    description: str
    confidence: str
    sources: list[str]
    body: str
    # validation derives the filename from type + topic of name

@dataclass
class Response:
    text: str
    model_id: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    cache_hit: bool
    raw: dict  # full Anthropic API response

@dataclass
class LintReport:
    duplicates: list[tuple[str, str]]
    contradictions: list[tuple[str, str]]
    stale: list[str]
    expired: list[str]
    orphans: list[str]
    schema_drift: list[tuple[str, list[str]]]  # (filename, list of broken fields)
```

---

## Implementation notes

### Frontmatter parsing

Use the `python-frontmatter` library or implement a small YAML parser. Don't roll your own — frontmatter has edge cases.

```python
import frontmatter

def load_atomic_unit(path: Path) -> AtomicUnit:
    parsed = frontmatter.load(path)
    return AtomicUnit(
        schema_version=parsed["schema_version"],
        name=parsed["name"],
        ...
        body=parsed.content,
        filename=path.name,
    )
```

### INDEX.md updates — atomic, with correct ordering

The note file MUST be on disk before the INDEX references it. Otherwise the INDEX can point at a non-existent file (orphan), and the spec considers that a bug.

**Correct write order:**

```python
def write_atomic_note(self, capture: Capture) -> Path:
    self._acquire_lock()  # see locking section below
    try:
        # Phase 1: write the note file atomically
        target = self._note_path_for(capture)
        self._enforce_write_path(target)
        self._validate(capture)

        tmp_note = target.with_suffix(target.suffix + ".tmp")
        tmp_note.write_text(self._render_note(capture))
        os.fsync(open(tmp_note).fileno())  # force to disk before rename
        tmp_note.rename(target)             # atomic on POSIX

        # Phase 2: update the INDEX, also atomically (temp + rename)
        index_path = self.vault_root / self.name / "memory" / "INDEX.md"
        new_index_text = self._render_index_with_entry(index_path, capture)

        tmp_index = index_path.with_suffix(".md.tmp")
        tmp_index.write_text(new_index_text)
        os.fsync(open(tmp_index).fileno())
        tmp_index.rename(index_path)

    finally:
        self._release_lock()
    return target
```

**Why this order:**

- The note file lands on disk **before** the INDEX is updated. If the INDEX update fails (rare — disk full, permission issue, crash), the result is an orphan note (file exists, no INDEX entry). The lint pass catches orphans and surfaces them.
- The reverse order — INDEX-first — would mean the INDEX could point at a missing file, which is a worse failure mode (broken pointers vs. unindexed file).
- Both writes use temp-file + `fsync` + `rename`. Rename is atomic on POSIX filesystems; the file either exists with full contents or doesn't exist at all. No partial-write states.

**On crash / mid-write recovery:**

- Temp files (`*.tmp`) left on disk after a crash are recoverable evidence — the lint pass detects them and offers to either complete the operation (re-render and rename) or discard.
- The lint pass should be idempotent: running it on a clean vault is a no-op.

### File locking — required, not optional

Cron and skill can run simultaneously. Obsidian Sync can mutate files concurrently. Git pull/deploy can race.

**Per-agent lock file:**

```python
import fcntl

def _acquire_lock(self):
    lock_path = self.vault_root / self.name / ".lock"
    self._lock_fd = open(lock_path, "w")
    try:
        fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # Lock held by another process. Either wait or fail.
        # For cron: fail fast and skip — try again next cycle.
        # For skill: wait up to 30 seconds, then surface to the operator.
        raise AgentLockBusy(f"Agent {self.name} is locked by another process")

def _release_lock(self):
    if hasattr(self, "_lock_fd"):
        fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        self._lock_fd.close()
```

**Stale lock recovery:**

- If a process holding the lock crashes, the OS releases the `flock` automatically — no manual cleanup needed.
- The `.lock` file itself can be deleted between sessions; the file is just a target for `flock`.

**Behavior matrix when lock is held:**

| Caller | Lock held by | Action |
|---|---|---|
| Cron job | Another cron job | Skip this run. Log skip. Try again next cycle. |
| Cron job | Skill session | Skip. Don't compete with interactive session. |
| Skill session | Cron job | Wait up to 30 seconds; cron is fast. If still locked, surface to the operator. |
| Skill session | Another skill session | Wait up to 30 seconds. Probably stale state from a crashed prior session. |

**Obsidian Sync considerations:**

Obsidian Sync writes can land mid-operation. The atomic rename pattern protects against partial-content writes, but doesn't prevent Sync from overwriting a file Caldwell just updated. Mitigation:

- Obsidian Sync detects local changes and queues them; brief writes from the helper land cleanly
- Run cron jobs at low-activity times (e.g., 3am) to minimize collision
- Lint pass detects content divergence (e.g., frontmatter inconsistency) and flags it for the operator
- Long-term fix: a small Sync-aware wrapper that quiesces sync briefly during writes (deferred to v2)

### Write-path enforcement

The helper reads `tools.md` at startup and stores allowed write paths. Every write call validates the destination:

```python
def _enforce_write_path(self, path: Path) -> None:
    allowed = self.tools.write_paths
    if not any(path.resolve().is_relative_to(p.resolve()) for p in allowed):
        raise WritePathViolation(f"Cannot write to {path}; not in tools.md write_paths")
```

This is the load-bearing safety mechanism. Don't skip it.

### Anthropic API client

Use the official `anthropic` Python SDK. Handle:

- 429 rate limits → exponential backoff, max 3 retries
- 5xx → same
- 400 (bad request) → don't retry, surface error
- Streaming for skill version, non-streaming for cron

Always pass `cache_control: {"type": "ephemeral"}` at the breakpoints from [../spec/04-runtime-assembly](../spec/04-runtime-assembly.md) to enable Anthropic's prompt cache.

### Cost calculation

Pull pricing from a constant in the helper:

```python
PRICING = {
    "claude-opus-4-7-20260101": {"input": 15.0, "output": 75.0},  # per MTok
    "claude-sonnet-4-6-20260101": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.0},
}

def calc_cost(model_id, input_tokens, output_tokens, cache_hit_tokens=0):
    p = PRICING[model_id]
    cache_input_tokens = input_tokens - cache_hit_tokens  # cache hits are 10x cheaper
    return (
        cache_hit_tokens * p["input"] / 10_000_000
        + cache_input_tokens * p["input"] / 1_000_000
        + output_tokens * p["output"] / 1_000_000
    )
```

Update `PRICING` when Anthropic changes rates.

### Cost guardrails — multi-tier warnings + cap actions

This is more involved than just "stop at the cap." Cost guardrails per [../spec/09-cost-observability](../spec/09-cost-observability.md) have:

- A master `enabled` switch (default `false` for new agents)
- Daily AND monthly caps (USD-denominated)
- Per-cap action: `skip` / `fallback` / `alert`
- Multi-tier warnings at 50% and 80% **before** the cap action fires
- Critical-flag override that bypasses caps

```python
from dataclasses import dataclass
from datetime import date
import json

@dataclass
class CostCheckResult:
    allow: bool             # if False, do not call the API
    action: str | None      # 'skip' | 'fallback' | 'alert' | None
    reason: str = ""
    fallback_model: str | None = None

class AtomicAgent:
    # ... other methods ...

    def _sum_cost_for_period(self, period: str) -> float:
        """period in {'today', 'this_month'}. Returns total cost_usd."""
        if period == "today":
            log_path = self._log_path_for(date.today())
            paths = [log_path] if log_path.exists() else []
        else:
            month_dir = self.vault_root / self.name / "log" / date.today().strftime("%Y-%m")
            paths = list(month_dir.glob("*.jsonl")) if month_dir.exists() else []

        total = 0.0
        for path in paths:
            for line in path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    total += rec.get("cost_usd", 0.0)
                except json.JSONDecodeError:
                    continue
        return total

    def _maybe_fire_warning(self, period: str, pct: float) -> None:
        """Fire 50% / 80% warnings idempotently — won't fire twice for same threshold/day."""
        thresholds = self.cost_guardrails.warning_thresholds  # e.g., [0.50, 0.80]
        warning_state_path = self.vault_root / self.name / ".cost-warnings.json"
        state = self._load_warning_state(warning_state_path)
        today_key = date.today().isoformat() if period == "today" else date.today().strftime("%Y-%m")

        for threshold in thresholds:
            already_fired = state.get(period, {}).get(today_key, {}).get(str(threshold), False)
            if pct >= threshold and not already_fired:
                self._send_alert(
                    severity="WARN" if threshold >= 0.80 else "INFO",
                    period=period,
                    pct=pct,
                    threshold=threshold,
                )
                state.setdefault(period, {}).setdefault(today_key, {})[str(threshold)] = True

        self._save_warning_state(warning_state_path, state)

    def _check_cost_guardrails(self, critical: bool = False) -> CostCheckResult:
        """Check before every API call. Returns a CostCheckResult."""
        if not self.cost_guardrails.enabled:
            return CostCheckResult(allow=True, action=None)

        if critical:
            # Critical flag bypasses caps but still fires warnings
            return CostCheckResult(allow=True, action=None, reason="critical_override")

        # sum_cost_for_period returns CostReadResult(total_usd, degraded, dropped_records)
        # (not a bare float) — see spec/09 §"Cost-read error posture" (#495 PR1)
        today_result = self._sum_cost_for_period("today")
        month_result = self._sum_cost_for_period("this_month")

        # Fail-closed if either read is degraded (unreadable / majority-corrupt)
        if today_result.degraded or month_result.degraded:
            return CostCheckResult(allow=False, action="skip",
                                   reason="cost data unreadable — fail-closed",
                                   cost_data_degraded=True)

        today_cost = today_result.total_usd
        month_cost = month_result.total_usd
        daily_pct = today_cost / self.cost_guardrails.daily_cap_usd
        monthly_pct = month_cost / self.cost_guardrails.monthly_cap_usd

        # Fire any 50% / 80% warnings
        self._maybe_fire_warning("today", daily_pct)
        self._maybe_fire_warning("this_month", monthly_pct)

        # Cap actions at 100%
        if daily_pct >= 1.0:
            return self._cap_action(self.cost_guardrails.daily_cap_action,
                                     reason=f"daily cap hit ({today_cost:.2f}/{self.cost_guardrails.daily_cap_usd:.2f})")
        if monthly_pct >= 1.0:
            return self._cap_action(self.cost_guardrails.monthly_cap_action,
                                     reason=f"monthly cap hit ({month_cost:.2f}/{self.cost_guardrails.monthly_cap_usd:.2f})")

        return CostCheckResult(allow=True, action=None)

    def _cap_action(self, action: str, reason: str) -> CostCheckResult:
        if action == "skip":
            self._send_alert(severity="CAP", action="skip", reason=reason)
            return CostCheckResult(allow=False, action="skip", reason=reason)
        elif action == "fallback":
            self._send_alert(severity="CAP", action="fallback", reason=reason)
            return CostCheckResult(allow=True, action="fallback",
                                    reason=reason,
                                    fallback_model=self.fallback_model_id)
        elif action == "alert":
            self._send_alert(severity="CAP", action="alert", reason=reason)
            return CostCheckResult(allow=True, action="alert", reason=reason)
        else:
            raise ValueError(f"Unknown cap action: {action}")

    def _send_alert(self, severity: str, **context) -> None:
        """Route to alert_channel (telegram | email | journal | log_only)."""
        channel = self.cost_guardrails.alert_channel
        message = self._format_alert_message(severity, **context)
        if channel == "telegram":
            self._telegram_send(message)
        elif channel == "email":
            self._email_send(message)
        elif channel == "journal":
            self._journal_append(f"\n## Cost guardrail alert\n\n{message}\n")
        elif channel == "log_only":
            pass  # the warning gets recorded in the next log line via metadata
        else:
            raise ValueError(f"Unknown alert_channel: {channel}")
```

### Integration into `agent.call()`

```python
def call(self, work_item: str, model_override: str | None = None,
         critical: bool = False, **kwargs) -> Response:
    """Make an LLM call with cost-guardrail enforcement."""
    self._acquire_lock()
    try:
        # 1. Check cost guardrails before any API call
        check = self._check_cost_guardrails(critical=critical)
        if not check.allow:
            self._log({
                "trigger": self.trigger,
                "model": self.model_id,
                "status": "skipped",
                "summary": f"Skipped: {check.reason}",
            })
            return Response.skipped(check.reason)

        # 2. If fallback action, swap model
        model = check.fallback_model if check.action == "fallback" else (model_override or self.model_id)

        # 3. Build prompt + call API
        system_prompt = self._assemble_system_prompt()
        response = self._anthropic_call(model, system_prompt, work_item, **kwargs)

        # 4. Compute cost (using actual model used, not just default)
        response.cost_usd = self._calc_cost(model, response.input_tokens,
                                              response.output_tokens,
                                              response.cache_hit_tokens)

        # 5. Log
        self._log({
            "trigger": self.trigger,
            "model": model,
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "cost_usd": response.cost_usd,
            "cache_hit_tokens": response.cache_hit_tokens,
            "cache_miss_tokens": response.input_tokens - response.cache_hit_tokens,
            "latency_ms": response.latency_ms,
            "status": "ok",
            "summary": response.summary or "",
            "fallback": check.action == "fallback",
            "critical": critical,
        })
        return response
    finally:
        self._release_lock()
```

### Important: log the model that was actually used

When fallback fires, the `model` field in the log is the **fallback model**, not the configured default. This is what makes the dashboard show model switches over time — it always reflects what really happened, never the intent.

### Suggested-caps generation

After 14 days of `enabled: false` operation, the dashboard's per-agent page surfaces a banner with suggested values. The shared helper exposes this:

```python
def suggest_cost_caps(self) -> dict | None:
    """Return suggested daily/monthly caps based on observed 14-day usage,
    or None if not enough data."""
    fourteen_days_ago = date.today() - timedelta(days=14)
    runs = load_runs(self.vault_root, self.name, fourteen_days_ago)
    if len(runs) < 7:  # need at least a week's data to suggest
        return None

    daily_costs: dict[date, float] = defaultdict(float)
    for r in runs:
        daily_costs[r.ts.date()] += r.cost_usd

    avg_daily = sum(daily_costs.values()) / max(len(daily_costs), 1)
    p95_daily = sorted(daily_costs.values())[int(len(daily_costs) * 0.95)] if len(daily_costs) > 1 else avg_daily
    monthly_total = sum(daily_costs.values()) * (30 / len(daily_costs))

    return {
        "daily_cap_usd": round(max(p95_daily * 2.0, avg_daily * 3.0), 2),
        "monthly_cap_usd": round(monthly_total * 1.5, 2),
        "based_on_days": len(daily_costs),
        "avg_daily": round(avg_daily, 2),
        "p95_daily": round(p95_daily, 2),
    }
```

Dashboard displays these as the "Apply suggested caps" UX.

---

### Old behavior (DEPRECATED — kept for reference)

The earlier version of this section had a token-cap-only check that didn't track cost or warnings. Removed in favor of the cost-guardrails approach above. Token caps still exist as a hard backstop in `model.md` (`max_input_tokens`, `max_output_tokens`) — they limit per-call size, not aggregate spend.

---

## Helper functions

Per [../spec/10-helpers](../spec/10-helpers.md), an Atomic Agent can delegate transformation subtasks (summarize, extract, classify, score) to cheaper LLMs via two methods on `AtomicAgent`. Helpers are stateless function calls — no persona load, no memory access, no vault writes.

### `helper_call()` — sequential

```python
def helper_call(
    self,
    prompt: str,
    model: str = "claude-haiku-4-5-20251001",
    max_tokens: int = 1024,
    temperature: float = 0.3,
    summary: str = "",
) -> str:
    """Make a single cheap-model call for a transformation subtask.

    Inherits the parent agent's tools.md (advisory) and cost guardrails
    (enforced via _check_cost_guardrails). Logged with trigger=helper.
    Returns the model's text response only — no metadata.
    """
    # Inherit cost guardrails from parent
    check = self._check_cost_guardrails(critical=False)
    if not check.allow:
        raise CostGuardrailBlocked(
            f"Helper call blocked: parent cap hit ({check.reason})"
        )

    actual_model = check.fallback_model if check.action == "fallback" else model

    start = time.time()
    response = self._llm_call(
        model=actual_model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        temperature=temperature,
    )
    latency_ms = int((time.time() - start) * 1000)

    cost = self._calc_cost(
        actual_model,
        response.input_tokens,
        response.output_tokens,
        cache_hit_tokens=0,  # helpers don't typically benefit from cache
    )

    self._log({
        "trigger": "helper",
        "parent_agent": self.name,
        "parent_run_id": self.current_run_id,
        "model": actual_model,
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
        "cost_usd": cost,
        "latency_ms": latency_ms,
        "status": "ok",
        "summary": summary or "helper call",
    })

    return response.text
```

### `helper_call_parallel()` — fan-out

```python
def helper_call_parallel(
    self,
    prompts: list[str],
    model: str = "claude-haiku-4-5-20251001",
    max_tokens: int = 1024,
    temperature: float = 0.3,
    max_concurrent: int = 5,
    summary_template: str = "helper call {idx} of {total}",
) -> list[str]:
    """Run multiple helper calls in parallel. Useful for fan-out / map-reduce.

    max_concurrent caps simultaneous in-flight calls (default 5) to stay
    under provider rate limits. Returns results in input order.
    """
    import concurrent.futures

    # Pre-check guardrails ONCE — if cap is already hit, refuse the whole batch
    check = self._check_cost_guardrails(critical=False)
    if not check.allow:
        raise CostGuardrailBlocked(
            f"Parallel helper batch blocked: parent cap hit ({check.reason})"
        )

    total = len(prompts)
    results: list[str | Exception] = [None] * total

    def call_one(idx: int, prompt: str) -> tuple[int, str]:
        text = self.helper_call(
            prompt=prompt,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            summary=summary_template.format(idx=idx + 1, total=total),
        )
        return idx, text

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_concurrent) as pool:
        futures = [pool.submit(call_one, i, p) for i, p in enumerate(prompts)]
        for future in concurrent.futures.as_completed(futures):
            try:
                idx, text = future.result()
                results[idx] = text
            except Exception as e:
                # On individual failure, store the exception; surface after the batch
                idx = futures.index(future)
                results[idx] = e

    # If any call failed, raise — but only after all the others finished
    failures = [(i, r) for i, r in enumerate(results) if isinstance(r, Exception)]
    if failures:
        raise HelperBatchPartialFailure(failures, results)

    return results  # type: ignore  # all str at this point
```

### Model recommendations (recap from spec/10)

| Task profile | Default helper model |
|---|---|
| Compression / summarization | `claude-haiku-4-5-20251001` |
| Compression with huge input (30K+ tokens) | `claude-sonnet-4-6-20260101` |
| Structured extraction needing strict format | Sonnet |
| Classification / scoring against fixed rubric | Haiku |
| Translation between specialized vocabularies | Sonnet |
| Long-context cheap | Kimi (`moonshot/kimi-2.6` once provider added) |
| OpenAI-side parity | `gpt-5-mini` or `gpt-5-nano` |

The helper takes a model parameter; the parent agent picks per call. No need to declare allowed helper models up front.

### Logging conventions for helpers

A helper call emits one JSONL log line with these fields:

```json
{
  "ts": "ISO 8601",
  "trigger": "helper",
  "parent_agent": "caldwell",
  "parent_run_id": "caldwell-2026-05-06-007",
  "model": "claude-haiku-4-5-20251001",
  "input_tokens": 8421,
  "output_tokens": 142,
  "cost_usd": 0.00094,
  "latency_ms": 612,
  "status": "ok",
  "summary": "summarize CPA memo (5 bullets)"
}
```

Both `helper_call` and `helper_call_parallel` write one log line per individual helper call. The parent run has its own log line with `trigger: cron` or `trigger: skill`. The dashboard joins by `parent_run_id` for the rollup view.

### Concurrency safety

Helper calls are **read-only by default**. They take a prompt string, return a result string. They do not write to the vault. This means:

- No file lock needed for the helpers themselves
- The parent agent's lock is held during the helper batch (acquired before the parent's main API call, released after)
- If a helper needs to inform a vault write, the parent does the write after collecting helper results

The parent's lock is still held during `helper_call_parallel` — the parent's main flow is single-threaded even when its helpers fan out.

### Provider routing for non-Anthropic helpers

When a helper uses a non-Anthropic model (Kimi, OpenAI), the shared helper routes to the right SDK:

```python
def _llm_call(self, model: str, messages: list[dict], **kwargs):
    if model.startswith("claude-"):
        return self._anthropic_call(model, messages, **kwargs)
    elif model.startswith("gpt-"):
        return self._openai_call(model, messages, **kwargs)
    elif model.startswith("moonshot/"):
        return self._moonshot_call(model, messages, **kwargs)
    else:
        raise ValueError(f"No provider routing for model: {model}")
```

Each provider's adapter normalizes input/output to a common shape so the helper API stays consistent regardless of which provider is behind a given call.

### Errors and partial failure

`helper_call` raises on individual failures (rate limit, network, etc.). The parent agent catches and decides — retry, skip, fail the whole run.

`helper_call_parallel` collects all individual failures and raises a single `HelperBatchPartialFailure` *after* all calls have completed. The exception carries the partial results so the parent can decide:
- All-or-nothing flow: re-raise
- Best-effort flow: use the successful subset and proceed

Default behavior is re-raise; opt into best-effort explicitly by catching the exception and using `e.partial_results`.

### Cost guardrail interaction

`_check_cost_guardrails()` runs **before** the helper call (or before the batch). If the parent's daily/monthly cap is already hit, the helper call is blocked with `CostGuardrailBlocked`. Helpers are not exempt from caps — they count against the same budget as the parent's main calls.

The 50% / 80% warnings fire on aggregate spend including helpers. Critical-flag override on the parent run cascades: `agent.call(work_item, critical=True)` sets `self.current_run_critical = True`, and helper calls within that run inherit the critical flag.

---

## Testing

The helper should ship with unit tests for:

- Frontmatter parsing (valid + invalid)
- Capture marker extraction (well-formed + malformed)
- Write path enforcement
- INDEX update logic
- Cost calculation
- Lint detection (dupes, contradictions, stale)

Plus an integration test that builds a fake agent in `/tmp/`, runs a full call cycle, and asserts the vault is in the expected state.

---

## Versioning

`AtomicAgent` should expose a `SPEC_VERSION` constant that matches the spec it implements:

```python
class AtomicAgent:
    SPEC_VERSION = 1
```

When the spec bumps (frontmatter schema changes, new required fields, etc.), the helper bumps too. Old agent vaults with `schema_version: 0` will be migrated by a `v0_to_v1.py` script (the script-discovery convention is `v<N>_to_v<N+1>.py`) — planned, not yet implemented: a true v0→v1 pass requires treating a missing `schema_version` as `0`, which is deferred to issue #439 and out of scope for the #429 backend refactor.

---

## Eventual integration with another agent / openclaw

another agent's openclaw runtime already provides equivalents to most of these helper methods (memory-core's `memory_search`, `memory_append`; memory-wiki's `wiki_apply`, `wiki_get`, `wiki_lint`). When we adapt Atomic Agents to another agent, we'll write a thin `OpenClawAtomicAgent` adapter that maps the helper's API onto openclaw's tools.

For now, `AtomicAgent` targets non-openclaw agents. The contract is identical; the implementation differs.

---

*This concludes the implementation guides. See [../README](../README.md) for navigation.*
