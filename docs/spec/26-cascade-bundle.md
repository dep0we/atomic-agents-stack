# 26 — Cascade bundle (skill-mode pre-render)

**Status:** DRAFT (ships with [#231](https://github.com/dep0we/atomic-agents-stack/issues/231)).
**Origin:** [#231](https://github.com/dep0we/atomic-agents-stack/issues/231) — filed from Muse Showrunner smoke test 2026-05-17 (~7 minute end-of-session wall time, dominated by per-file Read tool round-trips).

## Overview

Atomic Agents agents invoked via Claude Code skill mode load their cascade by issuing one Read tool call per cascade file. A realistic spec/06 three-layer cascade is ~15 files for the standard load (role layer + instance persona + project shared + memory INDEX + wiki INDEX + recent journal); real-world skill authors often append a few more files via the extras mechanism, pushing the realistic per-agent load to 18-24 files.

Each Read is a model round-trip — the model receives the prior turn's output, decides on the next tool call, emits the tool use, waits for the tool result, decides on the next tool call. Per-call latency is ~1-5 seconds. Cumulative cascade load on first response is **30-90 seconds before the agent says anything substantive**.

The cascade is *supposed* to be substantial — the framework's whole architecture rewards depth (full memory + wiki INDEX, recent journal, pinned atomic notes per spec/04). Telling operators to trim the cascade for speed would undermine the design.

This spec ships a small framework primitive — `atomic-agents bundle <agent>` — that pre-renders the cascade into one file the skill loads in **one** Read instead of N. Wall-time for cascade load drops from 30-90s to ~1-3s. Source files stay canonical; bundles are derived.

The bundle covers spec/04 steps [1]-[10] for single-agent layouts and the spec/06 three-layer equivalent for cascaded multi-agent projects — everything the agent's `AtomicAgent.load()` would assemble at startup. Step [11] (the work item) is per-invocation user input and is never in the bundle. On-demand content during conversation (wiki pages, non-pinned memory notes, draft files via tools.md read paths) stays on-demand — bundles do NOT prefetch the entire reachable vault.

## Module layout

```
atomic_agents/
├── bundle.py          # render_bundle + helpers (single module, no backend protocol)
└── cli.py             # `bundle` subcommand wired alongside info/run/doctor

docs/spec/
└── 26-cascade-bundle.md  # this file

tests/
└── test_cascade_bundle.py
```

Bundles are a **derived rendering** of state that already lives behind existing protocols (`MemoryBackend`, `AgentProfileBackend`, `LogBackend`, etc.). Per CLAUDE.md rule #2, protocols exist for *storage primitives*; pre-rendering existing state is not a storage primitive and does NOT get its own backend protocol. The bundle module composes the existing `_cascade` functions + filesystem walks; future revisions can route specific sections through `PersonaBackend` (#62) once that ships (see Decision 4 below).

## Load-bearing design decisions

### Decision 1: One bundle file, one cache directory, mtime-based staleness

**The shape.** `atomic-agents bundle <agent>` writes a single markdown file to:

```
$ATOMIC_AGENTS_CACHE_DIR/<agent-slug>.md
(default: ~/.cache/atomic-agents/bundles/<slug>.md)
```

The skill calls `atomic-agents bundle --if-stale <agent>` before its single Read. `--if-stale` regenerates only when any source file's mtime is newer than the bundle's. `--refresh` forces regeneration.

**Why mtime, not a file watcher.** A file watcher (`watchdog` / `inotify` / `fswatch`) would require either a daemon (operationally heavy for a stateless framework) or a per-skill-invocation setup (cost-prohibitive). Mtime comparison on local SSD is sub-millisecond for the ~25 file cascade and runs *inside* `atomic-agents bundle --if-stale`, so the staleness check is invisible in the skill's wall-time. No new process, no new dependency.

**Why one cache dir, not per-agent.** Bundles are operator-machine state, not agent state. Putting them in `<agent>/.bundle.md` would put derived artifacts in the vault — and the vault is the source of truth (rule #1). Per-operator-machine cache cleanly preserves the vault-is-canonical property.

### Decision 2: Skill mode reads, bundle command writes; agent runtime ignores bundles entirely

`AtomicAgent.load()` does NOT consult the bundle. The runtime path reads cascade files individually as it always has — bundles are a **skill-template optimization**, not a runtime cache.

**Why.** The runtime is in-process and already has the cascade content in memory after `load()`. There's no round-trip cost to amortize. A bundle-aware runtime would add complexity (cache invalidation on mid-run edits, divergence risk between bundle content and runtime view) without any operator-visible benefit. The bundle's value is *specifically* the elimination of N tool-call round-trips a Claude Code skill makes — a context that doesn't exist for in-process runtimes.

This separation keeps the bundle module purely additive: removing the bundle CLI would leave the runtime unchanged.

### Decision 3: Bundle covers `AtomicAgent.load()` startup content exactly — no more, no less

The bundle includes:

| Section | spec/04 step | spec/06 layer |
| --- | --- | --- |
| Role PROMPT.md | [1] | Layer 1 |
| Persona IDENTITY / SOUL / USER | [1-3] | Layer 3 |
| Tools.md (merged with override if present) | [4] | Layer 1 + 3 |
| Model.md (informational) | [5] | Layer 1 + 3 |
| Project canon.md | [3.5/7] | Layer 2 |
| Project style_guide.md | [8] | Layer 2 |
| Project goal.md | [3.5/7.5] | Layer 2 |
| Project policy/ (concatenated) | [9] | Layer 2 |
| Memory INDEX.md | [6] | Layer 3 |
| Pinned atomic notes | [8] | Layer 3 |
| Wiki INDEX.md | [7] | Layer 3 |
| Recent atomic notes (excluding pinned) | [9] | Layer 3 |
| Recent journal (last N) | [10] | Layer 3 |

The bundle does NOT include:

- The work item / user message (step [11]) — per-invocation user input.
- Non-pinned memory notes beyond `RECENT_NOTES_DEFAULT` — those are recalled on-demand via INDEX during conversation (rule #6, progressive disclosure).
- Wiki page bodies beyond INDEX — same; on-demand recall.
- Files reachable via tools.md `read_paths` — those are tool-fetched as the agent decides.

**Why this exact set.** It's the contract `AtomicAgent.load()` already commits to. By bundling exactly the assembled system prompt's source files, the bundle preserves rule #6: the LLM still pays context tokens only for *capability awareness* (INDEXes route to specific notes), not capability content (full wiki).

### Decision 4: No `CascadeBundleBackend` protocol; future PersonaBackend composes cleanly

Bundles are derived state. Per CLAUDE.md rule #2, protocols exist for storage primitives. A `CascadeBundleBackend` would be a protocol for *rendering* — wrong layer.

**Future composition.** When `PersonaBackend` (#62) ships, the persona section of the bundle should route through it instead of reading the filesystem directly. That's a small follow-up: replace the persona-file reads in `bundle._render_cascaded` with `persona_backend.load_persona(agent_id)` calls. The bundle's public interface (`render_bundle`, `BundleResult`, the cache layout, the CLI flags) stays stable. Bundle file format stays stable.

Similarly, when `CorpusBackend` ships (#65), wiki INDEX rendering can route through it. None of these compositions require a bundle protocol — they're implementation-internal edits to `bundle.py`.

The non-decision: we are NOT creating `BundleBackend` for alternate cache substrates (SQLite-backed bundle storage, S3-backed, etc.). The bundle cache is local-disk-per-operator-machine by design (Decision 1).

### Decision 5: Operator extras via `bundle.md` + `--extra-file`

Skill authors often need to bundle files beyond the standard spec/04+06 cascade. The motivating case: Muse's Showrunner skill reads operator-identity files (`~/ObsidianVault/personal/identity/{SOUL,USER,HEARTBEAT}.md`) that are NOT part of the framework cascade but ARE part of the skill's load.

Two equivalent shapes:

- **Declarative** — `<agent>/bundle.md`. One path per line. Supports `- path` markdown-list shape, `# comment` lines, backtick-quoted paths, and globs (`*` / `?` / `[`). Relative paths resolve under `<agent>`. Absolute paths and `~`-expanded paths are accepted as-is. **Preferred** — the extras list lives next to the agent's other config.
- **Ad-hoc** — repeatable `--extra-file <path>` CLI flag. Useful for one-off testing or skills that vary per invocation.

**Failure mode**: missing files raise `FileNotFoundError` rather than silently dropping. Operators learn about misspellings immediately. Globs that match zero files also raise — a `bundle.md` line like `~/missing-dir/*.md` is a configuration error, not an empty result.

**Trust model**: the bundle command reads + concatenates files the operator's process can already read. There is no privilege boundary to cross. Operators authoring `bundle.md` declare their own filesystem reads; the bundle does not execute the listed files or transmit them off-machine. Per-line `..` segments are accepted because operator extras legitimately reference paths outside the agent root (e.g., shared operator-identity files).

### Decision 6: Section headers mirror spec/04 cache breakpoints

The bundle uses these in-file markers:

```
# === BREAKPOINT 1: Stable cascade ===              ← persona + tools + model + project layer
# === BREAKPOINT 1.5: Operator extras ===           ← bundle.md / --extra-file
# === BREAKPOINT 2: Weekly (INDEXes + pinned) ===   ← memory INDEX + pinned + wiki INDEX
# === BREAKPOINT 3: Session (recent atomic notes) === ← recent atomic units excluding pinned
# === BREAKPOINT 4: Daily (recent journal) ===     ← last N journal entries
```

**Why.** A future caller that wants to map bundle sections back to Anthropic prompt-cache breakpoints can do so by parsing the headers. Today's runtime does not consume these markers (it reassembles the prompt from sources), but spec/04 §"Cache breakpoints" defines the boundaries and the bundle's section structure encodes them losslessly.

Sections within a breakpoint use `## Section · sub-label` shape with the source path in backticks on the next line:

```markdown
## Role layer · PROMPT.md
`~/agents/muse/roles/showrunner/PROMPT.md`

<file body, stripped>
```

This preserves both human readability and machine parseability.

## Contract

`atomic_agents.bundle.render_bundle(agent_root, *, agents_root=None, cache_dir=None, extra_files=None, if_stale=False)`:

| Behavior | Guarantee |
| --- | --- |
| Returns `BundleResult` | Always. `regenerated=False` only when `if_stale=True` and bundle was fresh. |
| Atomic write | Bundle path is either fully rewritten or unchanged. No partial state. Uses `_io.atomic_write` (temp + fsync + rename). |
| Missing agent_root | Raises `FileNotFoundError`. |
| Missing extras (declared in bundle.md or passed via `--extra-file`) | Raises `FileNotFoundError`. No silent drops. |
| Empty cascade (e.g., agent_root exists but has no PROMPT/persona/etc.) | Renders a header + any extras + an empty body. Operator gets a near-empty bundle but no exception. |
| Source files contain CRLF / unusual encoding | Read with `encoding="utf-8"`; bundle written with `encoding="utf-8"`. Non-UTF-8 sources raise `UnicodeDecodeError`. |
| Concurrent invocations on the same agent | Safe — `atomic_write`'s temp + rename pattern is atomic on POSIX. Two simultaneous renders produce a single final file equal to whichever finished last. |

`BundleResult` fields: `path`, `regenerated`, `section_count`, `total_bytes`, `source_count`.

## File format

Each bundle file is plain UTF-8 markdown with this shape:

```markdown
<!-- atomic-agents cascade bundle (spec/26) -->
<!-- agent: /abs/path/to/agent -->
<!-- generated: 2026-05-19T14:32:11+00:00 -->
<!-- sources: 24 files -->
<!--   2026-05-17T22:14:00+00:00  persona/IDENTITY.md -->
<!--   2026-05-17T22:14:01+00:00  persona/SOUL.md -->
<!--   ... -->

# === BREAKPOINT 1: Stable cascade ===

## Role layer · PROMPT.md
`<abs source path>`

<file body>

═══════════════════════════

## Instance persona · IDENTITY.md
`<abs source path>`

<file body>

═══════════════════════════

...

# === BREAKPOINT 2: Weekly (INDEXes + pinned) ===

...

<!-- end bundle -->
```

The header records timestamp + per-source mtime for debuggability (first 25 sources, with a summarizing tail line if more exist). Sections are separated by a horizontal-rule-like `═══════════════════════════` line matching `AtomicAgent.assemble_system_prompt`'s in-prompt separator — so a reader comparing the bundle to a system prompt sees identical structure.

## CLI surface

```text
atomic-agents bundle <agent>                       # generate (writes bundle)
atomic-agents bundle <agent> --if-stale            # skill-mode default; skip if fresh
atomic-agents bundle <agent> --refresh             # force regeneration
atomic-agents bundle <agent> --extra-file <path>   # repeatable
atomic-agents bundle <agent> --to-stdout           # print, don't write
atomic-agents bundle <agent> --print-path          # print bundle path without (re)generating
atomic-agents bundle <agent> --cache-dir <dir>     # override cache location
atomic-agents bundle <agent> --agents-root <dir>   # match other commands
```

Exit codes: `0` on success (including `--if-stale` skip), `1` on missing agent / missing extra / I/O failure.

Skill template (the recommended pattern documented in `docs/implementation/claude-skill-agent.md`):

```markdown
## Step 1 — Load the cascade

Run this Bash command, then Read the file it points to:

```bash
atomic-agents bundle --if-stale <agent-path>
```

Then Read: `~/.cache/atomic-agents/bundles/<slug>.md`

The bundle contains your full cascade in canonical spec/04 + spec/06 order.
```

## Doctor check

`atomic_agents.doctor.check_bundle_cache_writable` validates that the bundle cache directory exists (creating it if needed) and is writable. PASS/FAIL ladder matching the existing doctor check pattern. Runs as a host-level check (no `--agent` required) since the cache is per-operator-machine, not per-agent.

`fix_hint` strings cover the two failure modes: cache directory unreadable (`chmod`) and `ATOMIC_AGENTS_CACHE_DIR` pointing somewhere unwritable.

## Composition with future backends

| Future protocol | What it changes in the bundle | What stays |
| --- | --- | --- |
| `PersonaBackend` (#62) | Persona section reads route through `persona_backend.load_persona(agent_id)` instead of direct filesystem reads | Bundle format, CLI flags, cache layout, section ordering |
| `CorpusBackend` (#65) | Wiki INDEX rendering routes through the corpus backend's INDEX accessor | Bundle format |
| `PolicyBackend` (#89) | Project `policy/*` concatenation routes through the policy backend | Bundle format |
| `MCPServerRegistryBackend` (#201) | Not relevant — bundle doesn't include MCP server registration content |  |

The bundle's *external* interface is stable across these protocol additions. Each is an implementation-internal substitution inside `bundle.py`'s render functions.

## Non-goals

- **No file watcher.** Manual + mtime-staleness is sufficient. If watcher infrastructure ships for some other reason in the future, bundles can opt in, but `bundle.py` does not require it.
- **No bundling of on-demand content.** Wiki pages, non-pinned atomic notes, and draft files stay on-demand per rule #6.
- **No bundle versioning / migration.** Bundles are derived. Delete the cache and regenerate if the format changes between releases.
- **No `CascadeBundleBackend` Protocol.** See Decision 4.
- **No bundle-aware runtime.** See Decision 2.
- **No content compression.** A ~45KB bundle file is fine. Revisit if bundles grow past ~200KB.
- **No multi-agent bundles.** One agent → one bundle. Multi-agent compositions belong elsewhere (e.g., a future delegation-aware tool).

## Test surface

`tests/test_cascade_bundle.py` (~18 tests):

- Cascaded full layout — every spec/06 layer represented in the bundle.
- Flat single-agent layout — spec/04 order, no project sections, no BP1.5.
- `tools.override.md` merging — instance override appended to role base.
- Missing-optional-files graceful omission — empty sections omitted, not blank.
- `bundle.md` declarative extras — paths, comments, list shape, globs.
- `--extra-file` CLI ad-hoc extras.
- Missing extras raise `FileNotFoundError` (no silent drops).
- `--if-stale` skip when bundle is fresh.
- `--if-stale` regen when a source mutates.
- Atomic write — no partial file on simulated mid-write failure.
- Pinned-note detection via `pinned: true` frontmatter.
- Recent-notes selection excludes pinned, newest by mtime.
- Recent-journal selection — newest N by filename descending.
- `ATOMIC_AGENTS_CACHE_DIR` env var override.
- Slug generation — relative-to-agents-root path with `/` → `-`.
- Slug fallback when agent path is not under agents-root.

## Future work

- Wire `PersonaBackend` (#62) into `_render_cascaded`'s persona section when that protocol ships.
- Consider a `--validate` flag that compares bundle content against `AtomicAgent.assemble_system_prompt()` byte-for-byte to catch drift between the bundle command and the runtime assembly path.
- Operator-configurable `RECENT_JOURNAL_DEFAULT` per-agent (today the framework default of 1 applies uniformly; some skills want 3).
