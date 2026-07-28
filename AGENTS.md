# AGENTS.md: atomic-agents-stack

This file loads in every AI agent session for this repo, including Codex,
Cursor, GitHub Copilot, Gemini CLI, and any tool that follows the AGENTS.md
open standard. `CLAUDE.md` is canonical (the full, internal rules Claude Code
reads locally); this file mirrors its public-safe subset for cross-tool
agents. Where the two differ, `CLAUDE.md` wins.

---

## What this is

Atomic Agents is a vault-native AI agent framework: agents live as plain
markdown files, the runtime is stateless, and storage sits behind swappable
protocols. The spec is the product. The Python package in this repo is one
conforming reference implementation, and the framework is designed so anyone
could build agents to the spec without this code at all. **This package ships
to PyPI** (`atomic-agents-stack`), and other projects depend on its protocol
surface, so a breaking change here is never local to this repo.

Twenty-two backend protocols are shipped or in-flight (see
`docs/protocols-shipped.md`). The guiding property is that a home user running
one agent and an organization running a fleet go through the same code, the
same files, and the same guarantees, just with different backends registered
behind the same protocols.

---

## Commands

- **Install (dev):** `uv sync` (installs the dev environment)
- **Test (full suite):** `uv run pytest`
- **Test (one module):** `uv run pytest tests/test_<module>.py -v`
- **Live test count:** `uv run pytest --collect-only -q | tail -1`
- **Lint:** `uv run ruff check .`
- **Format check:** `uv run ruff format --check .`
- **CLI smoke check:** `atomic-agents doctor` (verifies a configured vault's wiring)

CI runs the full pytest matrix (Python 3.11 and 3.12) on every PR; it does not
run ruff. Contributors run both ruff commands locally before opening a PR.

Environment variables: `ATOMIC_AGENTS_ROOT` points the runtime at a vault
(defaults to `~/docs/agents`). Individual backend protocols read their own
`ATOMIC_AGENTS_<PROTOCOL>_BACKEND` override plus a connection string where a
database substrate applies (see the relevant `docs/spec/*.md`). No variable
here is a secret; secrets live in `.env` (gitignored) or a configured
`SecretBackend`, never hardcoded.

---

## Design principles: the taste rules

When a proposed change would violate one of these, stop and write down why
before proceeding. Often the answer is "the rule still holds, rework the
change." Sometimes the rule needs to flex, and that gets written down too
(see `docs/TENSIONS.md`). Silently breaking one is not acceptable.

### 1. The vault is the source of truth

Anything stateful lives in the agent's folder as markdown and JSONL, with
structured JSON sidecars for pipeline state. Backends translate vault state
into other forms (database rows, vector indexes); they are never the
authoritative copy for the default filesystem deployment. Kill the runtime,
restart it, move it from cron to an HTTP service: the agent is the same agent
because the files are the same. If a feature needs state that exists *only*
in a backend, reconsider it. Portability is load-bearing here.

### 2. Protocols, not subclassing

When a primitive touches storage, it gets a protocol: a typed interface plus
a filesystem-default reference implementation, a spec doc, and a conformance
test suite every alternate implementation must pass. Don't bolt on a new
backend as `if backend == "postgres": ...` branching inside existing code.
Define the protocol, ship the filesystem default, let alternate
implementations register at import time.

### 3. Cost is first class, not bolted on

Every code path that can spend money on an LLM call checks a cost guardrail
*before* the spend, not after. Every parallel batch reserves worst-case cost
before dispatch. Every delegation clamps its budget to the smaller of what
the parent and the child have left, so running many agents in parallel has a
hard ceiling instead of open-ended risk. Don't add a spend path that skips
its guardrail, and don't move a cost check after a subprocess spawn or
session setup: refuse before paying the overhead.

### 4. Audit trail is structural

Every agent run writes an audit record with a run ID; child calls (helper,
tool, delegate) write their own records carrying the parent's ID, so the full
call tree is reconstructable after the fact. Memory edits keep their own
version history. A new feature with events the existing audit shapes can't
represent should extend the right shape, not add a side channel that the
rollup can't see.

### 5. Markdown config or no config

Operator-facing configuration stays in markdown files, editable in any text
editor. Structured fields can live in an embedded YAML block inside a
markdown file; a pure YAML, TOML, or JSON file meant for a human to hand-edit
does not fit this project's config aesthetic.

### 6. Atomic and idempotent everywhere

Writes go through a temp-file-plus-rename pattern with fsync, so a crash
leaves a recoverable artifact, never a half-written file. Teardown paths
(connection pools, registrations, locks) run safely even when called twice or
after a partial failure. No code path should leave state that a retry can't
recover from cleanly.

### 7. Refusals are permanent, not bugs

Several things are impossible on purpose, each with a recorded rationale:
delegation is capped at one level (a coordinator delegates to specialists;
specialists don't delegate further); filesystem paths built from agent, tool,
or model output are refused unless they resolve inside the allowed directory
(guards against path traversal); and a call cannot escape its cost cap unless
explicitly marked critical. **An agent reading this code cold should not
"fix" any of these as bugs.** Lifting one is a spec conversation with the
maintainer, never a silent code change.

### 8. Backward compatibility by default

This package ships to PyPI and other projects build on its protocol surface,
so a breaking change is a deliberate, versioned event, not something that
slips in. New backends are opt-in; existing deployments keep working with no
config changes. When a break is genuinely necessary it ships with a
`### BREAKING` callout in the changelog, a migration script, and an upgrade
note, never a quiet signature or behavior change.

### 9. Verify before claim, empirically

When a review (human or AI) asserts that something behaves a certain way,
reproduce it before accepting the finding. Don't accept by plausibility.
This applies in both directions: when you claim something works, run it and
confirm, so documentation, docstrings, and error messages stay accurate. A
green test suite is evidence a specific behavior was exercised, not proof an
entire feature works end to end.

---

## Working methods

- **Adversarial review in rounds, not one pass.** Non-trivial changes get
  multiple review rounds before merge, not one thorough pass. Each round
  catches different things because a fix commit changes the diff and exposes
  new edges. Two rounds is the floor; larger or riskier diffs get more.
- **The changelog is the single source of truth.** Every change adds its own
  bullets to the `[Unreleased]` section of `CHANGELOG.md` as part of the
  diff. Release notes are pulled from it verbatim, never regenerated from
  commit history.
- **Bisectable commits, not save points.** A non-trivial PR splits into
  multiple logical commits (for example: one for the runtime change, one for
  tests, one for the spec doc and changelog entry), so a future regression
  hunt has clean atoms to bisect against.
- **Reversible vs. irreversible get different gates.** Local edits, branches,
  and commits proceed freely. Pushing tags, merging PRs, and publishing a
  release require explicit maintainer approval every time.
- **File scope creep inline.** When a follow-up surfaces mid-task that isn't
  the current change, file it as its own GitHub issue immediately and keep
  the current PR clean. Don't ask, don't accumulate untracked debt.
- **Documentation matches reality, not aspirations.** A doc describes what
  the code does today. If a doc claim and the implementation disagree, fix
  one or the other in the same change; a follow-up "make it match the ideal
  later" gets its own tracked issue instead.

---

## Conventions

- **Issues / backlog:** GitHub Issues at `dep0we/atomic-agents-stack`. Title
  prefixes: `[backend]`, `[deployment]`, `[polish]`, `[spec]`, `[infra]`.
  Labels: `enhancement`, `documentation`, `infrastructure`, `polish`,
  `backend`, `deployment`, `spec`, `bug`.
- **Branches + PRs:** cut a feature branch from `main`; never push to `main`
  directly, even for a small change. PR body is the audit trail.
- **Tests:** the suite is the conformance gate. Run it before pushing. A new
  backend protocol adds roughly 25 conformance tests plus about 10
  implementation-specific tests. New features ship with tests, not follow-up
  promises.
- **Releases + versioning:** Semantic Versioning (major.minor.patch); the full
  policy, including what counts as each bump, lives in
  `docs/deployment/versioning.md`. Every release is a git tag plus a GitHub
  Release whose notes are the changelog entry, verbatim. A breaking change
  gets a `### BREAKING` callout in the changelog and a migration script.

---

## Data handling

Read operational or sensitive data freely while helping with a task; that's
expected. Never let it leave the working tree: no raw exports, logs, or
record-level data in a commit; no identifying details in a commit message,
branch name, PR title, or PR body; no credentials, anywhere, including as a
filled-in example value.

---

## House style

- No em dashes anywhere: commits, PR bodies, comments, docs. Use periods,
  commas, or parentheses, or restructure the sentence.
- Plain language, no unglossed developer jargon. Define a load-bearing
  technical term in a few words right after using it.
- Lead with the recommendation, then the trade-off.
- Verify before claiming. A passing suite is not proof a feature works end
  to end; reproduce the specific claim.

---

## Durable memory

This project's durable-memory slug is `atomic-agents-stack`, at
`system/memory/atomic-agents-stack/` in the maintainer's notes vault. The
tool-neutral memory system (where it lives, how it loads, how a lesson gets
saved) is defined once, outside this repo. This section only names the slug.

---

## Where things live

| Doc | Purpose |
|-----|---------|
| `docs/architecture.md` | The mental model, in diagrams. Read first. |
| `docs/protocols-shipped.md` | Per-protocol summary of every shipped backend |
| `docs/spec/` | The numbered, locked spec, the product itself |
| `docs/TENSIONS.md` | Architectural tensions to protect as the framework scales |
| `docs/methodology.md` | The working-methods retrospective behind this file's process rules |
| `docs/getting-started.md` | Clone-to-running-agent walkthrough |
| `docs/deployment/versioning.md`, `docs/deployment/upgrading.md` | SemVer policy and the operator upgrade runbook |
| `CHANGELOG.md` | What shipped in each release; source of release notes |
| `CONTRIBUTING.md` | Full contributor guide (issue shape, PR shape, what needs an issue first) |
