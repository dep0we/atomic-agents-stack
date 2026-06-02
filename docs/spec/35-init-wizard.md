# 35: atomic-agents init wizard

Status: RFC (locks at PR 2 of init-wizard arc, per #94 + design doc)
Implements: home-user onboarding compression
Closes: #94

## Overview

`atomic-agents init` is the single-command on-ramp for new agent authors. A
non-developer runs it once, answers seven plain-English questions, and walks
away with a working agent scaffold in under ten minutes. That is the acceptance
test: non-developer Dan deploys a fresh demo agent end-to-end without reading
any other doc.

The wizard generates all seven required files documented in spec/01 anatomy
(IDENTITY.md, SOUL.md, USER.md, tools.md, model.md, memory/INDEX.md,
wiki/INDEX.md) plus two empty directories (journal/, log/) that the framework
populates on first run. Every file goes through `atomic_agents._io.atomic_write`
so partial writes from a crash or disk-full event leave no corrupted state.

After writing files, the wizard hands off to `doctor.run_doctor()` to verify
the scaffold. If doctor passes, it offers an opt-in test call so the operator
sees the agent respond before ending the session. The wizard is purely additive
to cli.py: one lazy import, one subparser block, one dispatch case.

---

## Operator surface

### Command shape

```
atomic-agents init <agent-name>
atomic-agents init <agent-name> --from-template advisor
atomic-agents init --list-templates
atomic-agents init <agent-name> --agents-root PATH
```

`--agents-root PATH` overrides the `ATOMIC_AGENTS_ROOT` environment variable for
this invocation.

### Entry guards (apply on every invocation path)

**Non-TTY refusal.** `sys.stdin.isatty()` is checked at `run_init()` entry,
before any rich import or Console initialization. A non-interactive terminal
(piped input, CI runner) exits status 2 and prints `constants.MSG_NO_TTY` to
stderr. This applies to the interactive Q&A path, `--from-template`, and
`--list-templates`.

**ANTHROPIC_API_KEY pre-flight.** The key is resolved via
`_llm._get_key(env_vars=constants.ANTHROPIC_ENV_VARS,
keychain_name=constants.ANTHROPIC_KEYCHAIN_NAME,
config_key=constants.ANTHROPIC_CONFIG_KEY)`, which checks environment
variables, macOS Keychain, and `~/.config/atomic_agents/keys.json` in that
order. If all three sources are empty, the wizard prints
`constants.MSG_NO_PROVIDER_KEY` to stderr and exits 1 with no files written.

**Persona-backend warning.** If `ATOMIC_AGENTS_PERSONA_BACKEND_URL` is set to a
non-empty value, the wizard prints `constants.MSG_PERSONA_BACKEND_WARNING` and
offers a Yes/No prompt before any `mkdir` or file write. Declining exits
status 0 with zero filesystem side effects. This guard does not apply to
`--list-templates` because that path writes nothing.

### The seven Q&A questions (verbatim wording)

Each question is rendered with `rich.prompt.Prompt.ask()` using the single
`Console` instance created at `run_init()` entry.

**Q1 -- Name.** Prompt: "What should I call this agent? (Letters, numbers,
dashes only; this becomes a folder name.)"
Validation: must match `constants.AGENT_NAME_REGEX`
(`^[a-zA-Z0-9][a-zA-Z0-9-]{0,62}[a-zA-Z0-9]$|^[a-zA-Z0-9]$`); must not
appear in `constants.RESERVED_AGENT_NAMES` (`init`, `doctor`, `run`, `info`,
`skills`, `version`, `restore`, `bundle`, `review`, `persona`, `corpus`).
Failures re-prompt with a plain-English message; no filesystem side effect
until Q1 validates.
Target: the scaffold directory name and `${agent_name}` variable.

**Q2 -- Mission.** Prompt: "What is this agent for? (One or two sentences. What
is its job, what does it produce.)"
Free text; empty re-prompts.
Target: IDENTITY.md Mission section via `${mission}`.

**Q3a + Q3b -- Scope (two sequential prompts, counted as one question).** Q3a:
"What is in scope? (A few bullets. What work should this agent accept.)"
Q3b: "What is out of scope? (A few bullets. What should it refuse.)"
Free text; both empty re-prompt their own sub-prompt.
Targets: `${scope_in}` and `${scope_out}` feed two distinct IDENTITY.md
subsections (In scope / Out of scope).

**Q4 -- Autonomy.** Prompt: "How much should this agent act on its own? (Pick
1-3 or 4 to set each class yourself.)"
Renders a `rich.table.Table` (max_width=78) showing the three presets from
`constants.AUTONOMY_PRESETS`:

| Action class | Cautious | Balanced | Autonomous |
|---|---|---|---|
| `read_only` | `bypass` | `bypass` | `bypass` |
| `reversible_write` | `allow_with_audit` | `allow_with_audit` | `allow_with_audit` |
| `external_side_effect` | `escalate` | `judge_required` | `judge_required` |
| `high_risk` | `escalate` | `escalate` | `judge_required` |

Option 4 (Customize) drops into a per-class sub-flow. Plain-English gloss for
each class appears next to the prompt (e.g., "`external_side_effect` = sending
email, posting messages, anything the world sees"). If `Console.is_dumb_terminal`
is True, the table falls back to plain formatted text.
Target: four variables in IDENTITY.md Autonomy section via
`${autonomy_read_only}`, `${autonomy_reversible_write}`,
`${autonomy_external_side_effect}`, `${autonomy_high_risk}`, and
`${autonomy_preset_label}`. Policy values are written verbatim from
`constants.ACTION_CLASSES` and `constants.POLICIES`.

**Q5 -- Voice.** Prompt: "How should this agent talk? (Two or three adjectives
separated by commas. Examples: calm, direct, witty.)"
Soft validation: if the comma-split count is outside 1-5, re-prompt once with
"I expected 2-3 adjectives separated by commas. Press Enter to keep your answer
as-is." Hard refusal is not applied.
Target: SOUL.md Voice section via `${voice}`.

**Q6 -- Communication preferences.** Prompt: "How do you prefer to communicate
with it? (A few bullets. For example: answer first then explain, or context
then answer; numbers vs prose; short vs detailed.)"
Free text.
Target: USER.md Communication section via `${comm_prefs}`.

**Q7 -- Hard refusals.** Prompt: "Anything this agent should never do? (Hard
refusals. Examples: never send email; never write outside its own folder; never
make medical recommendations.)"
Free text. Rendered to TWO files: verbatim as policy phrasing into tools.md
"Hard NOs" section; with behavioral framing into USER.md "Things to avoid"
section.
Target: `${hard_refusals}` used in both USER.md and tools.md templates.

---

## File inventory (what the wizard writes)

All writes go through `atomic_agents._io.atomic_write`. Path components are
validated via `_io.safe_resolve_under` before any write.

| File | Content source |
|---|---|
| `<agent>/persona/IDENTITY.md` | Q1 `${agent_name}`, Q2 `${mission}`, Q3 `${scope_in}` + `${scope_out}`, Q4 autonomy vars |
| `<agent>/persona/SOUL.md` | Q5 `${voice}` |
| `<agent>/persona/USER.md` | Q6 `${comm_prefs}`, Q7 `${hard_refusals}` (behavioral framing) |
| `<agent>/tools.md` | Q7 `${hard_refusals}` (policy phrasing) + locked defaults |
| `<agent>/model.md` | claude-opus-4-7 default, claude-sonnet-4-6 fallback, $0.50 daily / $7 monthly cost guardrails |
| `<agent>/memory/INDEX.md` | Seven structured sections: Critical Feedback / Locked Decisions / User Profile / Active Projects / Reference / Recently Promoted to Persona / Archive (superseded) |
| `<agent>/wiki/INDEX.md` | Three structured sections: Background and context / Reference material / How wiki pages cite sources |

Two empty directories are created via `mkdir` only (no file written):
`<agent>/journal/` and `<agent>/log/`. These are populated by the framework on
first run.

The 12 substitution variables (defined in `constants.py`) are:

`${agent_name}`, `${mission}`, `${scope_in}`, `${scope_out}`,
`${autonomy_preset_label}`, `${autonomy_read_only}`,
`${autonomy_reversible_write}`, `${autonomy_external_side_effect}`,
`${autonomy_high_risk}`, `${voice}`, `${comm_prefs}`, `${hard_refusals}`.

templates ship at `atomic_agents/init/templates/<name>/` as package data.
Access pattern: `importlib.resources.files("atomic_agents.init") / "templates"
/ template_name`. (`importlib.resources.files()` requires Python 3.9+, which
matches the framework minimum.)

---

## Recovery flow

**Collision detection.** If `<agent_dir>` already exists when the wizard
attempts to write, it presents three choices:

- **[overwrite]** -- replace the entire folder with a fresh scaffold.
- **[add_to_it]** -- merge new answers into existing files, preserving
  operator-authored sections and data directories. Only offered when a
  known template was used (the `--from-template` path). The interactive Q&A
  path offers only [overwrite] and [cancel] because there is no section
  schema to merge against.
- **[cancel]** (default) -- leave the folder untouched and exit status 0.

**Overwrite branch uses the backup+restore pattern.** On Overwrite:

1. Atomically rename `<agent_dir>` to `<agent_dir>.bak.<UTC-ISO>`.
2. Write all seven files and create the two empty directories to a fresh
   `<agent_dir>`.
3. On success: `shutil.rmtree(<agent_dir>.bak.<UTC-ISO>)` removes the backup.
4. On any write failure: rename the `.bak` directory back to `<agent_dir>` and
   exit with a plain-English error citing the path and reason.

**Add-to-it path.** On Add-to-it, the wizard uses file-level atomic merging
rather than a staging directory:

1. Section detection runs against the template's
   `constants.TEMPLATE_SECTION_SCHEMA`. If any schema-required h2 header is
   missing from an existing file, detection fails and the wizard falls back to
   [overwrite] or [cancel] only (fail-closed).
2. Missing template-owned files are announced and will be backfilled from the
   template.
3. For each schema-owned file, the wizard renders a fresh copy from the
   template and merges it with the existing file:
   - Schema-owned h2 sections are replaced with fresh content (so Q&A
     answers such as mission, voice, comm_prefs are applied).
   - Operator-authored orphan sections (h2 headers not in the schema) are
     preserved verbatim in their original relative position.
   - h3+ subsections inside any h2 block are preserved verbatim as part of
     their containing block's body.
   - The preamble (content before the first h2) is always kept from the
     existing file.
4. A unified diff preview is shown before any file is written.
5. On operator confirmation, each file is written via `_io.atomic_write`
   (tmp + fsync + rename). Each file commits independently; a crash
   mid-write leaves either the old file or the new file intact (per-file
   atomicity from `atomic_write`), never a half-written file.
6. Operator data directories -- `memory/`, `journal/`, `log/`, `raw/` -- are
   never touched. The merge only operates on files explicitly listed in
   `constants.TEMPLATE_SECTION_SCHEMA[template_name]`.
7. If any files fail to write, the wizard prints a partial-update warning
   listing which files succeeded and which failed, and exits status 1.

`OSError` from any `mkdir` or `atomic_write` call is caught and translated to
plain English per `constants.MSG_OSERROR_HEADER` and
`constants.MSG_OSERROR_FIX`. Stack traces never propagate to the operator.

---

## Doctor handoff and opt-in test call

After the scaffold is written, the wizard calls
`doctor.run_doctor(agent_name=<new_agent>, agents_root=resolved_root)` and
prints the doctor report.

If `doctor.overall_exit_code(results) != 0`: print "Doctor found problems with
the new agent. Review the output above and fix before running. Your files are at
`<path>`." Exit 1. The test-call prompt is skipped.

If exit_code is 0 and any results have status SKIP: print "Skipped checks are
normal for a new agent (MCP, logs, and write-paths are configured later)." before
offering the test-call prompt.

**Test-call prompt.** Default Yes. "Want to try a test call now? [Y/n]"
Y triggers `agent.call(constants.TEST_CALL_WORK_ITEM)`.

Exception catalog (every path exits status 0 -- scaffold succeeded; test call
is best-effort):

| Exception | Message |
|---|---|
| `anthropic.RateLimitError` | "The API is busy right now. Wait a minute and try: `atomic-agents run <agent> --work-item 'Hello'`." |
| `anthropic.AuthenticationError` | "Your API key was rejected. Check that it is active at console.anthropic.com." |
| `anthropic.APIConnectionError` / `httpx.ConnectError` / `httpx.TimeoutException` | "Could not reach the Anthropic API. Check your network connection." |
| `AtomicAgentsError` | "Atomic Agents error: `<message>`." |
| `Exception` (fallback) | "Something went wrong during the test call: `<type>: <message>`. Your agent scaffold is ready at `<path>`." |

The `anthropic` import is lazy (inside the `try` block).

---

## CLI rendering primitive (rich)

`rich` is added to runtime dependencies in `pyproject.toml`. One
`rich.console.Console` instance is created per `run_init()` invocation and
passed explicitly as `console=_console` to every `rich.prompt.Prompt.ask()`
call. No module-level Console is permitted.

The import is lazy in cli.py: `from .init import run_init` lives inside the
`if args.cmd == "init":` dispatch case. This matches the existing pattern at
cli.py:703 (`from . import doctor as doctor_module`) and cli.py:738
(`from .persona.backend import get_default_persona_backend`).

Future arcs migrate doctor / bundle / corpus output to rich (TODO-3 filed at PR
1 close).

---

## Templates (importlib.resources)

Templates ship at `atomic_agents/init/templates/<name>/` as package data.
`pyproject.toml`'s existing `packages = ["atomic_agents"]` (hatchling)
auto-includes them. No force-include directive is needed.

Access pattern:
```python
importlib.resources.files("atomic_agents.init") / "templates" / template_name
```

Variable rendering uses `string.Template.safe_substitute()`. `.substitute()` is
forbidden: operator free-text answers may contain `$` characters, which would
raise `KeyError` and abort the write.

The 12 substitution variables in `constants.py` drive every template.

---

## CHANGELOG interleave order

Within `[Unreleased]`, bullets are ordered newest-arc-at-top. On a merge
conflict with a parallel arc (for example, #201): the PR that merges last sits
at the top. Tiebreaker for ambiguous order: alphabetical by issue number.

---

## Implementer Contract -- 15 normative MUSTs

1. The wizard MUST validate `agent_name` against `constants.AGENT_NAME_REGEX`
   AND refuse names in `constants.RESERVED_AGENT_NAMES` before any filesystem
   side effect.

2. The wizard MUST reject non-interactive terminals via `sys.stdin.isatty()`
   BEFORE importing `rich` or instantiating any `Console`, on the interactive
   Q&A path. The `--from-template` and `--list-templates` paths are CI-friendly
   and MUST NOT require an interactive terminal (they are the documented
   non-interactive escape hatches). The non-TTY error message MUST name
   `--from-template <name>` as the alternative.

3. The wizard MUST catch `OSError` on every filesystem side effect (`mkdir` AND
   every `atomic_write` call) and translate it to a plain-English message per
   `constants.MSG_OSERROR_HEADER` and `constants.MSG_OSERROR_FIX`. Stack traces
   MUST NOT propagate.

4. The wizard MUST use `atomic_agents._io.atomic_write` for every file write.
   Direct `open(..., "w")` is forbidden. The wizard MUST also validate every
   path component derived from operator-controlled input through
   `atomic_agents._io.safe_resolve_under(child, agent_dir)` before passing it
   to `atomic_write`. On a fresh-write failure (no pre-existing scaffold to
   restore), the wizard MUST clean up the partial `agent_dir` it created so
   the operator sees either a complete scaffold or none of one.

5. Recovery atomicity: The collision Overwrite branch MUST use the
   backup+restore pattern: atomic rename to `<agent_dir>.bak.<UTC-ISO-microsecond>`,
   write all files, success rmtree the `.bak`, failure rename `.bak` back.
   The collision Add-to-it branch MUST use the file-level atomic pattern:
   compute merged content for each schema-owned file; display a unified diff
   preview between existing and merged content; on operator confirmation, write
   each file via `_io.atomic_write` (tmp + fsync + rename) directly into
   `agent_dir` in sorted relpath order. On operator decline, no files are
   written. On write failure mid-commit, already-written files are committed
   (per-file atomicity from `atomic_write`); not-yet-written files are left as
   their existing versions; the wizard prints a partial-update warning listing
   committed and failed relpaths and exits status 1. Operator-authored memory
   notes (under `memory/` except `INDEX.md`), journal entries (`journal/*.jsonl`),
   log files (`log/`), and raw documents (`raw/`) MUST NOT be touched during
   Add-to-it. Schema-owned scaffolding files (`memory/INDEX.md`, `wiki/INDEX.md`)
   ARE rewritten through the normal Add-to-it merge pattern because they are
   template-owned routing/structure files.

6. The wizard MUST warn before any mkdir or file write when
   `ATOMIC_AGENTS_PERSONA_BACKEND_URL` is set non-empty. Decline MUST exit 0
   with zero filesystem side effects.

7. The wizard MUST resolve the Anthropic API key via
   `atomic_agents._llm._get_key(env_vars=constants.ANTHROPIC_ENV_VARS,
   keychain_name=constants.ANTHROPIC_KEYCHAIN_NAME,
   config_key=constants.ANTHROPIC_CONFIG_KEY)` at pre-flight on the interactive
   Q&A path. The `--from-template <name>` and `--list-templates` paths MUST NOT
   require an API key at scaffold time because templates write file content only
   with no LLM call. The opt-in test call at end of `--from-template` still
   requires the key; when absent, the test-call prompt is skipped with a
   one-line notice.

8. The wizard MUST call `atomic_agents.doctor.run_doctor()` on the new agent
   and MUST block the test-call prompt when
   `doctor.overall_exit_code(results) != 0`.

9. The opt-in test call MUST catch the exception catalog via `isinstance`
   checks (NOT class-name string matching, which misses subclasses): lazy-
   import `anthropic` and `httpx` inside the `try` block; then check
   `isinstance(e, anthropic.RateLimitError)`, `isinstance(e,
   anthropic.AuthenticationError)`, `isinstance(e,
   (anthropic.APIConnectionError, httpx.ConnectError,
   httpx.TimeoutException))`, then `isinstance(e, AtomicAgentsError)`, with a
   generic `Exception` fallback. Every exception path MUST exit status 0.

10. The IDENTITY.md Autonomy section MUST use `constants.ACTION_CLASSES` and
    `constants.POLICIES` verbatim. The shorthand strings (`audit`, `judge`)
    MUST NOT appear.

11. Entry guards by invocation path:
    - Interactive Q&A: MUST 1 (name validation), MUST 2 (non-TTY rejection),
      MUST 6 (persona-backend warning before write), MUST 7 (API key
      pre-flight).
    - `--from-template <name>`: MUST 1 (name validation), MUST 6 (persona-
      backend warning before write), MUST 7 (API key pre-flight). Non-TTY is
      permitted. `agent_name` MUST be supplied; the wizard MUST refuse with a
      clear error if `--from-template` is given without `agent_name`.
    - `--list-templates`: no entry guards (read-only enumeration; no files
      written, no LLM calls, no name required). `--list-templates` MUST
      enumerate exactly the templates named in the `--from-template` argparse
      choices list. The enumeration MUST stay in sync with `--from-template`
      choices across all PRs that add or remove templates.

12. CHANGELOG `[Unreleased]` MUST interleave newest-arc-at-top with
    alphabetical-by-issue-number tiebreaker on conflict.

13. Template variables MUST be substituted via
    `string.Template.safe_substitute()`. `Template.substitute()` is forbidden
    because operator free-text answers may contain `$` characters.

14. The `cli.py` change MUST be additive only: one lazy `import` inside the
    `_cmd_init` function (matching the existing lazy-import pattern at
    `_cmd_doctor` and `_cmd_persona`), one `sub.add_parser("init", ...)` block
    with its arguments, one dispatch case in the doctor/persona/corpus
    early-branch, one `_cmd_init` function, and two docstring lines (Usage
    entry + Subcommands entry). NO existing code in `cli.py` may be modified.
    Total additions MUST stay under 60 lines (the natural cost of multi-line
    argparse `add_argument` calls with operator-facing help text on every
    argument, plus the subparser declaration and dispatch wiring).

15. Section-detection contract for Add-to-it: The wizard MUST detect existing
    template-owned sections via ATX-style h2 header match (`^##\s+(.+)$`)
    against `constants.TEMPLATE_SECTION_SCHEMA[template_name][file_relpath]`.
    The section-detection parser MUST skip header-shaped lines inside code
    fences (delimited by ` ``` ` or `~~~`), HTML comments (HTML comment
    toggle MUST trigger only on lines whose stripped form starts with `<!--`
    and ends with `-->` respectively, to avoid false positives from inline-code
    documentation of HTML comment syntax), and YAML frontmatter (delimited by
    `---` at file top). Files containing Setext-style headings (a line of only
    `=` or `-` characters following a non-empty text line) MUST cause section
    detection to fail and route to overwrite/cancel; operators MUST convert to
    ATX (`## Header`) before using Add-to-it. Files containing duplicate schema
    h2 headers (the same `## Header` appearing twice in one file) MUST cause
    section detection to fail and route to overwrite/cancel. When
    section-detection fails (file structure does not match schema), the wizard
    MUST fail closed by offering Overwrite or Cancel only. When a
    template-owned file is missing entirely, the wizard MUST backfill it from
    the template; the diff-preview MUST label backfilled files as `[new file]`
    and show full new content. Operator-authored h2 sections not in the schema
    (orphan sections) MUST be preserved verbatim including all h3+ subsections.
    For existing schema h2 blocks (Add-to-it merge of a block already in the
    file), the merge MUST be ADDITIVE: (a) the existing preamble between the
    `## Header` line and the first `###` MUST be preserved verbatim (operator
    filled this in; fresh template preamble is used only for missing-h2
    backfill cases); (b) h3+ subsections present in the existing file MUST be
    preserved verbatim in original order; (c) h3+ subsections in the fresh
    template not present in the existing file MUST be appended at the end of
    the schema h2 block.

---

## Future work

PR 2 of arc: researcher + writer templates; "Add to it" recovery merge
contract.

Fast-follows filed at PR 1 close: `--ai-assist` LLM-drafted persona (issue),
`/atomic-init` Claude Code skill (v1.1 issue), rich migration for
doctor/bundle/corpus output (polish umbrella issue).

v1.1+ when the registry expands beyond the filesystem: tighten MUST 6 to also
warn on non-filesystem backend URLs registered in
`ATOMIC_AGENTS_PERSONA_BACKEND_URL`.
