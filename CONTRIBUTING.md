# Contributing to atomic-agents-stack

Thanks for considering a contribution. This project is small, opinionated, and built around a specific design ethos — the docs below tell you what to read before opening a PR so your work has the best chance of landing.

---

## Before you start

Read these, in order:

1. **[`CLAUDE.md`](CLAUDE.md)** — the project's design ethos. 14 taste rules that govern what changes get accepted (vault-as-source-of-truth, protocols-not-subclassing, layers-compose-not-merge, cost-first-class, audit-trail-structural, progressive-disclosure, markdown-config, atomic-and-idempotent, one-level-constraints, spec-is-the-product, adversarial-review-in-rounds, verify-before-claim, docs-match-reality, backward-compat-by-default). A change that violates one of these gets pushed back unless the rationale is named.
2. **[`docs/TENSIONS.md`](docs/TENSIONS.md)** — architectural tensions to protect when changing the code. Read this before proposing structural changes; the tension you're about to resolve may already be tracked.
3. **[`docs/methodology.md`](docs/methodology.md)** — working-methods retrospective. The practices that produced this codebase's quality (codex review in rounds, verify-before-claim, bisectable commits, CHANGELOG-as-single-source-of-truth).

If your change is anything beyond a typo fix, file an issue first to discuss the approach. The fastest way to get a PR rejected is to skip alignment on direction and arrive with 800 lines.

---

## Issues

We use [GitHub Issues](https://github.com/dep0we/atomic-agents-stack/issues) for all work tracking. Title prefixes: `[backend]`, `[deployment]`, `[polish]`, `[spec]`, `[infra]`. Labels: `enhancement`, `documentation`, `infrastructure`, `polish`, `backend`, `deployment`, `spec`, `bug`.

When filing an issue, include:

- **Bug:** what you expected, what you got, what you ran, the version (`pip show atomic-agents-stack` or commit SHA), the relevant log line from `<vault>/<agent>/log/`.
- **Feature:** the operator problem you're solving (not the feature you want), what you tried first, why the existing surface didn't fit.
- **Spec proposal:** the gap you're closing, prior art, why the existing 41 spec docs don't already cover this.

---

## Pull requests

### Branch + commit shape

- Cut a feature branch from `main`. Never push to `main` directly.
- Bisectable commits, not save-points. Each commit is one logical change. A PR splits into multiple commits when the work is non-trivial (e.g., one commit for the runtime change, one commit for tests, one commit for the spec doc + CHANGELOG entry).
- Every PR adds its own bullets to `[Unreleased]` in [CHANGELOG.md](CHANGELOG.md) as part of the diff.

### Tests

Run the full suite before pushing:

```bash
uv run pytest
```

the full suite (run `pytest -q` for the live count) today; CI runs Python 3.11 + 3.12. New backend protocols add ~25 conformance tests + ~10 implementation-specific tests. New features ship with tests. Migration-shaped PRs need parameterized fixture tests across the backend protocol.

### Review

Non-trivial PRs go through 3-5 review rounds before merge. Most rounds run as cross-model adversarial review (Codex CLI for one independent voice; Claude subagent for another); each round catches different things because the diff changes with each fix. See `docs/methodology.md` for the retrospective on this practice. If you're a contributor without Codex access, that's fine — the maintainer runs the additional rounds.

### Verify before claim

This project mechanizes "you accept or reject by reproducing." If you say "this command behaves like X," run the command in the PR description. If you change error-message text, paste the actual `pytest` output. If you change docs, the doc claim must match the implementation; if it doesn't, fix one or the other before opening the PR.

### What kinds of changes land cleanly

- **Spec doc improvements** that clarify existing behavior without changing it.
- **Bug fixes** with a regression test that fails before the fix and passes after.
- **New backend implementations** that conform to the existing protocol surface (see `docs/spec/20-memory-backend.md` for the template).
- **Documentation** that matches what the code actually does today.
- **Tests** that fill coverage gaps.

### What kinds of changes need an issue first

- **Spec changes** that imply implementation changes.
- **New protocol surfaces** (LockBackend, LogBackend, etc. are roadmap; if you want to ship one, the issue is where the design lands).
- **Default-value changes** to `cost_guardrails`, `tools.md` paths, or any operator-facing config.
- **Anything that touches `agent.call()`, `_capture.py`, `_costs.py`, `_locks.py`, or the protocol surfaces** — these are load-bearing and warrant extra alignment.
- **Refactors** that don't fix a bug or close an issue. The bar for "make existing code cleaner" is high; the project has explicit YAGNI discipline.

---

## Backward compatibility

Pre-1.0, Minor releases may contain breaking changes. We still try hard to avoid them. When a breaking change is necessary:

- Add `### BREAKING` callout to the CHANGELOG entry with the symptom an operator would observe, the migration path, and a one-line rationale.
- Provide a migration script under `<vault>/_migrations/` or `docs/migrations/`.
- Open an issue ahead of the PR to flag it.

After v1.0, breaking changes become Major events with the same rigor.

---

## Code of Conduct

We follow the [Contributor Covenant](CODE_OF_CONDUCT.md). Be kind, be specific, and assume good faith. Disagreements about technical direction are welcome; personal attacks are not.

---

## Security

If you find a security vulnerability — anything that lets a non-operator read or write the vault, bypass cost guardrails, escape the autonomy ladder, or otherwise violate a documented invariant — do not file a public issue. See [SECURITY.md](SECURITY.md) for the disclosure path.

---

## Questions

Open a `[question]` issue. Issues are the project's discussion surface for now; Discussions may get enabled later as the contributor pool grows.
