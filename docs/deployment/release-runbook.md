# Release runbook — atomic-agents-stack

This runbook is the operator-facing companion to [`versioning.md`](versioning.md). Where `versioning.md` defines *what* a release is (SemVer bump levels, the CHANGELOG-is-source-of-truth convention, the GitHub Release prose contract), this runbook describes *how* to run one: the explicit steps every PR goes through, from branch to PR, plus the manual surface check before merge.

The steps below are the whole release procedure: tests, review, CHANGELOG, version, documentation sync, commit, push, PR. Run them by hand, in order, on every PR. They replace the retired `/ship` skill, which no longer exists in this setup.

---

## The two-mode workflow

This project ships **two kinds of PRs**, and the operator chooses the mode at step 6 of the procedure below:

| Mode | When | Version (step 6) | CHANGELOG (step 7) |
|------|------|------------------|--------------------|
| **PR-level update** | Default for ordinary feature / fix / refactor PRs that will be folded into a future release. | No version bump. The version stays at the current released version from the latest dated CHANGELOG header. | Bullets append to `## [Unreleased]`. The header is **not** promoted to a dated section. |
| **Release cut** | When promoting accumulated `[Unreleased]` work into a tagged release. | The operator picks the new 3-digit SemVer string, which must be greater than the latest dated CHANGELOG version. | `## [Unreleased]` is promoted to `## [<new-version>] - <today>`. A fresh empty `## [Unreleased]` is left above it for the next cycle. |

The project follows the "accumulate then promote" CHANGELOG convention codified in [`versioning.md`](versioning.md) §"How a release happens". There is no `VERSION` file at the repo root: versioning lives in the CHANGELOG, `pyproject.toml`, `atomic_agents/__init__.py` and git tags.

---

## The release procedure

Run these steps in order on every PR. The notes under each step are specific to this repo.

### 1. Branch and update

Work on a feature branch cut from `origin/main`. Before the checks below, merge `origin/main` into the branch (or rebase) so the tests run against current code. No merge conflicts are expected on clean feature branches.

### 2. Run lint and tests

```bash
uv sync --extra dev --extra openai --extra validation --extra redis --extra http --extra postgres --extra pgvector
uv run ruff check
uv run pytest
```

These are the extras CI installs (`.github/workflows/test.yml`). `ruff check` is advisory: CI does not run it and the tree carries existing findings, so judge only the lines your diff touches. The full suite must pass on Python 3.11 and 3.12. Run `uv run pytest --collect-only -q | tail -1` for the live test count when a doc claims one.

### 3. Test coverage audit

Read the diff and name each new code path with no test. New backend Protocols add about 25 conformance tests and about 10 impl-specific tests as a matter of policy, so coverage gaps tend to surface naturally.

### 4. Pre-landing review

Review the diff against `.claude/skills/review/checklist.md`, the project-local checklist. It is two-pass: Pass 1 critical (Backend Protocol Invariants, Cost Gate Placement, Audit Trail Shape, Atomic Write Discipline, LLM Output Trust, Schema/API Break Detection), Pass 2 informational. Anticipated false-positive patterns to watch for (hints, not verified knowledge):

- A maintainability reviewer may flag `@dataclass(frozen=True)` + `Protocol` shapes against an OO-inheritance default. These are the intentional design pattern for backend protocols (see `CLAUDE.md` §"Protocols, not subclassing"). Verify by reading the spec doc cited in the diff before accepting the finding.
- A testing reviewer may flag missing pytest fixtures for parameterized conformance suites. The project's conformance shape is `pytest.mark.parametrize("backend", [...])` with a `make_backend` factory per impl (see `tests/test_memory_backend_conformance.py`). Verify before accepting any "use shared fixtures" recommendation.
- A security reviewer may flag `_io.atomic_write` as an unusual file-write pattern. It is the project's standard atomic-write idiom. Verify against `atomic_agents/_io.py` before accepting.

When a false positive lands in a real review, append the pattern here (what was flagged, why it was wrong).

### 5. Adversarial review in rounds

Run the adversarial review in rounds until a round finds nothing, per [`docs/methodology.md`](../methodology.md) §"Reviewer roster". **Codex is rate-limited** (as of 2026-05-13), so the documented reviewers are:

1. **Opus subagent** with the verify-against-code prompt (same-family).
2. **`atomic-agents review --backend kimi`** (cross-family via Moonshot). Key in macOS Keychain at `atomic-agents-moonshot`; the international `.ai` endpoint requires `MOONSHOT_BASE_URL=https://api.moonshot.ai/v1` in env. Kimi K2.x thinking models have stronger reviewer signal, but their output lives in a `reasoning_content` field not yet extracted by the LLM backend ([#146](https://github.com/dep0we/atomic-agents-stack/issues/146)). Until that issue closes, expect an Opus subagent plus non-thinking Moonshot rather than two fully independent rounds.

### 6. Choose the version

Pick the mode from the two-mode table: PR-level update (default for feature and fix PRs) or release cut (a new SemVer string). For a release cut, derive the new version per [`versioning.md`](versioning.md) §"What counts as Major / Minor / Patch":

- **Major / breaking** (pre-1.0: still bumps Minor with a `### BREAKING` callout): schema break, framework API break, default backend change.
- **Minor**: new feature, new spec doc, new backend, new CLI subcommand.
- **Patch**: bug fix, doc, test, internal refactor with no observable change.

A release cut also bumps `pyproject.toml` and `atomic_agents/__init__.py` (`__version__`) to the same string.

### 7. Update the CHANGELOG

PR-level mode appends bullets to `## [Unreleased]` only. Release-cut mode promotes `## [Unreleased]` to `## [<new-version>] - <today>` and leaves a fresh empty `## [Unreleased]` above it.

When the diff has **no user-facing change** (CHANGELOG bullets only, branch resync, version-bump housekeeping), the honest entry is one sentence. Never pad with aspirational claims. See [`versioning.md`](versioning.md) and `CLAUDE.md` §"CHANGELOG is the single source of truth".

### 8. Sync the documentation by hand

Walk the "Operator manual surface check before merge" section below. No tool does this for you: the surfaces most likely to drift are exactly the ones a generic markdown sweep misses.

- `atomic_agents/__init__.py`: Python, not markdown. The `__version__` and `__all__` claims are the operator's job.
- `pyproject.toml`: the `version` string.
- `docs/spec/*.md`: spec counts and cross-references.
- `docs/architecture.md`: count claims (backends shipped, layers).
- `README.md` and `CLAUDE.md`: version, test count, shipped-protocol list.

### 9. Commit, push, open the PR

Split non-trivial work into bisectable commits. Push the branch and open a PR. Title format: `v<NEW_VERSION> <type>: <summary>`. In PR-level mode, `NEW_VERSION` is the current release version (edit it if the title would be misleading). In release-cut mode, it is the new release version. The body carries a Test Coverage section, a Pre-Landing Review section, a Documentation section, and a "Filed-inline follow-ups" section. Never push to `main` and never merge: the maintainer merges.

---

## Operator manual surface check before merge

**This is the load-bearing section.** A generic markdown sweep catches generic drift but does not know about this project's specific surfaces. The manual check below covers what would otherwise slip through. Historical reason: the v0.10.0 release was cut without a documentation sync, the README's "What's shipped" table drifted, and the maintainer caught it only by accident. The same shape recurred during the v0.13.0 cut, where a documentation-sync pass surfaced 21 doc-sync gaps total — 6 critical (version label off by two minors, `__all__` missing the new Protocol surface, related count claims stale), 6 polish-tier filed as [#156](https://github.com/dep0we/atomic-agents-stack/issues/156), and 9 already-in-sync. The numbers are documented verbatim in PR [#157](https://github.com/dep0we/atomic-agents-stack/pull/157)'s body. Don't replicate that.

Before merging any PR, verify these surfaces match what shipped:

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

- [ ] PR body's Test Coverage section, Pre-Landing Review section, and Documentation section accurately summarize what the procedure ran and found.
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

## When the procedure has friction

If a step is wrong for this project, file an issue against this repo (`[tooling] release procedure friction: ...`) and fix the step in this runbook. Do not skip it silently.

---

## References

- [`versioning.md`](versioning.md) — what counts as Major/Minor/Patch + the GitHub Release prose contract
- [`upgrading.md`](upgrading.md) — operator-facing runbook for applying a release on a running host
- [`../methodology.md`](../methodology.md) — working-methods retrospective (reviewer roster, codex-rounds-not-passes, verify-before-claim)
- [`../../CLAUDE.md`](../../CLAUDE.md) §"Always follow the release procedure" + §"Working methods"
- [`../../.claude/skills/review/checklist.md`](../../.claude/skills/review/checklist.md) — project-local Pre-Landing Review checklist (step 4 input)
- Issue [#155](https://github.com/dep0we/atomic-agents-stack/issues/155) — the forcing function for this runbook

### Maintainer-only paths (single-operator project today)

One path referenced in this runbook lives on the maintainer's machine, not in this repo, and external readers should treat it as a conceptual hint rather than a literal path to follow:

- `<VAULT_ROOT>/Atomic Agents/RESUME-NEXT-SESSION.md` — the cross-session brief the maintainer writes at the end of each working session. External readers should treat it as "the maintainer keeps a per-session handoff brief."

When the project grows beyond a single maintainer, this reference will either move to in-repo equivalents (e.g., a `CONTRIBUTING.md` section, or `docs/maintainer-handoff.md`) or be dropped. Pre-1.0 with a single contributor, the personal-machine ref is honest about the current shape.
