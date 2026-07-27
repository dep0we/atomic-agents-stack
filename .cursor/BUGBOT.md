# Bugbot review guide: atomic-agents-stack

A vault-native AI agent framework, MIT-licensed and published to PyPI. Agents are folders of markdown; the runtime is stateless; storage sits behind swappable protocols. Python 3.11 and 3.12, `uv` for dependencies, pytest for the suite (~6,200 tests).

**This has downstream consumers.** It ships to PyPI and other projects build on its protocol surface. A breaking change here is not local. Weight backward-compatibility and spec-drift defects accordingly.

The maintainer is not a developer and relies on this review. Prioritize correctness, cost, and contract defects over style. Say plainly what breaks and where.

## Hard invariants: flag any violation as high severity

**Every code path that calls an LLM has a cost gate, checked before the spend.** `agent.call()` checks guardrails before the first LLM call and re-checks each iteration of the multi-turn loop. Helper batches reserve worst-case before dispatch. Delegation clamps to `MIN(child_remaining, parent_remaining)`; that tree-cap is what makes running many agents affordable rather than unbounded. Long-running pipelines outside `call()` (`dream.py`, `eval.py`, `tuning.py`) have their own gates.

Flag any new LLM call path with no guardrail. Flag a gate moved to *after* a subprocess spawn or session establish, since the point is to refuse before paying the overhead. Flag `critical=True` becoming a default anywhere.

**Writes are atomic; teardown is idempotent.** Writes go through `_io.atomic_write` (temp, fsync, rename, parent-dir fsync). Teardown paths (MCP pool, tool registrations, lock release) must run safely on exception paths. Flag a direct `open(...,"w")` on vault state, and flag a cleanup path that breaks when called twice or after a partial failure. A crash must leave recoverable artifacts, never corruption.

**Path traversal is refused via `_io.safe_resolve_under`.** Flag any filesystem path built from agent, tool, or model output that does not resolve through it.

**Audit lines carry their linkage.** Every run writes a JSONL line with a `run_id`; helper, tool, and delegate calls write child lines carrying `parent_run_id`. Flag a new call surface that emits no audit line, or one that drops the parent linkage, which silently breaks the rollup.

**Delegation stays one level.** A coordinator delegates to specialists; specialists do not delegate. Flag any change enabling two-level delegation. It reads as flexibility and is how the call tree becomes unauditable.

**A protocol change is a contract change.** New storage primitive means a new protocol plus dataclasses, `WritePolicy`, capability advertisement, a filesystem default, a numbered spec doc in `docs/spec/`, and roughly 25 conformance plus 10 implementation-specific tests. Flag `if backend == "postgres": ...` style branching. Flag an implementation change that diverges from its LOCKED spec doc without the spec being updated in the same change.

**No secrets in code.** Flag any hardcoded key, token, or connection string in source, tests, or fixtures.

## Patterns

**Layers compose, they do not merge.** Persona is not memory. Notes are not the Wiki. `atomic_capture`, `helper_call`, `delegate`, and `tool` are four different things with different authors and lifecycles. Flag a change that collapses two of them without writing down why.

**Markdown config or no config.** Operator-facing config (`tools.md`, `model.md`, `mcp.md`, `roster.md`, `goal.md`, the persona files) stays markdown. Embedded YAML inside markdown is fine. Flag a new pure-YAML, TOML, or JSON config file intended for humans to edit.

**Progressive disclosure.** The model pays context tokens for capability *awareness*, not capability *content*. Skill metadata goes in the system prompt; bodies load lazily. Flag a change that eagerly loads full content where metadata would let the model decide.

**Backward compatibility by default.** Pre-1.0 minor releases may break, but a break is a deliberate, documented event with a `### BREAKING` changelog callout. Flag a silent signature, default, or file-shape change with no note.

**Errors fail loud.** Flag a swallowed exception or a plausible-looking default returned in place of a real failure, particularly on a write or cost path.

## Not bugs here: do not flag these

- **Refusal is a feature.** Several things are impossible on purpose and each has a documented rationale: two-level delegation, path traversal, escaping the cost cap without `critical=True`. Do not propose lifting one as an enhancement. If a change *needs* one lifted, that is a spec conversation, not a code review note.
- **The one-level constraints on delegation and skill file depth** are guardrails against complexity creep, not oversights.
- **Long procedural functions in the pipeline modules** (`dream.py`, `eval.py`, `tuning.py`) mirror a documented staged flow. Do not suggest decomposing them for style alone.
- **Duplicate-looking cost gates.** `call()`, helper batches, delegation, and the long-running pipelines each have their own gate rather than one shared one. That is deliberate: every path that spends must refuse independently.
- **Markdown as config.** Do not suggest migrating operator-facing config to YAML or TOML.
- **`.claude/` and `.gstack/`** are committed development tooling, not application code.
- **The large test count and its fixtures.** The conformance suite is the product's guarantee, not bloat.

## House style

- **Never use em dashes.** Periods, commas, or parentheses. Covers commits, PR bodies, comments, and docs.
- **Plain language, no developer jargon.** Define a load-bearing technical term in a few words right after using it. Frame a choice by what happens if you pick each option, not by what the pattern is called.
- **Lead with the recommendation**, then the trade-off.
- **Verify before claiming.** Reproduce a finding before asserting it. A passing suite is not proof a feature works.
