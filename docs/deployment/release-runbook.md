# Release runbook — atomic-agents-stack

This runbook is the operator-facing companion to [`versioning.md`](versioning.md). Where `versioning.md` defines *what* a release is (SemVer bump levels, the CHANGELOG-is-source-of-truth convention, the GitHub Release prose contract), this runbook describes *how* to actually run one through the `/ship` skill end-to-end on this project — including the project-specific adaptations that `/ship`'s gstack defaults don't cover.

It's the answer to issue [#155](https://github.com/dep0we/atomic-agents-stack/issues/155): "make `/ship` run end-to-end on this project without hard-failure steps, and document the manual sync responsibility for surfaces the bundled `/document-release` subagent misses."

---

## The two-mode workflow

This project ships **two kinds of `/ship` invocations**, and the operator chooses which mode at Step 12 when `/ship` asks:

| Mode | When | Step 12 behavior | Step 13 (CHANGELOG) behavior |
|------|------|------------------|------------------------------|
| **PR-level update** | Default for ordinary feature / fix / refactor PRs that will be folded into a future release. | No version bump. `NEW_VERSION` is set to the *current* released version from the latest dated CHANGELOG header. | Bullets append to `## [Unreleased]`. The header is **not** promoted to a dated section. |
| **Release cut** | When promoting accumulated `[Unreleased]` work into a tagged release. | Operator provides the new 3-digit SemVer string (validated `> CHANGELOG_VERSION`). | `## [Unreleased]` is promoted to `## [<new-version>] - <today>`. A fresh empty `## [Unreleased]` is left above it for the next cycle. |

This is the divergence from gstack's default model (in which every PR is its own dated release). The project follows the "accumulate then promote" CHANGELOG convention codified in [`versioning.md`](versioning.md) §"How a release happens". `/ship` supports both modes; the operator picks.

---

## Prerequisite: the local gstack `/ship` patch

`/ship` is a gstack-vendored skill at `~/.claude/skills/gstack/ship/`. The upstream skill assumes every project ships with a 4-digit `VERSION` file at the repo root. This project does not — versioning lives in CHANGELOG + git tags per `versioning.md`. Without an adaptation, `/ship` Step 9 hard-fails on the missing project-local review checklist, and Step 12 hard-fails on the missing `VERSION` file.

**The adaptation lives on a local feature branch in the gstack clone**, not on the gstack `main` branch (and not yet upstreamed):

- **Repo**: `~/.claude/skills/gstack/`
- **Branch**: `fix/ship-no-version-fallback` (from `main`, with one commit on top)
- **Patch summary**:
  - Step 9: when `.claude/skills/review/checklist.md` is missing, falls back to a built-in default checklist + warning. An unreadable-but-present file still STOPs.
  - Step 12: a `NO_VERSION_FILE` probe runs before the state machine. Projects without a `VERSION` file skip the bump-and-validate logic, derive the current version from the latest dated CHANGELOG header, and ask the operator to choose PR-level update vs. release cut.

**To confirm the patch is active** before running `/ship`:

```bash
git -C ~/.claude/skills/gstack branch --show-current
# Expected: fix/ship-no-version-fallback
```

If `main` is checked out instead, switch back:

```bash
git -C ~/.claude/skills/gstack checkout fix/ship-no-version-fallback
```

### Reapply procedure after `gstack-upgrade`

`gstack-upgrade` (or any `cd ~/.claude/skills/gstack && git fetch origin && git reset --hard origin/main`) will discard the local branch's changes from `main`'s checkout. Reapply by either:

1. **Cherry-pick the patch commit onto the new `main`** (preferred if upstream `main` has moved):

   ```bash
   cd ~/.claude/skills/gstack
   git fetch origin
   git checkout main && git pull
   git checkout fix/ship-no-version-fallback
   git rebase main
   bun run scripts/gen-skill-docs.ts --host all
   bun test test/skill-validation.test.ts test/gen-skill-docs.test.ts
   ```

2. **Re-create the patch from scratch** if the rebase has conflicts that aren't worth resolving:

   The patch only touches `ship/SKILL.md.tmpl` Step 9 (one paragraph + one bullet list) and Step 12 (one `NO_VERSION_FILE` probe + the dispatch prose). See the commit message on the patch itself for the design rationale.

The patch is filed for upstream consideration as a design discussion at `garrytan/gstack` — when (if) it lands upstream, drop the local branch and use `main`.

### Cross-project impact

The patch is strictly additive — both probes are guarded by "absent file" conditions:

- Step 9 fallback fires only when `.claude/skills/review/checklist.md` does not exist.
- Step 12 `NO_VERSION_FILE` path fires only when `VERSION` does not exist.

Other Dan projects (Meridian, Highland, DPIC, Bishop) that ship with `VERSION` files and project-local review checklists are unaffected.

---

## `/ship` end-to-end on this project

Once the gstack patch is in place and `.claude/skills/review/checklist.md` exists in this repo (it does, as of issue #155), `/ship` runs cleanly end-to-end. Step-by-step notes for what's specific to this project:

### Step 1 — Pre-flight

Plan / eng-review / design-review skill outputs in `~/.gstack-dev/skills/...` are checked. This project doesn't typically use those skills for backend / Protocol work; missing plan-review output is expected and `/ship` proceeds without it.

### Step 3 — Merge base branch

`origin/main`. No merge conflicts expected on clean feature branches.

### Step 5 — Run tests

`uv run pytest`. 928+ tests on Python 3.11/3.12. All must pass.

### Step 7 — Test Coverage Audit

Coverage audit runs against the diff. New backend Protocols add ~25 conformance tests + ~10 impl-specific tests as a matter of policy — coverage gaps tend to surface naturally.

### Step 9 — Pre-Landing Review

Reads `.claude/skills/review/checklist.md` (project-local, project-shaped). Two-pass: Pass 1 critical (Backend Protocol Invariants, Cost Gate Placement, Audit Trail Shape, Atomic Write Discipline, LLM Output Trust, Schema/API Break Detection), Pass 2 informational. See the checklist for the full rubric.

### Step 9.1 — Review Army (specialists)

`/ship` dispatches specialist subagents from `~/.claude/skills/gstack/review/specialists/`. Most are framework-agnostic and apply cleanly. Specialist false-positive patterns specific to this project will be captured here as they appear in real `/ship` runs — for now, the list is empty pending empirical observation. **Anticipated** patterns to watch for (speculative, treat as hints not knowledge):

- The maintainability specialist may flag `@dataclass(frozen=True)` + `Protocol` shapes against an OO-inheritance default. These are the intentional design pattern for backend protocols (see `CLAUDE.md` §"Protocols, not subclassing") — verify by reading the spec doc cited in the diff before accepting the finding.
- The testing specialist may flag missing pytest fixtures for parameterized conformance suites. The project's conformance shape is `pytest.mark.parametrize("backend", [...])` with a `make_backend` factory per impl (see `tests/test_memory_backend_conformance.py`) — verify before accepting any "use shared fixtures" recommendation.
- The security specialist may flag `_io.atomic_write` as an unusual file-write pattern. It's the project's standard atomic-write idiom — verify against `atomic_agents/_io.py` before accepting.

When a specialist false-positive does land in a real `/ship` run, capture the pattern (specialist name, what it flagged, why the finding was wrong) and append it here. Over time this section moves from anticipated → empirical.

### Step 11 — Codex adversarial review

**Codex is rate-limited** (as of 2026-05-13). Per [`docs/methodology.md`](../methodology.md) §"Reviewer roster", the documented fallbacks are:

1. **Opus subagent** with the verify-against-code prompt — same-family fallback.
2. **`atomic-agents review --backend kimi`** — cross-family fallback via Moonshot. Key in macOS Keychain at `atomic-agents-moonshot`; international `.ai` endpoint requires `MOONSHOT_BASE_URL=https://api.moonshot.ai/v1` in env. Note: Kimi K2.x thinking models have stronger reviewer signal, but their output lives in a `reasoning_content` field not yet extracted by the LLM backend ([#146](https://github.com/dep0we/atomic-agents-stack/issues/146)) — until that issue closes, expect Opus subagent + non-thinking Moonshot rather than two fully-independent rounds.

`/ship` falls back automatically when `codex` exits with rate-limit error codes; the operator just sees "Codex unavailable — running Opus subagent fallback" in the output.

### Step 12 — Version bump

Probes for the absence of a `VERSION` file (this project), enters `NO_VERSION_FILE` mode, and asks the operator: A) PR-level update (default for feature/fix PRs) or B) release cut (provide new SemVer string).

For a release cut, derive the new version per [`versioning.md`](versioning.md) §"What counts as Major / Minor / Patch":

- **Major / breaking** (pre-1.0: still bumps Minor with `### BREAKING` callout): schema break, framework API break, default backend change.
- **Minor**: new feature, new spec doc, new backend, new CLI subcommand.
- **Patch**: bug fix, doc, test, internal refactor with no observable change.

### Step 13 — CHANGELOG handling

Mode-dependent (see "two-mode workflow" table above). The PR-level mode appends bullets to `## [Unreleased]` only. The release-cut mode promotes `## [Unreleased]` to `## [<new-version>] - <today>` and leaves a fresh empty `## [Unreleased]` above it.

When the diff has **no user-facing change** (CHANGELOG bullets only, branch resync, version-bump housekeeping), the honest entry is one sentence — never pad with aspirational claims. See [`versioning.md`](versioning.md) and `CLAUDE.md` §"CHANGELOG is the single source of truth".

### Step 18 — Documentation sync (via subagent)

Dispatches `/document-release` as a subagent with a fresh context window. The subagent runs `find . -maxdepth 2 -name "*.md"` and applies named per-file checklists for `README.md`, `ARCHITECTURE.md`, `CONTRIBUTING.md`, `CLAUDE.md`; other top-two-level markdown files get a generic "determine purpose and audience" pass.

What the subagent **does not** reach (verified against `~/.claude/skills/gstack/document-release/SKILL.md`):

- `atomic_agents/__init__.py` — Python, not markdown. The `__version__` + `__all__` claims here are this project's job.
- `pyproject.toml` — TOML, not markdown.
- `docs/spec/*.md` — depth 3 under repo root (`./docs/spec/...`), past `-maxdepth 2`.
- `docs/architecture.md` — depth 2 (reachable) but the subagent's heuristics for `architecture.md` look in the repo root; the `docs/`-prefixed version may get the generic pass instead of the named pattern. Treat it as "the operator's job" rather than relying on subagent coverage.

The Operator Manual Surface Check below covers each of these explicitly.

### Step 19 — Create PR

PR title format: `v<NEW_VERSION> <type>: <summary>`. In PR-level mode, `NEW_VERSION` is the current release version (operator may edit if the title would be misleading). In release-cut mode, it's the new release version.

---

## Operator manual surface check before merge

**This is the load-bearing section.** The bundled `/document-release` subagent catches generic drift but does not know about this project's specific surfaces. The manual check below covers what would otherwise slip through. Historical reason: the v0.10.0 release shipped without `/ship`, the README's "What's shipped" table drifted, and the maintainer caught it only by accident. The same shape recurred during the v0.13.0 cut, where the `/document-release` subagent surfaced 21 doc-sync gaps total — 6 critical (version label off by two minors, `__all__` missing the new Protocol surface, related count claims stale), 6 polish-tier filed as [#156](https://github.com/dep0we/atomic-agents-stack/issues/156), and 9 already-in-sync. The numbers are documented verbatim in PR [#157](https://github.com/dep0we/atomic-agents-stack/pull/157)'s body. Don't replicate that.

Before merging any `/ship`-produced PR, verify these surfaces match what shipped:

### Version + public surface (CRITICAL — catches "wrong version" drift)

- [ ] **`atomic_agents/__init__.py`** — `__version__` matches the CHANGELOG's latest dated header (release cut) or the most-recently-released version (PR-level update). `__all__` includes every new Protocol, canonical type, or public exception added in this PR.
- [ ] **`pyproject.toml`** — `version = "X.Y.Z"` matches `__version__`.
- [ ] **`README.md`** — the version badge near the top matches `__version__`. The §Status section's "vX.Y, alpha" line matches.

### Architecture + status claims (catches "code shipped but the README doesn't know" drift)

- [ ] **`README.md`** "What's shipped" table — every shipped backend Protocol has a row with the right symbol (✅ shipped, 🟡 planned). When this PR ships a new backend Protocol, the table gets a new row.
- [ ] **`README.md`** repo-structure block — any new top-level module under `atomic_agents/` appears.
- [ ] **`CLAUDE.md`** §"Architecture in one breath" — the backend protocols block reflects the new shipped state.
- [ ] **`CLAUDE.md`** §"Status" — version, test count, shipped-protocol list are current.
- [ ] **`docs/architecture.md`** — count claims (number of backends shipped, number of layers) are current.

### Spec + test count claims (catches "stale numbers across N files" drift)

- [ ] **Test count** — claims like "928 tests on Python 3.11/3.12" appear in `README.md`, `CLAUDE.md`, possibly `docs/architecture.md`. All instances match the actual count after this PR.
- [ ] **Spec count** — claims like "22 spec docs today" need updating whenever a new `docs/spec/NN-*.md` lands. Search across `README.md`, `CLAUDE.md`, `docs/architecture.md`, and `docs/methodology.md`.
- [ ] **Spec cross-references** — when a new backend Protocol's spec doc lands (e.g., `docs/spec/31-llm-backend.md`), the spec docs that reference it (typically `04-runtime-assembly.md`, `17-tools.md`, `20-memory-backend.md`) get a cross-link.

### Module-internal docstring + README drift

- [ ] New module's `__init__.py` docstring describes the right surface.
- [ ] If `_llm.py` or any framework-private module's role changed, the corresponding `README.md` repo-structure description matches.

### CHANGELOG content gates

- [ ] Every substantive change in the diff has a corresponding `## [Unreleased]` (or dated, for release cut) bullet.
- [ ] Breaking changes carry a `### BREAKING` callout with operator-facing migration instructions.
- [ ] No bullets describe the branch development narrative — only the user-facing diff. See `CLAUDE.md` §"Working methods" / `docs/deployment/versioning.md` for the prose contract.

### Cross-cutting

- [ ] PR body's Test Coverage section, Pre-Landing Review section, and Documentation section accurately summarize what `/ship` ran and found.
- [ ] No new file accidentally committed (compiled binaries, `.DS_Store`, `__pycache__/`, etc.).
- [ ] Filed-inline follow-up issues from this session are linked in the PR body's "Filed-inline follow-ups" section.

---

## Pre-publish smoke

Run this sequence BEFORE pushing the git tag. A wheel with a broken entry point or unrendered README is the version forever on PyPI.

```bash
# 1. Build wheel + sdist.
uv build

# 2. Validate metadata and README rendering.
uv tool run twine check dist/*

# 3. Clean-venv install -- confirms the wheel installs from scratch with no dev deps.
python -m venv /tmp/smoke-venv && /tmp/smoke-venv/bin/pip install dist/*.whl

# 4. Verify CLI entry point is wired.
/tmp/smoke-venv/bin/atomic-agents --version

# 5. Verify doctor runs.
/tmp/smoke-venv/bin/atomic-agents doctor

# 6. Version assertion -- catches wheel-built-before-version-bumped errors.
/tmp/smoke-venv/bin/python -c "import atomic_agents; assert atomic_agents.__version__ == '1.0.0', f'Expected 1.0.0, got {atomic_agents.__version__}'"
```

If any step fails, fix before pushing the tag. The smoke must run on the actual built artifacts (not `uv run`), because `uv run` uses the live source tree.

---

## TestPyPI smoke

For the first publish ever to PyPI and for any release where the README or metadata changes significantly, publish to TestPyPI first.

**Why**: TestPyPI is separate from real PyPI; mistakes there do not affect production installs. It catches metadata rendering issues (e.g., README not rendering as Markdown, missing classifiers, broken long description) before they become permanent.

```bash
# Publish to TestPyPI (separate token required -- see Apple Passwords).
uv publish --publish-url https://test.pypi.org/legacy/ --token <TESTPYPI_TOKEN>

# Verify the project page renders correctly at:
#   https://test.pypi.org/project/atomic-agents-stack/

# Install from TestPyPI (uses real PyPI as fallback for dependencies).
pip install \
  --index-url https://test.pypi.org/simple/ \
  --extra-index-url https://pypi.org/simple/ \
  atomic-agents-stack

# Run smoke: atomic-agents --version + doctor
atomic-agents --version
atomic-agents doctor
```

Only after TestPyPI clears, publish to real PyPI:

```bash
uv publish --token <PYPI_TOKEN>
```

Both tokens (TestPyPI + PyPI) must be in Apple Passwords under `pypi-atomic-agents-stack-testpypi` and `pypi-atomic-agents-stack` respectively before the first publish.

---

## Rollback contract (yank semantics)

PyPI does not allow deleting or replacing a released version. If v1.0.0 has a critical bug post-publish:

1. **File v1.0.1 immediately** with the fix.
2. **Yank v1.0.0** on PyPI:
   - Via the PyPI web UI: go to the release page, click "Options", select "Yank this release", enter a reason.
   - Or via uv (if supported): `uv publish --yank "Critical bug: <description>" --token <PYPI_TOKEN>`.

**What yank does**: marks v1.0.0 as "not recommended for new installs." `pip install atomic-agents-stack` will skip it. `pip install atomic-agents-stack==1.0.0` still works for operators who have pinned it. The version history page still shows the yanked release with a warning banner.

**Do NOT** delete and republish. PyPI permanent history prevents true deletion, and attempting it creates confusion. The yank + v1.0.1 path is the correct recovery.

---

## Post-merge

For release-cut PRs only:

1. **Tag the merge commit** `vX.Y.Z` and push:
   ```bash
   VERSION=X.Y.Z
   git tag -a "v${VERSION}" -m "v${VERSION}"
   git push origin "v${VERSION}"
   ```
2. **Create the GitHub Release** using the CHANGELOG entry verbatim per [`versioning.md`](versioning.md) §"How a release happens" (the `awk`-extract + `gh release create --notes-file` pattern).
3. **Close issues** referenced in the CHANGELOG entry's "Closes #N" lines.

For PR-level merges: no post-merge ceremony. `[Unreleased]` continues accumulating until the next release cut.

---

## When `/ship` itself has friction

Per [`feedback_always_ship.md`](../../.claude/projects/-Users-dep0we/memory/feedback_always_ship.md) (Dan's user-level memory):

> If `/ship` has hard friction in this project: file an inline issue + an inline `gstack-config` fix or escalation, but still complete the workflow through the skill, not around it.

The friction-points that caused the issue #155 work are addressed. Anything new — file as an issue against this repo (`[tooling] /ship friction: ...`) and, if needed, on the gstack branch. Do not bypass `/ship`.

---

## References

- [`versioning.md`](versioning.md) — what counts as Major/Minor/Patch + the GitHub Release prose contract
- [`upgrading.md`](upgrading.md) — operator-facing runbook for applying a release on a running host
- [`../methodology.md`](../methodology.md) — working-methods retrospective (reviewer roster, codex-rounds-not-passes, verify-before-claim)
- [`../../CLAUDE.md`](../../CLAUDE.md) §"Always run `/ship` end-to-end — never bypass" + §"Working methods"
- [`../../.claude/skills/review/checklist.md`](../../.claude/skills/review/checklist.md) — project-local Pre-Landing Review checklist (Step 9 input)
- Issue [#155](https://github.com/dep0we/atomic-agents-stack/issues/155) — the forcing function for this runbook

### Maintainer-only paths (single-operator project today)

Two paths referenced earlier in this runbook live on the maintainer's machine, not in this repo, and external readers should treat them as conceptual hints rather than literal paths to follow:

- `~/.claude/projects/-Users-dep0we/memory/feedback_always_ship.md` — Dan's user-level Claude Code memory file enforcing the "every PR through `/ship`" discipline. External readers should treat it as "the maintainer keeps a long-lived workflow-discipline memory enforcing this rule."
- `~/ObsidianVault/Atomic Agents/RESUME-NEXT-SESSION.md` — the cross-session brief the maintainer writes at the end of each working session. External readers should treat it as "the maintainer keeps a per-session handoff brief."

When the project grows beyond a single maintainer, these references will either move to in-repo equivalents (e.g., a `CONTRIBUTING.md` section, or `docs/maintainer-handoff.md`) or be dropped. Pre-1.0 with a single contributor, the personal-machine refs are honest about the current shape.
