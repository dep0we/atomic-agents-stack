# Programmatic invocation

How to drive `atomic-agents-stack` from inside another Python program — what to
import, what to call, what to catch, and what is *not* yet part of the
public surface. Pair with [`versioning.md`](versioning.md) (which API
shape is covered by SemVer) and [`upgrading.md`](upgrading.md) (how to
move between releases without breaking your callers).

The CLI in `cli.py` is one operator of this same surface; everything it
does, your own code can do. This doc is the contract for that.

---

## When to use programmatic invocation

| You want…                                                                                  | Use…             |
|--------------------------------------------------------------------------------------------|------------------|
| Run an agent on a cron schedule with no custom logic around it.                            | The CLI: `atomic-agents run <name> --work-item "…"` |
| Hand-drive an agent at the shell to inspect / test it.                                     | The CLI          |
| Embed an agent inside a Python web service, queue worker, or notebook.                     | **Programmatic** |
| Wrap `agent.call()` in your own retry, alerting, observability, or auth code.              | **Programmatic** |
| Coordinate multiple agents with logic that does not fit the `delegate()` primitive.        | **Programmatic** |
| Call helpers/delegates from a job script that is itself not a full agent run.              | **Programmatic** |

The CLI is operator-driven: one process, one call, one stderr stream. The
programmatic surface is the same primitives, exposed for embedding —
same agents on disk, same `call()` flow, same audit trail in
`<agent>/log/`.

---

## Install + import

Same install as the CLI ([upgrading.md §2](upgrading.md)). The public
import path is the top-level package:

```python
from atomic_agents import AtomicAgent
```

Everything in `atomic_agents.__all__` (see [§Public surface](#public-surface))
is part of the SemVer contract. Anything else is internal.

### Minimum-viable invocation

```python
from pathlib import Path
from atomic_agents import AtomicAgent

agent = AtomicAgent(
    name="caldwell",
    trigger="manual",
    agents_root=Path("~/agents").expanduser(),
)
response = agent.call(work_item="Brief me on Q1.")

print(response.text)
print(f"cost=${response.cost_usd:.4f} captures={len(response.captures)}")
```

That is the entire contract. The vault at `~/agents/caldwell/` provides
the persona, tools, model, memory, journal, and log dir; the call writes
its run record to `~/agents/caldwell/log/YYYY-MM/YYYY-MM-DD.jsonl`.

If `agents_root` is omitted, `AtomicAgent` reads `ATOMIC_AGENTS_ROOT`
from the environment (defaulting to `~/docs/agents`). The `trigger=`
field is one of `cron`, `skill`, `manual`, `api`, `http`, or `delegate` — it
appears verbatim in the log record and influences lock-wait behaviour
(`skill` and `http` wait up to 30s for the agent lock; others fail fast).
`http` is set by the `atomic-agents serve` HTTP wrapper (spec/37); operators
pass the others directly.

For a runnable sample agent, see [`docs/samples/caldwell/`](../samples/caldwell/).
The same `name="caldwell"` example in the test suite is
[`tests/test_memory_integration.py::test_agent_memory_is_filesystem_backend`](../../tests/test_memory_integration.py).

---

## The `call()` surface

`AtomicAgent.call()` is the single entry point for "run this agent
against this work item." Signature:

```python
def call(
    self,
    work_item: str,
    model_override: str | None = None,
    critical: bool = False,
    max_tokens: int | None = None,
    temperature: float | None = None,
    write_captures: bool = True,
    parent_remaining_headroom_usd: float | None = None,
) -> Response
```

### Inputs

| kwarg                            | Meaning                                                                                       |
|----------------------------------|-----------------------------------------------------------------------------------------------|
| `work_item`                      | The user message / queue item / sub-goal text. Required.                                       |
| `model_override`                 | Force a specific model id. Overrides `model.md`'s `default_model` unless guardrails fall back. |
| `critical=True`                  | Bypass cost guardrails for this call. Still logged with `critical: true`. Use sparingly.       |
| `max_tokens` / `temperature`     | Override the per-call limits from `model.md` for this call only.                               |
| `write_captures=False`           | Extract captures from the response but do **not** write them to memory. Dry-run mode.          |
| `parent_remaining_headroom_usd`  | Internal: set by `delegate()` to clamp the child's effective cap to the parent's tree budget.  |

### The returned `Response`

`Response` is a dataclass — `from atomic_agents import Response`. Fields:

| Field                          | Meaning                                                                                       |
|--------------------------------|-----------------------------------------------------------------------------------------------|
| `text`                          | The model's text output for the final iteration of the tool loop.                            |
| `model`                         | The model id actually used (may differ from default if fallback fired).                       |
| `input_tokens` / `output_tokens`| Aggregate across all tool-loop iterations.                                                    |
| `cache_hit_tokens` / `cache_miss_tokens` | Anthropic prompt-cache accounting.                                                  |
| `cost_usd`                      | Aggregate cost across all iterations of the call.                                             |
| `cost_estimated_via_fallback`   | `True` if the pricing table did not know the model id and a fallback estimate was used.       |
| `latency_ms`                    | Total wall-clock time inside `call()`.                                                        |
| `summary`                       | First ~80 chars of the work item, for log readability.                                        |
| `raw`                           | The provider SDK's last raw response, for debugging.                                          |
| `captures`                      | The captures actually written this call (empty when `write_captures=False`).                  |
| `skipped` / `skip_reason`       | `True` if a cost guardrail blocked the call. `text` will be empty.                            |
| `tool_calls`                    | `list[ToolCallResult]` from all iterations of the custom-tool loop.                           |
| `tool_iterations`               | How many LLM turns ran. `1` means no custom tools were used.                                  |
| `tool_iterations_maxed`         | `True` if `max_tool_iterations` cap was hit.                                                  |

### Side effects of a call

A successful `call()` produces, in order:

1. **Agent lock acquired** at `<agent>/.lock` (per `_locks.AgentLock`).
   Released in `finally`.
2. **Cost guardrails checked** against the agent's `log/YYYY-MM/*.jsonl`
   history per `model.md`'s `cost_guardrails` block.
3. **MCP pool started** if `mcp.md` declared servers, and any MCP tools
   registered into the call's tool registry. Torn down in `finally`.
4. **System prompt assembled** per [`spec/04-runtime-assembly.md`](../spec/04-runtime-assembly.md).
5. **LLM called** (one or more iterations, depending on tool loop).
6. **Captures extracted** from each iteration (tool calls + fenced blocks).
7. **Captures written** to `<agent>/memory/` via the `MemoryBackend`,
   atomic + versioned per [`spec/02-atomic-memory.md`](../spec/02-atomic-memory.md).
   Skipped when `write_captures=False`.
8. **One JSONL run record appended** to `<agent>/log/YYYY-MM/YYYY-MM-DD.jsonl`
   with `helper_provenance`, `delegations`, `tool_calls` rolled up inline.

The audit trail is structural: do not try to suppress these side effects
to "speed up" testing. Use a temp `agents_root` and let them happen.

---

## Cost guardrails programmatically

Cost is first-class — every `call()` checks the agent's
`model.md` `cost_guardrails` block before the first LLM call and re-checks
each tool-loop iteration. See [`spec/09-cost-observability.md`](../spec/09-cost-observability.md)
for the rules.

### Reading config

`agent.config` is an `AgentConfig` dataclass — `from atomic_agents import AgentConfig`.
Relevant fields:

```python
agent = AtomicAgent(name="caldwell", agents_root=Path("~/agents").expanduser())
cfg = agent.config

cfg.cost_guardrails_enabled      # bool
cfg.daily_cap_usd                # float
cfg.monthly_cap_usd              # float
cfg.daily_cap_action             # 'skip' | 'fallback' | 'alert'
cfg.monthly_cap_action           # 'skip' | 'fallback' | 'alert'
cfg.fallback_model               # str | None
cfg.warning_thresholds           # e.g., [0.50, 0.80]
```

### Per-call overrides

There is no kwarg to raise a cap mid-call — the cap is the agent's
declared budget, not the caller's choice. The two levers are:

- `critical=True` — bypass guardrails for this call. The call still
  logs with `critical: true` and is auditable.
- Editing `model.md` between calls — the guardrails are read fresh on
  each call, so an operator-edited cap takes effect on the next call.

If you find your embedder needs runtime cap adjustments, that is a sign
the cap belongs in your wrapper layer, not in `model.md`. The framework
holds the line that the agent's declared cap is the cap.

### Reacting to a guardrail trip

Two shapes, depending on which path tripped:

1. **`call()` returned a skipped Response.** `response.skipped is True`,
   `response.text == ""`, `response.skip_reason` explains the trip. This
   is the happy path — your wrapper sees a real `Response` and can route
   it to whatever channel you choose.

2. **`helper_call()` or `delegate()` raised `CostGuardrailBlocked`.**
   These do not return skipped results — they raise. Catch the exception
   and decide what to do (retry tomorrow, alert, fall back to a manual
   path, etc.).

```python
from atomic_agents import AtomicAgent, CostGuardrailBlocked

agent = AtomicAgent(name="caldwell", agents_root=Path("~/agents").expanduser())

try:
    response = agent.call(work_item="What changed today?")
except CostGuardrailBlocked as e:
    notify_operator(f"caldwell over cap: {e}")
    return

if response.skipped:
    notify_operator(f"caldwell call skipped: {response.skip_reason}")
    return

publish(response.text)
```

---

## Memory operations

`agent.memory` is the agent's `MemoryBackend` instance. For the default
filesystem layout it is a `FilesystemBackend`; for future backends it is
whatever was registered. The protocol is in
[`spec/20-memory-backend.md`](../spec/20-memory-backend.md) and the
runtime shape is `atomic_agents.memory.backend.MemoryBackend`.

### Canonical API

```python
from atomic_agents.memory import WritePolicy  # public via the memory submodule
from atomic_agents import Capture

# Reads — no policy needed
agent.memory.list_notes()
agent.memory.read_note("feedback_comm_style.md")
agent.memory.list_pinned()
agent.memory.list_recent(n=5)
agent.memory.list_stale(threshold_days=90)
agent.memory.list_orphans()
agent.memory.list_by_type("feedback")
agent.memory.search("debt payoff", limit=10)
agent.memory.render_index_summary()

# Writes — require a WritePolicy from tools.md
policy = WritePolicy(
    write_paths=agent.config.write_paths,
    read_only_paths=agent.config.read_only_paths,
)
agent.memory.write_note(capture, policy)

# Versioning
versions = agent.memory.list_versions("feedback_comm_style.md")
agent.memory.read_version(versions[0])
agent.memory.restore_version("feedback_comm_style.md", versions[0], policy)
agent.memory.redact_version(versions[0], replacement="[REDACTED]")

# Stats (used by the dashboard)
stats = agent.memory.stats()
```

`WritePolicy.write_paths` comes from `tools.md` — the backend enforces
it on every write and raises `WritePathViolation` if a capture targets
something outside. Do not synthesize policies with broader paths just
to make a test pass; that defeats the boundary the agent declared.

### Deprecated re-exports

The module-level functions in `atomic_agents._capture` and
`atomic_agents._versioning` are compatibility shims that delegate to
the same backend and emit `DeprecationWarning`:

| Deprecated                                          | Replacement                                       |
|-----------------------------------------------------|---------------------------------------------------|
| `_capture.write_atomic_note(agent_root, capture, write_paths, …)` | `agent.memory.write_note(capture, policy)`        |
| `_versioning.list_versions(memory_dir, name)`       | `agent.memory.list_versions(name)`                |
| `_versioning.read_version(memory_dir, path)`        | `agent.memory.read_version(version_ref)`          |
| `_versioning.restore_version(memory_dir, name, snapshot)` | `agent.memory.restore_version(name, version_ref, policy)` |
| `_versioning.redact_version(memory_dir, path)`      | `agent.memory.redact_version(version_ref)`        |

Sunset target is v1.0 — switch to the `agent.memory.*` form before
upgrading past that line.

### `VersionRef` is opaque

`agent.memory.list_versions()` returns `VersionRef` objects whose
`backend_id` is **not** part of the public contract. Do not parse it,
construct it, or compare it across backends. Convert operator-supplied
tokens via `resolve_version_token`:

```python
vref = agent.memory.resolve_version_token(
    "feedback_comm_style.md",
    "20260508T120000Z_ab12cd34.md",
)
agent.memory.restore_version("feedback_comm_style.md", vref, policy)
```

`str(vref)` is fine for display.

---

## Helpers + delegates

Two cheap-parallelism primitives, both billed against the parent agent's
cap. See [`spec/10-helpers.md`](../spec/10-helpers.md) and
[`spec/15-delegation.md`](../spec/15-delegation.md).

### `helper_call()`

Cheap, single-call helper — defaults to Haiku, no persona, no memory.
Used for summarisation, fact extraction, structured rewrites.

```python
result = agent.helper_call(
    prompt="Summarise this memo in 5 bullets:\n\n" + memo_text,
    model="claude-haiku-4-5-20251001",
    max_tokens=1024,
    temperature=0.3,
    summary="memo summary",
    sources=["~/docs/cpa-memo.md"],   # optional, for provenance
)

print(result.text)
print(result.cost_usd, result.provenance_preserved)
```

Cost accounting: one JSONL log line with `trigger: helper`,
`parent_run_id`, the helper's tokens/cost, and (if sources were passed)
provenance fields. Rolled up inline into the parent run record's
`helper_provenance` array. If the parent's cap is hit at the time of the
call, `helper_call()` raises `CostGuardrailBlocked`.

Working examples: [`tests/test_helper_provenance.py`](../../tests/test_helper_provenance.py).

### `helper_call_parallel()`

Same shape but fans out N prompts concurrently (`max_concurrent`
defaults to 5):

```python
results = agent.helper_call_parallel(
    prompts=[f"Summarise {name}" for name in docs],
    sources_per_prompt=[[name] for name in docs],
    max_concurrent=5,
)
```

Pre-reserves worst-case cost against the parent's headroom **before**
dispatch, so a 10-helper batch cannot stealth-overrun the cap. If the
reservation exceeds headroom, the entire batch is refused with
`CostGuardrailBlocked`. If some helpers succeed and some fail mid-batch,
the call raises `HelperBatchPartialFailure` whose `.partial_results`
list contains both `HelperResult` objects and exceptions in the failed
slots.

### `delegate()`

Synchronous fan-out to another **fully-fledged agent** in this
coordinator's `roster.md`. The target loads its own persona, memory,
wiki, journal, config, and writes captures to its own memory dir.

```python
response = agent.delegate(
    target_agent_name="agent-b",
    work_item="Run an analysis on Q1 numbers and report back.",
    summary="Q1 analysis",
)
print(response.text, response.cost_usd)
```

The coordinator's cap is enforced as a **tree-cap**: the child's call
gets clamped to `min(child_remaining, parent_remaining)` so a fleet
cannot stealth-overrun the parent's budget. Multiple sequential
delegations accumulate against the parent — the second `delegate()`
sees the first's spend.

**One-level only** (per spec/15): if a delegated agent (i.e., its
`trigger == "delegate"`) tries to delegate further, it raises
`NestedDelegationRefused`. If you want a multi-hop workflow, the
coordinator must call each step explicitly rather than chaining.

`delegate_parallel()` fans out to multiple targets with the same
worst-case reservation pattern as `helper_call_parallel`.

---

## Public exception table

Every exception below is in `atomic_agents.__all__` and is covered by
the SemVer contract: names will not be renamed or moved without a Major
bump per [versioning.md §Framework API break](versioning.md).
Operators writing defensive `try/except` should catch these names.

`AtomicAgentsError` is the base — catching it catches every public
exception listed here. Catch the base for "log and continue" wrappers;
catch the specific subclasses when the recovery action differs.

| Exception                       | Raised when                                                                                                | Catchable mid-call?           | Recovery action                                                                                                |
|---------------------------------|------------------------------------------------------------------------------------------------------------|-------------------------------|----------------------------------------------------------------------------------------------------------------|
| `AtomicAgentsError`             | Base of all public exceptions. Also raised directly by `AtomicAgent.__init__` when the agent folder is missing. | n/a (base)                    | If raised directly: the agent folder doesn't exist — fix `agents_root` or create the folder.                   |
| `CostGuardrailBlocked`          | Daily/monthly cap hit on `helper_call`, `helper_call_parallel`, or `delegate`. (For `agent.call()` itself, a cap trip returns `skipped=True` instead of raising.) | Yes — only from helpers/delegates | Wait, alert, or pass `critical=True` for the next call. Inspect `agent.config.daily_cap_usd` vs accumulated log spend. |
| `AgentLockBusy`                 | Another process holds the agent's `.lock` file when `call()` tries to acquire it (no-wait when `trigger != "skill"`, 30s wait when `trigger == "skill"`). | Yes (surfaces from `call()`)  | Retry later. One agent runs at a time per process.                                                             |
| `SchemaValidationError`         | Frontmatter or capture payload fails schema validation per spec/03 (missing required field, bad type, conflicting merge target). | No — surfaces from `call()` via the capture writer | Fix the model's output (prompt-level), or fix the existing note that's conflicting. |
| `WritePathViolation`            | A capture or `agent.memory.write_note()` targets a path outside `tools.md`'s `write_paths` (or under a `read_only_path`). | No — surfaces from `call()` or direct `memory.write_note()` | Widen `tools.md` write paths, or change what the agent is trying to write. Never widen the policy silently in code. |
| `NoJudgeAvailable`              | No judge model is reachable for evals — typically a missing API key.                                       | Surfaces from `OutcomeRunner` / eval paths | Set the provider API key in env or keys.json. Run `atomic-agents doctor`. |
| `HelperBatchPartialFailure`     | One or more helpers in `helper_call_parallel` raised. `.failures` and `.partial_results` carry the per-index outcomes. | Yes — from `helper_call_parallel()` only | Inspect `.partial_results` for the successful slots; decide whether to retry the failed ones individually. |
| `NotInRoster`                   | `delegate()` target isn't in the coordinator's `roster.md`, or the target name resolves outside the agents root (path traversal). | Yes — from `delegate()` / `delegate_parallel()` | Add the target to `roster.md`, or fix the target name.                                                         |
| `SelfDelegationError`           | `delegate(target_agent_name=self.name, …)` — one-level delegation only.                                    | Yes — from `delegate()`        | Choose a different target, or use `helper_call()` if you wanted a cheap sub-call.                              |
| `DreamInProgress`               | `DreamRunner` started while another dream run holds the dream lock for this agent.                         | Yes — from `DreamRunner.run()` | Wait for the in-progress run to finish; if stale, manually clear the dream lock.                               |
| `DreamNotFound`                 | Looking up a dream by ID that doesn't exist for this agent.                                                | Yes — from `DreamRunner` lookups | Verify the dream id; list dreams via the dashboard.                                                            |
| `ToolNotRegistered`             | The model emitted a tool call for a name not in the `ToolRegistry` (and not `atomic_capture`).             | Logged inside `call()`; does not raise to caller — see `Response.tool_calls` for error rows. | Inspect `Response.tool_calls`; either register the tool or update the prompt so the model stops calling it.    |
| `ToolInputInvalid`              | A custom tool's input fails JSON Schema validation (missing required field / wrong type).                 | Caught by `ToolRegistry.execute()` and surfaced via `ToolCallResult.error` | Read the per-tool result; tighten the schema or the prompt so the model produces conformant input.             |
| `ToolHandlerError`              | A custom tool handler raised. Captured in `ToolCallResult.error`.                                          | Caught by `ToolRegistry.execute()` and surfaced via `ToolCallResult.error` | Read the per-tool result; fix the handler or the inputs.                                                       |
| `SkillFileTraversal`            | `load_skill_file` was called with a relative path containing `..` or resolving outside the skill dir.      | Caught by the skill tool handler and surfaced via `ToolCallResult.error` | Audit the skill's referenced files; make them one level deep, relative.                                        |
| `BackendNotRegistered`          | `atomic_agents.memory.get_backend("name")` for a name no package registered.                               | Yes — at config-read time      | Install the backend package that calls `register_backend()` for that name, or use `"filesystem"`.              |
| `VersionNotFound`               | `agent.memory.resolve_version_token(note, token)` cannot match the token against any known version.       | Yes — from version lookups     | List versions for the note via `agent.memory.list_versions(name)` and pick a real one.                         |
| `StagingNotApplied`             | A `StagedMemory` operation runs after `apply_staging()` / `discard_staging()` already finalised it.        | Yes — from dream / bulk paths  | Treat the staging handle as spent; create a new one if more writes are needed.                                 |

### Exceptions raised but not yet in `__all__`

These are raised inside the package but are not currently exported via
`atomic_agents.__all__`. **Treat them as internal** — name stability is
not yet guaranteed. If you find yourself reaching for one, file an
issue against `dep0we/atomic-agents-stack` so it can be promoted to
the public surface.

- `NestedDelegationRefused` — raised when a delegated agent tries to
  delegate further. Today it's reachable via
  `from atomic_agents.exceptions import NestedDelegationRefused`.
- `CaptureParseError` — capture marker parse failure.
- `GoalCorrupted` — `goal.md` missing required fields.
- `ToolNameCollision` — `ToolRegistry.register()` duplicate without
  `allow_overwrite=True`.
- `MemoryPreconditionFailed` — optimistic concurrency: the
  `expected_content_sha256` check on `write_note` did not match.
- `PathTraversalError` — internal guard from `_io.safe_resolve_under`;
  user-facing paths convert it into `NotInRoster` / `SkillFileTraversal`.
- `MCPServerConnectFailed`, `MCPServerNotConfigured`,
  `MCPToolDispatchFailed` — MCP layer (spec/19). Connect failures are
  logged inside `call()`; dispatch failures land in
  `ToolCallResult.error`.

When these get promoted into `__all__`, the next CHANGELOG entry under
`### Added` will call them out and the table above will move them.

---

## Concurrency model

The framework's concurrency rules, summarised:

| Shape                                                                | Allowed? |
|----------------------------------------------------------------------|----------|
| Multiple `AtomicAgent` instances for different agents in one process. | Yes — each acquires its own per-agent lock. |
| Multiple `AtomicAgent` instances for the *same* agent in one process. | Discouraged — only one will hold the lock at a time, others raise `AgentLockBusy`. |
| `helper_call_parallel` / `delegate_parallel` from inside `call()`.    | Yes — that's the intended fan-out path. |
| Calling `agent.call()` from inside another `agent.call()` of the *same* agent. | No — the inner call cannot acquire the lock and raises `AgentLockBusy`. |
| Two OS processes invoking the same agent at the same time.            | Lock-protected — second process raises `AgentLockBusy`. |

The lock is a `flock`-style file lock at `<agent>/.lock`. It is released
in `finally`, so an exception mid-call still releases the lock. Stale
lock files are normal — `flock` releases on process death even if the
file lingers. `atomic-agents doctor` reports only *actively-held* locks.

### Subprocess-safe embedding

If your embedder is multi-process (gunicorn workers, Celery, etc.), do
not share an `AtomicAgent` instance across processes. Each worker should
construct its own `AtomicAgent` and let the per-agent lock serialise
calls. The lock wait behaviour depends on the `trigger` you set:

- `trigger="skill"` waits up to 30s for the lock — appropriate when an
  interactive caller can tolerate the wait.
- Any other trigger (`cron`, `manual`, `api`, `delegate`) fails fast with
  `AgentLockBusy` — appropriate when the caller would rather see the
  conflict than block.

If your service routes the *same* agent's work through a queue, give the
worker a single-concurrency lane for that agent rather than relying on
the file lock to serialise. The file lock is correctness insurance, not
a queue.

---

## Worked examples

### a. Single-shot embed with cost-cap handling

A job that wakes up, asks the agent one question, alerts when the cap
trips, and writes the response to a downstream channel.

```python
from pathlib import Path
from atomic_agents import (
    AtomicAgent,
    AtomicAgentsError,
    CostGuardrailBlocked,
    AgentLockBusy,
)


def run_morning_brief(agents_root: Path, work_item: str) -> str:
    agent = AtomicAgent(
        name="caldwell",
        trigger="cron",
        agents_root=agents_root,
    )
    try:
        response = agent.call(work_item=work_item)
    except AgentLockBusy:
        # Another process is mid-call. Retry policy lives in our caller.
        raise
    except CostGuardrailBlocked as e:
        # Helpers/delegates inside this call hit the cap. call() itself returns
        # a skipped Response instead; this branch fires only when one of the
        # internal helpers/delegates raises. Surface it as an alert.
        alert(f"caldwell cap hit on internal call: {e}")
        raise
    except AtomicAgentsError as e:
        # Catch-all for any other framework exception (write-path violation,
        # schema validation, etc.). Don't silence — alert and re-raise so
        # the cron wrapper exits non-zero.
        alert(f"caldwell framework error: {type(e).__name__}: {e}")
        raise

    if response.skipped:
        alert(f"caldwell skipped: {response.skip_reason}")
        return ""

    return response.text


def alert(msg: str) -> None:
    # Your alerter — Telegram bot, PagerDuty, etc.
    print(msg)
```

What is *not* in this snippet, intentionally: a manual log write. The
JSONL run record is already in `<agent>/log/`. Add your alerter on top;
do not re-implement the audit trail.

### b. Custom orchestrator: agent-a routes to agent-b

Two agents, both top-level callable, where `agent-b`'s work item is
derived from `agent-a`'s response. Implemented as two independent
`call()` invocations rather than `delegate()` because we want each
agent's response object in our wrapper's hands (for retry logic, custom
routing, etc.).

```python
from pathlib import Path
from atomic_agents import AtomicAgent


def two_step_pipeline(agents_root: Path, initial_input: str) -> str:
    agent_a = AtomicAgent(
        name="agent-a",
        trigger="api",
        agents_root=agents_root,
    )
    response_a = agent_a.call(work_item=initial_input)
    if response_a.skipped or not response_a.text.strip():
        return ""

    routed_work_item = derive_b_input(response_a.text)

    agent_b = AtomicAgent(
        name="agent-b",
        trigger="api",
        agents_root=agents_root,
    )
    response_b = agent_b.call(work_item=routed_work_item)
    return response_b.text


def derive_b_input(a_text: str) -> str:
    # Your routing logic — pull out a structured field, extract intent, etc.
    return f"agent-a produced this; act on it:\n\n{a_text}"
```

Use `delegate()` instead when:
- agent-b should be in agent-a's `roster.md` (governed delegation), and
- agent-a's cap should cover agent-b's spend (tree-cap), and
- agent-a's log should roll up agent-b's call inline.

Use the two-call form above when:
- the routing logic is non-trivial and lives in your code, or
- agent-a and agent-b have separate budget envelopes, or
- you want full responses from both agents in your wrapper's hands.

### c. Subprocess-safe wrapper for a multi-worker web service

A worker process inside gunicorn / Celery / etc. that handles one
request at a time. The per-agent file lock serialises concurrent
requests for the same agent across workers.

```python
from pathlib import Path
from atomic_agents import AtomicAgent, AgentLockBusy

AGENTS_ROOT = Path("/var/lib/atomic-agents")


def handle_request(agent_name: str, work_item: str) -> dict:
    # Construct a fresh agent per request. AtomicAgent is cheap to create;
    # reuse across requests is only safe within a single process and only
    # for sequential calls.
    agent = AtomicAgent(
        name=agent_name,
        trigger="api",
        agents_root=AGENTS_ROOT,
    )
    try:
        response = agent.call(work_item=work_item)
    except AgentLockBusy:
        # Another worker is mid-call for this agent. Return a 429-shaped
        # signal to the caller; the upstream queue/lb will retry.
        return {"status": "busy", "retry_after_seconds": 5}

    if response.skipped:
        return {"status": "skipped", "reason": response.skip_reason}

    return {
        "status": "ok",
        "text": response.text,
        "model": response.model,
        "cost_usd": response.cost_usd,
        "run_id": agent.run_id,
    }
```

For long-running services, do not hold an `AtomicAgent` instance across
requests for the same agent in the same worker. The instance carries
per-call state (helper rollup, delegation rollup, MCP pool) that resets
inside `call()` — but the cleanest contract is one instance per call,
because that mirrors the CLI's lifecycle.

---

## Public surface

The contract is `atomic_agents.__all__`. As of v0.10.0:

```python
# The agent
AtomicAgent

# Exceptions
AtomicAgentsError, CostGuardrailBlocked, AgentLockBusy,
SchemaValidationError, NoJudgeAvailable, HelperBatchPartialFailure,
WritePathViolation, NotInRoster, SelfDelegationError,
DreamInProgress, DreamNotFound,
ToolNotRegistered, ToolInputInvalid, ToolHandlerError,
SkillFileTraversal

# Custom tools (spec/17)
ToolDefinition, ToolRegistry, ToolCallResult

# Skills (spec/18)
SkillManifest, discover_skills, load_skill_body, load_skill_referenced_file

# Shared dataclasses
Capture, Response, AgentConfig, CostCheckResult

# Outcomes (spec/14)
OutcomeRunner, OutcomeResult, IterationRecord

# Dreams (spec/16)
DreamRunner, DreamResult, DreamInputs,
ConsolidatedNote, PromotedNote, StaleMarking
```

Plus the memory submodule (always public):

```python
from atomic_agents.memory import (
    MemoryBackend, Note, NoteRef, VersionRef,
    WritePolicy, MemoryStats, StagedMemory,
    register_backend, get_backend,
    BackendNotRegistered, VersionNotFound, StagingNotApplied,
)
```

---

## What is NOT public yet

Anything **not** in `atomic_agents.__all__` (or the `atomic_agents.memory`
public surface) is internal. Specifically:

- **Underscore-prefixed modules** — `_capture`, `_cascade`, `_costs`,
  `_io`, `_llm`, `_locks`, `_model`, `_platform`, `_roster`, `_schema`,
  `_tools`, `_versioning`. Names inside may change without a major bump.
  The `write_atomic_note` / `list_versions` / `read_version` /
  `restore_version` / `redact_version` shims will emit
  `DeprecationWarning` and are scheduled for removal in v1.0.
- **`atomic_agents.cli`** — the CLI's `main()` is an entry point, not a
  library function. Don't import it; shell out to `atomic-agents` or
  call the primitives directly.
- **`atomic_agents.doctor`** — `run_doctor()`, `render_human()`,
  `render_json()`, `overall_exit_code()` work today and are documented
  as a Cloud Run liveness probe in the module docstring, but they are
  not yet in `__all__`. Treat them as internal for now; the
  [`doctor` spec doc](../spec/27-doctor.md) will lock the surface when
  it is.
- **`atomic_agents.migration`** — `FilesystemMigrationBackend`,
  `MigrationBackend` Protocol, `MigratableUnit`, and `MigrationSnapshotRef`
  are the public API (issue #429, T13 refactor). Construct a
  `FilesystemMigrationBackend(agents_root)` and call
  `backend.run_migration(target_version, dry_run)`. The old free functions
  (`run_migration()`, `build_migration_plan()`, `create_snapshot()`,
  `restore_snapshot()`) are **removed** (BREAKING — see `CHANGELOG.md`).
  Drive migrations via the documented CLI or the backend API in
  [`upgrading.md`](upgrading.md).
- **`atomic_agents.dashboard`** — the renderer is reachable via
  `from atomic_agents.dashboard import render_all`, but the per-tab
  aggregator surface (`aggregate_costs`, `aggregate_activity`, etc.) is
  not yet `__all__`-locked. Use `render_all()` only.
- **`atomic_agents.goal`, `atomic_agents.tuning`, `atomic_agents.eval`,
  `atomic_agents.mcp`** — under active design. `OutcomeRunner` and
  `DreamRunner` are public; their underlying machinery is not.

The protocol pattern from
[`spec/20-memory-backend.md`](../spec/20-memory-backend.md) is the
template every backend follows. Twelve backend protocols have shipped
at v1.0 (`MemoryBackend`, `LLMBackend`, `JudgeBackend`, `LockBackend`,
`LogBackend`, `AgentProfileBackend`, `ToolRegistryBackend`,
`MandateBackend`, `PolicyBackend`, `PersonaBackend`, `CorpusBackend`,
`MCPServerRegistryBackend`). Each ships with a
filesystem reference impl, a conformance suite, an operator override
env var + constructor kwarg, a `doctor.check_<backend>` coherence
check, and a numbered spec doc (`spec/20`, `21`, `22`, `24`, `25`,
`28`, `29`, `31`, `32`, `33`, `34`, `36`). Future SaaS / Postgres / git adapters
register via the matching `register_<backend>_backend()` entry point
without forking core.

---

## See also

- [`obsidian.md`](obsidian.md) — Obsidian-backed deployment shape;
  names the `AgentLockBusy` race window and the conflict-recovery
  flow when a synced device writes mid-call.
- [`disaster-recovery.md`](disaster-recovery.md) — symptom-organized
  runbook covering every exception in the table above plus the
  specific recovery commands when one fires.
- [`cost-guardrail-sizing.md`](cost-guardrail-sizing.md) — picking
  the `cost_guardrails` numbers that show up in `CostGuardrailBlocked`
  exceptions; role archetypes for sizing.
- [`versioning.md`](versioning.md) — SemVer rules that cover the public
  surface above.
- [`upgrading.md`](upgrading.md) — operator runbook for moving between
  tagged releases.
- [`../spec/04-runtime-assembly.md`](../spec/04-runtime-assembly.md) —
  the canonical `call()` flow.
- [`../spec/09-cost-observability.md`](../spec/09-cost-observability.md) —
  cost guardrail semantics.
- [`../spec/10-helpers.md`](../spec/10-helpers.md) — helper-call
  conventions, including provenance.
- [`../spec/13-research-integrity.md`](../spec/13-research-integrity.md) —
  the audit-trail rollup that `call()` produces.
- [`../spec/15-delegation.md`](../spec/15-delegation.md) — delegation
  semantics + the one-level constraint.
- [`../spec/20-memory-backend.md`](../spec/20-memory-backend.md) — the
  `MemoryBackend` protocol that `agent.memory.*` implements.
